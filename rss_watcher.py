#!/usr/bin/env python3
"""
RSS Watcher - Inoreader OPML巡回 → Slack DM通知
- 週1回（月曜朝）: まとめてDM
- 緊急度高: 即時DM
"""

import xml.etree.ElementTree as ET
import feedparser
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# === 設定 ===
_SCRIPT_DIR = Path(__file__).parent
OPML_PATH = str(_SCRIPT_DIR / "inoreader_feeds.xml")
STATE_FILE = str(_SCRIPT_DIR / "seen_articles.json")
SLACK_USER_ID = "U7VTCQ0SF"

# 優先カテゴリ（フィードを巡回する対象）
TARGET_CATEGORIES = ["子供・業務アラート", "児発/放デイ/障害者GH", "チェック", "ニュース", "経済新聞"]

# スコアリングキーワード
SCORE_KEYWORDS = {
    # 幼保・業界（+3点）
    "幼稚園": 3, "保育園": 3, "こども園": 3, "保育所": 3, "認定こども園": 3,
    "幼保": 3, "園長": 2, "理事長": 2, "フォトサービス": 3, "写真": 1,
    "補助金": 3, "制度改正": 3, "保育無償化": 3, "少子化": 2,
    # 療育・福祉（+3点）
    "児童発達支援": 3, "放課後等デイ": 3, "療育": 3, "発達障害": 2,
    "ASD": 2, "ADHD": 2, "報酬改定": 3, "加算": 2, "処遇改善": 2,
    "障害福祉": 2, "ヒトツナ": 3, "LITALICO": 2,
    # 事業・戦略（+2点）
    "採用": 2, "DX": 2, "テクノロジー": 1, "スタートアップ": 1,
    "差別化": 2, "営業": 1, "提案": 1,
    # 競合（+3点）
    "コドモン": 3, "CoDMON": 3, "おうちえん": 3, "バスキャッチ": 3,
    "スマートエデュケーション": 3, "メモリッジ": 3, "フォトクリ": 3,
}

# 緊急キーワード（これがあれば即時通知）
URGENT_KEYWORDS = [
    "締め切り", "期限", "至急", "速報", "緊急",
    "制度改正", "法改正", "廃止", "新制度",
    "倒産", "閉鎖", "不正", "事故",
    "報酬改定", "補助金", "助成金",
]

JST = timezone(timedelta(hours=9))


def load_seen_articles():
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen_articles(seen):
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen), f)


def parse_opml_feeds():
    tree = ET.parse(OPML_PATH)
    root = tree.getroot()
    feeds = []

    def parse_outline(outline, category=""):
        cat = outline.get("text", "") if outline.get("type") != "rss" else category
        if outline.get("type") == "rss":
            if not TARGET_CATEGORIES or category in TARGET_CATEGORIES:
                feeds.append({
                    "category": category,
                    "title": outline.get("title", ""),
                    "url": outline.get("xmlUrl", ""),
                })
        for child in outline:
            parse_outline(child, cat)

    for outline in root.find("body"):
        parse_outline(outline)
    return feeds


def score_article(title, summary=""):
    text = (title + " " + summary).lower()
    score = 0
    matched = []
    for kw, pts in SCORE_KEYWORDS.items():
        if kw.lower() in text:
            score += pts
            matched.append(kw)
    return score, matched


def is_urgent(title, summary=""):
    text = title + " " + summary
    return any(kw in text for kw in URGENT_KEYWORDS)


def fetch_articles(feeds, seen_ids, hours_back=168):
    """直近N時間の未読記事を取得"""
    cutoff = datetime.now(JST) - timedelta(hours=hours_back)
    articles = []

    for feed_info in feeds:
        try:
            feed = feedparser.parse(feed_info["url"])
            for entry in feed.entries[:20]:
                article_id = entry.get("id") or entry.get("link", "")
                if article_id in seen_ids:
                    continue

                # 日付チェック
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if published:
                    pub_dt = datetime(*published[:6], tzinfo=timezone.utc).astimezone(JST)
                    if pub_dt < cutoff:
                        continue

                title = entry.get("title", "")
                summary = entry.get("summary", "")[:200]
                link = entry.get("link", "")

                score, matched = score_article(title, summary)
                if score < 3:
                    continue

                articles.append({
                    "id": article_id,
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "category": feed_info["category"],
                    "feed": feed_info["title"],
                    "score": score,
                    "matched": matched,
                    "urgent": is_urgent(title, summary),
                    "published": pub_dt.strftime("%m/%d %H:%M") if published else "",
                })
        except Exception as e:
            print(f"Error fetching {feed_info['url']}: {e}", file=sys.stderr)

    return sorted(articles, key=lambda x: x["score"], reverse=True)


