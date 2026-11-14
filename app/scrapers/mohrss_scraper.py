"""
人力资源和社会保障部政策文件采集器
支持在线抓取 / MOHRSS 官方搜索接口(分页) / Scrapling / 百度搜索回退
"""

from __future__ import annotations

import os
import re
import time
import random
from datetime import datetime
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from app.scrapers.base import BaseScraper, ScrapedItem
from app.scrapers.baidu_search import discover_policies_with_baidu
from app.scrapers.tavily_client import create_tavily_client
from app.logger import logger

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
MOHRSS_MIN_CONTENT_LENGTH = 100
MOHRSS_MAX_CONTENT_LENGTH = 12000
MOHRSS_ALLOWED_HOSTS = frozenset({"www.mohrss.gov.cn"})
_MOHRSS_WAF_MARKERS = (
    "function a(a)",
    "__tst_status",
    "访问验证",
    "安全验证",
    "人机验证",
)
_MOHRSS_POLICY_TITLE_KEYWORDS = (
    "通知", "意见", "办法", "规定", "决定", "公告", "方案", "细则",
    "规划", "条例", "批复", "指南", "标准", "规则",
)
_MOHRSS_NON_POLICY_TITLE_KEYWORDS = (
    "解读", "图解", "问答", "一图读懂", "新闻", "动态", "招聘", "招考",
    "考试", "直播", "访谈", "视频", "图片", "名单", "公示", "采购",
)
_MOHRSS_CONTENT_BOUNDARY_RE = re.compile(
    r"字体\s*[：:]\s*\[\s*大\s*中\s*小\s*\]"
)
_MOHRSS_FOOTER_RE = re.compile(r"^[ \t]*网站声明[ \t]*$", re.MULTILINE)


