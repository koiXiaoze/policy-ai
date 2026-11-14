"""脱敏来源诊断的共享白名单。

这些值会进入公开的 Canary Manifest。新增值必须同时审查日志、异常和
响应正文是否会借由该字段泄露，因此只允许固定枚举，不接受自由文本。
"""

from __future__ import annotations

from collections.abc import Iterable

from app.scrapers.baidu_search import BaiduSearchErrorType
from app.scrapers.tavily_client import TavilyErrorType


ALLOWED_CHANNEL_FAILURES = frozenset(
    {
        "direct_failed",
        "baidu_failed",
        "tavily_failed",
        "scrapling_failed",
    }
)

ALLOWED_FAILURE_CATEGORIES = frozenset(
    {
        "http_412",
        "http_429",
        "http_5xx",
        "http_other",
        "timeout",
        "connection_error",
        "transport_error",
        "parse_empty",
        "not_configured",
        "channel_error",
    }
)

BAIDU_ERROR_TYPES = frozenset(
    {
        BaiduSearchErrorType.AUTH,
        BaiduSearchErrorType.CLIENT,
        BaiduSearchErrorType.QUOTA_EXHAUSTED,
        BaiduSearchErrorType.RATE_LIMIT,
        BaiduSearchErrorType.SERVER,
        BaiduSearchErrorType.TIMEOUT,
        BaiduSearchErrorType.NETWORK,
        BaiduSearchErrorType.INVALID_RESPONSE,
        BaiduSearchErrorType.UNKNOWN,
    }
)

TAVILY_ERROR_TYPES = frozenset(
    {
        TavilyErrorType.AUTH,
        TavilyErrorType.CLIENT,
        TavilyErrorType.QUOTA_EXHAUSTED,
        TavilyErrorType.RATE_LIMIT,
        TavilyErrorType.SERVER,
        TavilyErrorType.TIMEOUT,
        TavilyErrorType.SSL_EOF,
        TavilyErrorType.CONNECTION_RESET,
        TavilyErrorType.NETWORK,
        TavilyErrorType.CIRCUIT_OPEN,
        TavilyErrorType.INVALID_RESPONSE,
        TavilyErrorType.INCOMPLETE_READ,
        TavilyErrorType.BAD_STATUS_LINE,
        TavilyErrorType.UNKNOWN,
    }
)

ALLOWED_ERROR_TYPES = BAIDU_ERROR_TYPES | TAVILY_ERROR_TYPES | frozenset(
    {
        "all_channels_failed",
        "canary_exception",
        "canary_failed",
        "content_empty",
        "content_too_short",
        "empty_window",
        "fetch_failed",
        "unverified_fetch_status",
        "direct_failed",
        "baidu_error",
        "baidu_empty",
        "baidu_failed",
        "tavily_error",
        "tavily_empty",
        "tavily_failed",
        "scrapling_failed",
        "schema_changed",
        "waf_challenge",
        "server_error",
        "http_error",
        "parse_failed",
        "transport_error",
    }
)


def filter_diagnostic_values(
    values: object,
    allowed_values: frozenset[str],
) -> list[str]:
    """过滤为去重、保序的固定枚举，丢弃所有自由文本和非字符串值。"""
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, dict)):
        return []

    filtered: list[str] = []
    for value in values:
        if isinstance(value, str) and value in allowed_values and value not in filtered:
            filtered.append(value)
    return filtered


__all__ = [
    "ALLOWED_CHANNEL_FAILURES",
    "ALLOWED_ERROR_TYPES",
    "ALLOWED_FAILURE_CATEGORIES",
    "BAIDU_ERROR_TYPES",
    "TAVILY_ERROR_TYPES",
    "filter_diagnostic_values",
]
