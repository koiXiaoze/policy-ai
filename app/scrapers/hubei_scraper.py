"""湖北省政府政策文件采集器
支持百度搜索发现 / cloudscraper / Playwright / 本地文件回退
"""

from __future__ import annotations

import os
import re
import random
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper, ScrapedItem
from app.scrapers.baidu_search import discover_policies_with_baidu
from app.scrapers.content_cleaner import clean_title
from app.scrapers.tavily_client import create_tavily_client
from app.logger import logger

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")


class HubeiScraper(BaseScraper):
    def __init__(self, source_config):
        super().__init__(source_config)
        self.list_url = source_config.get(
            "list_url", "https://www.hubei.gov.cn/zfwj/list1.shtml"
        )
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
            "Referer": "https://www.hubei.gov.cn/",
        }
        self.headers = dict(self.base_headers)
        self.days_back = source_config.get("days_back")
        self._session = None

    def _get_session(self):
        if self._session is None:
            try:
                import cloudscraper
                self._session = cloudscraper.create_scraper()
                logger.info("使用 cloudscraper 创建会话")
                return self._session
            except ImportError:
                logger.debug("cloudscraper 未安装，使用 requests Session")
                self._session = requests.Session()
                self._session.headers.update(self.headers)
        return self._session

    def _random_headers(self, referer="https://www.hubei.gov.cn/"):
        h = dict(self.base_headers)
        h["Referer"] = referer
        if random.random() < 0.5:
            h["Accept-Encoding"] = "gzip, deflate"
        if random.random() < 0.5:
            h["Accept-Language"] = "zh-CN,zh;q=0.9"
        return h

    def _request(self, url):
        session = self._get_session()
        return session.get(url, timeout=30, headers=self._random_headers())

    def _normalize_url(self, href):
        if href.startswith("http://") or href.startswith("https://"):
            return href
        href = href.lstrip("./")
        if href.startswith("/"):
            return "https://www.hubei.gov.cn" + href
        return "https://www.hubei.gov.cn/" + href

    def _clean_and_normalize_url(self, url):
        url = url.strip()
        if '"' in url:
            url = url.split('"')[0].strip()
        if "'" in url:
            url = url.split("'")[0].strip()
        return self._normalize_url(url)

    def _tavily_api_call(self, endpoint, payload, timeout=60, max_retries=3):
        # 动态读取模块级 Key，兼容测试 monkeypatch 和运行时配置刷新。
        client = create_tavily_client(TAVILY_API_KEY, timeout=timeout, max_retries=max_retries, logger=logger)
        result = client.call_with_details(
            endpoint,
            payload,
            timeout=timeout,
            max_retries=max_retries,
        )
        self.last_fetch_error_type = result.error_type if not result.ok else None
        return result.data

    def _tavily_extract(self, url, extract_depth="basic", fmt=None):
        payload = {
            "urls": [url],
            "extract_depth": extract_depth,
        }
        if fmt:
            payload["format"] = fmt
        return self._tavily_api_call("/extract", payload)

    def fetch(self):
        logger.info("开始采集: %s", self.name)
        self.last_fetch_error = None
        self.last_fetch_error_type = None

        search_config = self.config.get("search_discovery") or {}
        search_mode = str(search_config.get("mode") or "disabled").lower()
        search_items: list[ScrapedItem] = []
        if search_mode == "preferred":
            discovery = discover_policies_with_baidu(self.config)
            if discovery.status == "ok":
                search_items = discovery.items
                logger.info("百度政策发现获取 %s 条", len(discovery.items))
            elif discovery.status == "empty":
                # 空结果不能证明官网没有新政策：实测既可能确实无更新，
                # 也可能百度尚未收录政府详情页。必须继续走官网通道。
                logger.warning("百度政策发现返回空结果，继续官网通道")
            else:
                self.last_fetch_error_type = discovery.error_type
                logger.warning(
                    "百度政策发现失败，尝试官网通道: %s",
                    discovery.error or discovery.error_type,
                )

        items = self._fetch_from_list_page()
        if items:
            logger.info("在线列表页获取 %s 条 (日期筛选后)", len(items))
            self._mark_live_success()
            return self._merge_unique_items(search_items, items)

        if search_mode == "fallback":
            discovery = discover_policies_with_baidu(self.config)
            if discovery.status == "ok":
                self._mark_live_success()
                return discovery.items
            if discovery.status == "empty":
                self._mark_empty()
                return []
            self.last_fetch_error_type = discovery.error_type

        # 第二通道：Scrapling 直连（实测可突破 412 WAF）
        logger.warning("百度搜索不可用，尝试 Scrapling 直连通道")
        try:
            from app.scrapers.scrapling_fetcher import ScraplingFetcher
            from bs4 import BeautifulSoup
            html = ScraplingFetcher.fetch_html(self.list_url, mode="stealth", retries=1)
            if html:
                soup = BeautifulSoup(html, "lxml")
                items = self._parse_list_page(soup, self.list_url)
                logger.info("Scrapling 通道获取湖北政策: %s 条", len(items))
                if items:
                    self._mark_live_success()
                    return self._merge_unique_items(search_items, items)
        except Exception as exc:
            logger.warning("Scrapling 通道失败: %s", exc)

        if search_items:
            logger.info("官网在线通道未取得数据，使用百度发现结果 %s 条", len(search_items))
            self._mark_live_success()
            return search_items

        logger.warning("在线通道不可用，尝试从本地文件导入")
        items = self._fetch_from_local_files()
        # 本地静态文件往往只有零星几条，不足以代表"最近政策"。
        # 少于 3 条时继续回退到库内数据，避免只返回 1 条陈旧政策。
        if len(items) >= 3:
            self._mark_degraded("在线通道未取得数据，使用本地文件旧数据", self.last_fetch_error_type)
            return items

        # 最终降级：从库内已有政策回填，避免整个来源返回空。
        logger.warning("本地文件也无数据，回退到库内已有政策: %s", self.name)
        items = self._fetch_from_database()
        if items:
            self._mark_degraded("在线通道未取得数据，使用数据库旧数据", self.last_fetch_error_type)
        else:
            self._mark_failed("在线通道未取得数据，本地文件和数据库均无可用数据", self.last_fetch_error_type)
        return items

    def _fetch_from_database(self):
        """从本地库回填政策，作为在线通道失败时的降级来源。"""
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
                logger.info("库内回退获取 %s 政策: %s 条", self.name, len(rows))
                return [
                    ScrapedItem(
                        title=p.title, url=p.url, source_name=p.source_name,
                        government_level=p.government_level,
                        department=p.department, city=p.city,
                        publish_time=p.publish_time, content=p.content,
                    )
                    for p in rows
                ]
            finally:
                db.close()
        except Exception as exc:
            logger.warning("库内回退失败 %s: %s", self.name, exc)
            return []

    def _fetch_from_list_page(self):
        try:
            resp = self._request(self.list_url)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                items = self._parse_list_page(soup, self.list_url)
                if items:
                    return items
            elif resp.status_code == 412:
                logger.warning("列表页遇到 412 WAF 拦截")
        except Exception as e:
            logger.warning("列表页获取异常: %s", e)
        return []

    def _parse_list_page(self, soup, base_url):
        items = []
        seen = set()
        cutoff_date = None
        if self.days_back is not None:
            cutoff_date = datetime.now().date() - timedelta(days=self.days_back)

        list_area = soup.find("div", class_="list") or soup.find("ul", class_="list") or soup.find("div", class_="xxgk_list") or soup
        
        for a in list_area.find_all("a", href=True):
            href = a.get("href", "").strip()
            title = clean_title(a.get_text(" ", strip=True))

            if not href or not title or len(title) < 5:
                continue
            # 排除解读/附件等噪音条目（官网列表里 "[解读1]" 之类）
            if "解读" in title or title.startswith("[") or title.startswith("【"):
                continue

            # 湖北列表页使用 ./ezbf/... 形式，必须相对列表页目录解析。
            url = self._clean_and_normalize_url(urljoin(base_url, href))
            if not self._is_same_origin_list_policy_url(url, base_url):
                continue
            if url in seen:
                continue
            seen.add(url)

            pub_time = None
            parent_li = a.find_parent("li")
            if parent_li:
                # 日期不一定在第一个 span 中；扫描整个条目既兼容官网
                # 结构变化，也保留“仅从条目可见文本取值”的可靠性边界。
                pub_time = self._extract_date_from_text(
                    parent_li.get_text(" ", strip=True), url
                )

            if not pub_time:
                pub_time = self._extract_date_from_text(title, url)

            if cutoff_date and pub_time:
                if pub_time.date() < cutoff_date:
                    continue

            items.append(ScrapedItem(
                title=title, url=url,
                source_name=self.name,
                government_level=self.level,
                department=self.config.get("department", "湖北省人民政府"),
                publish_time=pub_time,
            ))
        
        return items

    def _is_same_origin_list_policy_url(self, url, base_url):
        """列表解析只接受当前栏目目录内的本站具体文件页。"""
        try:
            candidate = urlsplit(url)
            base = urlsplit(base_url)
            candidate_host = (candidate.hostname or "").lower()
            base_host = (base.hostname or "").lower()
            base_path = base.path or "/"
            list_dir = base_path.rsplit("/", 1)[0].rstrip("/") + "/"
            return (
                candidate.scheme in {"http", "https"}
                and bool(candidate_host)
                and candidate_host == base_host
                and candidate.path.startswith(list_dir)
                and self._is_valid_policy_url(url)
                and re.search(r"/t\d{8}_", candidate.path) is not None
            )
        except ValueError:
            return False

    def _is_valid_policy_url(self, url):
        url_lower = url.lower()
        patterns = [
            "/zfwj/",
            "/xxgk/",
            "/zwgk/",
        ]
        return any(p in url_lower for p in patterns)

    def _extract_date_from_text(self, text, url=""):
        """从条目文本或 URL 提取明确日期，绝不使用采集时间兜底。"""
        text_patterns = (
            r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)",
            r"(?<!\d)(\d{4})\.(\d{1,2})\.(\d{1,2})(?!\d)",
            r"(?<!\d)(\d{4})年(\d{1,2})月(\d{1,2})日",
        )
        for pattern in text_patterns:
            m = re.search(pattern, text or "")
            if not m:
                continue
            try:
                return datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3))
                )
            except ValueError:
                continue

        # 先读详情页文件名中的真实日期（tYYYYMMDD_），不能使用归档
        # 目录年份/月份代替；跨月或跨年归档时两者可能不一致。
        url_patterns = (
            r"/t(\d{4})(\d{2})(\d{2})(?:_|\D|$)",
            r"/(\d{4})/(\d{2})/(\d{2})/",
            r"/(\d{4})(\d{2})(\d{2})/",
        )
        for pattern in url_patterns:
            m = re.search(pattern, url or "")
            if not m:
                continue
            try:
                return datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3))
                )
            except ValueError:
                continue
        return None

    def _fetch_from_local_files(self):
        data_dir = Path("data")
        if not data_dir.exists():
            return []
        
        items = []
        seen = set()
        cutoff = datetime.now() - timedelta(days=self.days_back) if self.days_back else None
        keywords = ["hubei", "湖北", "湖北省"]
        
        for f in sorted(data_dir.glob("*.html")) + sorted(data_dir.glob("*.htm")) + sorted(data_dir.glob("*.shtml")):
            try:
                fname = f.name.lower()
                if not any(kw in fname for kw in keywords):
                    continue
                html = f.read_text(encoding="utf-8", errors="ignore")
                soup = BeautifulSoup(html, "lxml")
                for a in soup.find_all("a", href=True):
                    href = a.get("href", "").strip()
                    title = a.get_text(" ", strip=True)
                    if not href or not title or len(title) < 5:
                        continue
                    if not self._is_valid_policy_url(href):
                        continue
                    url = self._normalize_url(href)
                    if url in seen:
                        continue
                    seen.add(url)
                    pub_time = self._extract_date_from_text(title, url)
                    if cutoff and pub_time and pub_time < cutoff:
                        continue
                    items.append(ScrapedItem(
                        title=title, url=url,
                        source_name=self.name,
                        government_level=self.level,
                        department=self.config.get("department", "湖北省人民政府"),
                        publish_time=pub_time,
                    ))
            except Exception:
                continue
        return items

    def _extract_by_selectors(self, soup, selectors):
        for tag, attrs in selectors:
            try:
                main = soup.find(tag, **attrs)
                if not main:
                    continue
                text = main.get_text(separator="\n", strip=True)
                if text and len(text) >= 50:
                    return text
            except Exception:
                continue
        return None

    def _try_playwright(self, url):
        try:
            import asyncio
            from playwright.async_api import async_playwright

            async def _extract():
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    page = await browser.new_page()
                    await page.goto(url, wait_until="networkidle", timeout=30000)

                    selectors = [
                        "div.view.TRS_UEDITOR.trs_paper_default.trs_web",
                        "div.hbgov-article-content",
                        "div.view",
                        "div.pages_content",
                        "div.content",
                        "div.article",
                        "div[id*='content']",
                        "div[class*='content']",
                        "article",
                        "main",
                    ]

                    for sel in selectors:
                        elem = await page.query_selector(sel)
                        if elem:
                            text = await elem.inner_text()
                            if len(text.strip()) >= 100:
                                await browser.close()
                                return text[:12000]

                    body_text = await page.inner_text("body")
                    await browser.close()
                    if len(body_text.strip()) >= 100:
                        return body_text[:12000]
                    return None

            return asyncio.run(_extract())
        except Exception as e:
            logger.debug("Playwright 提取失败: %s", e)
            return None

    def _try_scrapling_content(self, url):
        """用 Scrapling 复用反爬会话，提取湖北政策正文。"""
        try:
            from app.scrapers.scrapling_fetcher import ScraplingFetcher

            html = ScraplingFetcher.fetch_html(url, mode="stealth", retries=1)
            if not html:
                return None

            soup = BeautifulSoup(html, "lxml")
            text = self._extract_by_selectors(
                soup,
                [
                    ("div", {"class": "view TRS_UEDITOR trs_paper_default trs_web"}),
                    ("div", {"class": "hbgov-article-content"}),
                    ("div", {"class": "view"}),
                    ("div", {"class": "pages_content"}),
                    ("div", {"class": "content"}),
                    ("div", {"class": "article"}),
                    ("div", {"class": "text"}),
                    ("div", {"class": "TRS_Editor"}),
                    ("div", {"class": "article_content"}),
                    ("article", {}),
                    ("main", {}),
                ],
            )
            if text and len(text.strip()) >= 100:
                return text[:12000]
        except Exception as exc:
            logger.warning("Scrapling 正文提取失败: %s", exc)
        return None

    def _try_trafilatura(self, url):
        try:
            import trafilatura
            downloaded = trafilatura.fetch_url(url)
            if not downloaded:
                return None
            text = trafilatura.extract(downloaded, output_format="txt",
                                       include_links=False, include_images=False)
            if text and len(text.strip()) >= 50:
                return text[:12000]
        except Exception:
            pass
        return None

    def _extract_from_local(self, url):
        data_dir = Path("data")
        url_id_parts = url.split("/")[-1].replace(".shtml", "")
        url_id_digit = re.search(r"(\d+)$", url_id_parts)
        candidates = [url_id_parts]
        if url_id_digit:
            candidates.append(url_id_digit.group(1))
        extra_cands = re.findall(r"\d{4}-\d{2}-\d{2}|\d{8}", url)
        for ec in extra_cands:
            candidates.append(ec)
        keywords = ["hubei", "湖北", "湖北省"]
        for f in (
            sorted(data_dir.glob("*.html"))
            + sorted(data_dir.glob("*.htm"))
            + sorted(data_dir.glob("*.txt"))
            + sorted(data_dir.glob("*.shtml"))
        ):
            try:
                fname = f.name.lower()
                if not any(kw in fname for kw in keywords):
                    continue
                html = f.read_text(encoding="utf-8", errors="ignore")
                if not any(cand.lower() in html.lower() for cand in candidates):
                    continue
                soup = BeautifulSoup(html, "lxml")
                text = self._extract_by_selectors(
                    soup,
                    [
                        ("div", {"class": "view TRS_UEDITOR trs_paper_default trs_web"}),
                        ("div", {"class": "hbgov-article-content"}),
                        ("div", {"class": "view"}),
                        ("div", {"class": "pages_content"}),
                        ("div", {"class": "content"}),
                        ("div", {"class": "article"}),
                        ("div", {"class": "text"}),
                        ("div", {"class": "TRS_Editor"}),
                        ("div", {"class": "article_content"}),
                        ("article", {}),
                        ("main", {}),
                    ],
                )
                if text:
                    logger.info("从本地文件提取正文成功: %s (%s 字符)", f.name, len(text))
                    return text[:12000]
            except Exception:
                continue
        return None

    def extract_content(self, url):
        logger.info("提取正文: %s", url)

        text = self._try_trafilatura(url)
        if text and len(text.strip()) >= 100:
            logger.info("trafilatura 提取正文成功: %s 字符", len(text))
            return text[:12000]

        text = self._try_playwright(url)
        if text and len(text.strip()) >= 100:
            logger.info("Playwright 提取正文成功: %s 字符", len(text))
            return text[:12000]

        text = self._try_scrapling_content(url)
        if text and len(text.strip()) >= 100:
            logger.info("Scrapling 提取正文成功: %s 字符", len(text))
            return text[:12000]

        try:
            resp = self._request(url)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "lxml")
                text = self._extract_by_selectors(
                    soup,
                    [
                        ("div", {"class": "view TRS_UEDITOR trs_paper_default trs_web"}),
                        ("div", {"class": "hbgov-article-content"}),
                        ("div", {"class": "view"}),
                        ("div", {"class": "pages_content"}),
                        ("div", {"class": "content"}),
                        ("div", {"class": "article"}),
                        ("div", {"class": "text"}),
                        ("div", {"class": "TRS_Editor"}),
                        ("div", {"class": "article_content"}),
                        ("article", {}),
                        ("main", {}),
                    ],
                )
                if text and len(text) >= 100:
                    logger.info("在线提取正文成功: %s 字符", len(text))
                    return text[:12000]
            elif resp.status_code == 412:
                logger.warning("在线提取遇到 412 WAF 拦截")
        except Exception as e:
            logger.warning("在线提取异常: %s", e)

        if TAVILY_API_KEY:
            try:
                logger.info("使用 Tavily extract API 提取正文")
                extract_data = self._tavily_extract(url, "advanced")
                if extract_data and extract_data.get("results"):
                    for r in extract_data["results"]:
                        content = r.get("raw_content", r.get("content", ""))
                        if content and len(content) >= 100:
                            logger.info("Tavily extract 正文成功: %s 字符", len(content))
                            return content[:12000]
            except Exception as e:
                logger.warning("Tavily extract 正文失败: %s", e)

        text = self._extract_from_local(url)
        if text:
            return text

        return None
