"""两级匹配引擎：
第一级 硬性过滤 —— SQL 精确条件（学历/年龄/专业关键词），透明可审计；
第二级 择优排序 —— pgvector 余弦相似度粗排 Top K，再 LLM 逐条精排并生成理由。

结果统一落库 `match_results`（匹配结果表），按 (job_id, resume_id) 幂等 upsert：
- 简历上传解析完成 / 人工修正 -> match_resume()：与该简历 × 全部在招岗位匹配；
- 岗位新建 / Excel 导入     -> match_job()：与全部在池简历匹配；
- 匹配页手动重跑           -> match_job(..., source="manual")。
匹配页面只查询 match_results，无需再实时调用本模块。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import timing
from ..concurrency import ProcessingInterrupted, shutting_down
from ..config import settings
from ..database import release_connection
from ..models import JobRequest, MatchResult, Resume
from . import llm

logger = logging.getLogger(__name__)

# LLM 精排的并发度：写死 4，与下面的日志文案保持一致（改这里要同步改）
_LLM_WORKERS = 4

_EDU_RANK = {"其他": 0, "大专": 1, "本科": 2, "硕士": 3, "博士": 4}


def _edu_pass(resume_edu: str | None, required: str | None) -> bool:
    if not required:
        return True
    if not resume_edu:
        return False  # 硬性条件缺失不放行，交由人工补录后再匹配
    return _EDU_RANK.get(resume_edu, -1) >= _EDU_RANK.get(required, 99)


def _hard_pass(job: JobRequest, resume: Resume) -> bool:
    """单条 简历×岗位 硬性过滤：学历/年龄精确比对，专业关键词模糊匹配。"""
    hard = job.hard_conditions or {}
    s = resume.structured or {}
    if not _edu_pass(s.get("education"), hard.get("education")):
        return False
    max_age = hard.get("max_age")
    if max_age and s.get("age") and int(s["age"]) > int(max_age):
        return False
    majors: list[str] = [m for m in (hard.get("majors") or []) if m]
    if majors:
        major_text = (s.get("major") or "") + " " + (s.get("research") or "")
        if not any(m in major_text for m in majors):
            return False
    return True


def hard_filter(db: Session, job: JobRequest) -> list[Resume]:
    """第一级：硬性过滤。只让"在库/未选中"的简历参与匹配，已退出/被选中不再消费。"""
    stmt = select(Resume).where(Resume.status.in_(["in_pool", "rejected"]))
    return [r for r in db.scalars(stmt) if _hard_pass(job, r)]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5 or 1.0
    nb = sum(y * y for y in b) ** 0.5 or 1.0
    return dot / (na * nb)


def vector_rank(db: Session, job: JobRequest, candidates: list[Resume]) -> list[tuple[Resume, float]]:
    """第二级·粗排：余弦相似度取 Top K。

    PostgreSQL 走 pgvector SQL 检索；SQLite 演示模式退化为 Python 内存计算。
    无 embedding 的简历跳过。
    """
    if job.embedding is None:
        logger.warning("岗位 %s 无 embedding，跳过向量粗排", job.id)
        return [(r, 0.0) for r in candidates[: settings.match_vector_top_k]]

    if db.bind.dialect.name != "postgresql":  # SQLite 演示模式
        job_vec = [float(v) for v in job.embedding]
        scored = [(r, _cosine(job_vec, [float(v) for v in r.embedding]))
                  for r in candidates if r.embedding is not None]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [(r, round(s, 4)) for r, s in scored[: settings.match_vector_top_k]]

    ids = [r.id for r in candidates if r.embedding is not None]
    if not ids:
        return []
    distance = Resume.embedding.cosine_distance(job.embedding).label("distance")
    stmt = (
        select(Resume, distance)
        .where(Resume.id.in_(ids))
        .order_by(distance)
        .limit(settings.match_vector_top_k)
    )
    rows = db.execute(stmt).all()
    return [(r, round(1 - float(d), 4)) for r, d in rows]


def _pair_vector_score(job: JobRequest, resume: Resume) -> float:
    """单条 简历×岗位 的向量相似度（任一缺 embedding 记 0，不影响其余维度打分）。"""
    if job.embedding is None or resume.embedding is None:
        return 0.0
    return round(_cosine([float(v) for v in job.embedding],
                         [float(v) for v in resume.embedding]), 4)


def _job_payload(job: JobRequest) -> dict:
    return {
        "title": job.title,
        "hard_conditions": job.hard_conditions,
        "soft_conditions": job.soft_conditions,
    }


def _score_pairs(pairs: list[dict], timer: timing.StageTimer | None = None) -> list[dict]:
    """第二级·精排：LLM 逐条打分 + 生成理由，并发调用内部平台提升速度。

    `pairs` 里全是纯 Python 值（id / dict），不含任何 ORM 实例：调用方在外层
    已经 `release_connection()` 归还连接，LLM 阶段（单条最长 llm_timeout × 重试次数）
    不再占用连接池，否则并发上传时会 `QueuePool limit ... timeout`。
    """
    def _score(pair: dict) -> dict:
        t0 = time.perf_counter()
        try:
            score, reason = llm.generate_match_reason(pair["job_payload"], pair["resume_structured"])
        except Exception as e:  # 单条失败不拖垮整批
            logger.error("LLM 精排失败 job_id=%s resume_id=%s: %s",
                         pair["job_id"], pair["resume_id"], e)
            score, reason = 0.0, f"LLM 评估失败: {e}"
        return {**pair, "score": score, "reason": reason,
                "llm_seconds": round(time.perf_counter() - t0, 2)}

    if not pairs:
        return []

    if shutting_down():      # 服务正在退出：线程池已不再接受新任务
        raise ProcessingInterrupted(f"服务正在关闭，跳过 {len(pairs)} 条 LLM 精排")

    try:
        with timing.stage(timer, "LLM精排"):
            with ThreadPoolExecutor(max_workers=_LLM_WORKERS) as pool:
                scored = list(pool.map(_score, pairs))
    except RuntimeError as e:
        # 竞态：上面检查通过后、真正 submit 之前进程才进入关闭状态
        if not shutting_down() and "interpreter shutdown" not in str(e):
            raise
        raise ProcessingInterrupted(f"服务正在关闭，LLM 精排未完成（{e}）") from e

    # 精排是最慢的一环（每对一次大模型调用），单独汇总一条，便于判断该调并发还是调 Top K
    durations = [s["llm_seconds"] for s in scored]
    slowest = max(scored, key=lambda s: s["llm_seconds"])
    logger.info("LLM 精排 %d 条：平均 %.2fs，最长 %.2fs（最慢 resume_id=%s，并发 %d，"
                "单条合计 %.2fs）",
                len(scored), sum(durations) / len(durations), slowest["llm_seconds"],
                slowest["resume_id"], _LLM_WORKERS, sum(durations))
    return scored


def _job_pair(job_payload: dict, resume: Resume, job_id: int, vec_score: float) -> dict:
    """把「岗位×简历」候选压成纯数据，便于先归还连接再跑 LLM。"""
    return {
        "job_id": job_id,
        "resume_id": resume.id,
        "resume_path": resume.file_path or "",   # 导出名单时打包简历原件用
        "job_payload": job_payload,
        "resume_structured": resume.structured or {},
        "vector_score": vec_score,
    }


def save_results(db: Session, items: list[dict], source: str) -> list[MatchResult]:
    """把打分结果幂等落库到 match_results。

    按 (job_id, resume_id) 唯一约束更新已有记录：重跑只刷新分数/理由/来源/简历路径，
    保留 push_status（已推送/有意向/无意向）不被覆盖。
    """
    records: list[MatchResult] = []
    for item in items:
        job_id = item["job_id"]
        resume_id = item["resume_id"]
        rec = db.scalar(select(MatchResult).where(
            MatchResult.job_id == job_id, MatchResult.resume_id == resume_id))
        if rec is None:
            rec = MatchResult(job_id=job_id, resume_id=resume_id, match_source=source)
            db.add(rec)
        rec.resume_path = item["resume_path"]  # 导出名单时打包简历原件用
        rec.vector_score = item["vector_score"]
        rec.score = item["score"]
        rec.reason = item["reason"]
        rec.match_source = source
        records.append(rec)
    db.commit()
    for rec in records:   # 刷新出 id / 时间戳等库侧生成值，供接口直接序列化
        db.refresh(rec)
    return records


def match_job(db: Session, job: JobRequest, source: str = "manual",
              timer: timing.StageTimer | None = None) -> tuple[int, list[MatchResult]]:
    """岗位侧匹配：硬性过滤 -> 向量粗排 Top K -> LLM 精排 Top N -> 落库。

    返回 (硬性过滤通过数, 写入的匹配结果)。岗位新建 / Excel 导入 / 手动重跑均走此入口。
    `timer` 传入时把各阶段耗时计入调用方的汇总行（后台任务 / 请求日志）。
    """
    with timing.stage(timer, "硬性过滤"):
        candidates = hard_filter(db, job)
    with timing.stage(timer, "向量粗排"):
        ranked = vector_rank(db, job, candidates)
    if timer is not None:
        timer.note(f"硬过滤通过{len(candidates)}份 粗排{len(ranked)}份")

    job_id = job.id
    job_payload = _job_payload(job)
    pairs = [_job_pair(job_payload, r, job_id, v) for r, v in ranked]
    # 候选已压成纯数据，先归还连接：下面的 LLM 精排可能持续数十秒
    release_connection(db)

    scored = _score_pairs(pairs, timer)
    scored.sort(key=lambda x: x["score"], reverse=True)
    with timing.stage(timer, "落库"):
        records = save_results(db, scored[: settings.match_final_top_n], source)
    return len(candidates), records


def match_resume(db: Session, resume: Resume, source: str = "resume_upload",
                timer: timing.StageTimer | None = None) -> list[MatchResult]:
    """简历侧匹配：简历解析/修正完成后，与该简历 × 全部在招岗位匹配并落库。

    与岗位侧对称：先对所有在招岗位做硬性过滤，再按向量相似度取最相关的
    match_resume_job_top_k 个岗位交 LLM 精排，避免岗位多时无谓的大模型调用。
    """
    if resume.status not in ("in_pool", "rejected"):
        return []  # 已推送/被选中/已退出的简历不再参与新匹配

    resume_id = resume.id   # release_connection 之后 ORM 实例会过期，先取出纯值
    stmt = select(JobRequest).where(JobRequest.status == "open")
    pairs: list[dict] = []
    with timing.stage(timer, "硬性过滤"):
        for job in db.scalars(stmt):
            if not _hard_pass(job, resume):
                continue
            pairs.append(_job_pair(_job_payload(job), resume, job.id,
                                   _pair_vector_score(job, resume)))
    # 候选已压成纯数据，先归还连接：下面的 LLM 精排可能持续数十秒
    release_connection(db)

    if not pairs:
        logger.info("简历 %s 未通过任何在招岗位的硬性条件，无匹配结果落库", resume_id)
        return []

    pairs.sort(key=lambda p: p["vector_score"], reverse=True)
    selected = pairs[: settings.match_resume_job_top_k]
    if timer is not None:
        timer.note(f"硬过滤通过{len(pairs)}个岗位 精排{len(selected)}个")
    scored = _score_pairs(selected, timer)
    with timing.stage(timer, "落库"):
        return save_results(db, scored, source)
