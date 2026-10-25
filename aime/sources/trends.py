"""Content topic research: recent public HN stories about AI, automation, CRM and SaaS.

Only titles and links are stored, as context for a human editor and to weight which
service themes are timely. Story text is never copied into generated posts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aime.matching import match_services

API = "https://hn.algolia.com/api/v1/search"
QUERIES = ["AI agents", "workflow automation", "CRM", "LLM", "SaaS", "sales automation"]


def fetch_trending_topics(ctx, days: int = 7, min_points: int = 50) -> list[dict]:
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    seen: set[str] = set()
    topics: list[dict] = []
    with ctx.http() as client:
        for q in QUERIES:
            resp = client.get(API, params={
                "query": q, "tags": "story",
                "numericFilters": f"created_at_i>{since},points>{min_points}", "hitsPerPage": 10,
            })
            resp.raise_for_status()
            for hit in resp.json().get("hits", []):
                oid = str(hit.get("objectID"))
                title = (hit.get("title") or "").strip()
                if not title or oid in seen:
                    continue
                seen.add(oid)
                matches = match_services(title)
                topics.append({
                    "topic": title[:300],
                    "service": matches[0].service_id if matches else None,
                    "source": "hn_trending",
                    "source_url": f"https://news.ycombinator.com/item?id={oid}",
                    "score": float(hit.get("points") or 0),
                })
    return topics
