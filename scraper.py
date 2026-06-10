"""
網站文章爬取 → Notion 同步腳本
目標：hero-mi.com, techbang.com/categories/76
"""

import os
import re
import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )
}

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
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

        container = a.find_parent(["article", "li", "div"])
        published = None
        if container:
            ts = container.find(class_=re.compile(r"timestamp|date|time|author-info|author"))
            if ts:
                raw = ts.get_text(strip=True)
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

    seen = set()
    unique = []
    for a in articles:
        if a["url"] not in seen:
            seen.add(a["url"])
            unique.append(a)
    return unique


# ── Notion 操作（直接呼叫 HTTP API）─────────────────────────────

def get_all_pages() -> list[dict]:
    """取得 Notion Database 中所有頁面（含 page_id 和 URL）"""
    pages = []
    payload = {"page_size": 100}

    while True:
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query",
            headers=NOTION_HEADERS,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        pages.extend(data.get("results", []))

        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    return pages


def delete_old_articles(pages: list[dict], cutoff: datetime):
    """刪除 14 天前的文章"""
    deleted = 0
    for page in pages:
        date_prop = page["properties"].get("發布時間", {}).get("date")
        if not date_prop:
            continue
        try:
            pub_date = datetime.fromisoformat(date_prop["start"].replace("Z", "+00:00"))
            if pub_date.tzinfo is None:
                pub_date = pub_date.replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue

        if pub_date < cutoff:
            requests.patch(
                f"https://api.notion.com/v1/pages/{page['id']}",
                headers=NOTION_HEADERS,
                json={"archived": True},
                timeout=15,
            )
            deleted += 1
    return deleted


def add_article(article: dict):
    """新增一篇文章到 Notion Database"""
    props = {
        "標題": {"title": [{"text": {"content": article["title"]}}]},
        "URL": {"url": article["url"]},
        "來源": {"select": {"name": article["source"]}},
    }
    if article.get("published"):
        props["發布時間"] = {"date": {"start": article["published"]}}

    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json={"parent": {"database_id": NOTION_DATABASE_ID}, "properties": props},
        timeout=15,
    )
    resp.raise_for_status()


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
    all_pages = get_all_pages()
    existing_urls = set()
    for page in all_pages:
        url = page["properties"].get("URL", {}).get("url") or ""
        if url:
            existing_urls.add(url)
    print(f"Notion 中已有 {len(existing_urls)} 篇")

    # 刪除 14 天前的舊資料
    cutoff = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    from datetime import timedelta
    cutoff = cutoff - timedelta(days=14)
    deleted = delete_old_articles(all_pages, cutoff)
    if deleted:
        print(f"已刪除 {deleted} 篇 14 天前的舊文章")

    new_articles = [a for a in all_articles if a["url"] not in existing_urls]
    print(f"新增 {len(new_articles)} 篇\n")

    for article in new_articles:
        try:
            add_article(article)
            print(f"  ✓ {article['source']} | {article['title'][:40]}")
        except Exception as e:
            print(f"  ✗ 新增失敗: {article['title'][:40]} — {e}")

    # 重新取得最新資料產生網頁
    generate_html(get_all_pages())
    print("\n完成！")


def generate_html(pages: list[dict]):
    """從 Notion 資料產生靜態 index.html"""
    from datetime import timedelta

    # 整理並排序
    articles = []
    for page in pages:
        title_prop = page["properties"].get("標題", {}).get("title", [])
        title = title_prop[0]["text"]["content"] if title_prop else "(無標題)"
        url = page["properties"].get("URL", {}).get("url") or "#"
        source = page["properties"].get("來源", {}).get("select") or {}
        source_name = source.get("name", "")
        date_prop = page["properties"].get("發布時間", {}).get("date")
        pub_date = ""
        if date_prop and date_prop.get("start"):
            pub_date = date_prop["start"][:10]
        else:
            print(f"  [DEBUG] no date for: {title[:30]} | date_prop={date_prop}")
        articles.append({"title": title, "url": url, "source": source_name, "date": pub_date})

    articles.sort(key=lambda x: x["date"], reverse=True)

    updated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows = ""
    for a in articles:
        source_class = a["source"].replace(".", "-")
        rows += f"""
        <tr>
          <td class="date">{a["date"]}</td>
          <td><span class="tag {source_class}">{a["source"]}</span></td>
          <td><a href="{a["url"]}" target="_blank">{a["title"]}</a></td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>每日新聞彙整</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #f5f5f5; color: #333; padding: 24px; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
  .updated {{ color: #888; font-size: 0.85rem; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
           border-radius: 8px; overflow: hidden; box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
  th {{ background: #f0f0f0; padding: 10px 14px; text-align: left;
        font-size: 0.85rem; color: #555; }}
  td {{ padding: 10px 14px; border-top: 1px solid #eee; font-size: 0.9rem; vertical-align: middle; }}
  td.date {{ color: #888; white-space: nowrap; width: 100px; }}
  a {{ color: #1a73e8; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px;
          font-size: 0.78rem; font-weight: 600; white-space: nowrap; }}
  .hero-mi-com {{ background: #e8f4fd; color: #1a73e8; }}
  .techbang-com {{ background: #fde8e8; color: #d93025; }}
</style>
</head>
<body>
<h1>每日新聞彙整</h1>
<p class="updated">最後更新：{updated}（每天早上 8:00 自動更新）</p>
<table>
  <thead><tr><th>日期</th><th>來源</th><th>標題</th></tr></thead>
  <tbody>{rows}
  </tbody>
</table>
</body>
</html>"""

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("已產生 index.html")


if __name__ == "__main__":
    main()
