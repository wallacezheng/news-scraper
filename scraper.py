"""
網站文章爬取 → Notion 同步腳本
目標：wattbrother.com/c/news, hero-mi.com, techbang.com/categories/76
"""

import os
import re
import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from notion_client import Client
from dotenv import load_dotenv

load_dotenv()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

notion = Client(auth=NOTION_TOKEN)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}


# ── 各網站爬取 ────────────────────────────────────────────────

def fetch_heromi():
    """hero-mi.com — 使用 RSS feed"""
    feed = feedparser.parse("https://hero-mi.com/feed/")
    articles = []
    for entry in feed.entries:
        published = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()
        articles.append({
            "source": "hero-mi.com",
            "title": entry.title,
            "url": entry.link,
            "published": published,
        })
    return articles


def fetch_wattbrother():
    """wattbrother.com/c/news — BeautifulSoup"""
    resp = requests.get("https://wattbrother.com/c/news", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    articles = []
    # 文章連結格式：//wattbrother.com/數字
    for a in soup.find_all("a", href=re.compile(r"^//wattbrother\.com/\d+")):
        title = a.get_text(strip=True)
        if not title:
            continue
        url = "https:" + a["href"]

        # 日期在同一個父層級的文字節點，格式 YYYY-MM-DD
        parent = a.parent
        date_text = None
        for text in parent.stripped_strings:
            if re.match(r"\d{4}-\d{2}-\d{2}", text):
                date_text = text
                break

        published = None
        if date_text:
            try:
                published = datetime.strptime(date_text, "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                ).isoformat()
            except ValueError:
                pass

        articles.append({
            "source": "wattbrother.com",
            "title": title,
            "url": url,
            "published": published,
        })
    return articles


def fetch_techbang():
    """techbang.com/categories/76 — BeautifulSoup"""
    resp = requests.get(
        "https://www.techbang.com/categories/76", headers=HEADERS, timeout=15
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    articles = []
    for a in soup.find_all("a", href=re.compile(r"/posts/\d+")):
        title = a.get_text(strip=True)
        if not title or len(title) < 5:
            continue
        url = "https://www.techbang.com" + a["href"] if a["href"].startswith("/") else a["href"]

        # 找鄰近的 timestamp
        container = a.find_parent(["article", "li", "div"])
        published = None
        if container:
            ts = container.find(class_=re.compile(r"timestamp|date|time"))
            if ts:
                raw = ts.get_text(strip=True)
                # 格式：2026年6月10日 07:30
                m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日\s*(\d{2}):(\d{2})", raw)
                if m:
                    try:
                        published = datetime(
                            int(m.group(1)), int(m.group(2)), int(m.group(3)),
                            int(m.group(4)), int(m.group(5)),
                            tzinfo=timezone.utc
                        ).isoformat()
                    except ValueError:
                        pass

        articles.append({
            "source": "techbang.com",
            "title": title,
            "url": url,
            "published": published,
        })

    # 去除重複 URL
    seen = set()
    unique = []
    for a in articles:
        if a["url"] not in seen:
            seen.add(a["url"])
            unique.append(a)
    return unique


# ── Notion 操作 ───────────────────────────────────────────────

def get_existing_urls() -> set[str]:
    """取得 Notion Database 中已存在的所有文章 URL"""
    existing = set()
    cursor = None
    while True:
        kwargs = {"database_id": NOTION_DATABASE_ID, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = notion.databases.query(**kwargs)
        for page in resp["results"]:
            url_prop = page["properties"].get("URL", {})
            url = url_prop.get("url") or ""
            if url:
                existing.add(url)
        if not resp.get("has_more"):
            break
        cursor = resp["next_cursor"]
    return existing


def add_article(article: dict):
    """新增一篇文章到 Notion Database"""
    props = {
        "標題": {"title": [{"text": {"content": article["title"]}}]},
        "URL": {"url": article["url"]},
        "來源": {"select": {"name": article["source"]}},
    }
    if article.get("published"):
        props["發布時間"] = {"date": {"start": article["published"]}}

    notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties=props,
    )


# ── 主流程 ────────────────────────────────────────────────────

def main():
    print("開始爬取文章...")

    all_articles = []
    for fetch_fn in [fetch_heromi, fetch_techbang]:
        try:
            articles = fetch_fn()
            print(f"  {fetch_fn.__name__}: 取得 {len(articles)} 篇")
            all_articles.extend(articles)
        except Exception as e:
            print(f"  {fetch_fn.__name__} 失敗: {e}")

    print(f"\n共取得 {len(all_articles)} 篇，正在比對 Notion...")
    existing_urls = get_existing_urls()
    print(f"Notion 中已有 {len(existing_urls)} 篇")

    new_articles = [a for a in all_articles if a["url"] not in existing_urls]
    print(f"新增 {len(new_articles)} 篇\n")

    for article in new_articles:
        try:
            add_article(article)
            print(f"  ✓ {article['source']} | {article['title'][:40]}")
        except Exception as e:
            print(f"  ✗ 新增失敗: {article['title'][:40]} — {e}")

    print("\n完成！")


if __name__ == "__main__":
    main()
