"""简历上传解析 / 检索 / 人工修正 / 状态流转。"""
from __future__ import annotations

import logging
import re
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import timing
from ..concurrency import ProcessingInterrupted, submit_heavy_task
from ..config import settings
from ..database import get_db
from ..models import Resume
from ..schemas import ResumeOut, ResumeUpdate
from ..services import embedding, llm, matching, metrics, parser

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/resumes", tags=["resumes"])

ALLOWED_SUFFIX = {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".bmp", ".webp"}

# resumes.name 列为 VARCHAR(64)：超长文件名（如猎头推荐报告 PDF）直接 INSERT 会触发
# StringDataRightTruncation 导致整个上传请求 500，入库前必须截断
MAX_NAME_LEN = 64


def _match_resume(db, resume: Resume, timer: timing.StageTimer | None = None) -> None:
    """解析/修正完成后触发简历侧匹配，结果落库 match_results（匹配页直接查该表）。

    独立 try：匹配失败（如大模型平台不可用）不能反把简历标记成"解析失败"。
    timer 传入时把「匹配岗位」作为其中一个阶段计入同一份耗时汇总。
    """
    resume_id = resume.id   # 匹配内部会 rollback 归还连接，之后 ORM 实例已过期
    try:
        with timing.stage(timer, "匹配岗位"):
            records = matching.match_resume(db, resume, timer=timer)
        logger.info("简历 %s 匹配完成，写入 %d 条匹配结果", resume_id, len(records))
    except ProcessingInterrupted as e:
        # 服务正在关闭（Ctrl+C / --reload）：本轮匹配作废，留一行说明即可
        db.rollback()
        logger.warning("简历 %s 匹配被服务关闭中断，未写入结果（重启后可在匹配页点「执行匹配」重跑）：%s",
                       resume_id, e)
    except Exception as e:
        db.rollback()
        logger.exception("简历 %s 匹配失败: %s", resume_id, e)


def _match_resume_task(resume_id: int, enqueued_at: float | None = None) -> None:
    """后台任务版：人工修正字段后重新生成 embedding 再匹配（独立会话）。"""
    from ..database import SessionLocal

    timer = timing.StageTimer(f"简历重匹配 id={resume_id}")
    wait = timing.queue_wait_seconds(enqueued_at)
    if wait is not None:
        timer.record("排队等待", wait)

    db = SessionLocal()
    display = ""
    try:
        resume = db.get(Resume, resume_id)
        if resume is None:
            timer.note("简历已删除，跳过")
            return
        display = resume.name
        _match_resume(db, resume, timer)
    finally:
        db.close()
        timer.log()
        metrics.save(kind="resume_rematch", ref_id=resume_id, name=display,
                     metrics=timer.as_dict())


def _process_resume(resume_id: int, file_path: str, enqueued_at: float | None = None) -> None:
    """后台任务：提取文本 -> LLM 结构化 -> 生成 embedding -> 更新入库 -> 触发匹配。

    由 `submit_heavy_task` 排入专用工作线程池执行（并发数见 settings.heavy_task_concurrency），
    不会占用 FastAPI 请求线程。

    enqueued_at 为接口侧提交任务的时刻，用于把「排队等待」也算进耗时汇总
    （批量上传时排队时间常比解析本身还长，不区分会误判成解析慢）。
    """
    from ..database import SessionLocal

    timer = timing.StageTimer(f"简历处理 id={resume_id} file={Path(file_path).name}")
    wait = timing.queue_wait_seconds(enqueued_at)
    if wait is not None:
        timer.record("排队等待", wait)

    db = SessionLocal()
    failed = ""
    display = Path(file_path).name          # 报表展示名：解析成功后被姓名覆盖
    try:
        with timer.stage("文本提取/OCR"):
            raw_text = parser.extract_text(file_path)
        timer.note(f"文本{len(raw_text)}字")

        with timer.stage("大模型结构化"):
            structured, confidence = llm.extract_resume_fields(raw_text)

        with timer.stage("embedding"):
            emb = embedding.embed_one(embedding.resume_to_text(structured, raw_text))

        resume = db.get(Resume, resume_id)
        if resume is None:
            timer.note("简历已删除，跳过入库")
            return
        with timer.stage("入库"):
            resume.raw_text = raw_text
            resume.structured = structured
            resume.confidence = confidence
            resume.embedding = emb
            resume.name = (structured.get("name") or resume.name)[:MAX_NAME_LEN]
            display = resume.name
            db.commit()
        logger.info("简历解析完成 id=%s name=%s", resume_id, resume.name)
        _match_resume(db, resume, timer)
    except Exception as e:
        interrupted = isinstance(e, ProcessingInterrupted)
        failed = ("服务正在关闭，解析被中断（重启后请点「重新解析」）" if interrupted
                  else str(e)[:200])
        if interrupted:
            logger.warning("简历 %s 解析被服务关闭中断：%s", resume_id, e)
        else:
            logger.exception("简历解析失败 id=%s: %s", resume_id, e)
        # commit 失败后会话处于"待回滚"状态，必须先 rollback，否则 db.get 会抛
        # PendingRollbackError，_error 永远写不进去，简历会一直卡在「解析中」
        db.rollback()
        try:
            resume = db.get(Resume, resume_id)
            if resume is not None:
                resume.confidence = {"_error": failed}
                db.commit()
        except Exception:
            logger.exception("写回解析失败原因时再次出错 id=%s", resume_id)
    finally:
        db.close()
        # 汇总行放在最后：所有阶段（含失败前已完成的）耗时一次看全
        timer.log(error=failed)
        # 落库：整批任务跑完后由 concurrency 汇总打印（前台控制台看不到时也能查库）
        metrics.save(kind="resume_parse", ref_id=resume_id, name=display,
                     metrics=timer.as_dict(), error=failed)


