"""联网小工具：搜索 + 读网页。

给机器人查资料用。只用 httpx，不引第三方解析库。

两个必须记住的坑：
1. 本机 http_proxy / no_proxy 等环境变量是坏的（大小写冲突 + 端口畸形），
   所以每个请求都显式 trust_env=False，并且先把这些变量删掉。
2. 搜狗/百度返回的都是 GBK 编码的 HTML，得手动解码。
"""

from __future__ import annotations

import html as _html
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

log = logging.getLogger("fishbot.net")

_PROXY_KEYS = (
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_DROP_RE = re.compile(r"<(script|style|noscript|svg|head|nav|footer)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v\u00a0]+")
_NL_RE = re.compile(r"\n{3,}")

_BING_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.I | re.S)
_BING_TAG_RE = re.compile(r"<(\w+)[^>]*>(.*?)</\1>", re.I | re.S)
_BING_ALGO_RE = re.compile(
    r'<li class="b_algo"[^>]*>(.*?)</li>', re.I | re.S
)
_BING_LINK_RE = re.compile(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_BING_SNIP_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)
_BAIDU_BLOCK_RE = re.compile(r'<div[^>]+class="result[^"]*"[^>]*>(.*?)</div>\s*</div>', re.I | re.S)
_BAIDU_LINK_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_SOGOU_TITLE_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.I | re.S)
_SOGOU_LINK_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.I | re.S)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_STOP_TERMS = (
    "是什么", "什么", "为什么", "怎么", "怎么样", "如何", "哪个", "哪些",
    "多少", "是不是", "有没有", "能不能", "可以", "知道", "告诉", "解释",
    "介绍", "意思", "含义", "来源", "出处", "区别", "梗", "一下", "看看",
    "帮我", "请问", "现在", "最近", "最新", "今天", "昨天", "明天",
)


def _query_terms(query: str) -> list[str]:
    """从查询里挑出真正有信息量的中文词。

    做两件事：按非中文字符切开，再把"是什么/为什么/梗"这类疑问词剥掉。
    剥完不到两个字就丢掉——剩下的才是判断"这条结果跑题没有"的依据。
    """
    terms: list[str] = []
    for chunk in _CJK_RUN_RE.findall(query or ""):
        core = chunk
        for stop in _STOP_TERMS:
            core = core.replace(stop, "")
        core = core.strip()
        if len(core) < 2 or core in terms:
            continue
        terms.append(core)
    return terms


def _relevance(hits: list[Hit], terms: list[str]) -> int:
    """命中了几条结果真的包含查询词。terms 为空时按条数算。"""
    if not terms:
        return len(hits)
    return sum(1 for hit in hits if _hit_relevant(hit, terms))


def _hit_relevant(hit: Hit, terms: list[str]) -> bool:
    if not terms:
        return True
    hay = f"{hit.title} {hit.url} {hit.snippet}"
    return any(term in hay for term in terms)


def strip_proxy_env() -> None:
    """删掉本机那几个坏掉的代理变量。"""
    for key in _PROXY_KEYS:
        os.environ.pop(key, None)


def _plain(markup: str) -> str:
    """把一段 HTML 变成干净文本。"""
    text = _DROP_RE.sub(" ", markup or "")
    text = _TAG_RE.sub(" ", text)
    text = _html.unescape(text)
    text = text.replace("\u3000", " ").replace("\xa0", " ")
    text = _WS_RE.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    return _NL_RE.sub("\n\n", text).strip()


