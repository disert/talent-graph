"""上游依赖（大模型 / Embedding / 平台接口）的请求-响应日志。

内网排查最难受的是「请求到底有没有发出去、发到哪个地址、对端回了什么」——
OpenAI SDK 默认一句报文都不打，失败时只有 `Connection error.`，无法区分
DNS 解析不了 / 端口不通 / 路径拼错 404 / 模型名不对 400 / 向量维度不匹配。

本模块提供：
- `new_http_client(kind, timeout)`：带 httpx 事件钩子的客户端。钩子在**报文实际发出/回来**时触发，
  记的是真实 URL（能暴露 base_url 被 SDK 二次拼接的问题）、请求体预览、状态码、耗时、响应体预览。
- `ellipsis` / `json_preview`：统一截断，避免把几十 KB 的 prompt 灌进日志；密钥只报「是否设置」。
- `describe_exception` / `network_hint`：把异常链摊平成一行，并给出中文排查方向。
- `check_base_url`：启动时自检 base_url 是否已带接口路径（最常见的 404 原因）。

开关见 config.settings.debug_upstream（.env 的 DEBUG_UPSTREAM）；
关闭时只保留「慢调用 + 失败」，避免逐条精排时刷屏。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger(__name__)


# ---------- 通用格式化工具 ----------

def describe_secret(value: str | None) -> str:
    """密钥只报「有没有配 / 多长」，绝不打印内容（日志可能被贴到工单/聊天里）。"""
    return f"(已设置, {len(value)} 字符)" if value else "(未设置)"


def ellipsis(text: Any, limit: int | None = None) -> str:
    """按配置截断长文本（0 = 不截断）。"""
    limit = settings.upstream_log_max_chars if limit is None else limit
    text = str(text or "")
    if not limit or limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]} …(共 {len(text)} 字符，已截断)"


def json_preview(obj: Any, limit: int | None = None) -> str:
    """把任意对象转成一行 JSON 预览（序列化失败也返回可读文本，不影响主流程）。"""
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except Exception as e:                     # 极少数含不可序列化对象的情况
        text = f"<无法序列化: {e}> {obj!r}"
    return ellipsis(text, limit)


# ---------- 异常信息 ----------

def _iter_exceptions(exc: BaseException):
    """按 cause/context 逐层展开异常链（最多 5 层，避免自引用死循环）。"""
    cur: BaseException | None = exc
    for _ in range(5):
        if cur is None:
            return
        yield cur
        nxt = cur.__cause__ or cur.__context__
        cur = None if nxt is cur else nxt


def describe_exception(exc: BaseException) -> str:
    """把异常链摊平成一行，例如：`APIConnectionError: Connection error. <- ConnectError: ...`。

    同时带上 HTTP 状态码与响应体片段（`APIStatusError` 才有），400/404 一眼能看出对端说了什么。
    """
    parts: list[str] = []
    for err in _iter_exceptions(exc):
        text = str(err).strip().replace("\n", " ")
        status = getattr(err, "status_code", None)
        if status is not None:
            text = f"{text} [HTTP {status}]"
        detail = _response_snippet(err)
        if detail:
            text = f"{text} 响应体={detail}"
        parts.append(f"{type(err).__name__}: {ellipsis(text, 500)}")
    return " <- ".join(parts) if parts else repr(exc)


def _response_snippet(err: BaseException) -> str:
    """从 openai 的 APIStatusError 里取响应体片段（对端 400 的具体原因一般在这里）。"""
    resp = getattr(err, "response", None)
    if resp is None:
        return ""
    try:
        return ellipsis(resp.text, 300)
    except Exception:
        return ""


# 常见网络错误的排查方向：内网里这几种表现完全不同，提示清楚能省很多时间
_NET_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("name or service not known", "nodename nor servname", "getaddrinfo",
      "failed to resolve", "no address associated"),
     "域名解析失败：内网 DNS 解析不出该主机名。核对 BASE_URL 里的主机名/IP，或直接改用 IP。"),
    (("connection refused", "connect call failed", "actively refused"),
     "连接被拒绝：目标端口没有服务监听，或被安全管控/中间设备拦截（这类拦截常见于等保加固的服务器）。"),
    (("timed out", "timeout", "read timed out"),
     "连接或读取超时：网络不通或被黑洞丢包（对端不回 RST 时会一直挂到超时）。"
     "确认从**容器内**能连通该地址（docker exec 进去 curl 一下），注意宿主机代理对容器无效。"),
    (("certificate", "ssl", "tls"),
     "TLS 证书校验失败：内网自签证书需要把 CA 装进容器，或改用 http。"),
    (("proxy",), "代理相关错误：检查 HTTP_PROXY/HTTPS_PROXY/NO_PROXY（httpx 默认会读这些变量）。"),
)


def network_hint(exc: BaseException) -> str:
    """按异常文案匹配一条中文排查方向；匹配不到返回空串。"""
    text = " ".join(str(e) for e in _iter_exceptions(exc)).lower()
    for keys, hint in _NET_HINTS:
        if any(k in text for k in keys):
            return hint
    return ""


# ---------- base_url 自检 ----------

_warned_urls: set[str] = set()     # 同一个 URL 只提醒一次（匹配时会调用成百上千次）


def check_base_url(kind: str, base_url: str, endpoint: str) -> None:
    """启动时自检 BASE_URL 是否已经带了接口路径。

    OpenAI SDK 会自己在 base_url 后面拼 `/chat/completions`、`/embeddings`：
      - `LLM_BASE_URL=.../chat/completions` → 真实 URL 变成 `.../chat/completions/chat/completions`
      - `EMBEDDING_BASE_URL=.../chat/completions` → 真实 URL 变成 `.../chat/completions/embeddings`
    内网排查时表现为 404 / 400，且后端日志毫无线索（就是这里要提醒的原因）。
    """
    base = (base_url or "").rstrip("/")
    if not base:
        return
    if base.endswith("/chat/completions") and endpoint != "/chat/completions":
        logger.warning("[%s 配置可疑] BASE_URL=%s 指向 chat/completions，但本次要调的是 %s 接口，"
                       "SDK 会拼成 %s%s —— 通常是 404 的根因。BASE_URL 只需写到平台根路径（如 /v1）。",
                       kind, base_url, endpoint, base, endpoint)
    elif base.endswith(endpoint):
        logger.warning("[%s 配置可疑] BASE_URL=%s 已经以 %s 结尾，SDK 还会再拼一次，"
                       "真实请求会变成 %s%s。建议 BASE_URL 只写到 /v1 或平台根路径。",
                       kind, base_url, endpoint, base, endpoint)


def warn_url_once(kind: str, url: str) -> None:
    """实际发出的 URL 与预期接口不符时提醒一次（可能被平台前缀路由掩盖的配置问题）。"""
    if url in _warned_urls:
        return
    hits = url.count("/chat/completions")
    if kind == "Embedding" and (hits or "/embeddings" not in url):
        _warned_urls.add(url)
        logger.warning("[%s 请求地址可疑] 真实请求 URL = %s（其中 /chat/completions 出现 %d 次）。"
                       "Embedding 应为 <BASE_URL>/embeddings，请核对 EMBEDDING_BASE_URL。",
                       kind, url, hits)
    elif hits > 1:
        _warned_urls.add(url)
        logger.warning("[%s 请求地址可疑] 真实请求 URL = %s（/chat/completions 出现 %d 次），"
                       "说明 BASE_URL 里已含该路径、SDK 又拼了一次。", kind, url, hits)


# ---------- httpx 事件钩子 ----------

def _request_body_text(request: httpx.Request) -> str:
    """取请求体文本；取不到（流式/已消费）时返回空串，绝不因日志抛异常。"""
    try:
        content = request.content
    except Exception:
        try:
            content = request.read()
        except Exception:
            return ""
    try:
        return content.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _response_body_text(response: httpx.Response) -> str:
    """取响应体文本；SSE 流式响应不读（会把流消费掉），读失败也不影响主流程。"""
    ctype = (response.headers.get("content-type") or "").lower()
    if "event-stream" in ctype:
        return "(SSE 流式响应，不记录正文)"
    try:
        response.read()
        return response.text
    except Exception as e:
        return f"(响应体读取失败: {type(e).__name__}: {e})"


def new_http_client(kind: str, timeout: float) -> httpx.Client:
    """构造带日志钩子的 httpx 客户端，交给 OpenAI SDK 使用。

    钩子里读的是**报文实际内容**，而不是我们以为自己发了什么 —— 这正是排查
    「base_url 拼错 / 平台返 200 但内容是错误 JSON / 返回零向量」最可靠的一手信息。
    """
    def on_request(request: httpx.Request) -> None:
        request.extensions["tg_started_at"] = time.perf_counter()
        url = str(request.url)
        body = _request_body_text(request)
        line = (f"[{kind} 请求] {request.method} {url} | 请求体 {len(body)} 字节 | "
                f"{ellipsis(body)}")
        if settings.debug_upstream:
            logger.info(line)
        else:
            logger.debug(line)
        warn_url_once(kind, url)

    def on_response(response: httpx.Response) -> None:
        request = response.request
        started = request.extensions.get("tg_started_at") or time.perf_counter()
        seconds = time.perf_counter() - started
        body = _response_body_text(response)
        line = (f"[{kind} 响应] {response.status_code} {request.method} {request.url} | "
                f"{seconds:.2f}s | 响应体 {len(body)} 字节 | {ellipsis(body)}")
        if response.status_code >= 400:
            logger.warning(line)          # 400/404/500 一律打，这是最有价值的一行
        elif settings.debug_upstream:
            logger.info(line)
        else:
            logger.debug(line)

    return httpx.Client(timeout=timeout,
                        event_hooks={"request": [on_request], "response": [on_response]})
