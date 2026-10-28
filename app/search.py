"""联网搜索。新增搜索服务：写一个类实现 search()，再在 SEARCH_TYPES 里注册。"""
from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.parse
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

import httpx

from .config import ModelRef, SearchConfig
from .providers import Provider, ProviderError

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str

    def public(self) -> Dict[str, str]:
        return asdict(self)


class SearchError(Exception):
    pass


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


class Searcher:
    label = ""

    def __init__(self, cfg: SearchConfig):
        self.cfg = cfg
        self.api_key = cfg.api_key or (os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else "")
        self._cache: Dict[str, Tuple[float, List[SearchResult]]] = {}

    async def search(self, query: str) -> List[SearchResult]:
        hit = self._cache.get(query)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        try:
            async with httpx.AsyncClient(timeout=15, headers=UA, follow_redirects=True) as client:
                results = (await self._search(client, query))[: self.cfg.max_results]
        except httpx.HTTPError as e:
            raise SearchError(f"{self.label} 搜索失败：{e}") from e
        self._cache[query] = (time.time(), results)
        return results

    async def _search(self, client: httpx.AsyncClient, query: str) -> List[SearchResult]:
        raise NotImplementedError


class DuckDuckGoSearcher(Searcher):
    """免费、无需 key，抓取 DuckDuckGo 的 HTML 版结果。频繁请求可能被限流。"""

    label = "DuckDuckGo"

    async def _search(self, client, query):
        r = await client.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "cn-zh"})
        r.raise_for_status()
        out = []
        for block in r.text.split('class="result__a"')[1:]:  # 每块从一条结果的标题链接开始
            a = re.search(r'href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            sn = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
            if not a:
                continue
            url = html.unescape(a.group(1))
            if "uddg=" in url:  # 跳转链接，取出真实地址
                url = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("uddg", [url])[0])
            if "duckduckgo.com/y.js" in url:  # 广告
                continue
            out.append(SearchResult(_text(a.group(2)), url, _text(sn.group(1)) if sn else ""))
        if not out and "anomaly" in r.text:
            raise SearchError("DuckDuckGo 暂时限制了访问，请稍后再试，或改用 tavily / bocha")
        return out


class TavilySearcher(Searcher):
    """https://tavily.com ，为 AI 设计的搜索 API，每月有免费额度。"""

    label = "Tavily"

    async def _search(self, client, query):
        if not self.api_key:
            raise SearchError(f"Tavily 缺少 API key：请在 .env 里设置 {self.cfg.api_key_env or 'api_key'}")
        r = await client.post("https://api.tavily.com/search", json={
            "api_key": self.api_key, "query": query, "max_results": self.cfg.max_results, "search_depth": "basic",
        })
        r.raise_for_status()
        return [SearchResult(x.get("title", ""), x.get("url", ""), (x.get("content") or "")[:400]) for x in r.json().get("results", [])]


class BochaSearcher(Searcher):
    """https://open.bochaai.com ，国内的 AI 搜索 API，中文结果好。"""

    label = "博查"

    async def _search(self, client, query):
        if not self.api_key:
            raise SearchError(f"博查缺少 API key：请在 .env 里设置 {self.cfg.api_key_env or 'api_key'}")
        r = await client.post(
            "https://api.bochaai.com/v1/web-search",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={"query": query, "summary": True, "count": self.cfg.max_results},
        )
        r.raise_for_status()
        pages = ((r.json().get("data") or {}).get("webPages") or {}).get("value") or []
        return [SearchResult(x.get("name", ""), x.get("url", ""), (x.get("summary") or x.get("snippet") or "")[:400]) for x in pages]


SEARCH_TYPES = {"duckduckgo": DuckDuckGoSearcher, "tavily": TavilySearcher, "bocha": BochaSearcher}


def build_searcher(cfg: SearchConfig) -> Optional[Searcher]:
    if cfg.provider in ("", "none", None):
        return None
    if cfg.provider not in SEARCH_TYPES:
        raise ValueError(f"未知的搜索服务: {cfg.provider}")
    return SEARCH_TYPES[cfg.provider](cfg)


async def plan_queries(
    provider: Provider, ref: ModelRef, *, who: str, task: str, transcript: str, today: str, required: bool,
) -> List[str]:
    """让一个便宜的模型根据讨论内容，替发言者想好搜索关键词。"""
    rule = (
        "必须给出 1–2 个关键词。" if required
        else "如果接下来的发言不需要最新信息或事实核查，返回空数组 []。"
    )
    system = (
        f"你在帮群聊成员「{who}」准备联网搜索，今天是 {today}。\n"
        "根据讨论记录和他接下来的发言任务，给出最多 2 个搜索关键词：要具体、能搜到事实或数据，"
        f"涉及近期事件时带上年份。{rule}\n"
        '只输出 JSON 数组，例如 ["关键词1", "关键词2"]，不要输出其他内容。'
    )
    user = f"发言任务：{task}\n\n最近的讨论：\n{transcript}"
    try:
        out = await provider.complete(
            ref.model, [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=ref.max_tokens, extra_body=ref.extra_body,
        )
    except ProviderError:
        return []
    m = re.search(r"\[.*?\]", out, re.S)
    try:
        queries = json.loads(m.group(0)) if m else []
    except ValueError:
        queries = []
    return [str(q).strip() for q in queries if str(q).strip()][:2]


def format_results(results: List[SearchResult], queries: List[str], today: str) -> str:
    lines = [f"【联网搜索结果】（{today} 搜索，关键词：{'；'.join(queries)}）"]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.title}\n{r.snippet}\n来源：{r.url}")
    lines.append("发言时可以引用这些资料，引用处用 [编号] 标注。资料可能片面或有误，要有自己的判断；与讨论无关的就忽略。")
    return "\n\n".join(lines)