@dataclass
class Hit:
    """一条搜索结果。"""

    title: str
    url: str
    snippet: str = ""

    def render(self, index: int, snippet_chars: int = 140) -> str:
        sn = (self.snippet or "").strip().replace("\n", " ")
        if len(sn) > snippet_chars:
            sn = sn[:snippet_chars] + "…"
        head = (self.title or self.url).strip()
        out = f"{index}. {head}\n   {self.url}"
        if sn:
            out += f"\n   {sn}"
        return out

    def to_dict(self) -> dict:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class Web:
    """搜索 + 抓网页。带一个内存缓存，同一个词短时间内不重复请求。"""

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, Any]] = {}
        self.counters = {
            "search": 0,
            "search_failed": 0,
            "fetch": 0,
            "fetch_failed": 0,
            "cache_hits": 0,
        }
        self.last_error = ""

    # ---------- 生命周期 ----------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", False))

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            strip_proxy_env()
            self._client = httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(float(self.cfg.timeout), connect=8.0),
                follow_redirects=True,
                headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
            )
        return self._client

    async def start(self) -> None:
        if not self.enabled:
            return
        self._ensure_client()
        log.info("联网：就绪（搜索 %s 条/次，超时 %ss）", self.cfg.max_results, self.cfg.timeout)

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ---------- 缓存 ----------

    def _get_cached(self, key: str) -> Any | None:
        item = self._cache.get(key)
        if item is None:
            return None
        stamp, value = item
        if time.time() - stamp > float(self.cfg.cache_seconds):
            self._cache.pop(key, None)
            return None
        self.counters["cache_hits"] += 1
        return value

    def _put_cached(self, key: str, value: Any) -> None:
        if len(self._cache) > 64:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:16]
            for stale_key, _ in oldest:
                self._cache.pop(stale_key, None)
        self._cache[key] = (time.time(), value)

    # ---------- 搜索 ----------

    async def search(self, query: str, count: int | None = None) -> list[Hit]:
        query = (query or "").strip()
        if not query:
            return []
        want = int(count or self.cfg.max_results)
        cache_key = f"search:{want}:{query}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        terms = _query_terms(query)
        backends: list[tuple[str, Any]] = [
            ("bing-rss", self._search_bing_rss),
            ("bing-html", self._search_bing_html),
            ("baidu", self._search_baidu),
            ("sogou", self._search_sogou),
        ]
        # 中文查询优先百度/搜狗：bing 对中文长查询常常只抓住第一个词
        # （搜"蓝色大鲸鲸 是什么梗"，bing 回的全是"蓝色是什么颜色"）
        if _CJK_RUN_RE.search(query):
            prefer = {"baidu": 0, "sogou": 1, "bing-rss": 2, "bing-html": 3}
            backends.sort(key=lambda pair: prefer.get(pair[0], 9))

        best: list[Hit] = []
        best_score = -1
        for name, func in backends:
            try:
                hits = await func(query, want)
            except Exception as exc:  # noqa: BLE001
                log.warning("联网：%s 搜索失败 %s", name, exc)
                continue
            if not hits:
                continue
            relevant = [hit for hit in hits if _hit_relevant(hit, terms)]
            log.info(
                "联网：搜索「%s」经 %s 得到 %s 条，相关 %s", query, name, len(hits), len(relevant)
            )
            if len(relevant) > best_score:
                best, best_score = relevant, len(relevant)
            # 过半相关就够了，不再打后面的后端
            if len(relevant) >= max(1, (want + 1) // 2):
                break

        # 全部后端都跑题时宁可不给结果，也别让她拿着"蓝色是什么颜色"去答话
        hits = best[:want] if best else []

        if hits:
            self.counters["search"] += 1
            self._put_cached(cache_key, hits)
        else:
            self.counters["search_failed"] += 1
            self.last_error = "搜索没拿到结果"
        return hits

    async def _search_bing_rss(self, query: str, want: int) -> list[Hit]:
        client = self._ensure_client()
        url = f"https://www.bing.com/search?q={quote(query)}&format=rss&count={max(want, 5)}"
        resp = await client.get(url)
        if resp.status_code != 200:
            return []
        body = resp.text
        out: list[Hit] = []
        for chunk in _BING_ITEM_RE.findall(body):
            fields: dict[str, str] = {}
            for tag, value in _BING_TAG_RE.findall(chunk):
                if tag.lower() not in fields:
                    fields[tag.lower()] = value
            link = _plain(fields.get("link", ""))
            if not link.startswith("http"):
                continue
            out.append(
                Hit(
                    title=_plain(fields.get("title", "")),
                    url=link,
                    snippet=_plain(fields.get("description", "")),
                )
            )
            if len(out) >= want:
                break
        return out

    async def _search_bing_html(self, query: str, want: int) -> list[Hit]:
        client = self._ensure_client()
        resp = await client.get(f"https://www.bing.com/search?q={quote(query)}&setlang=zh-CN")
        if resp.status_code != 200:
            return []
        out: list[Hit] = []
        for block in _BING_ALGO_RE.findall(resp.text):
            link = _BING_LINK_RE.search(block)
            if not link:
                continue
            url = _html.unescape(link.group(1))
            if not url.startswith("http"):
                continue
            snip = _BING_SNIP_RE.search(block)
            out.append(
                Hit(
                    title=_plain(link.group(2)),
                    url=url,
                    snippet=_plain(snip.group(1)) if snip else "",
                )
            )
            if len(out) >= want:
                break
        return out

    async def _search_baidu(self, query: str, want: int) -> list[Hit]:
        client = self._ensure_client()
        resp = await client.get(f"https://www.baidu.com/s?wd={quote(query)}&rn={max(want, 10)}")
        if resp.status_code != 200:
            return []
        body = resp.text
        out: list[Hit] = []
        for block in _BAIDU_BLOCK_RE.findall(body):
            link = _BAIDU_LINK_RE.search(block)
            if not link:
                continue
            url = _html.unescape(link.group(1))
            if not url.startswith("http"):
                continue
            title = _plain(link.group(2))
            if len(title) < 3:
                continue
            if "top.baidu.com/board" in url or "baidu.com/link?url=" not in url and "www.baidu.com/s?" in url:
                continue
            out.append(Hit(title=title, url=url, snippet=_plain(block)[:200]))
            if len(out) >= want:
                break
        # 百度给的是 /link?url=xxx 跳转壳，换成真实地址（失败就留着，fetch 也能跟过去）
        for hit in out:
            hit.url = await self._unwrap(hit.url)
        return out

    async def _unwrap(self, url: str) -> str:
        """跟着跳转拿到真实地址。只处理百度/搜狗那种壳链接。"""
        shell = ("baidu.com/link?url=" in url
                 or "baidu.com/link?" in url
                 or ("sogou.com" in url and "/link?" in url))
        if not shell:
            return url
        try:
            client = self._ensure_client()
            resp = await client.get(url, follow_redirects=True)
        except Exception:  # noqa: BLE001
            return url
        final = str(resp.url)
        # 撞上人机校验页就别换，保留原壳链接（直接点开反而能用）
        if "wappass.baidu.com" in final or "captcha" in final:
            return url
        return final if final.startswith("http") else url

    async def _search_sogou(self, query: str, want: int) -> list[Hit]:
        client = self._ensure_client()
        resp = await client.get(f"https://www.sogou.com/web?query={quote(query)}")
        if resp.status_code != 200:
            return []
        out: list[Hit] = []
        for block in _SOGOU_TITLE_RE.findall(resp.text):
            link = _SOGOU_LINK_RE.search(block)
            if not link:
                continue
            url = _html.unescape(link.group(1))
            if url.startswith("/"):
                url = "https://www.sogou.com" + url
            if not url.startswith("http"):
                continue
            title = _plain(link.group(2))
            if len(title) < 3:
                continue
            out.append(Hit(title=title, url=url, snippet=""))
            if len(out) >= want:
                break
        for hit in out:
            hit.url = await self._unwrap(hit.url)
        return out

    # ---------- 抓网页 ----------

    async def fetch(self, url: str, max_chars: int | None = None) -> dict:
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        cache_key = f"fetch:{url}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        limit = int(max_chars or self.cfg.max_page_chars)
        client = self._ensure_client()
        try:
            resp = await client.get(url)
        except Exception as exc:  # noqa: BLE001
            self.counters["fetch_failed"] += 1
            self.last_error = f"打不开：{exc}"
            return {"ok": False, "url": url, "error": str(exc), "title": "", "text": ""}

        if resp.status_code != 200:
            self.counters["fetch_failed"] += 1
            self.last_error = f"HTTP {resp.status_code}"
            return {
                "ok": False,
                "url": str(resp.url),
                "error": f"HTTP {resp.status_code}",
                "title": "",
                "text": "",
            }

        markup = resp.text
        title_match = _TITLE_RE.search(markup)
        title = _plain(title_match.group(1)) if title_match else ""
        text = _plain(markup)
        truncated = len(text) > limit
        if truncated:
            text = text[:limit] + "…"

        result = {
            "ok": True,
            "url": str(resp.url),
            "title": title,
            "text": text,
            "truncated": truncated,
            "error": "",
        }
        self.counters["fetch"] += 1
        self._put_cached(cache_key, result)
        return result

    # ---------- 组装给模型看的文本 ----------

    def render_hits(self, hits: list[Hit], query: str) -> str:
        if not hits:
            return f"（搜「{query}」什么都没搜到）"
        lines = [f"搜「{query}」的结果：" if query else "搜到的结果："]
        for idx, hit in enumerate(hits, 1):
            lines.append(hit.render(idx))
        return "\n".join(lines)

    def render_page(self, page: dict) -> str:
        if not page.get("ok"):
            return f"（这个网页没打开：{page.get('error') or '未知原因'}）"
        head = page.get("title") or page.get("url")
        return f"网页「{head}」的正文（{page['url']}）：\n{page.get('text') or '（正文是空的）'}"

    def status_text(self) -> str:
        if not self.enabled:
            return "联网：关闭"
        c = self.counters
        tail = f"，最近出错：{self.last_error}" if self.last_error else ""
        return (
            f"联网：开启 —— 搜索 {c['search']} 次（失败 {c['search_failed']}）"
            f"／看网页 {c['fetch']} 次（失败 {c['fetch_failed']}）／命中缓存 {c['cache_hits']}{tail}"
        )

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "counters": dict(self.counters),
            "last_error": self.last_error,
            "cache": len(self._cache),
        }


def looks_like_url(text: str) -> bool:
    """粗判一段文字里是不是个网址。"""
    candidate = (text or "").strip()
    if not candidate or " " in candidate:
        return False
    if candidate.startswith(("http://", "https://")):
        return True
    # 中文一律不算网址：urlparse 碰到 CJK 会同 NFKC 归一化后带非法字符而抛 ValueError
    if _CJK_RUN_RE.search(candidate):
        return False
    try:
        host = urlparse(f"//{candidate}").hostname or ""
    except ValueError:
        log.debug("网址判定失败：%r", candidate[:60])
        return False
    return "." in host and not host.endswith(".")


def extract_url(text: str) -> str:
    """从聊天文字里抠出第一个网址。"""
    match = re.search(r"https?://[^\s，。、）)】\]]+", text or "")
    return unquote(match.group(0)) if match else ""
