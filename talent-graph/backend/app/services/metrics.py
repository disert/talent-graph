"""处理耗时的落库与汇总打印。

分工：`timing.StageTimer` 只负责测量，本模块负责两件事——

1. `save()`：把一份简历 / 一个岗位的各阶段耗时写进 `pipeline_metrics`
   （独立短事务 + 吞掉所有异常，统计失败绝不影响解析/匹配主流程）。
2. `print_batch_report()`：同一批任务（一次上传、一次 Excel 导入）共用一个 `batch_id`，
   由 `concurrency.py` 在**队列清空时**（简历与岗位都处理完）查出本批记录并打印一张表，
   一眼看出这批任务慢在哪一步、平均/最长耗时是多少。

批次 id 用进程内生成的 uuid 而不是自增整数：进程重启后不会和历史记录撞号。
"""
from __future__ import annotations

import logging
import threading
import unicodedata
import uuid

from sqlalchemy import select

from ..database import SessionLocal
from ..models import PipelineMetric

logger = logging.getLogger(__name__)

_batch_lock = threading.RLock()   # 可重入：current_batch() 内部会调 start_batch()
_batch_id = ""                    # 当前批次 id，空表示当前没有进行中的批次

# kind -> 中文名（报表展示用；新增 kind 时在这里登记）
_KIND_LABELS = {
    "resume_parse": "简历解析",
    "resume_rematch": "简历重匹配",
    "job_create": "岗位录入",
    "job_match": "岗位匹配",
}

# 阶段打印顺序（未登记的阶段按名字排在后面，保证新增打点也能出现在报表里）
_STAGE_ORDER = ("排队等待", "接收落盘", "入库", "文本提取/OCR", "大模型结构化", "embedding",
                "匹配岗位", "硬性过滤", "向量粗排", "LLM精排", "落库")


def start_batch() -> str:
    """新开一批任务（队列由空变非空时调用），返回新的批次 id。"""
    global _batch_id
    with _batch_lock:
        _batch_id = uuid.uuid4().hex[:12]
        return _batch_id


def current_batch() -> str:
    """当前批次 id；若当前没有活动批次（例如请求侧直接建岗）就顺手开一个。"""
    with _batch_lock:
        if not _batch_id:
            return start_batch()
        return _batch_id


def save(*, kind: str, ref_id: int, name: str = "", metrics: dict | None = None,
         error: str = "") -> None:
    """写入一条耗时记录（尽力而为，绝不抛异常）。"""
    data = metrics or {}
    try:
        db = SessionLocal()
        try:
            db.add(PipelineMetric(
                batch_id=current_batch(),
                kind=kind,
                ref_id=ref_id,
                name=(name or "")[:128],
                stages=data.get("stages") or {},
                total_seconds=float(data.get("total") or 0.0),
                ok=not error,
                error=(error or "")[:255],
            ))
            db.commit()
        finally:
            db.close()
    except Exception as e:   # 记录耗时失败不能影响业务
        logger.warning("耗时记录写入失败 kind=%s ref_id=%s: %s", kind, ref_id, e)


def fetch_batch(batch_id: str) -> list[PipelineMetric]:
    db = SessionLocal()
    try:
        return list(db.scalars(select(PipelineMetric)
                               .where(PipelineMetric.batch_id == batch_id)
                               .order_by(PipelineMetric.id)))
    finally:
        db.close()


# ---------- 报表 ----------

def _width(text: str) -> int:
    """终端显示宽度：中文/日文/韩文等宽字符占 2 列，否则表格会错位。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _width(text))


def format_report(rows: list[PipelineMetric]) -> str:
    """渲染汇总：**按类型分表**（简历解析和岗位匹配的阶段不同，混在一张表里列会太宽）。"""
    if not rows:
        return ""

    lines = [f"批量处理耗时汇总（批次 {rows[0].batch_id}，共 {len(rows)} 条）"]
    for kind in dict.fromkeys(r.kind for r in rows):
        group = [r for r in rows if r.kind == kind]
        present = {stage for r in group for stage in (r.stages or {})}
        stages = [s for s in _STAGE_ORDER if s in present]
        stages += sorted(present - set(stages))       # 未登记顺序的阶段排后面

        header = ["对象", *stages, "总计"]
        table: list[list[str]] = []
        for r in group:
            st = r.stages or {}
            obj = f"{r.ref_id} {r.name}".strip() + ("（失败）" if not r.ok else "")
            table.append([obj] + [f"{st[s]:.2f}s" if s in st else "-" for s in stages]
                         + [f"{r.total_seconds:.2f}s"])

        widths = [_width(h) for h in header]
        for row in table:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], _width(cell))
        sep = "-" * (sum(widths) + 3 * (len(widths) - 1))

        slowest = max(group, key=lambda r: r.total_seconds)
        avg = sum(r.total_seconds for r in group) / len(group)
        lines.append("")
        lines.append(f"【{_KIND_LABELS.get(kind, kind)}】{len(group)} 条"
                     f"（平均 {avg:.2f}s，最长 {slowest.total_seconds:.2f}s"
                     f"：{slowest.ref_id} {slowest.name}）")
        lines.append(sep)
        lines.append("   ".join(_pad(h, widths[i]) for i, h in enumerate(header)))
        lines.append(sep)
        lines += ["   ".join(_pad(c, widths[i]) for i, c in enumerate(row)) for row in table]
        lines.append(sep)
    return "\n".join(lines)


def print_batch_report(batch_id: str) -> None:
    """队列清空后调用：查询本批耗时并打印汇总（一条多行 INFO 日志，便于整体复制）。"""
    if not batch_id:
        return
    try:
        rows = fetch_batch(batch_id)
    except Exception as e:
        logger.warning("耗时汇总查询失败 batch=%s: %s", batch_id, e)
        return
    if not rows:      # 本批没有任何记录（例如任务在写记录前就崩了），不打空表
        return
    logger.info("简历/岗位处理已全部完成，耗时汇总如下：\n%s", format_report(rows))
