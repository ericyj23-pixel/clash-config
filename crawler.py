"""发布站爬虫：从"每日更新"文章页里找出真正可用的订阅链接。

思路：不硬编码地址（站点会不定期换格式防爬），而是
  抓页面 -> 收集候选 URL -> 实地下载验证（能解析出节点才算数）。
列表页没有订阅链接时，自动跟进最新文章。
"""
from __future__ import annotations

import html
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from parsers import SCHEME_RE, b64_decode

CST = timezone(timedelta(hours=8))

TEXT_URL_RE = re.compile(r"https?://[^\s\"'<>)\\\]`]{8,300}")
ANCHOR_RE = re.compile(r"<a[^>]+href=[\"']([^\"']+)[\"']", re.I)

ASSET_RE = re.compile(r"\.(png|jpe?g|gif|webp|css|js|ico|svg|woff2?|ttf|eot|mp4|zip|rar)(\?|$)", re.I)
JUNK_PATH_RE = re.compile(
    r"/(wp-content|wp-json|wp-admin|xmlrpc|feed|rss|comments?|author|tag|category|page/\d+|sitemap)"
    r"|/(about|archive|tags|categories|contact)(/|$)"
    r"|\.(xml|json|atom)(\?|$)",
    re.I,
)
JUNK_HOST_RE = re.compile(
    r"(w3\.org|schema\.org|googletagmanager|google-analytics|astro\.build|github\.com"
    r"|vercel\.app|rankmath|wordpress\.org|gravatar|purl\.org|wellformedweb)",
    re.I,
)

KEYWORDS = [
    (r"/sub\b|/subscribe|/api/v1/client|/link/\?|token=|sub=|/tree/", 3),
    (r"\.yaml(\?|$)|\.txt(\?|$)", 3),
    (r"clash|v2ray|shadowrocket|ssr|node", 2),
]
DATE_PATTERNS = [
    re.compile(r"(\d{4})[-/]?(\d{2})[-/]?(\d{2})"),
    re.compile(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)"),
]


def _dates_tokens() -> list[str]:
    """今天/昨天/前天的日期串，用于给候选 URL 排序。"""
    out = []
    for delta in (0, 1, 2):
        d = datetime.now(CST) - timedelta(days=delta)
        out += [d.strftime("%Y%m%d"), d.strftime("%y%m%d"),
                d.strftime("%Y-%m-%d"), d.strftime("%Y/%m/%d"), f"{d.month}月{d.day}日"]
    return out


def response_text(resp: requests.Response) -> str:
    """按 UTF-8 兜底解码响应体。

    多数订阅服务只返回 text/yaml、text/plain 而不声明 charset，
    requests 会按 latin-1 解码，产生非法控制字符，导致 YAML 解析直接崩。
    """
    if "charset" not in (resp.headers.get("content-type") or "").lower():
        resp.encoding = "utf-8"
    return resp.text


BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def fetch_html(url: str, session: requests.Session, timeout: int = 25, retries: int = 2) -> str:
    """抓 HTML。

    两个坑：
    1. 很多站点响应头不带 charset，requests 会按 latin-1 解出乱码，必须强制 UTF-8；
    2. 部分站点拦 python-requests 的 UA，必须换成浏览器 UA。
    """
    last_error = None
    for attempt in range(retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            return html.unescape(response_text(resp))
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                import time
                time.sleep(1.5 * (attempt + 1))
    raise last_error


def extract_urls(page_html: str, base: str) -> list[str]:
    """收集页面里的所有 URL：href 属性 + 正文文本，统一转成绝对地址。"""
    found: list[str] = []

    for href in ANCHOR_RE.findall(page_html):
        href = href.strip()
        if href.startswith(("mailto:", "javascript:", "#", "tel:")):
            continue
        found.append(urllib.parse.urljoin(base, href))

    found.extend(TEXT_URL_RE.findall(page_html))

    out, seen = [], set()
    for url in found:
        url = url.rstrip(".,;")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def is_junk(url: str) -> bool:
    if ASSET_RE.search(url) or JUNK_PATH_RE.search(url) or JUNK_HOST_RE.search(url):
        return True
    return url.startswith(("data:", "blob:"))


def score(url: str, hot_dates: list[str]) -> int:
    """给候选订阅地址打分，越高越可能是我们要的。"""
    if is_junk(url):
        return -1
    value = 0
    for pattern, weight in KEYWORDS:
        if re.search(pattern, url, re.I):
            value += weight
    if any(token in url for token in hot_dates):
        value += 5
    if url.rstrip("/").count("/") < 2:  # 站点首页这类
        value -= 2
    return value


def looks_like_subscription(text: str) -> bool:
    """内容是否像订阅：Clash YAML、分享链接，或 base64 后是这两者。"""
    text = (text or "").strip()
    if not text:
        return False
    if "proxies:" in text or SCHEME_RE.search(text):
        return True
    decoded = b64_decode(text)
    if decoded and ("proxies:" in decoded or SCHEME_RE.search(decoded)):
        return True
    return False


def validate(urls: list[str], session: requests.Session, timeout: int = 20,
             workers: int = 8, limit: int = 24) -> list[str]:
    """实地下载验证，只保留能解析出内容的订阅地址。"""
    def check(url: str) -> tuple[str, bool]:
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            return url, looks_like_subscription(response_text(resp))
        except Exception:  # noqa: BLE001
            return url, False

    picked = urls[:limit]
    ok: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for url, good in pool.map(check, picked):
            if good:
                ok.append(url)
    return ok


def find_article_links(page_html: str, base: str, limit: int = 4) -> list[str]:
    """从列表页挑出最新文章链接（列表通常按时间倒序，取靠前的即可）。"""
    base_host = urllib.parse.urlparse(base).netloc
    results: list[tuple[int, str]] = []

    for href, text in re.findall(
        r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", page_html, re.I | re.S
    ):
        url = urllib.parse.urljoin(base, href.strip())
        parsed = urllib.parse.urlparse(url)
        if parsed.netloc != base_host or is_junk(url):
            continue
        path = parsed.path.strip("/")
        if not path:
            continue
        anchor = re.sub(r"<[^>]+>", "", text)
        rank = 0
        if re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}日|20\d{6}", anchor + url):
            rank += 3
        if re.search(r"/\d{3,}|free-?node|jiedian|mianfei", url, re.I):
            rank += 2
        if rank:
            results.append((rank, url))

    results.sort(key=lambda x: -x[0])
    out, seen = [], set()
    for _, url in results:
        if url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= limit:
            break
    return out


def resolve_page(seed: str, session: requests.Session | None = None,
                 timeout: int = 25, max_depth: int = 2, log=print) -> list[str]:
    """入口：给定发布站地址，返回可用订阅链接列表。"""
    session = session or requests.Session()
    # 注意不能用 setdefault：requests 默认自带 python-requests UA，会被站点拦截
    session.headers["User-Agent"] = BROWSER_UA
    hot_dates = _dates_tokens()

    visited: set[str] = set()
    queue = [seed]
    for depth in range(max_depth + 1):
        next_queue: list[str] = []
        for page in queue:
            if page in visited:
                continue
            visited.add(page)
            try:
                page_html = fetch_html(page, session, timeout)
            except Exception as exc:  # noqa: BLE001
                log(f"[抓取失败] {page} -> {type(exc).__name__}")
                continue

            candidates = [u for u in extract_urls(page_html, page) if score(u, hot_dates) > 0]
            candidates.sort(key=lambda u: (-score(u, hot_dates), len(u)))

            if candidates:
                found = validate(candidates, session)
                if found:
                    log(f"[发布站] {page} -> {len(found)} 个可用订阅")
                    return found

            if depth < max_depth:
                next_queue.extend(find_article_links(page_html, page))

        queue = [u for u in next_queue if u not in visited]

    log(f"[发布站] {seed} -> 未找到可用订阅")
    return []
