"""武汉市人民政府政策采集器

武汉门户主站列表页是 JS 渲染（静态 HTML 无政策链接）。当前路径：

1. 发现：百度 AI 搜索（site:wuhan.gov.cn + 政策关键词）作为候选补充；
   实测百度可能未收录武汉政策详情页，因此空或少量结果不能短路官网。
2. 官网：通过 Scrapling 访问实际包含政策链接的栏目，与百度候选合并去重。
3. 正文：www.wuhan.gov.cn 政策详情页可直连抓取，正文不使用搜索 API。

不修改现有采集器，为武汉单独实现。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app.logger import logger
from app.scrapers.base import BaseScraper, ScrapedItem
from app.scrapers.baidu_search import discover_policies_with_baidu

# 只接受武汉市政策文件栏目，避免把新闻/图解/公示当成政策
POLICY_PATH_PATTERNS = [
    r"/zwgk/xxgk/zfwj/",       # 市政府文件（规范性文件/部门文件/政府文件）
    r"/gfxwj/",                # 规范性文件
]

# 标题必须命中的政策特征词，进一步排除新闻和会议报道
POLICY_TITLE_KEYWORDS = [
    "通知", "意见", "办法", "规定", "决定", "方案", "规划",
    "条例", "细则", "公告", "政策措施", "实施细则", "行动计划",
    "实施意见", "管理办法",
]


class WuhanScraper(BaseScraper):
    """武汉市政策采集器：百度发现 + 直连抓正文"""

    # Scrapling 直连时优先访问实际含政策链接的子栏目。
    # 主入口 /zfwj/ 的列表由脚本二次加载，实测没有可直接解析的政策链接。
    SCRAPLING_LIST_URLS = [
        "https://www.wuhan.gov.cn/zwgk/xxgk/zfwj/gfxwj/",
    ]

    def __init__(self, source_config: dict):
        super().__init__(source_config)
        self.days_back: Optional[int] = source_config.get("days_back")
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }

    # ---------- 发现 ----------

    @staticmethod
    def _is_policy_url(url: str) -> bool:
        """只接受政策文件页（.shtml）。

        排除搜索返回的 PDF 附件（如 /202003/P0201910226...pdf）：
        这类文件是历史政策的附件，路径日期与真实发布日期不符，
        且没有可靠发布日期，混入后会污染日报。
        """
        if not url:
            return False
        if not re.search(r"/t\d{8}_\d+\.s?html?$", url):
            return False
        return any(re.search(p, url) for p in POLICY_PATH_PATTERNS)

    @staticmethod
    def _is_policy_title(title: str) -> bool:
        if not title:
            return False
        return any(kw in title for kw in POLICY_TITLE_KEYWORDS)

    @staticmethod
    def _extract_publish_time(url: str) -> Optional[datetime]:
        # 优先从 /tYYYYMMDD_ 提取精确日期
        m = re.search(r"/t(\d{4})(\d{2})(\d{2})_", url)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        return None

    @staticmethod
    def _clean_title(title: str) -> str:
        # 搜索常返回 "标题 - 武汉市人民政府" / "标题_武汉市人民政府" 形式
        return re.split(r"\s*[-_|]\s*武汉市?人民政府.*$", title.strip())[0].strip()

    def _parse_list_html(self, html: str, base_url: str) -> List[ScrapedItem]:
        """解析武汉政策列表页，复用搜索通道的筛选和日期规则。"""
        if not html:
            return []

        cutoff = None
        if self.days_back is not None:
            cutoff = datetime.now().date() - timedelta(days=self.days_back)

        soup = BeautifulSoup(html, "lxml")
        items: List[ScrapedItem] = []
        seen = set()
        for link in soup.find_all("a", href=True):
            title = self._clean_title(" ".join(link.get_text(" ", strip=True).split()))
            url = urljoin(base_url, link["href"].strip())
            if url.startswith("http://"):
                url = "https://" + url[len("http://"):]
            if not title or len(title) < 8 or url in seen:
                continue
            if not self._is_policy_url(url) or not self._is_policy_title(title):
                continue

            publish_time = self._extract_publish_time(url)
            if cutoff and publish_time and publish_time.date() < cutoff:
                continue

            seen.add(url)
            items.append(ScrapedItem(
                title=title,
                url=url,
                source_name=self.name,
                government_level=self.level,
                department=self.config.get("department", "武汉市人民政府"),
                city=self.city or "武汉",
                publish_time=publish_time,
            ))

        items.sort(key=lambda i: i.publish_time or datetime.min, reverse=True)
        return items

    def _fetch_via_scrapling(self) -> List[ScrapedItem]:
        """百度请求失败后，直接从武汉官网抓取政策列表。"""
        try:
            from app.scrapers.scrapling_fetcher import ScraplingFetcher

            all_items: List[ScrapedItem] = []
            seen = set()
            for page_url in self.SCRAPLING_LIST_URLS:
                html = ScraplingFetcher.fetch_html(
                    page_url, mode="stealth", retries=1
                )
                for item in self._parse_list_html(html or "", page_url):
                    if item.url in seen:
                        continue
                    seen.add(item.url)
                    all_items.append(item)

            all_items.sort(
                key=lambda i: i.publish_time or datetime.min, reverse=True
            )
            logger.info("武汉 Scrapling 发现政策: %s 条", len(all_items))
            return all_items
        except Exception as exc:
            logger.warning("武汉 Scrapling 通道失败: %s", exc)
            return []

    def fetch(self) -> List[ScrapedItem]:
        logger.info("开始采集: %s", self.name)
        self.last_fetch_error = None
        self.last_fetch_error_type = None
        search_config = self.config.get("search_discovery") or {}
        search_mode = str(search_config.get("mode") or "disabled").lower()
        search_items: list[ScrapedItem] = []
        search_error_type: Optional[str] = None
        search_reason: Optional[str] = None

        def run_search() -> None:
            nonlocal search_items, search_error_type, search_reason
            discovery = discover_policies_with_baidu(self.config)
            if discovery.status == "ok":
                search_items = discovery.items
                logger.info("武汉百度发现政策: %s 条，继续官网补充", len(search_items))
                return
            if discovery.status == "empty":
                # 百度空结果不是官网无更新的可信证据。实时验证发现武汉政策
                # 详情页可能长期未被百度收录，因此仍须尝试官网和库内数据。
                search_error_type = "baidu_empty"
                search_reason = "百度搜索返回空结果"
                logger.warning("武汉百度搜索返回空结果，继续官网通道")
                return
            search_error_type = discovery.error_type
            search_reason = discovery.error or discovery.error_type or "百度搜索不可用"

        if search_mode == "preferred":
            run_search()

        # 搜索与官网结果必须合并，避免百度只收录部分页面时短路官网全量发现。
        items = self._fetch_via_scrapling()
        if items:
            self._mark_live_success()
            return self._merge_unique_items(search_items, items)

        if search_mode == "fallback":
            run_search()
        if search_items:
            self._mark_live_success()
            return search_items

        # 最终降级：所有实时通道失败后，才使用库内旧数据。
        reason = f"百度: {search_reason or '未启用'}; Scrapling: 未取得数据"
        logger.warning("武汉实时通道均失败，回退到库内已有政策: %s", reason)
        items = self._fetch_from_database()
        if items:
            self._mark_degraded(
                f"在线通道失败，使用数据库旧数据: {reason}",
                search_error_type or "all_channels_failed",
            )
            return items

        self._mark_failed(
            f"在线通道失败且数据库无可用旧数据: {reason}",
            search_error_type or "all_channels_failed",
        )
        return []

    def _fetch_from_database(self) -> List[ScrapedItem]:
        """从本地库回填武汉政策，作为搜索不可用时的降级来源。"""
        try:
            from app.database import SessionLocal
            from app.models.policy import Policy

            db = SessionLocal()
            try:
                rows = (
                    db.query(Policy)
                    .filter(Policy.source_name == self.name)
                    .order_by(Policy.publish_time.desc())
                    .limit(30)
                    .all()
                )
                logger.info("库内回退获取武汉政策: %s 条", len(rows))
                return [
                    ScrapedItem(
                        title=p.title,
                        url=p.url,
                        source_name=p.source_name,
                        government_level=p.government_level,
                        department=p.department,
                        city=p.city,
                        publish_time=p.publish_time,
                        content=p.content,
                    )
                    for p in rows
                ]
            finally:
                db.close()
        except Exception as exc:
            logger.warning("武汉库内回退失败: %s", exc)
            return []

    # ---------- 正文 ----------

    def extract_content(self, url: str) -> Optional[str]:
        """直连抓取武汉政策详情页正文。"""
        try:
            resp = requests.get(url, headers=self.headers, timeout=30)
            if resp.status_code != 200:
                logger.warning("武汉正文抓取状态码 %s: %s", resp.status_code, url[:80])
                return None
            resp.encoding = resp.apparent_encoding or "utf-8"
            html = resp.text
            # 主站软 404 会把任意错误 URL 返回为首页（固定体积），需识别
            if "武汉市人民政府门户网站" in html and "政策" not in html[:2000] and len(html) > 190000:
                logger.debug("武汉正文疑似回退首页，跳过: %s", url[:80])
                return None

            soup = BeautifulSoup(html, "lxml")
            for tag in soup(["script", "style", "nav", "header", "footer"]):
                tag.decompose()

            for selector in (
                "div.content", "div#content", "div.TRS_Editor",
                "div.article", "div.detail", "div.view", "div.zoom",
                "div.xxgk_content", "div.con",
            ):
                node = soup.select_one(selector)
                if node:
                    text = node.get_text("\n", strip=True)
                    if len(text) >= 80:
                        return text[:10000]

            text = soup.get_text("\n", strip=True)
            return text[:10000] if len(text) >= 80 else None
        except Exception as exc:
            logger.warning("武汉正文抓取异常: %s - %s", url[:80], exc)
            return None
