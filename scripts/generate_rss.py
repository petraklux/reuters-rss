import os
import time
import requests
from lxml import etree
from feedgen.feed import FeedGenerator
from openai import OpenAI

ROBOTS_URL = "https://www.reuters.com/robots.txt"
OUTPUT_FILE = "docs/reuters.xml"

HEADERS = {
    "User-Agent": "PersonalRSSReader/1.0"
}

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def get_sitemap_urls():
    response = requests.get(
        ROBOTS_URL,
        headers=HEADERS,
        timeout=30
    )
    response.raise_for_status()

    result = []

    for line in response.text.splitlines():
        if line.lower().startswith("sitemap:"):
            result.append(line.split(":", 1)[1].strip())

    return result


def parse_sitemap(url):
    response = requests.get(
        url,
        headers=HEADERS,
        timeout=30
    )
    response.raise_for_status()

    root = etree.fromstring(response.content)
    root_type = etree.QName(root).localname

    if root_type == "sitemapindex":
        sitemap_urls = root.xpath(
            "//*[local-name()='sitemap']/*[local-name()='loc']/text()"
        )

        articles = []

        for sitemap_url in sitemap_urls:
            try:
                articles.extend(parse_sitemap(sitemap_url))
                time.sleep(1)
            except Exception as error:
                print(f"子 Sitemap 解析失败: {sitemap_url} - {error}")

        return articles

    if root_type == "urlset":
        articles = []

        for node in root.xpath("//*[local-name()='url']"):
            loc = node.xpath("./*[local-name()='loc']/text()")
            lastmod = node.xpath("./*[local-name()='lastmod']/text()")
            news_title = node.xpath(
                ".//*[local-name()='news:title']/text()"
            )
            publication_date = node.xpath(
                ".//*[local-name()='news:publication_date']/text()"
            )

            if not loc:
                continue

            articles.append({
                "url": loc[0].strip(),
                "title": news_title[0].strip()
                    if news_title else "",
                "published": publication_date[0].strip()
                    if publication_date else (
                        lastmod[0].strip() if lastmod else ""
                    )
            })

        return articles

    return []


def summarize_title(title):
    """
    Sitemap 不一定有新闻正文。
    因此这里只根据标题生成简短说明，
    不让模型虚构文章内容。
    """
    if not title:
        return "请点击原文查看详情。"

    if not client:
        return title

    prompt = f"""
请把下面的英文新闻标题翻译成简洁、准确的中文。
只翻译标题，不添加标题中没有的事实。

标题：
{title}
"""

    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=prompt
        )
        return response.output_text.strip()
    except Exception as error:
        print("AI 处理失败:", error)
        return title


def generate_rss(articles):
    # URL 去重
    unique = {}

    for article in articles:
        unique[article["url"]] = article

    # 最多保留 100 条
    articles = list(unique.values())[:100]

    feed = FeedGenerator()
    feed.id("https://YOUR_USERNAME.github.io/YOUR_REPOSITORY/reuters.xml")
    feed.title("Reuters 新闻更新")
    feed.link(
        href="https://www.reuters.com/",
        rel="alternate"
    )
    feed.description("Reuters 公开 Sitemap 新闻链接及中文标题")
    feed.language("zh-CN")

    for article in articles:
        entry = feed.add_entry()

        title = article["title"] or article["url"]
        translated_title = summarize_title(title)

        entry.id(article["url"])
        entry.title(translated_title)
        entry.link(href=article["url"])
        entry.description(
            f"原标题：{title}<br>"
            f"来源：Reuters<br>"
            f"<a href=\"{article['url']}\">阅读原文</a>"
        )

        if article["published"]:
            try:
                entry.pubDate(article["published"])
            except Exception:
                pass

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    feed.rss_file(OUTPUT_FILE)

    print(f"已生成 RSS：{OUTPUT_FILE}")
    print(f"新闻数量：{len(articles)}")


if __name__ == "__main__":
    sitemap_urls = get_sitemap_urls()

    if not sitemap_urls:
        raise RuntimeError("robots.txt 中没有找到 Sitemap 地址")

    articles = []

    for sitemap_url in sitemap_urls:
        try:
            articles.extend(parse_sitemap(sitemap_url))
            time.sleep(1)
        except Exception as error:
            print(f"Sitemap 解析失败: {sitemap_url} - {error}")

    if not articles:
        raise RuntimeError("没有获取到任何新闻")

    generate_rss(articles)