@router.post("/upload", response_model=list[ResumeOut])
def upload_resumes(
    files: list[UploadFile],
    source: str = Query("", description="渠道：猎头/校园/邮箱/交流会"),
    db: Session = Depends(get_db),
):
    """批量上传简历，立即返回占位记录，解析走后台任务队列（一期免 Celery）。

    这里只统计「接口侧耗时」（接收落盘 + 入库 + 入队）；OCR/LLM/embedding/匹配属于后台
    任务，由 `_process_resume` 自己打一行阶段耗时汇总。
    """
    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)

    # 先整体校验文件类型，避免"部分入库后报错"的半截状态
    for f in files:
        suffix = Path(f.filename or "").suffix.lower()
        if suffix not in ALLOWED_SUFFIX:
            raise HTTPException(400, f"不支持的文件类型: {f.filename}")

    batch_timer = timing.StageTimer(f"简历上传批次 files={len(files)}")
    created: list[Resume] = []
    failed: list[str] = []
    for f in files:
        # 逐文件容错：单个文件失败不影响同批其他文件
        file_timer = timing.StageTimer(f"简历上传 file={f.filename}")
        try:
            suffix = Path(f.filename or "").suffix.lower()
            save_path = upload_dir / f"{uuid.uuid4().hex}{suffix}"
            with file_timer.stage("接收落盘"):
                # 分块落盘：附件上限放到 100MB 后，不再把整份文件读进内存再写盘
                size = 0
                with save_path.open("wb") as dst:
                    while True:
                        chunk = f.file.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        dst.write(chunk)
            file_timer.note(f"{size / 1024 / 1024:.2f}MB")

            with file_timer.stage("入库"):
                resume = Resume(
                    name=Path(f.filename or "未命名").stem[:MAX_NAME_LEN],
                    file_path=str(save_path),
                    source=source,
                    status="in_pool",
                    confidence={"_parsing": True},  # 标记解析中
                )
                db.add(resume)
                db.commit()
                db.refresh(resume)
            submit_heavy_task(_process_resume, resume.id, str(save_path),
                              enqueued_at=time.perf_counter())
            file_timer.note(f"id={resume.id}")
            created.append(resume)
        except Exception as e:
            db.rollback()
            logger.exception("简历入库失败 file=%s: %s", f.filename, e)
            failed.append(f"{f.filename}: {e}")
            file_timer.log(error=str(e))
            continue
        file_timer.log()

    batch_timer.note(f"成功{len(created)} 失败{len(failed)}")
    if not created:
        batch_timer.log(error="全部上传失败；" + "；".join(failed))
        raise HTTPException(400, "全部上传失败；" + "；".join(failed))
    if failed:
        logger.warning("部分简历上传失败: %s", failed)
    batch_timer.log()
    return created


# ---------- 简历库检索：structured 为 JSON 列，枚举类字段统一在 Python 侧比对 ----------

def _text(value) -> str:
    return str(value or "").strip()


def _split_multi(value: str) -> list[str]:
    """逗号/顿号分隔的多值参数 -> 去空的小写列表。"""
    return [p.strip().lower() for p in re.split(r"[,，、]", value or "") if p.strip()]


def _hits(text, candidates: list[str]) -> bool:
    """候选值与文本互相包含即命中。

    学历/专业写法不统一（「博士」vs「博士研究生」、「电气工程」vs「电气工程及其自动化」），
    双向包含比精确相等更贴合筛选预期；文本为空时不命中。
    """
    t = _text(text).lower()
    if not t:
        return False
    return any(c in t or t in c for c in candidates)


