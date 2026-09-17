"""简历文件文本提取：PDF/Word 直取文本，图片与扫描件走 PaddleOCR。"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ocr_engine = None          # PaddleOCR 懒加载
_ocr_init_error: str | None = None   # 初始化失败原因（避免每次上传都重试一遍慢失败）
# PaddleOCR 初始化很重（要载入 4 个模型），且 C++ 推理层不是线程安全的：
# 并发解析时既会重复初始化，也可能直接 native crash。这里统一串行化。
_ocr_lock = threading.Lock()


def _is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _ascii_model_home() -> Path | None:
    """挑一个纯 ASCII 且可写的目录作为 PaddleX 模型缓存根；找不到返回 None。

    优先放后端包同级的 data/ 下（与 settings.upload_dir 同处），用 __file__ 定位而不是
    相对路径 —— 否则从不同工作目录启动会指向不同位置，导致模型反复重新下载。
    """
    backend_root = Path(__file__).resolve().parents[2]      # .../backend
    candidates = [
        backend_root / "data" / "paddlex_models",
        Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "paddlex_models",
    ]
    for path in candidates:
        if not _is_ascii(str(path)):
            continue
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        return path
    return None


def _migrate_models(src: Path, dst: Path) -> None:
    """把已下载在旧目录（中文路径）的模型复制到 ASCII 目录，避开重复下载。"""
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for child in src.iterdir():
        target = dst / child.name
        if target.exists():
            continue
        try:
            if child.is_dir():
                shutil.copytree(child, target)
            else:
                shutil.copy2(child, target)
        except OSError as e:
            logger.warning("迁移 OCR 模型 %s 失败: %s", child.name, e)


def _prepare_paddlex_home() -> None:
    """把 PaddleX 模型缓存目录切到纯 ASCII 路径（必须在 import paddleocr 之前调用）。

    Paddle Inference 的 C++ 层用窄字符打开模型文件，路径含中文（如 Windows 用户名
    「中科」→ C:\\Users\\中科\\.paddlex\\...）时读不到内容，会报
    `json.exception.parse_error.101 ... attempting to parse an empty input`。
    """
    if os.environ.get("PADDLE_PDX_CACHE_HOME"):
        return                                   # 已显式指定，尊重用户配置
    default_home = Path.home() / ".paddlex"
    if _is_ascii(str(default_home)):
        return                                   # 家目录本就是 ASCII，无需处理
    target = _ascii_model_home()
    if target is None:
        logger.warning("找不到可写的 ASCII 目录存放 OCR 模型，中文路径下 OCR 可能无法加载")
        return
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(target)
    logger.info("用户目录含非 ASCII 字符，OCR 模型缓存改至 %s", target)
    _migrate_models(default_home / "official_models", target / "official_models")


def _prepare_ocr_offline_env() -> None:
    """强制 PaddleX「只用本地缓存」加载 OCR 模型（必须在 import paddleocr 之前调用）。

    paddlex 3.x 的默认模型源是 **huggingface**（`paddlex/utils/flags.py` 里
    `MODEL_SOURCE = os.environ.get("PADDLE_PDX_MODEL_SOURCE", "huggingface")`），
    而且首次初始化时还会先对 huggingface.co / modelscope / bos 逐个做连通性探测。
    内网既连不上、探测也常被黑洞丢包卡住，所以这里：

    - `PADDLE_PDX_MODEL_SOURCE=bos`：模型源换掉 HF（百度官方源）；命中本地缓存后根本不会用到它；
    - `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True`：跳过联网探测，初始化少等一大截。

    用 `setdefault` 是为了不覆盖运维显式设置的环境变量。
    模型目录由镜像烘焙（见 `backend/Dockerfile`），Docker 里对应 `PADDLE_PDX_CACHE_HOME`。
    """
    os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

    cache_home = os.environ.get("PADDLE_PDX_CACHE_HOME") or str(Path.home() / ".paddlex")
    models_dir = Path(cache_home) / "official_models"
    missing = [m for m in ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec",
                           "PP-LCNet_x1_0_textline_ori")
               if not (models_dir / m).is_dir()]
    if missing:
        # 内网无外网，缺失的模型下不回来，直接提示清楚比等超时好排查
        logger.warning("OCR 模型缓存缺少：%s（目录 %s）。内网环境无法自动下载，请把模型目录补齐。",
                       "、".join(missing), models_dir)
    else:
        logger.info("OCR 模型就绪（本地缓存，不联网）：%s", models_dir)


def _build_ocr_engine():
    """构造 PaddleOCR 引擎（兼容 3.x / 2.x 的参数名）。

    两个参数是刻意关的：

    1. `use_doc_orientation_classify=False` + `use_doc_unwarping=False`：
       PaddleOCR 3.x 默认开着「文档方向分类（PP-LCNet_x1_0_doc_ori）」和「文档矫正（UVDoc）」，
       等于每页多跑两个模型（UVDoc 单权重 32MB）且对简历扫描件收益很低（基本都是正的、无明显扭曲）。
       关掉后 DocPreprocessor 整段跳过：初始化少载 2 个模型、单页识别更省 CPU。
       文本行方向（PP-LCNet_x1_0_textline_ori）仍保留，它负责把倒置的**行**转正。

    2. `enable_mkldnn=False`：paddlepaddle 3.3.x 的 PIR 新执行器 + oneDNN 在
       PP-OCRv6 检测模型上会抛 `NotImplementedError: ConvertPirAttribute2RuntimeAttribute
       not support`（onednn_instruction.cc）。关掉 oneDNN 走普通 CPU 内核即正常识别，
       代价是少量 CPU 推理速度（OCR 在后台任务里跑，可接受）。
    """
    from paddleocr import PaddleOCR

    try:
        # PaddleOCR 3.x：文本行方向参数名为 use_textline_orientation
        return PaddleOCR(lang="ch", use_textline_orientation=True,
                         use_doc_orientation_classify=False,
                         use_doc_unwarping=False,
                         enable_mkldnn=False)
    except TypeError:
        # 旧版 PaddleOCR 2.x：参数名为 use_angle_cls，且没有文档预处理开关
        return PaddleOCR(lang="ch", use_angle_cls=True, enable_mkldnn=False)


def _run_ocr(engine, img) -> Any:
    """PaddleOCR 3.x 用 predict()，2.x 用 ocr()。"""
    if hasattr(engine, "predict"):
        return engine.predict(img)
    return engine.ocr(img, cls=True)


def _collect_ocr_text(result) -> list[str]:
    """兼容两种返回格式：3.x 为 dict（rec_texts），2.x 为 [[box, (text, score)], ...]。"""
    lines: list[str] = []
    for block in result or []:
        texts = block.get("rec_texts") if hasattr(block, "get") else None
        if texts:
            lines.extend(str(t) for t in texts)
            continue
        for item in block or []:                 # 2.x 旧格式
            try:
                lines.append(str(item[1][0]))
            except (TypeError, IndexError):
                continue
    return lines


def _extract_pdf(path: Path) -> str:
    import fitz  # PyMuPDF

    t0 = time.perf_counter()
    text_parts: list[str] = []
    with fitz.open(path) as doc:
        pages = doc.page_count
        for page in doc:
            text_parts.append(page.get_text())
    text = "\n".join(text_parts).strip()
    if len(text) < 50:  # 判定为扫描件，回退 OCR
        logger.info("PDF 文本过少（%d 字 / %d 页），按扫描件走 OCR: %s",
                    len(text), pages, path.name)
        return _extract_pdf_ocr(path)
    logger.info("PDF 文本直取完成：%s，%d 页 / %d 字，耗时 %.2fs",
                path.name, pages, len(text), time.perf_counter() - t0)
    return text


def _extract_pdf_ocr(path: Path) -> str:
    import fitz

    t0 = time.perf_counter()
    texts: list[str] = []
    with fitz.open(path) as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            img_bytes = pix.tobytes("png")
            texts.append(_ocr_image_bytes(img_bytes))
    text = "\n".join(texts)
    logger.info("扫描件 OCR 完成：%s，%d 页 / %d 字，耗时 %.2fs",
                path.name, len(texts), len(text), time.perf_counter() - t0)
    return text


def _extract_docx(path: Path) -> str:
    import docx

    doc = docx.Document(str(path))
    return "\n".join(p.text for p in doc.paragraphs if p.text.strip())


def _ocr_image_bytes(img_bytes: bytes) -> str:
    global _ocr_engine, _ocr_init_error
    try:
        import numpy as np
        from PIL import Image
    except ImportError as e:
        raise ValueError(
            "该文件为扫描件/图片，需要 OCR 才能解析，但当前环境未安装 OCR 依赖"
            "（numpy / Pillow / paddleocr）。请在 backend 环境执行："
            "pip install numpy Pillow paddlepaddle paddleocr 后重新解析。"
        ) from e

    if _ocr_init_error:
        raise ValueError(f"OCR 引擎不可用（初始化时失败）：{_ocr_init_error}")

    if _ocr_engine is None:
        with _ocr_lock:                  # 并发解析时只初始化一次
            if _ocr_engine is None:
                if _ocr_init_error:      # 等锁期间别的线程已确认初始化失败
                    raise ValueError(f"OCR 引擎不可用（初始化时失败）：{_ocr_init_error}")
                _prepare_paddlex_home()        # 必须早于 import paddleocr
                _prepare_ocr_offline_env()     # 同上：联网相关开关都必须在导入前设好
                try:
                    init_start = time.perf_counter()
                    _ocr_engine = _build_ocr_engine()
                    # 首次初始化要载入 4 个模型（含下载），常常是整条链上最慢的一步
                    logger.info("PaddleOCR 引擎初始化完成，耗时 %.2fs",
                                time.perf_counter() - init_start)
                except ImportError as e:
                    raise ValueError(
                        "该文件为扫描件/图片，需要 OCR 才能解析，但当前环境未安装 paddleocr。"
                        "请在 backend 环境执行：pip install paddlepaddle paddleocr 后重新解析。"
                    ) from e
                except Exception as e:
                    logger.exception("OCR 引擎初始化失败")
                    _ocr_init_error = str(e)
                    raise ValueError(
                        f"OCR 引擎初始化失败：{e}。"
                        "常见原因：OCR 模型缓存缺失（内网无法自动下载），"
                        "或缓存目录不可写（详见后端日志）。"
                    ) from e

    import io

    img = np.array(Image.open(io.BytesIO(img_bytes)).convert("RGB"))
    lock_start = time.perf_counter()
    try:
        # 识别阶段同样加锁：PaddleOCR 的 C++ 推理不保证线程安全，
        # 并发调用在 Windows 上实测会卡死/崩溃（OCR 在后台任务里跑，串行化可接受）。
        with _ocr_lock:
            lock_wait = time.perf_counter() - lock_start
            recog_start = time.perf_counter()
            result = _run_ocr(_ocr_engine, img)
            recog_seconds = time.perf_counter() - recog_start
    except Exception as e:
        logger.exception("OCR 文字识别失败")
        raise ValueError(f"OCR 文字识别失败：{e}") from e
    lines = _collect_ocr_text(result)
    # 「等锁」= 被其他并发解析任务挡住的时间，与真正的识别耗时分开看才能判断该调并发还是调 CPU
    logger.info("OCR 识别完成：等锁 %.2fs + 识别 %.2fs，输出 %d 行文本",
                lock_wait, recog_seconds, len(lines))
    return "\n".join(lines)


def extract_text(path: str | Path) -> str:
    """按扩展名分发提取。支持 .pdf/.docx/.doc(提示转docx)/.png/.jpg。"""
    path = Path(path)
    suffix = path.suffix.lower()
    start = time.perf_counter()
    if suffix == ".pdf":
        text = _extract_pdf(path)
    elif suffix in (".docx",):
        text = _extract_docx(path)
    elif suffix in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
        text = _ocr_image_bytes(path.read_bytes())
    elif suffix == ".doc":
        raise ValueError("旧版 .doc 请先转换为 .docx 再上传")
    else:
        raise ValueError(f"不支持的文件类型: {suffix}")
    logger.info("文本提取完成：%s（%s），%d 字，耗时 %.2fs",
                path.name, suffix, len(text), time.perf_counter() - start)
    return text
