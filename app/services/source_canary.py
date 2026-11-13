"""对已启用政策来源执行最小真实可用性验收。

该模块只验证公开页面的实际采集与正文提取能力，不写政策库、不调用 AI，
也不发送通知。验收结果不包含正文、请求头、凭据或私有 URL。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from app.config import get_enabled_sources
from app.logger import logger
from app.notifiers.safety import redact_sensitive_text
from app.scrapers.factory import create_scraper


class SourceCanaryService:
    """逐个来源验证列表采集和首条正文，并返回脱敏结果。"""

    @staticmethod
    def run(
        *,
        check_content: bool = True,
        min_content_chars: int = 100,
    ) -> Dict[str, Any]:
        if min_content_chars < 1:
            raise ValueError("min_content_chars 必须大于 0")

        sources = get_enabled_sources()
        checked_at = datetime.now(timezone.utc).isoformat()
        results: List[Dict[str, Any]] = []

        for source_config in sources:
            source_name = str(source_config.get("name") or "未命名来源")
            item_count = 0
            newest_publish_time = None
            fetch_status = "failed"
            content_status = "skipped" if not check_content else "not_checked"
            content_chars = None
            error = None
            error_type = None

            try:
                scraper = create_scraper(source_config)
                items = scraper.fetch() or []
                item_count = len(items)
                fetch_status = getattr(scraper, "last_fetch_status", "unknown")
                dates = [item.publish_time for item in items if item.publish_time]
                if dates:
                    newest_publish_time = max(dates).isoformat()
                raw_error = getattr(scraper, "last_fetch_error", None)
                error = redact_sensitive_text(raw_error) if raw_error else None
                error_type = getattr(scraper, "last_fetch_error_type", None)

                if check_content:
                    if items:
                        content = scraper.extract_content(items[0].url)
                        content_chars = len(content.strip()) if content else 0
                        if content_chars >= min_content_chars:
                            content_status = "ok"
                        elif content_chars:
                            content_status = "too_short"
                            error = (
                                f"首条正文仅 {content_chars} 字符，"
                                f"低于阈值 {min_content_chars}"
                            )
                            error_type = "content_too_short"
                        else:
                            content_status = "empty"
                            error = "首条正文提取为空"
                            error_type = "content_empty"
                    else:
                        content_status = "no_items"
                        error = "未取得政策条目，无法验证正文"
                        error_type = "no_items"
            except Exception as exc:
                error = redact_sensitive_text(exc) or exc.__class__.__name__
                logger.error(
                    "来源 canary 异常: %s (%s: %s)",
                    source_name,
                    exc.__class__.__name__,
                    error,
                )
                error_type = "canary_exception"
                fetch_status = "failed"
                content_status = "failed" if check_content else "skipped"

            passed = (
                fetch_status == "ok"
                and item_count > 0
                and (not check_content or content_status == "ok")
            )
            if passed:
                error = None
                error_type = None
            elif fetch_status != "failed":
                error = error or "来源未通过 canary 硬门禁"
                error_type = error_type or "canary_failed"

            result = {
                "source_name": source_name,
                "status": "ok" if passed else "failed",
                "fetch_status": fetch_status,
                "item_count": item_count,
                "newest_publish_time": newest_publish_time,
                "content_status": content_status,
                "content_chars": content_chars,
                "error": error,
                "error_type": error_type,
            }
            results.append(result)

            try:
                from app.services.source_health import source_health_service

                source_health_service.record(
                    source_name=source_name,
                    item_count=item_count,
                    newest_publish_time=(
                        datetime.fromisoformat(newest_publish_time)
                        if newest_publish_time
                        else None
                    ),
                    status=fetch_status if fetch_status in {
                        "ok", "empty", "degraded", "failed"
                    } else None,
                    error=error,
                    error_type=error_type,
                    suppress_alert=True,
                )
            except Exception as exc:
                logger.warning(
                    "写入来源 canary 健康记录失败 %s: %s",
                    source_name,
                    redact_sensitive_text(exc),
                )

        failed = [item for item in results if item["status"] != "ok"]
        summary = {
            "total": len(results),
            "ok": len(results) - len(failed),
            "failed": len(failed),
        }
        result = {
            "status": "ok" if results and not failed else "failed",
            "checked_at": checked_at,
            "check_content": check_content,
            "min_content_chars": min_content_chars,
            "summary": summary,
            "sources": results,
        }
        logger.info(
            "来源 canary 完成：%s/%s 通过",
            summary["ok"],
            summary["total"],
        )
        return result


source_canary_service = SourceCanaryService()