def _age_of(resume: Resume) -> int | None:
    try:
        return int(float(_text((resume.structured or {}).get("age"))))
    except (TypeError, ValueError):
        return None


def _filtered_resumes(db: Session, *, keyword: str = "", status: str = "",
                      low_confidence_only: bool = False, education: str = "", major: str = "",
                      school: str = "", source: str = "", age_min: int | None = None,
                      age_max: int | None = None) -> list[Resume]:
    """简历库检索的公共过滤逻辑（列表接口与看板接口共用，保证两者口径一致）。

    能下沉到 SQL 的条件（状态/渠道）先过滤；学历/专业/院校/年龄存在 structured JSON 里，
    无法走 SQL，统一在 Python 侧比对；limit 由调用方在过滤完成后再截断，
    避免"先截断后过滤"导致符合条件的简历被漏掉。
    """
    stmt = select(Resume).order_by(Resume.created_at.desc())
    if status:
        stmt = stmt.where(Resume.status == status)
    if source:
        stmt = stmt.where(Resume.source == source)
    resumes = list(db.scalars(stmt))

    kw = _text(keyword).lower()
    if kw:
        def hit(r: Resume) -> bool:
            s = r.structured or {}
            hay = " ".join([
                r.name, str(s.get("major") or ""), str(s.get("school") or ""),
                str(s.get("research") or ""), " ".join(s.get("skills") or []),
                str(s.get("work_history") or ""),
            ]).lower()
            return kw in hay
        resumes = [r for r in resumes if hit(r)]

    edu = _text(education).lower()
    if edu:
        resumes = [r for r in resumes if _hits((r.structured or {}).get("education"), [edu])]

    majors = _split_multi(major)
    if majors:
        resumes = [r for r in resumes if _hits((r.structured or {}).get("major"), majors)]

    school_kw = _text(school).lower()
    if school_kw:
        resumes = [r for r in resumes
                   if school_kw in _text((r.structured or {}).get("school")).lower()]

    if age_min is not None or age_max is not None:
        def age_ok(r: Resume) -> bool:
            age = _age_of(r)
            if age is None:  # 未提取到年龄的简历，按年龄筛选时不计入
                return False
            return (age_min is None or age >= age_min) and (age_max is None or age <= age_max)
        resumes = [r for r in resumes if age_ok(r)]

    if low_confidence_only:
        resumes = [
            r for r in resumes
            if any(isinstance(v, (int, float)) and v < 0.7 for v in (r.confidence or {}).values())
        ]
    return resumes


def _field_value(resume: Resume, field: str, blank: str) -> str:
    """取字段值用于分布统计，空值归入 blank 分类（保证饼图总数 = 简历总数）。"""
    return _text((resume.structured or {}).get(field)) or blank


