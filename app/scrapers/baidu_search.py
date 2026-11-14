"""百度 AI 搜索客户端与政策发现适配器。

百度在这里只承担政策发现：返回标题、原文链接、摘要和网页日期。
正文仍由各政府来源的采集器从官网读取；百度不会作为正文可信来源。
"""

from __future__ import annotations

import http.client
import json
import os
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from app.config import settings
from app.logger import logger
from app.scrapers.base import ScrapedItem
from app.scrapers.content_cleaner import clean_title


DEFAULT_ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"
DEFAULT_TOP_K = 50
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_QUERY = "政策文件 通知 意见 办法 方案 规划 决定 公告"


class BaiduSearchErrorType:
    """稳定的百度搜索错误类型，供来源健康和 Canary 复用。"""

    AUTH = "baidu_auth"
    CLIENT = "baidu_client"
    QUOTA_EXHAUSTED = "baidu_quota_exhausted"
    RATE_LIMIT = "baidu_rate_limit"
    SERVER = "baidu_server"
    TIMEOUT = "baidu_timeout"
    NETWORK = "baidu_network"
    INVALID_RESPONSE = "baidu_invalid_response"
    UNKNOWN = "baidu_unknown"


@dataclass(frozen=True)
class BaiduSearchCallResult:
    data: Optional[dict[str, Any]]
    error_type: Optional[str] = None
    attempts: int = 0
    status_code: Optional[int] = None
    request_id: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.data is not None and self.error_type is None


@dataclass(frozen=True)
class BaiduDiscoveryResult:
    items: list[ScrapedItem]
    status: str
    error_type: Optional[str] = None
    error: Optional[str] = None
    calls: int = 0
    request_ids: tuple[str, ...] = ()


def _urllib_post_transport(
    url: str,
    data: bytes,
    headers: dict[str, str],
    timeout: int,
):
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST",
    )
    return urllib.request.urlopen(request, timeout=timeout)


