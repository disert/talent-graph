"""FastAPI 入口：智慧引才图谱后端（一期 MVP 单体版）。"""
import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from .database import SessionLocal, init_db
from .models import Resume
from .routers import departments, jobs, match, resumes

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

logger = logging.getLogger(__name__)

app = FastAPI(title="智慧引才图谱 API", version="0.1.0",
              description="高层次人才引进「岗位需求 × 人才简历」智能匹配（内网版）")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """默认 uvicorn 访问日志只打 "400 Bad Request"，看不出原因。

    这里把 detail 一并写进日志（错误码 >=400），排查前端报错时终端即可直接看到原因。
    """
    logger.warning("%s %s -> %s: %s", request.method, request.url.path, exc.status_code, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )

# 一期内部工具：放开内网跨域；二期接 SSO 后收紧
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(departments.router)
app.include_router(resumes.router)
app.include_router(jobs.router)
app.include_router(match.router)


@app.on_event("startup")
def startup() -> None:
    init_db()
    _seed_default_departments()
    _backfill_department_provinces()
    _recover_stuck_parsing()
    _log_embedding_backend()


def _log_embedding_backend() -> None:
    """启动时交代向量通道走哪条路：出问题时不必翻代码猜是本地模型还是平台接口。

    本地模型首次使用还有个坑：sentence-transformers 会先联网校验 huggingface.co 上的文件，
    内网/国内直连不通时会长时间挂着（详见 embedding.py 顶部说明）。
    """
    from .config import settings

    if settings.embedding_base_url:
        logger.info("Embedding 通道：平台接口 %s（model=%s）",
                    settings.embedding_base_url, settings.embedding_model)
    else:
        logger.info("Embedding 通道：本地模型 %s（EMBEDDING_BASE_URL 为空）",
                    settings.embedding_local_path)


def _seed_default_departments() -> None:
    """部门表为空时播种内置默认组织架构，保证级联选择/岗位录入开箱可用。"""
    from .services import departments as dept_svc

    db = SessionLocal()
    try:
        dept_svc.ensure_default_seeded(db)
    finally:
        db.close()


def _backfill_department_provinces() -> None:
    """旧库升级：给已存在的内置示例部门补省份（新加的 departments.province 默认为空）。"""
    from .services import departments as dept_svc

    db = SessionLocal()
    try:
        updated = dept_svc.backfill_default_provinces(db)
        if updated:
            logging.getLogger(__name__).info("已为 %d 个内置示例部门补齐省份", updated)
    finally:
        db.close()


def _recover_stuck_parsing() -> None:
    """服务重启会把后台解析任务打断，导致简历永远卡在"解析中"。
    启动时将这些记录标记为解析中断，用户可在前端一键重新解析。"""
    db = SessionLocal()
    try:
        stuck = list(db.scalars(select(Resume)))
        recovered = 0
        for r in stuck:
            conf = r.confidence or {}
            if conf.get("_parsing"):
                r.confidence = {"_error": "解析被服务重启中断，请点击「重新解析」"}
                recovered += 1
        if recovered:
            db.commit()
            logging.getLogger(__name__).info("恢复 %d 份卡在解析中的简历", recovered)
    finally:
        db.close()


@app.get("/api/health")
def health() -> dict:
    """健康检查：顺带返回后台解析/匹配队列的积压情况，便于排查"上传后一直不出结果"。"""
    from .concurrency import heavy_queue_status

    return {"status": "ok", "heavy_tasks": heavy_queue_status()}
