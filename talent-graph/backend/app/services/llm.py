"""内部大模型平台客户端。

- 默认按 OpenAI 兼容协议（/v1/chat/completions）对接；若平台为自研协议，仅需改写本文件的 _chat()。
- 稳定性兜底：失败自动重试 N 次 + 指数退避。
- 结构化抽取优先使用 response_format=json_object（若平台支持 JSON 模式）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from typing import Any

from openai import OpenAI

from ..config import settings
from ..upstream_log import (check_base_url, describe_exception, describe_secret, ellipsis,
                            network_hint, new_http_client)

logger = logging.getLogger(__name__)

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        check_base_url("LLM", settings.llm_base_url, "/chat/completions")
        logger.info("初始化 LLM 客户端：base_url=%s | model=%s | key=%s | timeout=%ss",
                    settings.llm_base_url, settings.llm_model,
                    describe_secret(settings.llm_api_key), settings.llm_timeout)
        # http_client 挂日志钩子：记录真实请求 URL + 请求体预览 + 响应状态/正文预览
        # （OpenAI SDK 默认一句报文都不打，内网排查时完全看不到发到哪、回了什么）
        _client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                         timeout=settings.llm_timeout, max_retries=0,  # 重试自行控制
                         http_client=new_http_client("LLM", settings.llm_timeout))
    return _client


def _chat(messages: list[dict[str, str]], json_mode: bool = True) -> str:
    """带重试与指数退避的对话调用。"""
    kwargs: dict[str, Any] = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    last_err: Exception | None = None
    max_attempts = max(1, settings.llm_max_retries)
    for attempt in range(max_attempts):
        start = time.perf_counter()
        try:
            resp = get_client().chat.completions.create(
                model=settings.llm_model, messages=messages, temperature=0.1, **kwargs
            )
            _log_call(time.perf_counter() - start, attempt, messages, resp)
            return resp.choices[0].message.content or ""
        except Exception as e:  # 平台不支持 json 模式时降级
            if json_mode and "response_format" in str(e):
                logger.info("平台不支持 response_format=json_object，降级为普通调用重试")
                return _chat_no_json(messages)
            last_err = e
            already = time.perf_counter() - start
            # 完整异常链 + HTTP 状态码 + 对端响应体片段，内网失败时这一行最关键
            logger.warning("LLM 调用失败（第 %d/%d 次，本次耗时 %.2fs）：%s",
                           attempt + 1, max_attempts, already, describe_exception(e))
            hint = network_hint(e)
            if hint:
                logger.warning("LLM 失败排查方向：%s（当前 BASE_URL=%s，如需看真实请求 URL 请开 DEBUG_UPSTREAM=true）",
                               hint, settings.llm_base_url)
            if attempt + 1 < max_attempts:
                wait = 2 ** attempt
                logger.warning("%ds 后重试（共 %d 次）", wait, max_attempts)
                time.sleep(wait)
    raise RuntimeError(f"LLM 调用连续失败 {max_attempts} 次: {last_err}")


# 单次大模型调用超过该秒数就记一条 INFO（逐条精排会有成百上千次调用，全部打日志太吵）
_LLM_SLOW_SECONDS = 5.0


def _log_call(seconds: float, attempt: int, messages: list[dict[str, str]], resp: Any) -> None:
    """单次 LLM 调用汇总：耗时 / 入参规模 / 输出规模 / finish_reason / token 用量。

    常规走 DEBUG（逐条精排上千次调用，全打 INFO 会刷屏），偏慢或开了 DEBUG_UPSTREAM 才升 INFO。
    逐字输出不在这里打 —— 原始报文由 httpx 钩子打印，避免同一份内容重复两遍。
    """
    choice = resp.choices[0] if getattr(resp, "choices", None) else None
    content = (getattr(getattr(choice, "message", None), "content", "") or "")
    usage = getattr(resp, "usage", None)
    usage_text = ""
    if usage is not None:
        usage_text = (f" | tokens(prompt={getattr(usage, 'prompt_tokens', None)}"
                      f", completion={getattr(usage, 'completion_tokens', None)}"
                      f", total={getattr(usage, 'total_tokens', None)})")
    line = (f"[LLM 调用] model={settings.llm_model} 第{attempt + 1}次 | "
            f"入 {len(messages)} 条 / {sum(len(m.get('content') or '') for m in messages)} 字符 | "
            f"出 {len(content)} 字符 | finish={getattr(choice, 'finish_reason', None)} | "
            f"耗时 {seconds:.2f}s{usage_text}")
    if settings.debug_upstream or seconds >= _LLM_SLOW_SECONDS:
        logger.info(line)
    else:
        logger.debug(line)
    # 输出为空是最隐蔽的失败（模型没按格式回、被网关截断），单独提示
    if not content.strip():
        logger.warning("[LLM 输出为空] model=%s 第%d次调用返回空内容，finish_reason=%s "
                       "（可能是触发内容过滤/网关拦截/超时截断，可开 DEBUG_UPSTREAM=true 看原始响应）",
                       settings.llm_model, attempt + 1, getattr(choice, "finish_reason", None))


def _chat_no_json(messages: list[dict[str, str]]) -> str:
    start = time.perf_counter()
    resp = get_client().chat.completions.create(
        model=settings.llm_model, messages=messages, temperature=0.1
    )
    _log_call(time.perf_counter() - start, 0, messages, resp)
    return resp.choices[0].message.content or ""


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出中稳健地提取 JSON 对象。

    模型输出常见三种形态，都要能解析出来：
    1. 干净的 JSON；
    2. JSON 后面多了一段说明文字（或**再来一个 JSON 对象**）；
    3. 包在 ```json 代码块里。

    直接用 `json.loads` 在第 2 种情况下会抛
    `Extra data: line 3 column 1 (char ...)`（简历 115 就是这么失败的）；
    贪婪正则 `\\{.*\\}` 会把两段一起匹配，同样失败。
    这里用 `raw_decode`：从每个 `{` 起只解析**一个**完整对象，多余内容直接忽略。
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("LLM 输出为空，无法解析 JSON")
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue          # 这个 `{` 不是对象起点（如模板里的花括号），继续往后找
        if isinstance(obj, dict):
            return obj
    # 解析失败会把整份简历/岗位的解析一起拖失败，原始输出必须留证（截断，避免刷屏）
    logger.error("[LLM 输出解析失败] 原始输出（截断）：%s", ellipsis(text))
    raise ValueError(f"无法从 LLM 输出解析 JSON: {text[:200]}")


# ---------- 业务封装 ----------

RESUME_EXTRACT_PROMPT = """你是简历信息抽取专家。请从以下简历文本中抽取结构化字段，以 JSON 返回，字段如下：
{
  "name": "姓名",
  "education": "最高学历（博士/硕士/本科/其他）",
  "school": "毕业院校（最高学历对应的学校全称，如 华中科技大学；原文未提及填 null）",
  "major": "专业",
  "birth_date": "出生年月，原文照抄（如 1983年6月 / 1983-06-23），仅供后端推算年龄，未知填 null",
  "age": 年龄（数字；若原文只有出生年月，请按当前日期推算；未知填 null）,
  "phone": "电话",
  "email": "邮箱",
  "work_history": "工作履历摘要（100字内）",
  "work_experiences": [
    {"company": "单位名称", "title": "职务/岗位", "period": "起止时间（如 2018.03-2021.05）", "description": "主要工作内容与业绩（50字内）"}
  ],
  "research": "研究方向（100字内）",
  "achievements": {
    "papers": ["代表性论文/著作（含期刊或会议，逐条列出，最多5条）"],
    "patents": ["专利（含类型与数量，逐条列出，最多5条）"],
    "projects": ["主持或参与的科研项目（含级别，逐条列出，最多5条）"],
    "awards": ["人才称号/获奖（逐条列出，最多5条）"]
  },
  "skills": ["技能1", "技能2"],
  "intention": "求职意向概述（50字内）",
  "intention_detail": {
    "city": "期望工作地点（如 深圳 / 不限），未知填 null",
    "salary": "期望薪酬（如 60W / 面议 / 合理增长），未知填 null",
    "status": "当前工作状态（在职 / 离职 / 应届 / 博士后出站等），未知填 null",
    "reason": "跳槽原因/求职动机，未知填 null"
  },
  "confidence": {"education": 0.0~1.0, "school": 0.0~1.0, "major": 0.0~1.0, "work_experiences": 0.0~1.0, "research": 0.0~1.0, "achievements": 0.0~1.0, "skills": 0.0~1.0, "intention": 0.0~1.0}
}
要求：
1. 只返回 JSON，不要任何其他内容。无法确定的字段填 null 或空数组，并给低置信度。
2. 工作经历（work_experiences）必须逐段提取，按时间倒序，不要合并成一段文字。
3. 科研成果（achievements）中简历未提及的类别返回空数组，不要编造。
4. 姓名，专业，学历，年龄四个元素一定存在，请确保准确提取了这四项信息。
5. intention_detail 四个子项必须分别从原文独立判断，不要把整段话塞进同一个子项。
6. school（毕业院校）从教育经历/学历信息中提取最高学历对应的院校全称；原文确实没有出现院校信息时填 null，不要编造。"""

JOB_STRUCTURE_PROMPT = """你是招聘需求分析专家。请将以下岗位需求描述结构化为 JSON：
{
  "hard_conditions": {
    "education": "最低学历要求（博士/硕士/本科，无要求填 null）",
    "majors": ["可接受的专业或专业大类"],
    "max_age": 年龄上限（数字，无要求填 null）
  },
  "soft_conditions": {
    "research": "期望研究方向",
    "skills": ["期望技能"],
    "experience": "期望履历经验",
    "intention": "对个人意愿的要求（如可到深圳工作）"
  },
  "summary": "该岗位一句话画像（用于语义检索，100字内）"
}
只返回 JSON。"""

MATCH_REASON_PROMPT = """你是人才匹配评估专家。请评估以下简历与该岗位的匹配程度。

