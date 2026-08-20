# -*- coding: utf-8 -*-
"""内置网络搜索工具(白名单安全)。

国内网络下 DuckDuckGo 经常连不通,故默认走 Bing(cn.bing.com → www.bing.com),
失败再兜底 DuckDuckGo HTML 版;均无需 API key。
"""
import re
import urllib.parse

import requests

import logger

TOOL = {
    "name": "web_search",
    "description": "联网搜索最新信息,返回前 5 条结果的标题/摘要/链接。适合天气、新闻、实时事件等问题。",
    "safe": True,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
        },
        "required": ["query"],
    },
}

_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0.0.0 Safari/537.36")}


def _clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = s.replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", s).strip()


def _parse_bing(html):
    """解析 Bing 结果页的 b_algo 块 → [(title, url, snippet), ...]。"""
    items = []
    blocks = re.findall(r'<li class="b_algo".*?</li>', html, re.S)
    for b in blocks:
        m = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            continue
        url = urllib.parse.unquote(m.group(1))
        title = _clean(m.group(2))
        sm = re.search(r'<p[^>]*>(.*?)</p>', b, re.S)
        snippet = _clean(sm.group(1)) if sm else ""
        if title and url:
            items.append((title, url, snippet))
    return items


def _parse_ddg(html):
    """解析 DuckDuckGo HTML 版结果。"""
    items = []
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.S)
    hrefs = re.findall(r'class="result__a"[^>]*href="([^"]+)"', html)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
    for i in range(min(len(hrefs), len(titles))):
        url = hrefs[i]
        m = re.search(r"uddg=([^&]+)", url)
        if m:
            url = urllib.parse.unquote(m.group(1))
        items.append((_clean(titles[i]), url,
                      _clean(snippets[i]) if i < len(snippets) else ""))
    return items


def _format(items):
    lines = []
    for i, (title, url, snippet) in enumerate(items[:5]):
        lines.append(f"{i + 1}. {title}\n   {snippet}\n   {url}")
    return "\n".join(lines) if lines else "[搜索无结果]"


def run(query):
    errors = []
    # 1) Bing(cn → www)
    for host in ("https://cn.bing.com/search", "https://www.bing.com/search"):
        try:
            resp = requests.get(host, params={"q": query, "count": "10"},
                                headers=_UA, timeout=10)
            resp.raise_for_status()
            items = _parse_bing(resp.text)
            if items:
                return _format(items)
            errors.append(f"{host} 未解析到结果")
        except Exception as e:
            errors.append(f"{host}: {type(e).__name__}")
    # 2) DuckDuckGo 兜底(海外网络可用)
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query}, headers=_UA, timeout=10)
        resp.raise_for_status()
        items = _parse_ddg(resp.text)
        if items:
            return _format(items)
        errors.append("duckduckgo 未解析到结果")
    except Exception as e:
        errors.append(f"duckduckgo: {type(e).__name__}")
    # 全部失败:写日志(便于事后排查网络/反爬问题),再返回错误文本
    logger.log_error("联网搜索失败(所有搜索源均不可用)",
                     query=str(query)[:100], detail="; ".join(errors))
    return f"[搜索失败] 所有搜索源均不可用({'; '.join(errors)}),请稍后重试"