class BaiduSearchClient:
    """带有限重试和错误分类的百度 AI 搜索客户端。"""

    RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        api_key: str = "",
        *,
        endpoint: str = DEFAULT_ENDPOINT,
        max_retries: int = 2,
        timeout: int = 30,
        backoff_base: float = 1.0,
        backoff_max: float = 4.0,
        transport: Optional[Callable[..., Any]] = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_key = api_key or ""
        self.endpoint = endpoint
        self.max_retries = max(1, int(max_retries))
        self.timeout = max(1, int(timeout))
        self.backoff_base = max(0.0, float(backoff_base))
        self.backoff_max = max(self.backoff_base, float(backoff_max))
        self._transport = transport or _urllib_post_transport
        self._sleep = sleep

    def _retry_delay(self, attempt: int) -> float:
        base = min(
            self.backoff_max,
            self.backoff_base * (2 ** max(0, attempt - 1)),
        )
        return base + random.uniform(0, min(0.25, base * 0.1))

    @staticmethod
    def _classify_exception(exc: Exception) -> tuple[str, Optional[int]]:
        if isinstance(exc, urllib.error.HTTPError):
            code = int(exc.code)
            if code in (401, 403):
                return BaiduSearchErrorType.AUTH, code
            if code == 429:
                return BaiduSearchErrorType.RATE_LIMIT, code
            if 500 <= code <= 599:
                return BaiduSearchErrorType.SERVER, code
            return BaiduSearchErrorType.CLIENT, code
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return BaiduSearchErrorType.TIMEOUT, None
        if isinstance(exc, urllib.error.URLError):
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                return BaiduSearchErrorType.TIMEOUT, None
            if isinstance(reason, ssl.SSLError):
                return BaiduSearchErrorType.NETWORK, None
            return BaiduSearchErrorType.NETWORK, None
        if isinstance(
            exc,
            (
                ConnectionError,
                ConnectionResetError,
                http.client.IncompleteRead,
                http.client.BadStatusLine,
            ),
        ):
            return BaiduSearchErrorType.NETWORK, None
        return BaiduSearchErrorType.UNKNOWN, None

    @classmethod
    def _is_retryable(cls, error_type: str) -> bool:
        return error_type in {
            BaiduSearchErrorType.RATE_LIMIT,
            BaiduSearchErrorType.SERVER,
            BaiduSearchErrorType.TIMEOUT,
            BaiduSearchErrorType.NETWORK,
            BaiduSearchErrorType.INVALID_RESPONSE,
        }

    @staticmethod
    def _api_error_type(code: object, message: object) -> str:
        text = f"{code} {message}".lower()
        if any(
            marker in text
            for marker in ("quota", "balance", "insufficient", "额度", "余额", "配额")
        ):
            return BaiduSearchErrorType.QUOTA_EXHAUSTED
        if any(marker in text for marker in ("rate", "qps", "limit", "限流", "频率")):
            return BaiduSearchErrorType.RATE_LIMIT
        if any(
            marker in text
            for marker in ("auth", "token", "apikey", "permission", "鉴权", "权限")
        ):
            return BaiduSearchErrorType.AUTH
        return BaiduSearchErrorType.CLIENT

    @staticmethod
    def _request_id(parsed: dict[str, Any]) -> Optional[str]:
        return str(
            parsed.get("request_id")
            or parsed.get("requestId")
            or ""
        ).strip() or None

    @classmethod
    def _http_error_body_details(
        cls,
        exc: urllib.error.HTTPError,
    ) -> tuple[Optional[str], Optional[str]]:
        """从 HTTP 错误体读取百度错误码和请求 ID。

        ``urllib`` 对 401/403/429/5xx 会抛 ``HTTPError``，状态码本身无法
        区分 QPS 限流与每日免费额度耗尽，需要保留响应体中的错误码。
        """
        try:
            raw = exc.read(65536)
            parsed = json.loads(
                raw.decode("utf-8")
                if isinstance(raw, (bytes, bytearray))
                else raw
            )
        except (OSError, TypeError, ValueError, UnicodeDecodeError):
            return None, None
        if not isinstance(parsed, dict):
            return None, None
        request_id = cls._request_id(parsed)
        code = str(parsed.get("code") or "").strip()
        message = str(parsed.get("message") or "").strip()
        if not code and not message:
            return None, request_id
        return cls._api_error_type(code, message), request_id

    @staticmethod
    def _page_time_range(lookback_days: int) -> tuple[str, str]:
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
        return (
            (today - timedelta(days=lookback_days)).isoformat(),
            (today + timedelta(days=1)).isoformat(),
        )

    def search(
        self,
        query: str,
        *,
        sites: Iterable[str] = (),
        top_k: int = DEFAULT_TOP_K,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    ) -> BaiduSearchCallResult:
        if not self.api_key:
            return BaiduSearchCallResult(None, BaiduSearchErrorType.AUTH, 0)

        clean_sites = [
            str(site).strip()
            for site in sites
            if str(site).strip()
        ]
        payload: dict[str, Any] = {
            "messages": [
                {
                    "role": "user",
                    "content": str(query or DEFAULT_QUERY).strip()[:72],
                }
            ],
            "search_source": "baidu_search_v2",
            "edition": "standard",
            "resource_type_filter": [
                {
                    "type": "web",
                    "top_k": max(1, min(int(top_k), 50)),
                }
            ],
            "safe_search": True,
        }
        if clean_sites:
            payload["search_filter"] = {
                "match": {"site": clean_sites},
            }
            if lookback_days > 0:
                start_date, exclusive_end_date = self._page_time_range(
                    int(lookback_days)
                )
                payload["search_filter"]["range"] = {
                    "page_time": {
                        # 百度要求起止边界同时存在，并提示 lte 可能因缓存
                        # 导致结果滞后；用“起始日 <= 发布时间 < 明天”避开该
                        # 问题，同时包含今天。仅支持固定套餐或 YYYY-MM-DD。
                        "gte": start_date,
                        "lt": exclusive_end_date,
                    }
                }

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        last_error: Optional[str] = None
        last_status: Optional[int] = None
        last_request_id: Optional[str] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._transport(
                    self.endpoint, data, headers, self.timeout
                )
                last_status = getattr(response, "status", None)
                try:
                    raw = response.read()
                finally:
                    close = getattr(response, "close", None)
                    if close:
                        close()

                try:
                    parsed = json.loads(
                        raw.decode("utf-8")
                        if isinstance(raw, (bytes, bytearray))
                        else raw
                    )
                except (TypeError, ValueError, UnicodeDecodeError):
                    last_error = BaiduSearchErrorType.INVALID_RESPONSE
                    if attempt < self.max_retries:
                        self._sleep(self._retry_delay(attempt))
                        continue
                    return BaiduSearchCallResult(
                        None, last_error, attempt, last_status
                    )

                if not isinstance(parsed, dict):
                    last_error = BaiduSearchErrorType.INVALID_RESPONSE
                    if attempt < self.max_retries:
                        self._sleep(self._retry_delay(attempt))
                        continue
                    return BaiduSearchCallResult(
                        None, last_error, attempt, last_status
                    )

                request_id = self._request_id(parsed)
                last_request_id = request_id
                api_code = str(parsed.get("code") or "").strip()
                if api_code.lower() not in {"", "0", "none"}:
                    error_type = self._api_error_type(
                        api_code, parsed.get("message")
                    )
                    return BaiduSearchCallResult(
                        None,
                        error_type,
                        attempt,
                        last_status,
                        request_id,
                    )

                references = parsed.get("references")
                if references is None:
                    parsed["references"] = []
                elif isinstance(references, dict):
                    parsed["references"] = [references]
                elif not isinstance(references, list):
                    return BaiduSearchCallResult(
                        None,
                        BaiduSearchErrorType.INVALID_RESPONSE,
                        attempt,
                        last_status,
                        request_id,
                    )
                return BaiduSearchCallResult(
                    parsed, None, attempt, last_status, request_id
                )
            except Exception as exc:
                error_type, status = self._classify_exception(exc)
                last_error, last_status = error_type, status
                if isinstance(exc, urllib.error.HTTPError):
                    api_error_type, request_id = self._http_error_body_details(exc)
                    last_request_id = request_id or last_request_id
                    # 429 同时表示 QPS 限流和每日免费额度耗尽。只有响应体
                    # 能区分二者；额度耗尽不应重试，避免继续撞额度上限。
                    if status == 429 and api_error_type in {
                        BaiduSearchErrorType.QUOTA_EXHAUSTED,
                        BaiduSearchErrorType.RATE_LIMIT,
                    }:
                        last_error = api_error_type
                        error_type = api_error_type
                logger.warning(
                    "百度搜索请求失败 error_type=%s attempt=%s/%s status=%s request_id=%s",
                    error_type,
                    attempt,
                    self.max_retries,
                    status,
                    last_request_id,
                )
                if not self._is_retryable(error_type):
                    break
                if attempt < self.max_retries:
                    self._sleep(self._retry_delay(attempt))

        return BaiduSearchCallResult(
            None,
            last_error or BaiduSearchErrorType.UNKNOWN,
            attempt,
            last_status,
            last_request_id,
        )


