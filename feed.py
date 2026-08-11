#!/usr/bin/env python3
"""
Reuters -> RSS (方案B: 规则化抓取 + 三级降级)
个人订阅用途：仅保留 标题 / 链接 / 摘要 / 时间，不转载全文。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, quote_plus

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from feedgen.feed import FeedGenerator

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "state" / "seen.json"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": UA,
    "Accept": "application/rss+xml,application/xml,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})


# ----------------------------- 基础工具 -----------------------------

def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def load_cfg() -> dict:
    return yaml.safe_load((ROOT / "config.yml").read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("WARN: seen.json 损坏，重置")
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # 只保留最近 90 天，避免文件无限增长
    cutoff = time.time() - 90 * 86400
    pruned = {k: v for k, v in state.items() if v.get("first_seen", 0) > cutoff}
    STATE_PATH.write_text(
        json.dumps(pruned, ensure_ascii=False, indent=1, sort_keys=True),
        encoding="utf-8",
    )


def guid(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()


def get(url: str, cfg: dict, **kw) -> requests.Response | None:
    """带重试的 GET，失败返回 None 而不抛异常。"""
    for attempt in range(cfg["site"]["retry"] + 1):
        try:
            r = SESSION.get(url, timeout=cfg["site"]["request_timeout"], **kw)
            if r.status_code == 200 and r.content:
                return r
            log(f"  HTTP {r.status_code} <- {url[:90]}")
        except requests.RequestException as e:
            log(f"  {type(e).__name__} <- {url[:90]}")
        if attempt < cfg["site"]["retry"]:
            time.sleep(1.5 * (attempt + 1) + random.random())
    return None


def clean(text: str | None, limit: int = 500) -> str:
    if not text:
        return ""
    t = re.sub(r"\s+", " ", BeautifulSoup(text, "html.parser").get_text(" ")).strip()
    return t[:limit]


def canonical(url: str) -> str:
    """去掉追踪参数，保证 GUID 稳定。"""
    url = url.split("?")[0].split("#")[0]
    return url.rstrip("/") if url.count("/") > 3 else url


def is_reuters_article(url: str) -> bool:
    return bool(re.match(r"^https?://(www\.)?reuters\.com/[^/]+/.+", url))


# --------------------- Source 1: Reuters ARC feeds ---------------------

ARC_TEMPLATES = [
    "https://www.reuters.com/arc/outboundfeeds/rss/category/{cat}/?outputType=xml",
    "https://www.reuters.com/arc/outboundfeeds/v3/mobile/section/{cat}/?outputType=json&size=40",
    "https://www.reuters.com/arc/outboundfeeds/news-sitemap-index/?outputType=xml",
]


def from_arc(section: dict, cfg: dict) -> list[dict]:
    cat = section.get("arc_category")
    if not cat:
        return []

    # 1a. 标准 RSS 端点
    r = get(ARC_TEMPLATES[0].format(cat=cat), cfg)
    if r:
        items = parse_rss_bytes(r.content)
        if items:
            log(f"  ✓ ARC rss: {len(items)} 条")
            return items

    # 1b. 移动端 JSON 端点
    r = get(ARC_TEMPLATES[1].format(cat=cat), cfg)
    if r:
        try:
            data = r.json()
            items = []
            for a in (data.get("result", {}).get("articles") or []):
                url = canonical(urljoin("https://www.reuters.com", a.get("canonical_url", "")))
                if not is_reuters_article(url):
                    continue
                items.append({
                    "title": clean(a.get("title"), 300),
                    "url": url,
                    "summary": clean(a.get("description")),
                    "published": a.get("published_time") or a.get("updated_time"),
                })
            if items:
                log(f"  ✓ ARC json: {len(items)} 条")
                return items
        except (ValueError, AttributeError):
            pass

    return []


def parse_rss_bytes(raw: bytes) -> list[dict]:
    d = feedparser.parse(raw)
    items = []
    for e in d.entries:
        url = canonical(e.get("link", ""))
        if not is_reuters_article(url):
            continue
        items.append({
            "title": clean(e.get("title"), 300),
            "url": url,
            "summary": clean(e.get("summary") or e.get("description")),
            "published": e.get("published") or e.get("updated"),
        })
    return [i for i in items if i["title"] and i["url"]]


# ------------------ Source 2: Google News RSS (公开合法) ------------------

GNEWS = ("https://news.google.com/rss/search?q={q}+when:2d"
         "&hl=en-US&gl=US&ceid=US:en")


def unwrap_google_link(url: str, title_hint: str = "") -> str:
    """Google News 链接是跳转包装，尝试还原真实 URL。"""
    m = re.search(r"[?&]url=(https?[^&]+)", url)
    if m:
        return canonical(requests.utils.unquote(m.group(1)))
    return canonical(url)


def from_google_news(section: dict, cfg: dict) -> list[dict]:
    q = section.get("google_query")
    if not q:
        return []
    r = get(GNEWS.format(q=quote_plus(q)), cfg)
    if not r:
        return []

    d = feedparser.parse(r.content)
    items = []
    for e in d.entries:
        title = clean(e.get("title"), 300)
        # Google 会在标题尾部拼 " - Reuters"
        title = re.sub(r"\s+-\s+Reuters\s*$", "", title)
        url = unwrap_google_link(e.get("link", ""))
        items.append({
            "title": title,
            "url": url,
            "summary": clean(e.get("summary")),
            "published": e.get("published"),
        })
    items = [i for i in items if i["title"] and i["url"]]
    if items:
        log(f"  ✓ google news: {len(items)} 条")
    return items


# ------------------ Source 3: HTML 选择器（最后手段） ------------------

def from_html(section: dict, cfg: dict) -> list[dict]:
    url = section.get("html_url")
    if not url:
        return []
    r = get(url, cfg, headers={"Referer": "https://www.reuters.com/"})
    if not r:
        log("  × HTML 抓取被拒（Actions 环境属预期行为）")
        return []

    sel = cfg["html_selectors"]
    soup = BeautifulSoup(r.text, "lxml")
    scope = soup.select_one("main") or soup

    items, seen = [], set()
    for node in scope.select(sel["item"]):
        a = node.select_one(sel["link"]) or node.find("a", href=True)
        if not a:
            continue
        link = canonical(urljoin("https://www.reuters.com", a["href"]))
        if link in seen or not is_reuters_article(link):
            continue
        seen.add(link)

        t_el = node.select_one(sel["title"])
        title = clean(t_el.get_text() if t_el else a.get_text(), 300)
        s_el = node.select_one(sel["summary"])
        d_el = node.select_one(sel["date"])
        if not title:
            continue
        items.append({
            "title": title,
            "url": link,
            "summary": clean(s_el.get_text() if s_el else ""),
            "published": (d_el.get("datetime") if d_el else None),
        })
    if items:
        log(f"  ✓ html: {len(items)} 条")
    return items


# ----------------------------- Feed 组装 -----------------------------

def parse_date(raw) -> datetime | None:
    if not raw:
        return None
    try:
        st = feedparser._parse_date(raw)
        if st:
            return datetime(*st[:6], tzinfo=timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(str(raw), fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def build_section(section: dict, cfg: dict, state: dict) -> bool:
    slug = section["slug"]
    log(f"→ {slug}")

    items: list[dict] = []
    for fn in (from_arc, from_google_news, from_html):
        items = fn(section, cfg)
        if items:
            break
        time.sleep(1)

    out_dir = ROOT / cfg["site"]["output_dir"]
    out_path = out_dir / f"{slug}.xml"

    if not items:
        log(f"  ! 全部来源失败，保留旧文件 {out_path.name}")
        return out_path.exists()

    # pubDate 稳定化：以首次见到的时间兜底，避免阅读器重复置顶
    now = time.time()
    enriched = []
    for it in items:
        key = guid(it["url"])
        rec = state.setdefault(key, {"first_seen": now, "url": it["url"]})
        dt = parse_date(it.get("published")) or datetime.fromtimestamp(
            rec["first_seen"], tz=timezone.utc)
        enriched.append({**it, "dt": dt, "guid": key})

    enriched.sort(key=lambda x: x["dt"], reverse=True)
    enriched = enriched[: cfg["site"]["max_items"]]

    base = cfg["site"]["base_url"].rstrip("/")
    fg = FeedGenerator()
    fg.id(f"{base}/{slug}.xml")
    fg.title(section["title"])
    fg.link(href=section.get("html_url", "https://www.reuters.com/"), rel="alternate")
    fg.link(href=f"{base}/{slug}.xml", rel="self")
    fg.description(f"Unofficial personal RSS mirror of Reuters {slug} headlines. "
                   f"Headlines & links only — full text at reuters.com.")
    fg.language("en")
    fg.generator("reuters-rss / feedgen")
    fg.lastBuildDate(datetime.now(timezone.utc))

    for it in enriched:
        fe = fg.add_entry()
        fe.id(it["guid"])
        fe.guid(it["guid"], permalink=False)
        fe.title(it["title"])
        fe.link(href=it["url"])
        fe.pubDate(it["dt"])
        if it["summary"]:
            fe.description(it["summary"])

    out_dir.mkdir(parents=True, exist_ok=True)
    fg.rss_file(str(out_path), pretty=True)
    log(f"  ✓ 写入 {out_path.name}（{len(enriched)} 条）")
    return True


def write_index(cfg: dict, results: dict) -> None:
    base = cfg["site"]["base_url"].rstrip("/")
    rows = "\n".join(
        f'<tr><td>{s["title"]}</td>'
        f'<td><code>{base}/{s["slug"]}.xml</code></td>'
        f'<td>{"✅" if results.get(s["slug"]) else "⚠️"}</td></tr>'
        for s in cfg["sections"]
    )
    html = f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<title>Reuters RSS</title>
<style>body{{font-family:system-ui,sans-serif;max-width:820px;margin:3rem auto;padding:0 1rem;line-height:1.6}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.5rem;text-align:left;font-size:14px}}
code{{background:#f4f4f5;padding:2px 5px;border-radius:4px;font-size:12px}}</style></head>
<body>
<h1>Reuters RSS</h1>
<p>个人订阅用途，仅含标题、链接与摘要，全文请访问 reuters.com。</p>
<table><tr><th>板块</th><th>订阅地址</th><th>状态</th></tr>{rows}</table>
<p style="color:#888;font-size:13px">最后更新：{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC</p>
</body></html>"""
    (ROOT / cfg["site"]["output_dir"] / "index.html").write_text(html, encoding="utf-8")


def main() -> int:
    cfg = load_cfg()
    state = load_state()
    results = {}

    for section in cfg["sections"]:
        try:
            results[section["slug"]] = build_section(section, cfg, state)
        except Exception as e:
            log(f"  ✗ {section['slug']} 异常: {type(e).__name__}: {e}")
            results[section["slug"]] = False
        time.sleep(2 + random.random() * 2)   # 温和限速

    save_state(state)
    write_index(cfg, results)

    ok = sum(1 for v in results.values() if v)
    log(f"完成：{ok}/{len(results)} 个 feed 可用")
    # 全挂才失败，部分成功不打断定时任务
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
