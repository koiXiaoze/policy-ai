"""政策正文回填工具

用途：补齐历史入库但正文缺失（或无有效正文）的政策。

背景：`PolicyService._fill_content_for()` 只对「本次新入库」的政策补正文，
历史遗留的缺失记录不会自动回填。实测这些政策页仍可正常抓取，因此提供
一个独立入口做一次性/周期性回填。

用法：
    python -m app.tools.backfill_content              # 回填全部缺失
    python -m app.tools.backfill_content --limit 10   # 限量
    python -m app.tools.backfill_content --dry-run    # 只统计不写入
    python -m app.tools.backfill_content --source 湖北省人民政府
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from app.config import get_enabled_sources
from app.database import SessionLocal, init_db
from app.logger import logger
from app.models.policy import Policy
from app.scrapers.content_cleaner import MIN_CONTENT_LENGTH
from app.scrapers.content_fetch import fetch_policy_content, get_url_lock, is_content_ready


def find_missing_policies(source: Optional[str] = None, limit: Optional[int] = None) -> List[Policy]:
    """查询正文缺失的政策。"""
    db = SessionLocal()
    try:
        query = db.query(Policy)
        if source:
            query = query.filter(Policy.source_name == source)
        rows = query.order_by(Policy.publish_time.desc()).all()
        missing = [
            p for p in rows
            if not (p.content and len(p.content.strip()) >= MIN_CONTENT_LENGTH)
        ]
        if limit is not None:
            missing = missing[:limit]
        # 立即转成普通对象快照，避免会话关闭后属性失效
        return missing
    finally:
        db.close()


def backfill(dry_run: bool = False, limit: Optional[int] = None,
             source: Optional[str] = None) -> dict:
    """执行正文回填，返回统计结果。"""
    init_db()
    missing = find_missing_policies(source=source, limit=limit)

    stats = {
        "missing": len(missing),
        "filled": 0,
        "failed": 0,
        "skipped": 0,
        "details": [],
    }
    if not missing:
        logger.info("没有需要回填正文的政策")
        return stats

    sources = {s["name"]: s for s in get_enabled_sources()}
    logger.info("待回填政策 %s 条（dry_run=%s）", len(missing), dry_run)

    for policy in missing:
        cfg = sources.get(policy.source_name or "")
        if not cfg:
            stats["skipped"] += 1
            stats["details"].append({
                "policy_id": policy.id,
                "source": policy.source_name,
                "status": "skipped",
                "reason": "无匹配采集器配置",
            })
            logger.warning("跳过 policy_id=%s：无匹配采集器 %s", policy.id, policy.source_name)
            continue

        lock = get_url_lock(policy.url or "")
        if not lock.acquire(blocking=False):
            stats["skipped"] += 1
            stats["details"].append({
                "policy_id": policy.id,
                "source": policy.source_name,
                "status": "fetching",
                "reason": "正文正在由其他任务读取",
            })
            continue

        try:
            db = SessionLocal()
            try:
                current = db.query(Policy).filter(Policy.id == policy.id).first()
                if not current or is_content_ready(current.content):
                    stats["skipped"] += 1
                    stats["details"].append({
                        "policy_id": policy.id,
                        "source": policy.source_name,
                        "status": "skipped",
                        "reason": "记录已删除或正文已被其他任务补齐",
                    })
                    continue

                fetched = fetch_policy_content(current.url, current.source_name)
                if not fetched.ready:
                    stats["failed"] += 1
                    stats["details"].append({
                        "policy_id": current.id,
                        "source": current.source_name,
                        "status": fetched.status,
                        "reason": fetched.error,
                        "error_code": fetched.error_code,
                        "retryable": fetched.retryable,
                    })
                    logger.warning(
                        "正文回填失败 policy_id=%s status=%s: %s",
                        current.id, fetched.status, fetched.error,
                    )
                    continue

                stats["filled"] += 1
                stats["details"].append({
                    "policy_id": current.id,
                    "source": current.source_name,
                    "status": "filled",
                    "chars": len(fetched.content or ""),
                })
                logger.info(
                    "正文回填成功 policy_id=%s (%s 字) %s",
                    current.id, len(fetched.content or ""), (current.title or "")[:30],
                )
                if not dry_run:
                    current.content = fetched.content
                    db.commit()
            except Exception as exc:
                db.rollback()
                stats["failed"] += 1
                stats["details"].append({
                    "policy_id": policy.id,
                    "source": policy.source_name,
                    "status": "failed",
                    "reason": str(exc)[:200],
                })
                logger.error("正文回填异常 policy_id=%s: %s", policy.id, exc)
            finally:
                db.close()
        finally:
            lock.release()

    return stats


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="回填政策正文")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--limit", type=int, default=None, help="最多处理条数")
    parser.add_argument("--source", default=None, help="只处理指定来源")
    args = parser.parse_args(argv)

    stats = backfill(dry_run=args.dry_run, limit=args.limit, source=args.source)
    print()
    print("=" * 50)
    print(f"待回填: {stats['missing']}")
    print(f"成功:   {stats['filled']}")
    print(f"失败:   {stats['failed']}")
    print(f"跳过:   {stats['skipped']}")
    print("=" * 50)
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
