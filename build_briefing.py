import json
import time
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Any

import feedparser
import requests

BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
COMMON_DRONE_TERMS = [
    "drone", "drones", "uav", "uas", "unmanned", "vtol",
    "reconnaissance", "surveillance", "aerial", "uavs"
]

HEADERS = {
    "User-Agent": "drone-briefing/1.0"
}

@dataclass
class Article:
    company: str
    title: str
    link: str
    published: str
    summary: str
    score: int

def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()

def build_query(cfg: Dict[str, Any]) -> str:
    tokens = []

    for term in cfg.get("aliases", []) + cfg.get("priority_terms", []):
        if " " in term:
            tokens.append(f"\"{term}\"")
        else:
            tokens.append(term)

    for term in COMMON_DRONE_TERMS:
        tokens.append(term)

    # One query per company, broad enough to catch relevant articles,
    # but still anchored by the company aliases and drone vocabulary.
    return "(" + " OR ".join(tokens) + ")"

def passes_local_filter(cfg: Dict[str, Any], title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()

    alias_hit = any(norm(alias) in text for alias in cfg.get("aliases", []))
    priority_hit = any(norm(term) in text for term in cfg.get("priority_terms", []))
    drone_hit = any(term in text for term in COMMON_DRONE_TERMS)

    if not (alias_hit or priority_hit):
        return False
    if not drone_hit:
        return False

    for bad in cfg.get("exclude_terms", []):
        if norm(bad) in text:
            return False

    return True

def score_article(cfg: Dict[str, Any], title: str, summary: str, published: str) -> int:
    text = f"{title} {summary}".lower()
    score = 0

    if any(norm(alias) in norm(title) for alias in cfg.get("aliases", [])):
        score += 4
    if any(norm(term) in norm(title) for term in cfg.get("priority_terms", [])):
        score += 3
    if any(term in text for term in COMMON_DRONE_TERMS):
        score += 2

    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        hours_old = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        if hours_old <= 24:
            score += 3
        elif hours_old <= 72:
            score += 2
        elif hours_old <= 168:
            score += 1
    except Exception:
        pass

    return score

def fetch_rss(session: requests.Session, url: str, retries: int = 4) -> str:
    backoff = 6
    for attempt in range(retries):
        resp = session.get(url, timeout=40)
        text = resp.text or ""

        if resp.status_code == 429 or "please limit requests to one every five seconds" in text.lower():
            time.sleep(backoff)
            backoff *= 2
            continue

        resp.raise_for_status()
        return text

    raise RuntimeError("GDELT rate limit kept triggering after retries.")

def fetch_company_articles(session: requests.Session, cfg: Dict[str, Any]) -> List[Article]:
    query = build_query(cfg)
    params = {
        "query": query,
        "mode": "artlist",
        "format": "rss",
        "sort": "datedesc",
        "timespan": "7d",
        "maxrecords": "50",
    }

    rss_url = requests.Request("GET", BASE_URL, params=params).prepare().url
    feed = feedparser.parse(fetch_rss(session, rss_url))

    articles: List[Article] = []
    for entry in feed.entries:
        title = getattr(entry, "title", "")
        link = getattr(entry, "link", "")
        published = getattr(entry, "published", "") or getattr(entry, "updated", "")
        summary = re.sub(r"<[^>]+>", " ", getattr(entry, "summary", "") or "")
        summary = re.sub(r"\s+", " ", summary).strip()

        if not passes_local_filter(cfg, title, summary):
            continue

        score = score_article(cfg, title, summary, published)
        articles.append(
            Article(
                company=cfg["name"],
                title=title,
                link=link,
                published=published,
                summary=summary,
                score=score,
            )
        )

        # Gentle spacing even inside the per-company loop
        time.sleep(6.1)

    return articles

def dedupe_articles(items: List[Article]) -> List[Article]:
    seen = set()
    out = []
    for a in sorted(items, key=lambda x: x.score, reverse=True):
        key = norm(a.link or a.title)
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out

def make_briefing(all_articles: List[Article]) -> str:
    grouped: Dict[str, List[Article]] = {}
    for a in all_articles:
        grouped.setdefault(a.company, []).append(a)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# Weekly Drone Press Briefing ({today})", ""]
    lines.append("## Top developments")
    lines.append("")

    top_overall = sorted(all_articles, key=lambda x: x.score, reverse=True)[:5]
    for a in top_overall:
        lines.append(f"- **{a.company}**: {a.title} ({a.published[:10]})")
        lines.append(f"  {a.link}")

    lines.append("")
    lines.append("## Company watchlist")
    lines.append("")

    for company in sorted(grouped.keys()):
        lines.append(f"### {company}")
        for a in sorted(grouped[company], key=lambda x: x.score, reverse=True)[:2]:
            lines.append(f"- {a.title} ({a.published[:10]})")
            lines.append(f"  {a.link}")
        lines.append("")

    lines.append("## Sources")
    lines.append("")
    for a in top_overall:
        lines.append(f"- {a.company}: {a.link}")

    return "\n".join(lines).strip() + "\n"

def main() -> None:
    cfg_path = Path("manufacturers.json")
    companies = json.loads(cfg_path.read_text(encoding="utf-8"))

    session = requests.Session()
    session.headers.update(HEADERS)

    all_articles: List[Article] = []
    for i, cfg in enumerate(companies):
        if i > 0:
            time.sleep(6.2)  # stays safely above the observed 5-second limit
        all_articles.extend(fetch_company_articles(session, cfg))

    all_articles = dedupe_articles(all_articles)
    briefing = make_briefing(all_articles)

    Path("briefing.md").write_text(briefing, encoding="utf-8")
    Path("briefing.json").write_text(
        json.dumps([a.__dict__ for a in all_articles], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

if __name__ == "__main__":
    main()
