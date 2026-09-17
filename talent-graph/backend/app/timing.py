"""阶段耗时统计：把「上传落盘 → OCR/文本提取 → LLM 结构化 → embedding → 匹配」各阶段耗时
汇总成**一行**日志，便于排查"上传后为什么半天不出结果""到底卡在哪一步"。

一期只打后端日志，不落库、不对前端暴露（后续要出统计报表时再考虑写表）。

用法：

    timer = StageTimer(f"简历处理 id={resume_id}")
    with timer.stage("文本提取/OCR"):
        raw_text = parser.extract_text(path)
    timer.note(f"文本{len(raw_text)}字")     # 附加信息，跟随汇总行打印
    timer.log()

深层函数（如 matching.py 内部）不必层层传"要不要计时"，统一用模块级 `stage(timer, name)`：
`timer` 为 None 时它什么都不做，调用方无需写 if 分支。
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

SLOW_STAGE_SECONDS = 5.0    # 单阶段超过该秒数时额外打一条 WARNING，便于快速定位瓶颈
SLOW_TOTAL_SECONDS = 30.0   # 整单超过该秒数时汇总行升为 WARNING


def fmt(seconds: float) -> str:
    """统一时长格式：秒 + 两位小数（读日志时比毫秒直观）。"""
    return f"{seconds:.2f}s"


class StageTimer:
    """一个"业务单子"（一份简历 / 一个岗位 / 一次匹配）的分阶段计时器。

    线程安全说明：`record/note` 会被 `_score_pairs` 的并发线程调用，list.append 在
    CPython 下是原子的，允许少量顺序抖动（仅影响日志展示，不影响计时本身）。
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self.stages: list[tuple[str, float]] = []
        self.notes: list[str] = []
        self._start = time.perf_counter()

    @property
    def elapsed(self) -> float:
        """从创建计时器到现在（即整单耗时）。"""
        return time.perf_counter() - self._start

    def record(self, name: str, seconds: float) -> None:
        """记录一个已量好的阶段耗时（用于无法用 with 包起来的场景）。"""
        self.stages.append((name, seconds))
        if seconds >= SLOW_STAGE_SECONDS:
            logger.warning("%s | 阶段「%s」耗时 %s（偏慢）", self.label, name, fmt(seconds))

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """测量 with 块内部耗时；异常也会如实记录（finally）。"""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.record(name, time.perf_counter() - t0)

    def note(self, text: str) -> None:
        """附加信息（文本字数 / 候选数量 / 记录 id 等），跟随汇总行一起打印。"""
        self.notes.append(text)

    def summary(self) -> str:
        parts = [f"{name}={fmt(sec)}" for name, sec in self.stages]
        parts.append(f"总计={fmt(self.elapsed)}")
        parts.extend(self.notes)
        return " | ".join(parts)

    def as_dict(self) -> dict:
        """结构化结果 {"stages": {阶段名: 秒}, "total": 秒}，供落库 / 汇总报表使用。"""
        return {"stages": {name: round(sec, 2) for name, sec in self.stages},
                "total": round(self.elapsed, 2)}

    def log(self, error: str = "") -> None:
        """打印汇总行。`error` 非空表示这一步失败了（日志级别 ERROR，便于 grep）。"""
        line = f"{self.label} | {self.summary()}"
        if error:
            logger.error("%s | 失败：%s", line, error)
        elif self.elapsed >= SLOW_TOTAL_SECONDS:
            logger.warning("%s（整单偏慢）", line)
        else:
            logger.info(line)


@contextmanager
def stage(timer: StageTimer | None, name: str) -> Iterator[None]:
    """在给定计时器上打点；`timer` 为 None 时退化为空操作。"""
    if timer is None:
        yield
        return
    with timer.stage(name):
        yield


def queue_wait_seconds(enqueued_at: float | None) -> float | None:
    """后台任务的排队等待时长（接口侧提交 → 工作线程真正开始执行）。

    批量上传几百份简历时，这个值往往远大于解析本身 —— 单看"解析慢"会误判为性能问题。
    """
    if not enqueued_at:
        return None
    return max(0.0, time.perf_counter() - enqueued_at)