_CLIENT_CACHE: dict[tuple[str, str], BaiduSearchClient] = {}
_CLIENT_CACHE_LOCK = threading.RLock()


def create_baidu_client(api_key: str = "", **kwargs: Any) -> BaiduSearchClient:
    key = api_key or ""
    endpoint = str(kwargs.get("endpoint", DEFAULT_ENDPOINT))
    if any(name in kwargs for name in ("transport", "sleep")):
        return BaiduSearchClient(api_key=key, **kwargs)
    cache_key = (key, endpoint)
    with _CLIENT_CACHE_LOCK:
        client = _CLIENT_CACHE.get(cache_key)
        if client is None:
            client = BaiduSearchClient(api_key=key, **kwargs)
            _CLIENT_CACHE[cache_key] = client
        return client


def reset_baidu_clients() -> None:
    with _CLIENT_CACHE_LOCK:
        _CLIENT_CACHE.clear()


def _configured_api_key() -> str:
    return (
        os.environ.get("BAIDU_API_KEY", "").strip()
        or settings.baidu_api_key.strip()
    )


def _as_queries(value: object) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    queries = [
        str(item).strip()[:72]
        for item in values
        if isinstance(item, str) and item.strip()
    ]
    return queries or [DEFAULT_QUERY]


def _host_matches(hostname: str, site: str) -> bool:
    hostname = (hostname or "").lower().rstrip(".")
    site = (site or "").lower().rstrip(".")
    return bool(
        hostname
        and site
        and (hostname == site or hostname.endswith("." + site))
    )