def _distribution(resumes: list[Resume], getter) -> list[dict]:
    """按取值计数（降序），供前端饼图直接使用。"""
    counts: dict[str, int] = {}
    for r in resumes:
        key = getter(r)
        counts[key] = counts.get(key, 0) + 1
    return [{"name": k, "value": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]


@router.get("", response_model=list[ResumeOut])
def list_resumes(
    keyword: str = Query("", description="姓名/专业/毕业院校/研究方向/技能关键词"),
    status: str = Query("", description="状态过滤"),
    low_confidence_only: bool = Query(False, description="只看低置信待修正"),
    education: str = Query("", description="学历过滤（博士/硕士/本科，按包含匹配）"),
    major: str = Query("", description="专业过滤，多个用逗号分隔（任一命中即可）"),
    school: str = Query("", description="毕业院校关键词"),
    source: str = Query("", description="渠道过滤"),
    age_min: int | None = Query(None, description="年龄下限"),
    age_max: int | None = Query(None, description="年龄上限"),
    limit: int = Query(100, le=500),
    db: Session = Depends(get_db),
):
    resumes = _filtered_resumes(
        db, keyword=keyword, status=status, low_confidence_only=low_confidence_only,
        education=education, major=major, school=school, source=source,
        age_min=age_min, age_max=age_max,
    )
    return resumes[:limit]


@router.get("/stats")
def resume_stats(
    keyword: str = Query("", description="与列表接口同口径的关键词过滤"),
    status: str = Query(""),
    low_confidence_only: bool = Query(False),
    education: str = Query(""),
    major: str = Query(""),
    school: str = Query(""),
    source: str = Query(""),
    age_min: int | None = Query(None),
    age_max: int | None = Query(None),
    db: Session = Depends(get_db),
):
    """简历库看板：按筛选条件聚合「学校分布 / 专业分布」，并回传筛选项候选值。

    与列表接口共用同一套过滤逻辑，因此看板数字与下方表格始终一致（不受 limit 截断影响）。
    饼图数据按数量倒序；筛选候选项取自全库，避免选中某个条件后其他选项消失。
    """
    resumes = _filtered_resumes(
        db, keyword=keyword, status=status, low_confidence_only=low_confidence_only,
        education=education, major=major, school=school, source=source,
        age_min=age_min, age_max=age_max,
    )
    all_resumes = list(db.scalars(select(Resume)))

    def options(field: str) -> list[str]:
        values = {(r.structured or {}).get(field) for r in all_resumes}
        return sorted({str(v).strip() for v in values if str(v or "").strip()})

    return {
        "total": len(resumes),
        "by_school": _distribution(resumes, lambda r: _field_value(r, "school", "未填毕业院校")),
        "by_major": _distribution(resumes, lambda r: _field_value(r, "major", "未填专业")),
        "filters": {
            "educations": options("education"),
            "majors": options("major"),
            "sources": sorted({r.source for r in all_resumes if r.source}),
        },
    }


@router.get("/{resume_id}", response_model=ResumeOut)
def get_resume(resume_id: int, db: Session = Depends(get_db)):
    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(404, "简历不存在")
    return resume


@router.get("/{resume_id}/raw")
def get_resume_raw(resume_id: int, db: Session = Depends(get_db)):
    """查看解析原文（匹配理由可追溯用）。"""
    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(404, "简历不存在")
    return {"id": resume.id, "raw_text": resume.raw_text}


@router.patch("/{resume_id}", response_model=ResumeOut)
def update_resume(resume_id: int, body: ResumeUpdate,
                  db: Session = Depends(get_db)):
    """人工修正低置信字段 / 更新状态标签。修正后重新生成 embedding 并后台重跑匹配。

    请求内只统计 embedding 重算耗时；后续的完整重匹配在后台任务里另打一行汇总。
    """
    timer = timing.StageTimer(f"简历修正 id={resume_id}")
    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(404, "简历不存在")

    rematch = False
    embed_text: str | None = None
    if body.status:
        if body.status not in ("in_pool", "pushed", "selected", "rejected", "withdrawn"):
            raise HTTPException(400, "非法状态")
        resume.status = body.status
        rematch = rematch or body.status in ("in_pool", "rejected")
    if body.source is not None:
        resume.source = body.source
    if body.structured:
        resume.structured = {**(resume.structured or {}), **body.structured}
        resume.confidence = {k: v for k, v in (resume.confidence or {}).items()
                             if k not in body.structured}  # 修正过的字段清掉低置信标记
        embed_text = embedding.resume_to_text(resume.structured, resume.raw_text)
        rematch = True
    if embed_text is not None:
        # 先把字段修改落库并归还连接：embedding 计算（首次可能触发模型加载，几十秒）
        # 期间不占用连接池名额，否则并发修正会拖垮其他请求
        db.commit()
        with timer.stage("embedding"):
            resume.embedding = embedding.embed_one(embed_text)
    db.commit()
    db.refresh(resume)
    if rematch:
        # 关键字段变了，历史匹配结果已失效，后台重跑覆盖 match_results
        submit_heavy_task(_match_resume_task, resume.id, enqueued_at=time.perf_counter())
        timer.note("已排后台重匹配")
    timer.log()
    return resume


@router.delete("/{resume_id}")
def delete_resume(resume_id: int, db: Session = Depends(get_db)):
    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(404, "简历不存在")
    file_path = resume.file_path
    db.delete(resume)  # 关联匹配记录随 cascade 一并删除
    db.commit()
    if file_path:  # 原件一并清理，失败不影响删除结果
        try:
            Path(file_path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("原件删除失败 path=%s: %s", file_path, e)
    return {"ok": True}


@router.post("/{resume_id}/reparse", response_model=ResumeOut)
def reparse_resume(resume_id: int, db: Session = Depends(get_db)):
    """对解析失败 / 卡在解析中的简历重新触发解析。"""
    resume = db.get(Resume, resume_id)
    if resume is None:
        raise HTTPException(404, "简历不存在")
    if not resume.file_path or not Path(resume.file_path).exists():
        raise HTTPException(400, "原件文件已丢失，无法重新解析")
    resume.confidence = {"_parsing": True}
    db.commit()
    db.refresh(resume)
    submit_heavy_task(_process_resume, resume.id, resume.file_path,
                      enqueued_at=time.perf_counter())
    logger.info("简历重新解析已入队 id=%s file=%s", resume.id, resume.file_path)
    return resume
