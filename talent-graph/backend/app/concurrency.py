"""重负载后台任务的执行队列（进程内，单实例内部工具）。

简历解析/匹配包含 OCR、本地 BGE-M3 推理、LLM HTTP 调用，单个任务可能跑数十秒到
几分钟。之前把这些任务直接丢给 FastAPI 的 BackgroundTasks，会踩两个坑：

1. **线程被占满**：BackgroundTasks 的同步函数跑在 anyio 的全局线程池（默认 40 线程），
   与所有同步接口共用。批量上传简历时几十个后台任务会把线程池占满，普通查询接口
   （简历列表等）拿不到线程，页面表现为卡死。
2. **并发无上限**：这些任务同时抢数据库连接（`QueuePool limit ... timeout`）、抢 CPU、
   触发上游 LLM 平台限流，反而拖慢整批。

所以改成「专用工作线程池 + 内存任务队列」：接口侧只 `submit_heavy_task(...)` 入队
（内存操作，立即返回，不占用请求线程），真正并发执行的数量固定为
`settings.heavy_task_concurrency`。

注意：队列在内存里，进程重启会丢未执行的任务 —— 与原先 BackgroundTasks 的行为一致，
启动时 `main._recover_stuck_parsing()` 会把卡在「解析中」的简历标出来供人工重跑。
二期换 Celery/RQ 时本模块可直接删掉。
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .config import settings

logger = logging.getLogger(__name__)

_QUEUE_WARN_THRESHOLD = 20   # 积压超过该数量时告警，便于发现任务堆积
_PENDING_LOG_STEP = 20       # 每积压这么多任务打印一次进度
_STALL_WARN_SECONDS = 120    # 单个任务运行超过该时长就告警一次（"卡住却没日志"的兜底）
_SHUTDOWN_GRACE_SECONDS = 30  # 关闭时绐重活线程的收尾时间，超时直接硬退出进程

_executor: ThreadPoolExecutor | None = None
# 可重入：_get_executor() 持锁期间会调 _start_exit_watchdog()，后者也要这把锁
# （普通 Lock 会自死锁，导致第一次 submit_heavy_task 就卡住 —— 已踩过）
_executor_lock = threading.RLock()
_count_lock = threading.Lock()
_pending = 0                 # 已入队、尚未执行完的任务数
_batch_id = ""               # 当前批次 id（队列由空变非空时新开一批，清空后打印汇总）
_exit_watchdog_started = False   # 关闭兜底线程是否已启动（见 _start_exit_watchdog）


class ProcessingInterrupted(RuntimeError):
    """进程正在退出（Ctrl+C / --reload 重启 / 容器停止）导致处理被打断。

    与业务失败区分开：调用方只需安静结束并留一行说明 —— 不要打异常堆栈，
    也不要把简历标成「解析失败」（重启时 `main._recover_stuck_parsing()` 会接管
    仍在「解析中」的简历，提示用户点「重新解析」）。
    """


def shutting_down() -> bool:
    """解释器是否正在关闭。

    关闭时 `ThreadPoolExecutor` 会拒绍新任务并抛
    `RuntimeError: cannot schedule new futures after interpreter shutdown`，
    后台任务里的并发调用（如 LLM 精排）就会这样报错 —— 本质是"进程正在退出"。

    判定依据：`sys.is_finalizing()`，以及 concurrent.futures 的 `_shutdown` 标志
    （后者由 `_python_exit` 在退出流程最早期置位，比 is_finalizing 更早生效；
    私有属性用 getattr 容错，不同 Python 版本拿不到时退化为 False）。
    """
    if sys.is_finalizing():
        return True
    return bool(getattr(concurrent.futures.thread, "_shutdown", False))


def _get_executor() -> ThreadPoolExecutor:
    """懒创建专用线程池（首次提交任务时启动，避免 import 阶段就起线程）。"""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                workers = max(1, settings.heavy_task_concurrency)
                _executor = ThreadPoolExecutor(max_workers=workers,
                                               thread_name_prefix="tg-worker")
                logger.info("后台任务工作线程池已启动：%d 个并发", workers)
                _start_exit_watchdog()
    return _executor


def _start_exit_watchdog() -> None:
    """启动「关闭兜底」线程（只在第一次提交重活任务时启动一次）。

    ThreadPoolExecutor 的工作线程是**非 daemon** 的：解释器退出时 concurrent.futures
    会 `_python_exit` → join 它们，而 torch / PaddleOCR 的 C++ 计算**无法被打断** ——
    进程于是永远退不掉，`uvicorn --reload` 的 reloader 卡在 `process.join()` 上，
    表现为"端口还在监听但请求全部挂起"的假死（实测出现过两次，一次几十小时）。

    这里在检测到进程开始退出后最多再等 `_SHUTDOWN_GRACE_SECONDS` 秒，到点直接
    `os._exit()`：正在跑的活本来就会丢（重启后可在页面点「重新解析」），
    但服务能立即恢复，而不是假死。
    """
    global _exit_watchdog_started
    with _executor_lock:
        if _exit_watchdog_started:
            return
        _exit_watchdog_started = True
    threading.Thread(target=_exit_countdown, name="tg-exit-watchdog", daemon=True).start()


def _exit_countdown() -> None:
    """每秒检查一次进程是否开始退出；退出时超时则硬退。"""
    while True:
        time.sleep(1.0)
        if not shutting_down():
            continue
        logger.warning("服务正在关闭：%d 秒内若未能正常退出（有 OCR/embedding 重活无法打断），"
                       "将强制结束进程以免 reloader 假死", _SHUTDOWN_GRACE_SECONDS)
        time.sleep(_SHUTDOWN_GRACE_SECONDS)
        logger.error("重活线程未能在 %d 秒内结束，强制退出进程（未完成的任务重启后请重新解析）",
                     _SHUTDOWN_GRACE_SECONDS)
        os._exit(1)


def _stall_watchdog(name: str, started: float, stop: threading.Event) -> None:
    """心跳线程：任务长时间没结束就定期告警。

    任务是同步阻塞调用（OCR / 大模型 / embedding 网络请求），卡住时既不会打日志也不会返回，
    表现为"上传后一直没结果、后台也没日志"。这里每 `_STALL_WARN_SECONDS` 秒点名一次，
    让卡住这件事在日志里可见；线程栈可用 `py-spy dump --pid <后端进程>` 直接看。
    """
    while not stop.wait(_STALL_WARN_SECONDS):
        logger.warning("后台任务 %s 已运行 %.0f 秒仍未结束（可能卡在 OCR/大模型/embedding 网络请求），"
                       "排查线程栈：py-spy dump --pid %d",
                       name, time.perf_counter() - started, os.getpid())


def _run(func: Callable[..., Any], args: tuple, kwargs: dict) -> None:
    """工作线程入口：兜住异常，保证单个任务失败不会打挂线程。"""
    global _pending
    name = getattr(func, "__name__", str(func))
    started = time.perf_counter()
    stop = threading.Event()
    watchdog = threading.Thread(target=_stall_watchdog, args=(name, started, stop),
                                name=f"tg-watchdog-{name}", daemon=True)
    watchdog.start()
    try:
        func(*args, **kwargs)
    except ProcessingInterrupted as e:
        # 进程正在退出：只留一行说明，不打异常堆栈也不当业务失败
        logger.warning("后台任务 %s 因服务关闭被中断（已耗时 %.2fs）：%s",
                       name, time.perf_counter() - started, e)
    except Exception:
        # 兜底日志：任务内部已各自打阶段耗时汇总，走到这里说明连会话/计时都没建起来
        logger.exception("后台任务 %s 执行失败（已耗时 %.2fs）", name, time.perf_counter() - started)
    finally:
        stop.set()
        with _count_lock:
            _pending -= 1
            finished_batch = _batch_id if _pending == 0 else ""
        if finished_batch:
            if shutting_down():
                # 关闭途中：本批任务是被打断的，打印汇总会误导，指引去查库
                logger.info("服务正在关闭，跳过本批耗时汇总打印（已入库的耗时记录仍可在 "
                            "pipeline_metrics 表中查询）")
            else:
                # 队列已清空：简历与岗位都处理完了，把本批各阶段耗时查出来打印
                from .services import metrics
                metrics.print_batch_report(finished_batch)


def submit_heavy_task(func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
    """把重负载任务排入队列后立即返回（不阻塞请求线程）。

    任务内部各自负责建会话、捕获异常并写回失败原因，这里只做并发调度。
    队列由空变非空时新开一个「批次」：整批任务跑完（_pending 归零）会打印耗时汇总表。
    """
    global _pending, _batch_id
    if shutting_down():
        # 服务正在关闭：提交上去也不会被执行（线程池已停止接受任务），直接丢弃并说明
        logger.warning("服务正在关闭，忽略新任务 %s（重启后可在页面点「重新解析」重跑）",
                       getattr(func, "__name__", str(func)))
        return
    executor = _get_executor()
    with _count_lock:
        if _pending == 0:
            from .services import metrics
            _batch_id = metrics.start_batch()
        _pending += 1
        pending = _pending
    if pending > _QUEUE_WARN_THRESHOLD and pending % _PENDING_LOG_STEP == 0:
        logger.warning("后台任务积压：待执行 %d 个（并发上限 %d），解析/匹配会延迟完成",
                       pending, settings.heavy_task_concurrency)
    logger.debug("后台任务入队：%s（待执行 %d 个）",
                 getattr(func, "__name__", str(func)), pending)
    executor.submit(_run, func, args, kwargs)


def heavy_queue_status() -> dict:
    """当前排队情况，便于排障（日志 / 健康检查）。"""
    with _count_lock:
        pending = _pending
    workers = max(1, settings.heavy_task_concurrency)
    return {"pending": pending, "workers": workers,
            "running": min(pending, workers), "waiting": max(0, pending - workers)}
