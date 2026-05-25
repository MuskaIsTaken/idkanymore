import json
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
REQUEST_INTERVAL_SECONDS = 6.2
MAX_RECORDS = 50

COMMON_DRONE_TERMS = [
    "drone", "drones", "uav", "uas", "unmanned", "vtol",
    "reconnaissance", "surveillance", "aerial", "uavs"
]

@dataclass
class Article:
    company: str
    title: str
    link: str
    published: str
    summary: str
    score: int

_last_request_at = 0.0

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()

def quote_term(term: str) -> str:
    term = term.strip()
    if not term:
        return term
    return f'"{term}"' if " " in term else term

def text_has_any(text: str, terms: List[str]) -> bool:
    t = normalize(text)
    return any(normalize(term) in t for term in terms)

def make_query(cfg: Dict[str, Any], strict: bool = True) -> str:
    aliases = [
        quote_term(t)
        for t in cfg.get("aliases", [])
        if t.strip()
    ]

    drone_terms = [
        quote_term(t)
        for t in (COMMON_DRONE_TERMS + cfg.get("priority_terms", []))
        if t.strip()
    ]

    alias_expr = " OR ".join(aliases)
    drone_expr = " OR ".join(drone_terms)

    if strict:
        return f"({alias_expr}) AND ({drone_expr})"

    return f"({alias_expr})"

def rate_limited_get(session: requests.Session, url: str, params: Dict[str, Any], retries: int = 4) -> requests.Response:
    global _last_request_at
    backoff = 6.0

    for _ in range(retries):
        elapsed = time.monotonic() - _last_request_at
        if elapsed < REQUEST_INTERVAL_SECONDS:
            time.sleep(REQUEST_INTERVAL_SECONDS - elapsed)

        response = session.get(url, params=params, timeout=45)
        _last_request_at = time.monotonic()

        body = (response.text or "").lower()
        if response.status_code == 429 or "please limit requests to one every five seconds" in body:
            time.sleep(backoff)
            backoff *= 2
            continue

        response.raise_for_status()
        return response

    raise RuntimeError("GDELT kept rate-limiting the requests after retries.")

def fetch_json(session: requests.Session, query: str) -> Dict[str, Any]:
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "sort": "datedesc",
        "timespan": "7d",
        "maxrecords": str(MAX_RECORDS),
    }
    response = rate_limited_get(session, BASE_URL, params)
    try:
        return response.json()
    except Exception as exc:
        snippet = (response.text or "")[:500]
        raise RuntimeError(f"Could not parse GDELT JSON. First response chars: {snippet!r}") from exc

def extract_items(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return payload

    if not isinstance(payload, dict):
        return []

    candidates = [
        payload.get("articles"),
        payload.get("items"),
        payload.get("results"),
        payload.get("response", {}).get("items") if isinstance(payload.get("response"), dict) else None,
        payload.get("response", {}).get("articles") if isinstance(payload.get("response"), dict) else None,
        payload.get("feed", {}).get("items") if isinstance(payload.get("feed"), dict) else None,
    ]

    for candidate in candidates:
        if isinstance(candidate, list):
            return candidate

    return []

def extract_field(item: Dict[str, Any], keys: List[str]) -> str:
    for key in keys:
        value = item.get(key)
        if value:
            return str(value)
    return ""

def passes_local_filter(cfg: Dict[str, Any], title: str, summary: str) -> bool:
    text = f"{title} {summary}".lower()

    alias_hit = text_has_any(text, cfg.get("aliases", []))
    priority_hit = text_has_any(text, cfg.get("priority_terms", []))
    drone_hit = text_has_any(text, COMMON_DRONE_TERMS)

    if not (alias_hit or priority_hit):
        return False
    if not drone_hit:
        return False

    for bad in cfg.get("exclude_terms", []):
        if normalize(bad) in normalize(text):
            return False

    return True

def score_article(cfg: Dict[str, Any], title: str, summary: str, published: str) -> int:
    score = 0
    full_text = f"{title} {summary}"

    if text_has_any(title, cfg.get("aliases", [])):
        score += 4
    if text_has_any(title, cfg.get("priority_terms", [])):
        score += 3
    if text_has_any(full_text, COMMON_DRONE_TERMS):
        score += 2

    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        if age_hours <= 24:
            score += 3
        elif age_hours <= 72:
            score += 2
        elif age_hours <= 168:
            score += 1
    except Exception:
        pass

    return score

def dedupe_articles(items: List[Article]) -> List[Article]:
    seen = set()
    deduped = []
    for a in sorted(items, key=lambda x: x.score, reverse=True):
        key = normalize(a.link or a.title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)
    return deduped

def fetch_company_articles(session: requests.Session, cfg: Dict[str, Any]) -> List[Article]:
    strict_query = make_query(cfg, strict=True)
    fallback_query = make_query(cfg, strict=False)

    payload = fetch_json(session, strict_query)
    raw_items = extract_items(payload)

    if not raw_items:
        print(f"[DEBUG] {cfg['name']}: strict query returned 0 raw items, trying fallback query")
        payload = fetch_json(session, fallback_query)
        raw_items = extract_items(payload)

    print(f"[DEBUG] {cfg['name']}: fetched {len(raw_items)} raw items")

    articles: List[Article] = []
    for item in raw_items:
        title = extract_field(item, ["title", "headline", "name"])
        link = extract_field(item, ["url", "link"])
        published = extract_field(item, ["seendate", "published", "date", "datetime", "time"])
        summary = extract_field(item, ["snippet", "summary", "description", "content"])

        summary = re.sub(r"<[^>]+>", " ", summary)
        summary = re.sub(r"\s+", " ", summary).strip()

        if not title or not link:
            continue

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

    print(f"[DEBUG] {cfg['name']}: kept {len(articles)} after local filter")
    return articles

def make_briefing(articles: List[Article]) -> str:
    today = datetime.now().strftime("%Y-%m-%d")

    if not articles:
        return (
            f"# Weekly Drone Press Briefing ({today})\n\n"
            "No qualifying drone-related articles were found in the last 7 days after filtering.\n"
        )

    grouped: Dict[str, List[Article]] = {}
    for article in articles:
        grouped.setdefault(article.company, []).append(article)

    top_overall = sorted(articles, key=lambda x: x.score, reverse=True)[:5]

    lines = [f"# Weekly Drone Press Briefing ({today})", ""]
    lines.append("## Top developments")
    lines.append("")

    for a in top_overall:
        lines.append(f"- **{a.company}**: {a.title} ({a.published[:10] if a.published else 'date unavailable'})")
        lines.append(f"  {a.link}")

    lines.append("")
    lines.append("## Company watchlist")
    lines.append("")

    for company in sorted(grouped.keys()):
        lines.append(f"### {company}")
        for a in sorted(grouped[company], key=lambda x: x.score, reverse=True)[:2]:
            lines.append(f"- {a.title} ({a.published[:10] if a.published else 'date unavailable'})")
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
    session.headers.update({"User-Agent": "drone-briefing/1.0"})

    all_articles: List[Article] = []

    for cfg in companies:
        company_articles = fetch_company_articles(session, cfg)
        all_articles.extend(company_articles)
        print(f"[DEBUG] total articles so far: {len(all_articles)}")

    all_articles = dedupe_articles(all_articles)
    print(f"[DEBUG] final article count after dedupe: {len(all_articles)}")

    briefing = make_briefing(all_articles)
    Path("briefing.md").write_text(briefing, encoding="utf-8")
    Path("briefing.json").write_text(
        json.dumps([asdict(a) for a in all_articles], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

if __name__ == "__main__":
    main()