def format_article_block(a):
    """記事1件のフォーマット: タイトルリンク + 概要3行"""
    summary = a.get("summary", "").strip()
    # 概要を最大3行・120文字に収める
    if summary:
        lines = summary.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        lines = [l.strip() for l in lines if l.strip()]
        summary_text = " / ".join(lines[:3])
        if len(summary_text) > 120:
            summary_text = summary_text[:117] + "..."
    else:
        summary_text = ""

    block = f"*<{a['link']}|{a['title']}>*"
    if summary_text:
        block += f"\n　{summary_text}"
    block += f"\n　`{a['category']}` score:{a['score']}"
    return block


def format_weekly_digest(articles):
    if not articles:
        return "今週は注目記事がありませんでした。"

    lines = [f"📰 *今週の業界情報まとめ* （{datetime.now(JST).strftime('%Y/%m/%d')}）\n"]
    lines.append(f"スコア3以上の記事: {len(articles)}件\n")

    urgent = [a for a in articles if a["urgent"]]
    if urgent:
        lines.append("🚨 *緊急・重要*")
        for a in urgent[:5]:
            lines.append(format_article_block(a))
            lines.append("")
        lines.append("")

    high = [a for a in articles if not a["urgent"] and a["score"] >= 5]
    if high:
        lines.append("⭐ *注目記事*")
        for a in high[:8]:
            lines.append(format_article_block(a))
            lines.append("")
        lines.append("")

    mid = [a for a in articles if not a["urgent"] and 3 <= a["score"] < 5]
    if mid:
        lines.append("📌 *その他気になる記事*")
        for a in mid[:5]:
            lines.append(format_article_block(a))
            lines.append("")

    return "\n".join(lines)


def format_urgent_alert(article):
    summary = article.get("summary", "").strip()
    if summary and len(summary) > 120:
        summary = summary[:117] + "..."
    body = (
        f"🚨 *緊急アラート*\n"
        f"*<{article['link']}|{article['title']}>*\n"
    )
    if summary:
        body += f"　{summary}\n"
    body += f"カテゴリ: {article['category']} | スコア: {article['score']}\n"
    body += f"キーワード: {', '.join(article['matched'])}"
    return body


def post_to_slack(message):
    """Claude Code MCP経由でSlack DM送信（外部呼び出し用にメッセージをprint）"""
    # このスクリプトはメッセージを標準出力に出力し、
    # Claude Codeがそれを読み取ってSlack APIで送信する
    print(f"SLACK_DM:{SLACK_USER_ID}:{message}")


def main(mode="weekly"):
    seen = load_seen_articles()
    feeds = parse_opml_feeds()
    print(f"対象フィード: {len(feeds)}件", file=sys.stderr)

    hours = 168 if mode == "weekly" else 1  # weekly=7日分、urgent=1時間分
    articles = fetch_articles(feeds, seen, hours_back=hours)
    print(f"スコア3以上の記事: {len(articles)}件", file=sys.stderr)

    new_ids = {a["id"] for a in articles}

    if mode == "weekly":
        message = format_weekly_digest(articles)
        post_to_slack(message)
    elif mode == "urgent":
        urgent = [a for a in articles if a["urgent"]]
        for a in urgent:
            post_to_slack(format_urgent_alert(a))
        if not urgent:
            print("緊急記事なし", file=sys.stderr)

    # 既読として保存
    seen.update(new_ids)
    save_seen_articles(seen)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "weekly"
    main(mode)
