"""匹配结果查询 / 手动重跑 / 推送 / 反馈回流 / Excel 导出。

匹配结果统一存放在 match_results 表（简历上传、岗位新建时已预计算落库），
因此本路由的查询接口只读该表，页面打开即可秒出结果；只有「执行匹配」才重跑引擎。
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from .. import timing
from ..database import get_db
from ..models import JobRequest, MatchResult, Resume
from ..schemas import FeedbackRequest, MatchOut, MatchRunOut, PushRequest
from ..services import exporter, matching, metrics

router = APIRouter(prefix="/api/match", tags=["match"])


def _to_out(rec: MatchResult) -> MatchOut:
    return MatchOut(
        id=rec.id, job_id=rec.job_id, resume_id=rec.resume_id,
        resume_name=rec.resume.name if rec.resume else "",
        vector_score=rec.vector_score, score=rec.score,
        reason=rec.reason, push_status=rec.push_status,
        match_source=rec.match_source, created_at=rec.created_at, updated_at=rec.updated_at,
    )


@router.post("/run/{job_id}", response_model=MatchRunOut)
def run_match(job_id: int, db: Session = Depends(get_db)):
    """手动重跑两级匹配：硬性过滤 -> 向量粗排 -> LLM 精排+理由，结果覆盖 match_results。

    日常无需调用：简历上传 / 岗位新建时已自动匹配落库，本接口用于人工触发刷新。
    """
    job = db.get(JobRequest, job_id)
    if job is None:
        raise HTTPException(404, "岗位不存在")
    timer = timing.StageTimer(f"匹配重跑 job_id={job_id}")
    job_title = job.title          # 匹配内部会 rollback 归还连接，实例随即过期，先取纯值
    total, records = matching.match_job(db, job, source="manual", timer=timer)
    timer.note(f"写入{len(records)}条")
    timer.log()
    metrics.save(kind="job_match", ref_id=job_id, name=job_title, metrics=timer.as_dict())
    return MatchRunOut(job_id=job_id, total_after_hard_filter=total,
                       candidates=[_to_out(r) for r in records])


@router.get("/{job_id}", response_model=list[MatchOut])
def list_matches(job_id: int, db: Session = Depends(get_db)):
    """查询某岗位的匹配结果（直接读 match_results，不触发任何计算）。"""
    records = (
        db.query(MatchResult)
        .filter(MatchResult.job_id == job_id)
        .order_by(MatchResult.score.desc())
        .all()
    )
    return [_to_out(r) for r in records]


@router.get("/resume/{resume_id}", response_model=list[MatchOut])
def list_matches_by_resume(resume_id: int, db: Session = Depends(get_db)):
    """反向查询：某份简历在各岗位上的匹配结果（简历上传时已预计算）。"""
    if db.get(Resume, resume_id) is None:
        raise HTTPException(404, "简历不存在")
    records = (
        db.query(MatchResult)
        .filter(MatchResult.resume_id == resume_id)
        .order_by(MatchResult.score.desc())
        .all()
    )
    return [_to_out(r) for r in records]


@router.post("/push")
def push_candidates(body: PushRequest, db: Session = Depends(get_db)):
    """HR 复核精选后触发推送：匹配记录 -> pushed，简历状态 -> 已推送。"""
    records = db.query(MatchResult).filter(MatchResult.id.in_(body.match_ids)).all()
    if not records:
        raise HTTPException(404, "未找到匹配记录")
    for rec in records:
        rec.push_status = "pushed"
        if rec.resume and rec.resume.status in ("in_pool", "rejected"):
            rec.resume.status = "pushed"
    db.commit()
    return {"ok": True, "pushed": len(records)}


@router.post("/feedback")
def feedback(body: FeedbackRequest, db: Session = Depends(get_db)):
    """二期预留：用人单位一键反馈 有意向/无意向，状态自动回流。"""
    rec = db.get(MatchResult, body.match_id)
    if rec is None:
        raise HTTPException(404, "匹配记录不存在")
    if body.result not in ("selected", "rejected"):
        raise HTTPException(400, "result 仅支持 selected / rejected")
    rec.push_status = body.result
    if rec.resume:
        # 被选中 -> selected；未选中 -> 回退在库（rejected 状态仍可参与他岗匹配）
        rec.resume.status = "selected" if body.result == "selected" else "rejected"
    db.commit()
    return {"ok": True}


@router.get("/{job_id}/export")
def export_matches(
    job_id: int,
    with_files: bool = Query(True, description="是否连同简历原件一起打包为 ZIP"),
    db: Session = Depends(get_db),
):
    """导出候选名单：默认打包「名单 Excel + 简历原件」ZIP，with_files=false 时仅导出 Excel。"""
    job = db.get(JobRequest, job_id)
    if job is None:
        raise HTTPException(404, "岗位不存在")

    if with_files:
        data = exporter.export_match_bundle(db, job_id, job.title)
        media_type = "application/zip"
        filename = quote(exporter.bundle_filename(job.title))
    else:
        data = exporter.export_match_list(db, job_id)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = quote(exporter.export_filename(job.title))
    # RFC 5987：中文文件名必须 URL 编码，否则 latin-1 编码报错 500
    return Response(
        content=data,
        media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
