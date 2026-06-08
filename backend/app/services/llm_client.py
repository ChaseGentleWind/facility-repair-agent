"""统一 LLM 客户端：timeout + tenacity 重试 + 调用计数 + 结构化输出校验。

职责：
- 封装单例 AsyncOpenAI（httpx 显式 timeout，trust_env=False 绕开 Windows 系统代理）
- structured_call(schema, ...) 走 response_format=json_object，失败时给 LLM 一次"按错误修一下"的机会
- stream_call(...) 流式调用统一兜底，按 purpose 计数
- CallCounter 用 contextvars 实现"每次 process_message 一桶"，process_message 末尾打 INFO 日志

不做：熔断、token/成本统计、预算阻断（只记录，不干预）。
"""
from __future__ import annotations

import contextvars
import json
import logging
from collections import Counter
from typing import AsyncIterator, Type, TypeVar

import httpx
from openai import AsyncOpenAI, APIError, APITimeoutError, RateLimitError
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    RetryError,
)

from app.agent.schemas import ErrorResult
from app.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_RETRYABLE_EXC = (APITimeoutError, RateLimitError, httpx.TimeoutException, httpx.ConnectError)
_EXTRA_BODY = {"enable_thinking": False}


# ── CallCounter ──────────────────────────────────────────────────────────────
_counter_var: contextvars.ContextVar[Counter | None] = contextvars.ContextVar(
    "llm_call_counter", default=None
)


def reset_counter() -> Counter:
    """每轮 process_message 入口调用，返回新桶。"""
    bucket: Counter = Counter()
    _counter_var.set(bucket)
    return bucket


def get_counter() -> Counter | None:
    return _counter_var.get()


def _bump(purpose: str) -> None:
    bucket = _counter_var.get()
    if bucket is not None:
        bucket[purpose] += 1


# ── Client 单例 ───────────────────────────────────────────────────────────────
_http_client: httpx.AsyncClient | None = None
_async_openai: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _http_client, _async_openai
    if _async_openai is None:
        _http_client = httpx.AsyncClient(
            trust_env=False,
            timeout=httpx.Timeout(settings.llm_timeout_seconds, connect=5.0),
        )
        _async_openai = AsyncOpenAI(
            api_key=settings.qwen_api_key,
            base_url=settings.qwen_base_url,
            http_client=_http_client,
        )
    return _async_openai


async def aclose() -> None:
    """供 lifespan 注册，进程退出时关闭 httpx 连接。"""
    global _http_client, _async_openai
    if _http_client is not None:
        await _http_client.aclose()
    _http_client = None
    _async_openai = None


# ── 重试包装 ───────────────────────────────────────────────────────────────────
def _retrying() -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(settings.llm_max_retries),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
        retry=retry_if_exception_type(_RETRYABLE_EXC),
        reraise=True,
    )


# ── 公共调用入口 ──────────────────────────────────────────────────────────────
async def structured_call(
    schema: Type[T],
    *,
    purpose: str,
    system: str,
    user: str | list[dict],
    temperature: float = 0.1,
    max_tokens: int | None = None,
) -> T | ErrorResult:
    """非流式 JSON 结构化调用。

    流程：调一次 LLM → 解析 JSON → schema.model_validate → 失败给 LLM 修一次 → 还失败返回 ErrorResult。
    user 可以是字符串或 OpenAI 多模态 content 列表。
    """
    client = _get_client()
    user_message = _user_msg(user)
    messages: list[dict] = [
        {"role": "system", "content": system},
        user_message,
    ]

    raw = await _call_once(client, purpose, messages, temperature, max_tokens, json_mode=True)
    if raw is None:
        return ErrorResult(code="llm_call_failed", purpose=purpose)

    parsed, err = _parse_into(schema, raw)
    if parsed is not None:
        return parsed

    # 给 LLM 一次按错误修复的机会
    repair_prompt = (
        f"你刚才的回复不符合 JSON Schema，错误如下：\n{err}\n"
        f"原始回复：\n{raw}\n"
        "请按 system 中的 schema 重新输出，只输出 JSON。"
    )
    repair_messages: list[dict] = [
        {"role": "system", "content": system},
        user_message,
        {"role": "assistant", "content": raw},
        {"role": "user", "content": repair_prompt},
    ]
    raw2 = await _call_once(
        client, f"{purpose}_repair", repair_messages, temperature, max_tokens, json_mode=True
    )
    if raw2 is None:
        return ErrorResult(code="llm_call_failed", purpose=purpose)

    parsed2, err2 = _parse_into(schema, raw2)
    if parsed2 is not None:
        return parsed2

    logger.warning("[%s] structured_call 修复后仍失败: %s", purpose, err2)
    return ErrorResult(code="llm_validation_failed", purpose=purpose, message=str(err2))


async def text_call(
    *,
    purpose: str,
    system: str,
    user: str,
    temperature: float = 0,
    max_tokens: int | None = None,
) -> str | None:
    """非流式纯文本调用（用于 RAG 标准化、时间解析等小任务）。失败返回 None。"""
    client = _get_client()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    return await _call_once(client, purpose, messages, temperature, max_tokens, json_mode=False)


async def stream_call(
    *,
    purpose: str,
    messages: list[dict],
    temperature: float = 0.4,
) -> AsyncIterator[str]:
    """流式调用：yield 文字片段。中途异常吞掉 + 写 WARNING，由上层降级。"""
    _bump(purpose)
    client = _get_client()
    try:
        async for attempt in _retrying():
            with attempt:
                stream = await client.chat.completions.create(
                    model=settings.qwen_model,
                    messages=messages,
                    stream=True,
                    temperature=temperature,
                    extra_body=_EXTRA_BODY,
                )
                async for chunk in stream:
                    content = chunk.choices[0].delta.content or ""
                    if content:
                        yield content
                return
    except RetryError as exc:
        logger.warning("[%s] stream_call retries exhausted: %s", purpose, exc)
    except Exception as exc:
        logger.warning("[%s] stream_call failed: %s", purpose, exc)


# ── 内部辅助 ──────────────────────────────────────────────────────────────────
def _user_msg(user: str | list[dict]) -> dict:
    if isinstance(user, str):
        return {"role": "user", "content": user}
    return {"role": "user", "content": user}


async def _call_once(
    client: AsyncOpenAI,
    purpose: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int | None,
    *,
    json_mode: bool,
) -> str | None:
    """单次完成调用，包重试。失败返回 None。"""
    _bump(purpose)
    kwargs: dict = {
        "model": settings.qwen_model,
        "messages": messages,
        "temperature": temperature,
        "extra_body": _EXTRA_BODY,
    }
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    try:
        async for attempt in _retrying():
            with attempt:
                resp = await client.chat.completions.create(**kwargs)
                return resp.choices[0].message.content or ""
    except RetryError as exc:
        logger.warning("[%s] retries exhausted: %s", purpose, exc)
    except APIError as exc:
        logger.warning("[%s] API error: %s", purpose, exc)
    except Exception as exc:
        logger.warning("[%s] unexpected: %s", purpose, exc)
    return None


def _strip_code_fence(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        nl = s.index("\n") if "\n" in s else 3
        s = s[nl + 1 :]
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
    return s


def _parse_into(schema: Type[T], raw: str) -> tuple[T | None, str | None]:
    text = _strip_code_fence(raw)
    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"json decode error: {exc}"
    try:
        return schema.model_validate(obj), None
    except ValidationError as exc:
        return None, exc.json()
