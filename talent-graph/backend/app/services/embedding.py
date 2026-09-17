"""向量化服务：优先走内部平台 Embedding 接口；未配置时本地加载 BGE-M3。

两种实现输出维度必须一致（默认 1024），与 resumes/job_requests 表的 Vector 列对齐。

**本地模型务必走离线加载**：sentence-transformers / transformers 默认 `local_files_only=False`，
即使模型已在本地缓存，也会先向 huggingface.co 发一次 HEAD 请求校验文件。国内/内网访问
huggingface.co 常被黑洞丢包（TCP 连不上、也不回 RST），该请求会一直挂着；又因为模型加载在
`_local_model_lock` 里，所有后台工作线程会一起卡死，整条简历解析队列停滞（实测 25 分钟零进展）。
所以这里先判断「是否已能离线拿到模型」，命中就设 `HF_HUB_OFFLINE=1`，彻底不联网。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

_local_model = None  # 懒加载，避免未装 sentence-transformers 的环境启动失败
_local_model_lock = threading.Lock()   # 并发解析时保证模型只加载一次（否则每个线程各载一份 ~2GB）

# 判定「缓存里有模型权重」时认这些文件名（只有 config.json 的半截缓存照样要联网）
_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin", "tf_model.h5")


def _hf_cache_root() -> Path:
    """HuggingFace 缓存根目录（尊重 HF_HOME / HUGGINGFACE_HUB_CACHE 环境变量）。"""
    if os.environ.get("HUGGINGFACE_HUB_CACHE"):
        return Path(os.environ["HUGGINGFACE_HUB_CACHE"])
    if os.environ.get("HF_HOME"):
        return Path(os.environ["HF_HOME"]) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _model_available_offline(path: str) -> bool:
    """模型能否**不联网**直接用：给了本地目录，或 HF 缓存里已有含权重的完整快照。"""
    local_dir = Path(path)
    if local_dir.is_dir():
        return True
    snapshots = _hf_cache_root() / ("models--" + path.replace("/", "--")) / "snapshots"
    if not snapshots.is_dir():
        return False
    return any((snap / name).exists()
               for snap in snapshots.iterdir()
               for name in _WEIGHT_FILES)


def _prepare_hf_env(path: str) -> bool:
    """在 import transformers 之前配置 HF 环境变量，返回是否走离线模式。

    必须在导入前设置：huggingface_hub 的 HF_HUB_OFFLINE / HF_ENDPOINT 是 import 时读取的常量，
    之后再改环境变量不生效。

    离线：命中本地缓存 / 本地目录 -> HF_HUB_OFFLINE=1（快，且不会卡）。
    硬离线（OFFLINE_MODE=true，内网镜像默认开）：模型不在本地就直接报错，
          绝不去连 huggingface.co —— 内网连不上又可能被黑洞丢包，会把整条解析队列挂死。
    在线：确实需要首次下载 -> 走镜像（HF_ENDPOINT）+ 有界超时，失败快速抛出，
          不再无限重试把整条解析队列拖死。
    """
    if _model_available_offline(path):
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        logger.info("本地 Embedding 模型命中本地缓存，离线加载（不联网）: %s", path)
        return True

    if settings.offline_mode:
        raise RuntimeError(
            f"OFFLINE_MODE=true（内网离线模式），但本地 Embedding 模型 {path} 不在本地缓存。"
            "二选一：① 把 EMBEDDING_BASE_URL 配成内网平台接口（推荐，镜像已默认这么配）；"
            "② 在能联网的机器上把模型下好，把目录拷进容器并把 EMBEDDING_LOCAL_PATH 指向它。"
        )

    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)
    if settings.hf_endpoint:
        os.environ.setdefault("HF_ENDPOINT", settings.hf_endpoint)
    timeout = str(int(settings.hf_timeout))
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", timeout)
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", timeout)
    logger.warning(
        "本地 Embedding 模型 %s 不在本地缓存，将尝试联网下载（endpoint=%s，单次超时 %ss）。"
        "若长时间无响应，请改配 EMBEDDING_BASE_URL 走平台接口，或先在有网环境把模型下到缓存/本地目录。",
        path, os.environ.get("HF_ENDPOINT") or "https://huggingface.co", timeout)
    return False


def _embed_via_platform(texts: list[str]) -> list[list[float]]:
    from openai import OpenAI

    client = OpenAI(base_url=settings.embedding_base_url, api_key=settings.embedding_api_key,
                    timeout=settings.llm_timeout)
    # 显式声明 encoding_format=float：SiliconFlow 等平台的 bge-m3 不传该参数会报 20015
    start = time.perf_counter()
    resp = client.embeddings.create(model=settings.embedding_model, input=texts,
                                    encoding_format="float")
    logger.info("平台 Embedding 完成：%d 条，耗时 %.2fs", len(texts),
                time.perf_counter() - start)
    return [item.embedding for item in resp.data]


def _load_local_model():
    """线程安全地懒加载本地模型（双重检查，避免并发上传时多份模型同时载入内存）。"""
    global _local_model
    if _local_model is None:
        with _local_model_lock:
            if _local_model is None:
                path = settings.embedding_local_path
                offline = _prepare_hf_env(path)
                import_start = time.perf_counter()
                from sentence_transformers import SentenceTransformer

                logger.info("import sentence-transformers 完成，耗时 %.2fs（含 torch）",
                            time.perf_counter() - import_start)
                logger.info("加载本地 Embedding 模型: %s（离线=%s）", path, offline)
                start = time.perf_counter()
                try:
                    _local_model = SentenceTransformer(path)
                except Exception as e:
                    # 把话说清楚：模型要么在本地缓存/本地目录，要么得能联网下载；否则改平台接口
                    raise RuntimeError(
                        f"本地 Embedding 模型加载失败（{path}）：{e}。"
                        "可改配 EMBEDDING_BASE_URL 走平台接口，或在可联网环境先把模型下到本地"
                        "（内网可设 HF_ENDPOINT=https://hf-mirror.com 走镜像），"
                        "再把 EMBEDDING_LOCAL_PATH 指向本地目录。"
                    ) from e
                # 首次加载要读 ~2GB 权重，比推理本身慢得多；单独打点避免误判 embedding 慢
                logger.info("本地 Embedding 模型加载完成，耗时 %.2fs",
                            time.perf_counter() - start)
    return _local_model


def _embed_local(texts: list[str]) -> list[list[float]]:
    model = _load_local_model()
    start = time.perf_counter()
    vecs = model.encode(texts, normalize_embeddings=True)
    logger.info("本地 Embedding 推理完成：%d 条，耗时 %.2fs", len(texts),
                time.perf_counter() - start)
    return [v.tolist() for v in vecs]


def embed_texts(texts: list[str]) -> list[list[float]]:
    if settings.embedding_base_url:
        return _embed_via_platform(texts)
    return _embed_local(texts)


def embed_one(text: str) -> list[float]:
    return embed_texts([text])[0]


def _work_experiences_to_text(exps: list) -> str:
    lines = []
    for e in exps or []:
        if isinstance(e, dict):
            lines.append(" ".join(str(e.get(k) or "") for k in ("company", "title", "period", "description")).strip())
        else:
            lines.append(str(e))
    return "\n".join(x for x in lines if x)


def _achievements_to_text(ach: dict) -> str:
    if not isinstance(ach, dict):
        return str(ach or "")
    parts = []
    for key in ("papers", "patents", "projects", "awards"):
        vals = ach.get(key) or []
        parts.extend(str(v) for v in vals if v)
    return "\n".join(parts)


def _intention_to_text(structured: dict) -> str:
    detail = structured.get("intention_detail")
    parts = [str(structured.get("intention") or "")]
    if isinstance(detail, dict):
        parts.extend(str(v) for v in detail.values() if v)
    return " ".join(p for p in parts if p)


def resume_to_text(structured: dict, raw_text: str) -> str:
    """把简历揉成一段用于向量化的文本。"""
    parts = [
        str(structured.get("research") or ""),
        " ".join(structured.get("skills") or []),
        str(structured.get("work_history") or ""),
        _work_experiences_to_text(structured.get("work_experiences")),
        _achievements_to_text(structured.get("achievements")),
        _intention_to_text(structured),
        str(structured.get("major") or ""),
        str(structured.get("school") or ""),
        raw_text[:500],
    ]
    return "\n".join(p for p in parts if p)


def job_to_text(title: str, soft: dict, summary: str = "") -> str:
    parts = [
        title, summary,
        str(soft.get("research") or ""),
        " ".join(soft.get("skills") or []),
        str(soft.get("experience") or ""),
        str(soft.get("intention") or ""),
    ]
    return "\n".join(p for p in parts if p)