【岗位需求】
{job}

【候选人简历】
{resume}

请以 JSON 返回：
{
  "score": 0-100 的综合匹配分,
  "reason": "匹配理由（150字内，指出具体哪些研究方向/技能/履历与岗位匹配，以及明显短板）"
}
只返回 JSON。"""


_BIRTH_RE = re.compile(r"(\d{4})\s*[年\-./]\s*(\d{1,2})")


def _age_from_birth(birth_date: Any) -> int | None:
    """从出生年月文本（如 1983年6月23日 / 1983-06 / 1983.06）按当前日期推算周岁。
    LLM 不知道"今天"是哪天，年龄必须由代码确定性计算，不能依赖模型口算。"""
    if not birth_date:
        return None
    m = _BIRTH_RE.search(str(birth_date))
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not (1930 <= year <= 2015 and 1 <= month <= 12):
        return None
    today = date.today()
    return today.year - year - (1 if today.month < month else 0)


def _normalize_resume_fields(result: dict[str, Any], confidence: dict[str, Any]) -> None:
    """规整 LLM 抽取结果：年龄转整数、出生年月反推年龄、列表字段兜底。"""
    # 优先用出生年月确定性推算年龄（LLM 口算年龄不可靠）
    derived = _age_from_birth(result.get("birth_date"))
    if derived is not None:
        result["age"] = derived
        confidence["age"] = max(float(confidence.get("age") or 0), 0.9)
    else:
        age = result.get("age")
        if isinstance(age, str) and age.strip().isdigit():
            result["age"] = int(age.strip())
        elif age is not None and not isinstance(age, (int, float)):
            result["age"] = None
    # 列表/字典字段兜底，避免前端渲染炸掉
    if not isinstance(result.get("skills"), list):
        result["skills"] = [result["skills"]] if result.get("skills") else []
    if not isinstance(result.get("work_experiences"), list):
        result["work_experiences"] = []
    if not isinstance(result.get("achievements"), dict):
        result["achievements"] = {}
    for key in ("papers", "patents", "projects", "awards"):
        val = result["achievements"].get(key)
        if not isinstance(val, list):
            result["achievements"][key] = [str(val)] if val else []
    # 个人意愿拆分项兜底：保证四个子键齐全
    if not isinstance(result.get("intention_detail"), dict):
        result["intention_detail"] = {}
    for key in ("city", "salary", "status", "reason"):
        val = result["intention_detail"].get(key)
        result["intention_detail"][key] = str(val) if val else None


def extract_resume_fields(raw_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """简历结构化抽取，返回 (结构化字段, 置信度字典)。"""
    text = raw_text[:]  
    result = _extract_json(_chat([
        {"role": "system", "content": RESUME_EXTRACT_PROMPT},
        {"role": "user", "content": text},
    ]))
    confidence = result.pop("confidence", {}) or {}
    _normalize_resume_fields(result, confidence)
    return result, confidence


def structure_job_request(raw_text: str) -> dict[str, Any]:
    """岗位需求结构化（对话访谈结果 -> 硬性+择优条件）。"""
    return _extract_json(_chat([
        {"role": "system", "content": JOB_STRUCTURE_PROMPT},
        {"role": "user", "content": raw_text[:4000]},
    ]))


def generate_match_reason(job: dict[str, Any], resume: dict[str, Any]) -> tuple[float, str]:
    """对单个候选人生成匹配分与理由（LLM 精排）。

    注意：prompt 模板内含 JSON 示例大括号，不能用 str.format（会误解析占位符），
    必须用 replace 注入。
    """
    prompt = (MATCH_REASON_PROMPT
              .replace("{job}", json.dumps(job, ensure_ascii=False))
              .replace("{resume}", json.dumps(resume, ensure_ascii=False)))
    result = _extract_json(_chat([
        {"role": "system", "content": prompt},
        {"role": "user", "content": "请评估。"},
    ], json_mode=True))
    score = float(result.get("score", 0))
    reason = str(result.get("reason", ""))
    return score, reason