def _parse_reference_date(reference: dict[str, Any], url: str) -> Optional[datetime]:
    candidates = (
        reference.get("date"),
        reference.get("snippet"),
        reference.get("content"),
    )
    for value in candidates:
        text = str(value or "").strip()
        if not text:
            continue
        iso_match = re.search(
            r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)",
            text,
        )
        if iso_match:
            try:
                return datetime(
                    int(iso_match.group(1)),
                    int(iso_match.group(2)),
                    int(iso_match.group(3)),
                )
            except ValueError:
                pass
        chinese_match = re.search(
            r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日",
            text,
        )
        if chinese_match:
            try:
                return datetime(
                    int(chinese_match.group(1)),
                    int(chinese_match.group(2)),
                    int(chinese_match.group(3)),
                )
            except ValueError:
                pass

    for pattern in (
        r"/t(\d{4})(\d{2})(\d{2})(?:_|\D|$)",
        r"/(\d{4})/(\d{2})/(\d{2})/",
        r"/(\d{4})(\d{2})(\d{2})/",
    ):
        match = re.search(pattern, url or "")
        if not match:
            continue
        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            )
        except ValueError:
            continue
    return None


def _reference_is_valid(
    reference: dict[str, Any],
    *,
    source_config: dict[str, Any],
    search_config: dict[str, Any],
    site: str,
) -> bool:
    if str(reference.get("type") or "web").lower() != "web":
        return False
    url = str(reference.get("url") or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    if not _host_matches(parsed.hostname or "", site):
        return False

    path = parsed.path or "/"
    prefixes = (
        search_config.get("path_prefixes")
        or source_config.get("policy_path_prefixes")
        or []
    )
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    if prefixes and not any(path.startswith(str(prefix)) for prefix in prefixes):
        return False

    patterns = search_config.get("url_patterns") or []
    if isinstance(patterns, str):
        patterns = [patterns]
    if patterns and not any(re.search(str(pattern), path) for pattern in patterns):
        return False
    if bool(search_config.get("exclude_pdf", True)) and path.lower().endswith(".pdf"):
        return False
    if re.search(r"/(?:index|list\d*)\.s?html?$", path, re.IGNORECASE):
        return False
    return True


def _reference_to_item(
    reference: dict[str, Any],
    *,
    source_config: dict[str, Any],
    search_config: dict[str, Any],
    site: str,
    lookback_days: int,
) -> Optional[ScrapedItem]:
    if not _reference_is_valid(
        reference,
        source_config=source_config,
        search_config=search_config,
        site=site,
    ):
        return None

    title = clean_title(reference.get("title"))
    if not title or len(title) < 5:
        return None

    title_keywords = search_config.get("title_keywords") or []
    if isinstance(title_keywords, str):
        title_keywords = [title_keywords]
    if title_keywords and not any(
        str(keyword) in title for keyword in title_keywords
    ):
        return None

    blocked_title_keywords = search_config.get("blocked_title_keywords") or []
    if isinstance(blocked_title_keywords, str):
        blocked_title_keywords = [blocked_title_keywords]
    if any(str(keyword) in title for keyword in blocked_title_keywords):
        return None

    url = str(reference.get("url") or "").split("#", 1)[0].strip()
    publish_time = _parse_reference_date(reference, url)
    cutoff = (
        (datetime.now() - timedelta(days=lookback_days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if lookback_days > 0
        else None
    )
    if bool(search_config.get("require_date", True)):
        if publish_time is None:
            return None
        if cutoff and publish_time < cutoff:
            return None
    elif cutoff and publish_time and publish_time < cutoff:
        return None

    summary = str(
        reference.get("snippet")
        or reference.get("content")
        or ""
    ).strip()
    return ScrapedItem(
        title=title,
        url=url,
        department=source_config.get("department") or source_config.get("name"),
        source_name=source_config.get("name"),
        government_level=source_config.get("level"),
        city=source_config.get("city"),
        publish_time=publish_time,
        summary=summary[:500] or None,
    )


def discover_policies_with_baidu(
    source_config: dict[str, Any],
    *,
    lookback_days: Optional[int] = None,
) -> BaiduDiscoveryResult:
    """按来源配置执行百度政策发现。

    ``status`` 区分 disabled、ok、empty、failed；调用方必须使用该状态，
    不能仅凭空列表判断故障。
    """
    search_config = source_config.get("search_discovery")
    if not isinstance(search_config, dict):
        return BaiduDiscoveryResult([], "disabled")
    mode = str(search_config.get("mode") or "disabled").strip().lower()
    if mode not in {"preferred", "fallback"}:
        return BaiduDiscoveryResult([], "disabled")
    if str(search_config.get("provider") or "baidu").strip().lower() != "baidu":
        return BaiduDiscoveryResult(
            [],
            "failed",
            BaiduSearchErrorType.CLIENT,
            "不支持的搜索供应商",
            calls=0,
        )

    api_key = _configured_api_key()
    if not api_key:
        return BaiduDiscoveryResult(
            [],
            "failed",
            BaiduSearchErrorType.AUTH,
            "BAIDU_API_KEY 未配置",
            calls=0,
        )

    website = str(source_config.get("website") or "").strip()
    site = (urlparse(website).hostname or "").lower()
    if not site:
        return BaiduDiscoveryResult(
            [],
            "failed",
            BaiduSearchErrorType.CLIENT,
            "来源缺少有效 website",
            calls=0,
        )

    effective_lookback = (
        int(lookback_days)
        if lookback_days is not None
        else int(search_config.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    )
    effective_lookback = max(1, min(effective_lookback, 365))
    top_k = max(
        1,
        min(int(search_config.get("top_k", DEFAULT_TOP_K)), 50),
    )
    queries = _as_queries(search_config.get("queries"))
    client = create_baidu_client(
        api_key,
        timeout=max(1, int(search_config.get("timeout_seconds", 30))),
        max_retries=max(1, int(search_config.get("max_retries", 2))),
    )

    items: list[ScrapedItem] = []
    seen_urls: set[str] = set()
    request_ids: list[str] = []
    errors: list[str] = []
    successful_calls = 0

    for query in queries:
        result = client.search(
            query,
            sites=[site],
            top_k=top_k,
            lookback_days=effective_lookback,
        )
        if result.request_id:
            request_ids.append(result.request_id)
        if not result.ok:
            errors.append(result.error_type or BaiduSearchErrorType.UNKNOWN)
            logger.warning(
                "百度政策发现失败 source=%s error_type=%s",
                source_config.get("name"),
                result.error_type,
            )
            continue

        successful_calls += 1
        for reference in result.data.get("references", []):
            if not isinstance(reference, dict):
                continue
            item = _reference_to_item(
                reference,
                source_config=source_config,
                search_config=search_config,
                site=site,
                lookback_days=effective_lookback,
            )
            if item is None or item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            items.append(item)

    items.sort(
        key=lambda item: item.publish_time or datetime.min,
        reverse=True,
    )
    first_error = next((error for error in errors if error), None)
    if items:
        status = "ok"
    elif successful_calls and not errors:
        status = "empty"
    else:
        status = "failed"
    if status == "failed" and not first_error:
        first_error = BaiduSearchErrorType.UNKNOWN
    logger.info(
        "百度政策发现完成 source=%s status=%s calls=%s items=%s",
        source_config.get("name"),
        status,
        len(queries),
        len(items),
    )
    return BaiduDiscoveryResult(
        items=items,
        status=status,
        error_type=first_error if status == "failed" else None,
        error=(
            f"百度搜索失败: {first_error}"
            if status == "failed" and first_error
            else None
        ),
        calls=len(queries),
        request_ids=tuple(request_ids),
    )


__all__ = [
    "BaiduDiscoveryResult",
    "BaiduSearchCallResult",
    "BaiduSearchClient",
    "BaiduSearchErrorType",
    "create_baidu_client",
    "discover_policies_with_baidu",
    "reset_baidu_clients",
]