class MohrssScraper(BaseScraper):
    def __init__(self, source_config: dict):
        super().__init__(source_config)
        # 列表页（备用）
        self.list_url = source_config.get(
            "list_url", "https://www.mohrss.gov.cn/xxgk2020/zcwjk/"
        )
        # MOHRSS 官方搜索接口
        self.search_api_base = "https://www.mohrss.gov.cn/was5/web/search"
        self.base_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
            "Cache-Control": "max-age=0",
            "Referer": "https://www.mohrss.gov.cn/",
        }
        self.headers = dict(self.base_headers)
        self.days_back: Optional[int] = source_config.get("days_back")
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

    def _random_headers(self, referer: str = "https://www.mohrss.gov.cn/"):
        h = dict(self.base_headers)
        h["Referer"] = referer
        if random.random() < 0.5:
            h["Accept-Encoding"] = "gzip, deflate"
        if random.random() < 0.5:
            h["Accept-Language"] = "zh-CN,zh;q=0.9"
        return h

    def _request(self, url: str, params: dict = None, headers: dict = None):
        session = self._get_session()
        h = self._random_headers()
        if headers:
            h.update(headers)
        return session.get(url, timeout=30, headers=h, params=params)

    def _normalize_url(self, href: str) -> str:
        if href.startswith("http://") or href.startswith("https://"):
            return href
        href = href.lstrip("./")
        if href.startswith("/"):
            return "https://www.mohrss.gov.cn" + href
        return "https://www.mohrss.gov.cn/" + href

    def _clean_and_normalize_url(self, url: str) -> str:
        url = url.strip()
        if '"' in url:
            url = url.split('"')[0].strip()
        if "'" in url:
            url = url.split("'")[0].strip()
        return self._normalize_url(url)

    def _ensure_utf8(self, text: str) -> str:
        """仅在 latin-1 -> UTF-8 严格转换成功时修复 mojibake。"""
        if not text:
            return text
        # 保留原触发条件，避免把正常的拉丁字符正文误当作乱码重解码。
        if "锟" not in text and "?" not in text:
            return text
        try:
            return text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            # 无法完整还原时必须保留原文，不能静默删除不可恢复字符。
            return text

    @staticmethod
    def _has_reliable_content_boundary(text: str) -> bool:
        """判断文本是否包含人社部详情页的稳定正文起始边界。"""
        return bool(text and _MOHRSS_CONTENT_BOUNDARY_RE.search(text))

    def _clean_mohrss_content(self, text: str) -> Optional[str]:
        """按稳定页面边界剔除人社部详情页外壳，不对无边界文本做猜测。"""
        if not text:
            return None

        boundary = _MOHRSS_CONTENT_BOUNDARY_RE.search(text)
        if boundary is None:
            # 来源可能是已清洗正文；没有稳定证据时保持原文完全不变。
            return text

        cleaned = text[boundary.end():].lstrip(" \t\r\n")
        footer = _MOHRSS_FOOTER_RE.search(cleaned)
        if footer is not None:
            cleaned = cleaned[:footer.start()].rstrip(" \t\r\n")

        if len(cleaned.strip()) < MOHRSS_MIN_CONTENT_LENGTH:
            return None
        return cleaned

    def _finalize_content(self, text: Optional[str]) -> Optional[str]:
        """统一执行编码修复、正文清洗、最小长度门禁和长度上限。"""
        if not text:
            return None
        normalized = self._ensure_utf8(text)
        cleaned = self._clean_mohrss_content(normalized)
        if not cleaned:
            return None
        cleaned = cleaned.strip()
        if len(cleaned) < MOHRSS_MIN_CONTENT_LENGTH:
            return None
        return cleaned[:MOHRSS_MAX_CONTENT_LENGTH]

    def _tavily_api_call(self, endpoint: str, payload: dict, timeout: int = 90, max_retries: int = 3):
        client = create_tavily_client(TAVILY_API_KEY, timeout=timeout, max_retries=max_retries, logger=logger)
        result = client.call_with_details(
            endpoint,
            payload,
            timeout=timeout,
            max_retries=max_retries,
        )
        self.last_fetch_error_type = result.error_type if not result.ok else None
        return result.data

    def _tavily_extract(self, url: str, depth: str = "advanced"):
        return self._tavily_api_call("/extract", {"urls": [url], "extract_depth": depth})

    @staticmethod
    def _is_waf_challenge_text(text: str) -> bool:
        """识别返回 HTTP 200 但实际为 WAF/人机挑战的空壳页面。"""
        if not text:
            return False
        lowered = text.lower()
        return any(marker.lower() in lowered for marker in _MOHRSS_WAF_MARKERS)

    def _is_valid_policy_url(self, url: str) -> bool:
        """校验 URL 是否为 MOHRSS 主站的有效政策详情页。"""
        if not url:
            return False
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        if (parsed.hostname or "").lower() not in MOHRSS_ALLOWED_HOSTS:
            return False
        # 过滤明确的解读页面；不能用 "jd" 子串，否则会误伤 xgfzjd 等合法路径。
        interpretation_paths = ("/zcjd/", "/zhengcejiedu/", "/interpret/")
        path_lower = parsed.path.lower()
        if "解读" in parsed.path or any(path in path_lower for path in interpretation_paths):
            return False
        # 多种详情页格式
        patterns = [
            r"/\d{6}/t\d{8}_",           # /202607/t20260724_580859.html
            r"/\d{4}/\d{2}/\d{2}/",      # /2026/07/24/
            r"/t\d{8}_",                 # /t20260724_580859.html
            r"t\d{8}_\d+\.html",         # t20260724_580859.html
        ]
        for pat in patterns:
            if re.search(pat, parsed.path):
                return True
        logger.debug("URL 格式不符合政策详情页: %s", url)
        return False

    def _is_policy_discovery_title(self, title: str) -> bool:
        """搜索发现结果必须同时像政策标题且不是常见非政策内容。"""
        normalized = (title or "").strip()
        if not normalized:
            return False
        if self._is_interpretation_page(normalized):
            return False
        if any(word in normalized for word in _MOHRSS_NON_POLICY_TITLE_KEYWORDS):
            return False
        return any(word in normalized for word in _MOHRSS_POLICY_TITLE_KEYWORDS)

    @staticmethod
    def _search_result_has_structure(soup: BeautifulSoup) -> bool:
        """数据表及 td-date 同时存在，才可作为可信搜索结果结构。"""
        for table in soup.find_all("table"):
            classes = table.get("class") or []
            if "i_nav" in classes:
                continue
            if table.find("td", class_="td-date"):
                return True
        return False

    def _list_page_has_structure(self, soup: BeautifulSoup) -> bool:
        """列表页至少包含一个受支持的官方政策详情链接。"""
        html_text = str(soup)
        patterns = [
            r'href=["\']([^"\']+?/\d{6}/t\d{8}_[^"\']*?)["\']',
            r'href=["\']([^"\']+?/t\d{8}_[^"\']*?)["\']',
            r'href=["\']([^"\']+?/\d{4}/\d{2}/\d{2}/[^"\']*?)["\']',
        ]
        for pattern in patterns:
            for href in re.findall(pattern, html_text):
                candidate = self._clean_and_normalize_url(href)
                if self._is_valid_policy_url(candidate):
                    return True
        return False

    def _is_interpretation_page(self, title: str) -> bool:
        """判断是否为解读页面"""
        if not title:
            return False
        interpret_keywords = ["解读", "【解读】", "[解读]", "图解", "问答", "一图读懂"]
        return any(kw in title for kw in interpret_keywords)

    def _extract_date_from_text(self, text: str, url: str):
        """从文本或 URL 中提取发布日期"""
        if not text:
            text = ""
        # 从文本中提取
        m = re.search(r"(\d{4}[-./年]\d{1,2}[-./月]\d{1,2})", text)
        if m:
            try:
                date_str = m.group(1).replace("年", "-").replace("月", "-").replace("日", "").replace("/", "-")
                return datetime.strptime(date_str[:10], "%Y-%m-%d")
            except Exception:
                pass
        # 从 URL 中提取
        patterns = [
            r"/(\d{4})(\d{2})/t\d{4}(\d{2})(\d{2})",
            r"/(\d{4})(\d{2})/t\d{8}_",
            r"/(\d{4})/(\d{2})/(\d{2})/",
            r"t(\d{4})(\d{2})(\d{2})_",
        ]
        for pat in patterns:
            m = re.search(pat, url)
            if m:
                try:
                    groups = m.groups()
                    if len(groups) >= 4:
                        return datetime(int(groups[0]), int(groups[1]), int(groups[3]))
                    elif len(groups) >= 3:
                        return datetime(int(groups[0]), int(groups[1]), int(groups[2]))
                    elif len(groups) >= 2:
                        return datetime(int(groups[0]), int(groups[1]), 1)
                except Exception:
                    pass
        return None

    def _parse_search_results_page(self, soup: BeautifulSoup, page_num: int) -> List[ScrapedItem]:
        """解析搜索结果页面的表格 - 处理不规范的 HTML 结构"""
        items = []
        
        # 查找数据表格：优先找 style="border-collapse:separate;" 且包含 td-date 的表格
        # 排除 class="i_nav" 的表头表格
        table = None
        tables = soup.find_all("table")
        for t in tables:
            # 跳过表头导航表格
            if t.get("class") and "i_nav" in t.get("class"):
                continue
            # 必须包含 td-date 才是数据表格
            if t.find("td", class_="td-date"):
                table = t
                break
        
        if not table:
            logger.debug("第 %s 页未找到数据表格", page_num)
            return items
        
        # 由于 HTML 可能不规范（td 直接在 table 下），我们需要特殊处理
        # 策略：找到所有 td-date，然后向后查找对应的链接 td
        all_tds = table.find_all("td")
        logger.debug("第 %s 页表格中找到 %s 个 td 元素", page_num, len(all_tds))
        
        i = 0
        page_items = 0
        while i < len(all_tds):
            td = all_tds[i]
            # 寻找日期单元格
            if td.get("class") and "td-date" in td.get("class"):
                date_span = td.find("span")
                pub_time = None
                if date_span:
                    date_text = date_span.get_text(strip=True)
                    try:
                        pub_time = datetime.strptime(date_text, "%Y-%m-%d")
                    except Exception:
                        pass
                
                # 向后查找链接单元格（通常是紧接着的下一个非 td-date, 非 td-right 的 td）
                link_td = None
                j = i + 1
                while j < len(all_tds):
                    next_td = all_tds[j]
                    cls = next_td.get("class", [])
                    if "td-date" not in cls and "td-right" not in cls:
                        # 这个应该是链接单元格
                        a_tag = next_td.find("a", href=True)
                        if a_tag:
                            link_td = next_td
                            break
                    j += 1
                
                if link_td:
                    a_tag = link_td.find("a", href=True)
                    url = a_tag.get("href", "")
                    title = a_tag.get_text(strip=True)
                    
                    if title and url:
                        url = self._clean_and_normalize_url(url)
                        if self._is_valid_policy_url(url):
                            if not self._is_interpretation_page(title):
                                # 如果没从表格获取到日期，从标题/URL 提取
                                if not pub_time:
                                    pub_time = self._extract_date_from_text(title, url)
                                
                                # 日期筛选
                                if self.days_back:
                                    if not pub_time or (datetime.now() - pub_time).days > self.days_back:
                                        logger.debug("政策日期缺失或过期，跳过: %s (发布: %s)", title[:30], pub_time)
                                    else:
                                        items.append(ScrapedItem(
                                            title=title,
                                            url=url,
                                            source_name=self.name,
                                            government_level=self.level,
                                            department=self.config.get("department", ""),
                                            city=self.city,
                                            publish_time=pub_time,
                                        ))
                                        page_items += 1
                                else:
                                    items.append(ScrapedItem(
                                        title=title,
                                        url=url,
                                        source_name=self.name,
                                        government_level=self.level,
                                        department=self.config.get("department", ""),
                                        city=self.city,
                                        publish_time=pub_time,
                                    ))
                                    page_items += 1
            
            i += 1
        
        logger.info("第 %s 页解析结果: %s 条", page_num, page_items)
        return items

    def _fetch_from_search_api(self) -> Tuple[List[ScrapedItem], bool]:
        """使用 MOHRSS 官方搜索接口获取政策列表。

        api_succeeded 只有在确认数据表结构后才为 True。HTTP 200 的 WAF 页面、
        空壳页面或结构变化均视为失败，避免把故障误报成合法的空结果。
        """
        all_items = []
        max_pages = 10
        pagesize = 50
        api_succeeded = False
        self.last_fetch_error_type = None

        try:
            for page_num in range(1, max_pages + 1):
                params = {
                    "channelid": "203464",
                    "orderby": "date",
                    "default": "isall",
                    "page": str(page_num),
                    "pagesize": str(pagesize),
                }
                logger.info("调用 MOHRSS 官方搜索接口 (第 %s 页)", page_num)

                try:
                    resp = self._request(self.search_api_base, params=params)
                except Exception as exc:
                    self.last_fetch_error_type = "transport_error"
                    logger.warning("MOHRSS 搜索接口传输失败: %s", exc)
                    break

                if resp.status_code != 200:
                    if resp.status_code in {403, 412, 429}:
                        self.last_fetch_error_type = "waf_challenge"
                    elif resp.status_code >= 500:
                        self.last_fetch_error_type = "server_error"
                    else:
                        self.last_fetch_error_type = "http_error"
                    logger.warning("搜索接口第 %s 页返回状态码: %s", page_num, resp.status_code)
                    break

                resp.encoding = resp.apparent_encoding or "utf-8"
                if self._is_waf_challenge_text(resp.text):
                    self.last_fetch_error_type = "waf_challenge"
                    logger.warning("搜索接口第 %s 页返回 HTTP 200，但内容为 WAF/挑战页面", page_num)
                    break

                soup = BeautifulSoup(resp.text, "lxml")
                if not self._search_result_has_structure(soup):
                    self.last_fetch_error_type = "schema_changed"
                    logger.warning("搜索接口第 %s 页缺少可信数据表结构", page_num)
                    break

                # 只有确认列表结构后，才承认此次在线调用有效。
                api_succeeded = True
                page_items = self._parse_search_results_page(soup, page_num)

                if not page_items:
                    logger.info("第 %s 页结构有效但日期窗口内无条目，停止分页", page_num)
                    break

                all_items.extend(page_items)
                if len(page_items) < pagesize:
                    logger.info("第 %s 页数据少于 pagesize，已到最后一页", page_num)
                    break

                time.sleep(0.5)

            logger.info("搜索接口总计解析: %s 条", len(all_items))

        except Exception as e:
            self.last_fetch_error_type = "parse_failed"
            logger.warning("MOHRSS 搜索接口异常: %s", e)
            import traceback
            logger.debug(traceback.format_exc())

        if all_items:
            self.last_fetch_error_type = None
        return all_items, api_succeeded

    def fetch(self) -> List[ScrapedItem]:
        logger.info("开始采集 人力资源和社会保障部 政策列表...")
        all_items = []
        online_succeeded = False
        errors = []
        error_types = []

        def remember_error(message: str, error_type: Optional[str]) -> None:
            if message:
                errors.append(message)
            if error_type:
                error_types.append(error_type)

        # ========== Level 1: MOHRSS 官方搜索接口（优先，支持分页）==========
        items, api_succeeded = self._fetch_from_search_api()
        all_items.extend(items)
        online_succeeded = online_succeeded or api_succeeded
        if items:
            logger.info("官方搜索接口成功: %s 条", len(items))
            self._mark_live_success()
            return all_items
        if api_succeeded:
            logger.info("官方搜索接口结构有效但无日期窗口内数据，继续尝试在线列表页")
        else:
            remember_error(
                "官方搜索接口未返回可信列表结构",
                self.last_fetch_error_type or "schema_changed",
            )

        # ========== Level 2: 在线抓取列表页（搜索接口无数据时兜底）==========
        logger.info("尝试在线抓取列表页: %s", self.list_url)
        try:
            resp = self._request(self.list_url)
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding or "utf-8"
                if self._is_waf_challenge_text(resp.text):
                    logger.warning("检测到 WAF/挑战页面，跳过列表页解析")
                    remember_error("列表页返回 WAF/挑战页面", "waf_challenge")
                else:
                    soup = BeautifulSoup(resp.text, "lxml")
                    if self._list_page_has_structure(soup):
                        online_succeeded = True
                        items = self._parse_list_page(soup, self.list_url)
                        all_items.extend(items)
                        logger.info("在线列表页结构有效，解析到 %s 条", len(items))
                        if items:
                            self._mark_live_success()
                            return all_items
                    else:
                        logger.warning("列表页缺少可信政策列表结构")
                        remember_error("列表页缺少可信政策列表结构", "schema_changed")
            else:
                error_type = (
                    "waf_challenge"
                    if resp.status_code in {403, 412, 429}
                    else "server_error"
                    if resp.status_code >= 500
                    else "http_error"
                )
                remember_error(f"列表页 HTTP {resp.status_code}", error_type)
                if resp.status_code == 412:
                    logger.warning("在线抓取遇到 412 WAF 拦截")
        except Exception as e:
            logger.warning("在线抓取列表页异常: %s", e)
            remember_error(f"列表页异常: {e}", "transport_error")

        # ========== Level 3: 百度搜索（官方接口和列表页均失败）==========
        if not all_items:
            logger.info("使用百度 AI 搜索补充 MOHRSS 政策")
            discovery = discover_policies_with_baidu(self.config)
            if discovery.status == "ok":
                all_items.extend(discovery.items)
                online_succeeded = True
                self._mark_live_success()
                return all_items
            if discovery.status == "empty":
                online_succeeded = True
            else:
                remember_error(
                    discovery.error or "百度搜索失败",
                    discovery.error_type or "baidu_unknown",
                )

        # 去重
        seen = set()
        unique_items = []
        for item in all_items:
            if item.url not in seen:
                seen.add(item.url)
                unique_items.append(item)

        if unique_items:
            self._mark_live_success()
        elif online_succeeded:
            self._mark_empty()
        else:
            priority = (
                "waf_challenge",
                "server_error",
                "transport_error",
                "schema_changed",
                "parse_failed",
                "http_error",
                "baidu_auth",
                "baidu_quota_exhausted",
                "baidu_rate_limit",
                "baidu_timeout",
                "baidu_network",
                "baidu_server",
                "baidu_invalid_response",
                "baidu_client",
                "baidu_unknown",
            )
            final_error_type = next(
                (kind for kind in priority if kind in error_types),
                error_types[0] if error_types else "all_channels_failed",
            )
            self._mark_failed(
                "；".join(errors) or "所有在线采集通道均失败",
                final_error_type,
            )

        logger.info("MOHRSS 采集完成: %s 条 (去重后)", len(unique_items))
        return unique_items

    def _parse_list_page(self, soup: BeautifulSoup, base_url: str) -> List[ScrapedItem]:
        """解析列表页 HTML，提取政策链接"""
        items = []
        html_text = str(soup)
        patterns = [
            r'href=["\']([^"\']+?/\d{6}/t\d{8}_[^"\']*?)["\']',
            r'href=["\']([^"\']+?/t\d{8}_[^"\']*?)["\']',
            r'href=["\']([^"\']+?/\d{4}/\d{2}/\d{2}/[^"\']*?)["\']',
        ]
        urls = []
        for pat in patterns:
            urls.extend(re.findall(pat, html_text))

        for href in urls:
            url = self._clean_and_normalize_url(href)
            if not self._is_valid_policy_url(url):
                continue
            title = ""
            a_tag = soup.find("a", href=re.compile(re.escape(href)))
            if a_tag:
                title = a_tag.get_text(strip=True)
            if not title:
                m = re.search(r"/t(\d{8})_(\d+)", url)
                if m:
                    title = "政策文件 " + m.group(1)
            if self._is_interpretation_page(title):
                logger.debug("跳过解读页面: %s", title)
                continue
            pub_time = self._extract_date_from_text(title, url)
            if self.days_back and (
                not pub_time or (datetime.now() - pub_time).days > self.days_back
            ):
                continue
            items.append(ScrapedItem(
                title=title or "未知标题",
                url=url,
                source_name=self.name,
                government_level=self.level,
                department=self.config.get("department", ""),
                city=self.city,
                publish_time=pub_time,
            ))

        if not items:
            for a in soup.find_all("a", href=True):
                href = a.get("href", "")
                title = a.get_text(strip=True)
                if not title or len(title) < 5:
                    continue
                url = self._clean_and_normalize_url(href)
                if not self._is_valid_policy_url(url):
                    continue
                if self._is_interpretation_page(title):
                    continue
                pub_time = self._extract_date_from_text(title, url)
                if self.days_back and pub_time:
                    if (datetime.now() - pub_time).days > self.days_back:
                        continue
                items.append(ScrapedItem(
                    title=title,
                    url=url,
                    source_name=self.name,
                    government_level=self.level,
                    department=self.config.get("department", ""),
                    city=self.city,
                    publish_time=pub_time,
                ))

        return items

    def _extract_with_scrapling(self, url: str):
        """复用 Scrapling 0.5 兼容封装，从真实详情页提取正文。"""
        try:
            from app.scrapers.scrapling_fetcher import ScraplingFetcher

            html = ScraplingFetcher.fetch_html(url, mode="stealth", retries=1)
            if not html:
                return None

            content = self._extract_content_from_html(html, url)
            if content and content.strip():
                return content
        except Exception as exc:
            logger.warning("Scrapling 提取异常: %s", exc)
        return None

    def _extract_content_from_html(self, html: str, url: str) -> str:
        """从 HTML 中提取正文内容"""
        # 检测 WAF/挑战页面
        if "function a(a)" in html or "__tst_status" in html or len(html) < 2000:
            return ""
        
        soup = BeautifulSoup(html, "lxml")
        text = self._extract_by_selectors(soup, [
            ("div", {"class": "view TRS_UEDITOR trs_paper_default trs_web"}),
            ("div", {"class": "article_content"}),
            ("div", {"class": "content"}),
            ("div", {"class": "article"}),
            ("div", {"class": "text"}),
            ("div", {"class": "TRS_Editor"}),
            ("div", {"class": "con"}),
            ("article", {}),
            ("main", {}),
        ])
        return text or ""

    def _extract_by_selectors(self, soup: BeautifulSoup, selectors: list) -> str:
        """按选择器列表尝试提取正文"""
        for tag, attrs in selectors:
            element = soup.find(tag, attrs)
            if element:
                text = element.get_text("\n", strip=True)
                if text and len(text) >= 100:
                    return text
        # 兜底：整页 body 只有在稳定正文边界生效时才可作为最后候选。
        body = soup.find("body")
        if body:
            body_text = body.get_text("\n", strip=True)
            if (
                self._has_reliable_content_boundary(body_text)
                and len(body_text) >= 200
            ):
                return self._clean_mohrss_content(body_text) or ""
        return ""

    
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
                        "div.article_content",
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
                            if text.strip():
                                await browser.close()
                                return text

                    body_text = await page.inner_text("body")
                    await browser.close()
                    if self._has_reliable_content_boundary(body_text):
                        return self._clean_mohrss_content(body_text)
                    return None

            return asyncio.run(_extract())
        except Exception as e:
            logger.debug("Playwright extraction failed: %s", e)
            return None

    def _try_trafilatura(self, url):
        try:
            import trafilatura
            downloaded = trafilatura.fetch_url(url)
            if not downloaded:
                return None
            text = trafilatura.extract(downloaded, output_format="txt",
                                       include_links=False, include_images=False)
            if text and text.strip():
                return text
        except Exception:
            pass
        return None

    def extract_content(self, url: str):
        """提取正文：Trafilatura → Playwright → Scrapling → 基础请求 → Tavily。"""
        logger.info("提取正文: %s", url)
        if not self._is_valid_policy_url(url):
            logger.warning("跳过非政策详情页 URL: %s", url)
            return None

        # 1. trafilatura 提取
        content = self._finalize_content(self._try_trafilatura(url))
        if content:
            logger.info("trafilatura 提取正文成功（最终清洗后）: %s 字符", len(content))
            return content

        # 2. Playwright 提取
        content = self._finalize_content(self._try_playwright(url))
        if content:
            logger.info("Playwright 提取正文成功（最终清洗后）: %s 字符", len(content))
            return content

        # 3. Scrapling 绕过 WAF
        logger.info("尝试 Scrapling 绕过 WAF 提取正文...")
        try:
            content = self._finalize_content(self._extract_with_scrapling(url))
            if content:
                logger.info("Scrapling 提取正文成功（最终清洗后）: %s 字符", len(content))
                return content
            logger.warning("Scrapling 提取失败或清洗后内容过短")
        except Exception as e:
            logger.warning("Scrapling 调用异常: %s", e)

        # 4. 在线基础提取
        try:
            resp = self._request(url)
            if resp.status_code == 200:
                resp.encoding = resp.apparent_encoding or "utf-8"
                content = self._finalize_content(
                    self._extract_content_from_html(resp.text, url)
                )
                if content:
                    logger.info("在线提取正文成功（最终清洗后）: %s 字符", len(content))
                    return content
                logger.warning("在线页面未提取到有效正文 (内容长度: %s)", len(resp.text))
            elif resp.status_code == 412:
                logger.warning("在线提取遇到 412 WAF 拦截")
        except Exception as e:
            logger.warning("在线提取异常: %s", e)

        # 5. Tavily Extract 兜底
        if TAVILY_API_KEY:
            try:
                logger.info("使用 Tavily extract API 提取正文 (advanced)")
                extract_data = self._tavily_extract(url, "advanced")
                if extract_data and extract_data.get("results"):
                    for r in extract_data["results"]:
                        raw_content = r.get("raw_content", r.get("content", ""))
                        content = self._finalize_content(raw_content)
                        if content:
                            logger.info("Tavily extract 正文成功（最终清洗后）: %s 字符", len(content))
                            return content
                        logger.debug(
                            "Tavily extract 返回内容清洗后过短: %s 字符",
                            len(raw_content) if raw_content else 0,
                        )
                else:
                    logger.warning("Tavily extract 返回空结果: %s", extract_data)
            except Exception as e:
                logger.warning("Tavily extract 失败: %s", e)
                import traceback
                logger.debug(traceback.format_exc())
        else:
            logger.debug("Tavily API Key 未配置，跳过 Tavily extract")

        # 所有真实通道均失败时保留失败语义，交由工作流记录并重试。
        logger.warning("所有正文提取通道均失败: %s", url)
        return None
