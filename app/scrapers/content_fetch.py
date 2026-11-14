"""共享政策正文抓取与状态契约。

所有同步、后台和 Agent 触发的正文读取都通过这里完成，避免各入口
对“空正文”作出不同解释。这里的锁只保证当前进程内同一 URL 只有一个
抓取动作，不是跨进程分布式锁。
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Dict, Optional
from urllib.parse import urlsplit

from app.config import get_enabled_sources
from app.scrapers.content_cleaner import MIN_CONTENT_LENGTH, strip_site_noise
from app.scrapers.factory import create_scraper


CONTENT_READY = "ready"
CONTENT_MISSING = "missing"
CONTENT_FETCHING = "fetching"
CONTENT_QUALITY_REJECTED = "quality_rejected"
CONTENT_FAILED = "failed"

CONTENT_NOT_READY = "CONTENT_NOT_READY"
CONTENT_FETCH_FAILED = "CONTENT_FETCH_FAILED"
CONTENT_QUALITY_REJECTED_ERROR = "CONTENT_QUALITY_REJECTED"


@dataclass(frozen=True)
class ContentFetchResult:
    """正文抓取结果。status 是业务状态，不是 Python 异常状态。"""

    status: str
    content: Optional[str] = None
    error_code: Optional[str] = None
    error: Optional[str] = None
    retryable: bool = False

    @property
    def ready(self) -> bool:
        return self.status == CONTENT_READY and bool(self.content)


_url_locks: Dict[str, Lock] = {}
_url_locks_guard = Lock()


def get_url_lock(url: str) -> Lock:
    """返回当前进程内按 URL 共享的锁。"""
    key = str(url or "").strip()
    with _url_locks_guard:
        lock = _url_locks.get(key)
        if lock is None:
            lock = Lock()
            _url_locks[key] = lock
        return lock


def is_content_ready(content: Optional[str]) -> bool:
    return bool(content and len(content.strip()) >= MIN_CONTENT_LENGTH)


def _host(value: Optional[str]) -> str:
    try:
        return (urlsplit(str(value or "").strip()).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _find_source_config(
    sources: list[dict], url: Optional[str], source_name: Optional[str]
) -> Optional[dict]:
    """按显式来源名或 URL 主机名定位采集器配置。"""
    name = str(source_name or "").strip()
    if name:
        matched = next((item for item in sources if item.get("name") == name), None)
        if matched:
            return matched

    host = _host(url)
    if not host:
        return None
    matches = []
    for item in sources:
        website_host = _host(item.get("website"))
        if website_host and (host == website_host or host.endswith("." + website_host)):
            matches.append(item)
    # 更具体的子域优先，避免一个泛域名误匹配多个来源。
    matches.sort(key=lambda item: len(_host(item.get("website"))), reverse=True)
    return matches[0] if matches else None


def resolve_source_name(url: str, source_name: Optional[str] = None) -> Optional[str]:
    """解析 URL 对应的已启用来源名；配置不可用时返回 None。"""
    try:
        source = _find_source_config(get_enabled_sources(), url, source_name)
    except Exception:
        return None
    return source.get("name") if source else None


def fetch_policy_content(url: str, source_name: Optional[str]) -> ContentFetchResult:
    """抓取并清洗正文，返回可判定的结构化结果。

    调用方负责决定是否等待 URL 锁；本函数本身不获取锁，便于详情查询
    在后台已抓取时立即返回 fetching，而不用阻塞用户请求。
    """
    if not url:
        return ContentFetchResult(
            status=CONTENT_FAILED,
            error_code=CONTENT_FETCH_FAILED,
            error="政策缺少原文链接，无法读取正文",
            retryable=False,
        )

    try:
        source_config = _find_source_config(
            get_enabled_sources(), url, source_name
        )
    except Exception as exc:
        return ContentFetchResult(
            status=CONTENT_FAILED,
            error_code=CONTENT_FETCH_FAILED,
            error=f"政策来源配置读取失败: {exc}",
            retryable=True,
        )

    if not source_config:
        return ContentFetchResult(
            status=CONTENT_FAILED,
            error_code=CONTENT_FETCH_FAILED,
            error="未找到该政策 URL 对应的采集器配置",
            retryable=False,
        )
    source_name = source_config.get("name") or source_name

    try:
        raw_content = create_scraper(source_config).extract_content(url)
    except Exception as exc:
        return ContentFetchResult(
            status=CONTENT_FAILED,
            error_code=CONTENT_FETCH_FAILED,
            error=f"官网正文读取失败: {exc}",
            retryable=True,
        )

    if not raw_content or not str(raw_content).strip():
        return ContentFetchResult(
            status=CONTENT_FAILED,
            error_code=CONTENT_FETCH_FAILED,
            error="官网未返回可用正文",
            retryable=True,
        )

    cleaned = strip_site_noise(raw_content)
    if not cleaned or len(cleaned.strip()) < MIN_CONTENT_LENGTH:
        return ContentFetchResult(
            status=CONTENT_QUALITY_REJECTED,
            error_code=CONTENT_QUALITY_REJECTED_ERROR,
            error="页面已读取，但有效正文不足",
            retryable=False,
        )

    return ContentFetchResult(
        status=CONTENT_READY,
        content=cleaned[:10000],
        retryable=False,
    )


def content_error_payload(
    *,
    status: str,
    error_code: str,
    error: str,
    retryable: bool,
    policy_id: Optional[int] = None,
    title: Optional[str] = None,
    url: Optional[str] = None,
    source_name: Optional[str] = None,
    publish_time: Optional[str] = None,
    department: Optional[str] = None,
) -> dict:
    """Agent 与展示层共用的结构化业务错误。"""
    payload = {
        "error": error,
        "error_code": error_code,
        "retryable": retryable,
        "content_status": status,
    }
    if policy_id:
        payload["policy_id"] = policy_id
    if title:
        payload["title"] = title
    if url:
        payload["url"] = url
    if source_name:
        payload["source_name"] = source_name
    if publish_time:
        payload["publish_time"] = publish_time
    if department:
        payload["department"] = department
    return payload
