#!/usr/bin/env python3
"""
LinkedIn Analytics Extractor

Connects to Chrome via CDP and extracts analytics into SQLite + JSON.

Usage:
  python extract.py                                      # all metrics, past_7_days
  python extract.py --metrics overview,impressions       # selective
  python extract.py --metrics all --period past_28_days
  python extract.py --output json                        # stdout JSON only
  python extract.py --db-path /path/to/linkedin.db
  python extract.py --dry-run                            # show plan, no scrape
"""

import argparse
import datetime
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import sqlite_utils
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://www.linkedin.com"

URLS = {
    "dashboard": "/dashboard/",
    "impressions": "/analytics/creator/content/?lineChartType=daily&metricType=IMPRESSIONS&timeRange={period}",
    "engagements": "/analytics/creator/content/?lineChartType=daily&metricType=ENGAGEMENTS&timeRange={period}",
    "followers": "/analytics/creator/audience/?lineChartType=daily&timeRange={period}",
    "demographics": "/analytics/demographic-detail/urn:li:fsd_profile:profile/?metricType=MEMBER_FOLLOWERS",
    "profile_views": "/analytics/profile-views/",
    "top_posts": "/analytics/creator/top-posts/?metricType={metric_type}&timeRange={period}",
}

PERIOD_DAYS = {
    "past_7_days": 7,
    "past_14_days": 14,
    "past_28_days": 28,
    "past_90_days": 90,
    "past_365_days": 365,
}

ALL_METRICS = [
    "overview",
    "impressions",
    "engagements",
    "followers",
    "demographics",
    "profile_views",
    "top_posts",
]

DEFAULT_DB = Path(__file__).parent / "linkedin.db"
CHROME_APP = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_DEBUG_DIR = Path.home() / ".chrome-linkedin-debug"


# ---------------------------------------------------------------------------
# Chrome helpers
# ---------------------------------------------------------------------------


def chrome_running(port: int = 9222) -> bool:
    import urllib.request

    try:
        urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=2)
        return True
    except Exception:
        return False


def launch_chrome(port: int = 9222) -> bool:
    """Launch Chrome with CDP on given port using a persistent debug profile."""
    CHROME_DEBUG_DIR.mkdir(exist_ok=True)
    subprocess.Popen(
        [
            CHROME_APP,
            "--headless=new",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={CHROME_DEBUG_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        time.sleep(0.5)
        if chrome_running(port):
            return True
    return False


def get_page(p, cdp_port: int = 9222):
    """Connect to running Chrome, return (page, browser)."""
    browser = p.chromium.connect_over_cdp(f"http://localhost:{cdp_port}")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    return ctx.new_page(), browser


def nav(page, url: str, wait_ms: int = 5000):
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_timeout(wait_ms)

# ---------------------------------------------------------------------------
# JS helpers (inline strings to keep eval calls clean)
# ---------------------------------------------------------------------------

_HC_JS = """() => {
    if (!window.Highcharts) return null;
    return window.Highcharts.charts.filter(c => c).map(c => ({
        title: c.title?.textStr || '',
        series: c.series?.map(s => ({
            name: s.name || '',
            data: (s.data || []).map(p => ({ x: p.x, y: p.y }))
        })) || []
    }));
}"""

_TEXTNODES_JS = """() => {
    const results = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while (node = walker.nextNode()) {
        const v = node.textContent.trim();
        if (/^[\\d,]+$/.test(v) && parseInt(v.replace(/,/g,'')) > 0) {
            results.push({
                val: parseInt(v.replace(/,/g,'')),
                cls: (node.parentElement?.className?.baseVal ?? node.parentElement?.className ?? '').toString().substring(0, 80)
            });
        }
    }
    return results;
}"""


# ---------------------------------------------------------------------------
# Extractor: Highcharts
# ---------------------------------------------------------------------------


def extract_highcharts(page) -> list | None:
    # Retry up to 3x in case context was destroyed by a SPA navigation
    for attempt in range(3):
        try:
            result = page.evaluate(_HC_JS)
            return result
        except Exception as e:
            if attempt < 2:
                page.wait_for_timeout(2000)
            else:
                print(f"  [warn] extract_highcharts failed: {e}")
                return None


def hc_to_daily(charts, period: str, today: datetime.date | None = None) -> list[dict]:
    """Convert Highcharts x-offset points → dated records."""
    if not charts:
        return []
    today = today or datetime.date.today()
    n = PERIOD_DAYS.get(period, 7)
    start = today - datetime.timedelta(days=n - 1)

    for chart in charts:
        for series in chart.get("series", []):
            pts = series.get("data", [])
            if pts:
                return [
                    {
                        "date": (
                            start + datetime.timedelta(days=int(p.get("x", 0)))
                        ).isoformat(),
                        "value": p.get("y", 0),
                    }
                    for p in pts
                ]
    return []


# ---------------------------------------------------------------------------
# Extractor: Dashboard (Vue comment-node pattern)
# ---------------------------------------------------------------------------

_DASHBOARD_JS = """() => {
    // LinkedIn wraps reactive values in <!---->VALUE<!---->
    // innerText misses them; textContent works.
    const cards = Array.from(document.querySelectorAll(
        'section, article, li, [class*="insight"], [class*="analytics"]'
    )).map(el => el.textContent.replace(/\\s+/g, ' ').trim().substring(0, 300))
      .filter(t => t.length > 10 && /\\d{2,}/.test(t));

    const nums = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let node;
    while (node = walker.nextNode()) {
        const v = node.textContent.trim();
        if (/^[\\d,]+$/.test(v) && parseInt(v.replace(/,/g,'')) > 0) {
            nums.push({ val: v, cls: node.parentElement?.className?.substring(0,80) || '' });
        }
    }
    return { cards, nums };
}"""


def extract_dashboard(page) -> dict:
    raw = page.evaluate(_DASHBOARD_JS)
    result = {}
    for card in raw.get("cards", []):
        c = card.lower()
        nums = [
            int(n.replace(",", ""))
            for n in re.findall(r"[\d,]+", card)
            if n.replace(",", "").isdigit()
        ]
        if not nums:
            continue
        v = nums[0]
        if "impression" in c and "newsletter" not in c:
            result.setdefault("impressions", v)
        if "follower" in c and "newsletter" not in c and "new follower" not in c:
            result.setdefault("followers_total", v)
        if "profile viewer" in c or (
            "profile" in c and "view" in c and "post" not in c
        ):
            result.setdefault("profile_views_90d", v)
        if "search" in c and "appearance" in c:
            result.setdefault("search_appearances", min(nums))
        if "newsletter" in c and "subscriber" in c:
            result.setdefault("newsletter_new_subscribers", v)
        if "newsletter" in c and "view" in c and "subscriber" not in c:
            result.setdefault("newsletter_article_views", v)
    return result


# ---------------------------------------------------------------------------
# Extractor: Per-post metrics
# ---------------------------------------------------------------------------

_POSTS_JS = """() => {
    // LinkedIn stores post stats in included[] as two linked types:
    // 1. SocialActivityCounts: {numLikes, numComments, numImpressions, urn}
    // 2. MiniUpdate / UGCPost: {entityUrn, commentary.text, *socialActivityCounts}

    const countsByUrn = {};  // urn -> stats object
    const postEntities = [];  // list of post objects

    const codeTags = Array.from(document.querySelectorAll('code[id]'));
    for (const tag of codeTags) {
        if (tag.textContent.length < 50000) continue;
        try {
            const d = JSON.parse(tag.textContent);
            const inc = d.included || [];
            for (const item of inc) {
                const t = (item['$type'] || '').toString();
                // Collect SocialActivityCounts
                if (t.includes('SocialActivityCounts') || item.numImpressions !== undefined) {
                    const u = item.urn || item.entityUrn || '';
                    if (u) countsByUrn[u] = item;
                }
                // Collect post entities (MiniUpdate, UGCPost, Article)
                if (item.entityUrn && (
                    item.entityUrn.includes('ugcPost') ||
                    item.entityUrn.includes('share') ||
                    item.entityUrn.includes('article')
                )) {
                    postEntities.push(item);
                }
            }
        } catch(e) {}
    }

    const posts = [];
    for (const post of postEntities) {
        // Find counts: may be linked via *socialActivityCounts or directly on entity
        let counts = null;
        const socRef = post['*socialActivityCounts'] || post['*totalSocialActivityCounts'] || '';
        if (socRef && countsByUrn[socRef]) counts = countsByUrn[socRef];
        else if (countsByUrn[post.entityUrn]) counts = countsByUrn[post.entityUrn];
        else {
            // Last resort: find any counts object that references this urn
            for (const [u, c] of Object.entries(countsByUrn)) {
                if (u.includes(post.entityUrn.split(':').pop())) { counts = c; break; }
            }
        }

        const snippet = (
            (post.commentary?.text?.text || post.commentary?.text || post.title?.text || '')
        ).toString().substring(0, 120);

        posts.push({
            urn:             post.entityUrn,
            snippet:         snippet,
            impressions:     counts?.numImpressions ?? counts?.impressionCount ?? null,
            members_reached: counts?.numUniqueImpressions ?? counts?.uniqueImpressionsCount ?? null,
            reactions:       counts?.numLikes ?? counts?.likeCount ?? null,
            comments:        counts?.numComments ?? counts?.commentCount ?? null,
            reposts:         counts?.numShares ?? counts?.repostCount ?? null,
        });
    }

    // Deduplicate by urn
    const seen = new Set();
    const deduped = posts.filter(p => {
        if (!p.urn || seen.has(p.urn)) return false;
        seen.add(p.urn);
        return true;
    });

    // Fallback: visible post list items if nothing found
    if (deduped.length === 0) {
        const items = Array.from(document.querySelectorAll('ul li, article'))
            .filter(li => li.querySelectorAll('p, span').length > 2 && /\\d{3,}/.test(li.textContent));
        for (const li of items.slice(0, 20)) {
            const text = li.textContent.replace(/\\s+/g, ' ').trim();
            const nums = (text.match(/[\\d,]+/g) || []).map(n => parseInt(n.replace(/,/g,'')));
            if (nums.some(n => n > 200)) {
                deduped.push({ urn: null, snippet: text.substring(0, 120), nums });
            }
        }
    }

    return deduped.slice(0, 30);
}"""


# ---------------------------------------------------------------------------
# Extractor: Top-posts analytics page
# ---------------------------------------------------------------------------

_TOP_POSTS_JS = """() => {
    const items = Array.from(
        document.querySelectorAll('a.member-analytics-addon__mini-update-item')
    );
    return items.map(item => {
        const postUrl = item.href || '';
        const urnMatch = postUrl.match(/urn:li:activity:(\\d+)/);
        const urnId = urnMatch ? urnMatch[1] : null;

        const metricEl = item.querySelector(
            '.member-analytics-addon__cta-item-with-secondary-list-item-title'
        );
        const metricLabelEl = item.querySelector(
            '.member-analytics-addon__cta-item-with-secondary-list-item-text'
        );
        const metricVal = metricEl
            ? parseInt(metricEl.textContent.replace(/[^0-9]/g, ''), 10)
            : null;
        const metricLabel = metricLabelEl
            ? metricLabelEl.textContent.trim().toLowerCase()
            : '';

        // Full post text: use the canonical accessibility span (clean, no duplication)
        const hiddenSpan = item.querySelector('span.visually-hidden[id^="mini-update-a11y-description"]');
        const postText = hiddenSpan ? hiddenSpan.textContent.trim() : '';

        // Reactions count from social button aria-label (e.g. "793 reactions")
        const reactBtn = item.querySelector('button[data-reaction-details]');
        const reactLabel = reactBtn ? (reactBtn.getAttribute('aria-label') || '') : '';
        const reactMatch = reactLabel.match(/(\d[\d,]*)/);
        const reactions = reactMatch ? parseInt(reactMatch[1].replace(/,/g, ''), 10) : null;

        // Comments count from social button aria-label (e.g. "51 comments")
        const commentsBtn = item.querySelector('button[aria-label*="comments"]');
        const commentsLabel = commentsBtn ? (commentsBtn.getAttribute('aria-label') || '') : '';
        const commentsMatch = commentsLabel.match(/(\d[\d,]*)/);
        const comments = commentsMatch ? parseInt(commentsMatch[1].replace(/,/g, ''), 10) : null;

        const imgEl = item.querySelector('img');
        const imageUrl = imgEl ? (imgEl.src || '') : '';

        return { postUrl, urnId, metricVal, metricLabel, postText, imageUrl, reactions, comments };
    });
}"""


def _urn_to_published_at(urn_id_str: str) -> str | None:
    """Decode published_at from LinkedIn activity URN ID.
    The top 41 bits of the URN ID encode Unix milliseconds directly.
    """
    try:
        urn_id = int(urn_id_str)
        ms = urn_id >> 22  # top 41 bits = Unix ms
        import datetime as _dt
        return _dt.datetime.fromtimestamp(
            ms / 1000, tz=_dt.timezone.utc
        ).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def extract_top_posts(page, period: str = "past_28_days") -> list[dict]:
    """Scrape top-posts analytics pages for IMPRESSIONS and ENGAGEMENTS.
    Returns a merged list of post dicts keyed by post_urn.
    IMPRESSIONS page is used as the base (more posts: ~48 vs ~27).
    ENGAGEMENTS page provides the engagements metric for matching URNs.
    """

    def _scrape_page(metric_type: str) -> dict[str, dict]:
        url = BASE_URL + URLS["top_posts"].format(
            metric_type=metric_type, period=period
        )
        nav(page, url, wait_ms=6000)
        raw = page.evaluate(_TOP_POSTS_JS)
        result = {}
        for item in raw:
            urn_id = item.get("urnId")
            if not urn_id:
                continue
            post_urn = f"urn:li:activity:{urn_id}"
            result[post_urn] = item
        return result

    impressions_data = _scrape_page("IMPRESSIONS")
    engagements_data = _scrape_page("ENGAGEMENTS")

    posts = []
    for urn, imp in impressions_data.items():
        eng = engagements_data.get(urn, {})
        urn_id_str = urn.split(":")[-1]
        posts.append({
            "post_urn": urn,
            "post_url": imp.get("postUrl", ""),
            "post_text": imp.get("postText", ""),
            "image_url": imp.get("imageUrl", ""),
            "published_at": _urn_to_published_at(urn_id_str),
            "impressions": imp.get("metricVal"),
            "engagements": eng.get("metricVal"),
            "reactions": imp.get("reactions"),
            "comments": imp.get("comments"),
        })
    return posts


def run_top_posts(page, period: str = "past_28_days") -> list[dict]:
    """Navigate to top-posts analytics and return parsed post records."""
    print(f"  [top_posts] Scraping top posts ({period})")
    posts = extract_top_posts(page, period)
    print(f"  [top_posts] Extracted {len(posts)} posts")
    return posts


def extract_posts(page) -> list[dict]:
    return page.evaluate(_POSTS_JS)


# ---------------------------------------------------------------------------
# Extractor: Engagements breakdown (reactions / comments / clicks)
# ---------------------------------------------------------------------------

_ENGAGEMENTS_JS = """() => {
    const texts = Array.from(document.querySelectorAll('p, span, h1, h2, h3'))
        .filter(el => el.children.length === 0)
        .map(el => el.textContent.trim())
        .filter(t => t.length > 0 && t.length < 120);
    return texts;
}"""


def parse_engagements(texts: list[str]) -> dict:
    result = {}
    for i, t in enumerate(texts):
        if not re.match(r"^[\d,]+$", t):
            continue
        v = int(t.replace(",", ""))
        nxt = texts[i + 1].lower() if i + 1 < len(texts) else ""
        if "reaction" in nxt:
            result["engagements_reactions"] = v
        elif "comment" in nxt:
            result["engagements_comments"] = v
        elif "click" in nxt:
            result["engagements_clicks"] = v
        elif v > 30:
            result.setdefault("engagements_total", v)
    return result


# ---------------------------------------------------------------------------
# Extractor: Demographics (LinkedIn API JSON in <code> tags)
# ---------------------------------------------------------------------------

_DEMO_JS = """() => {
    // LinkedIn embeds bar chart data deep inside <code id='bpr-guid-*'> tags
    // Structure: code[id] -> JSON.data/included[] -> ... -> barChartList.barCharts[]
    // Each barChart has .category (e.g. 'STRUCTURED_TITLE_OCCUPATION') + .dataPoints[].xLabel.text + .yPercent

    const CAT_MAP = {
        'STRUCTURED_TITLE_OCCUPATION': 'job_title',
        'OCCUPATION_SENIORITY':        'seniority',
        'REGION_GEO':                  'location',
        'INDUSTRY':                    'industry',
        'FUNCTION':                    'function',
        'STAFF_COUNT_RANGE':           'company_size',
        'ORGANIZATION':                'company',
    };

    const result = {
        job_title:    [],
        seniority:    [],
        location:     [],
        industry:     [],
        function:     [],
        company_size: [],
        company:      [],
        _found_keys:  []
    };

    // Recursively find all barCharts arrays anywhere in the object tree
    const allCharts = [];
    function walk(obj, depth) {
        if (!obj || typeof obj !== 'object' || depth > 12) return;
        if (obj.barCharts && Array.isArray(obj.barCharts)) {
            allCharts.push(...obj.barCharts);
            return;
        }
        for (const v of Object.values(obj)) walk(v, depth + 1);
    }

    for (const tag of document.querySelectorAll('code[id]')) {
        try { walk(JSON.parse(tag.textContent), 0); } catch(e) {}
    }

    for (const chart of allCharts) {
        const key = CAT_MAP[chart.category];
        if (!key) continue;
        result._found_keys.push(chart.category);
        const parsed = (chart.dataPoints || []).map(dp => ({
            label: (dp.xLabel && dp.xLabel.text) ? dp.xLabel.text
                 : (dp.image && dp.image.accessibilityText) ? dp.image.accessibilityText
                 : (dp.yFormattedValue && dp.yFormattedValue.text) ? 'item'
                 : '',
            pct: Math.round((dp.yPercent || 0) * 10000) / 100
        })).filter(d => d.label && d.pct > 0).sort((a,b) => b.pct - a.pct);
        if (parsed.length > 0) result[key] = parsed;
    }

    return result;
}"""


def extract_demographics(page) -> dict:
    return page.evaluate(_DEMO_JS)


# ---------------------------------------------------------------------------
# Extractor: Profile views
# ---------------------------------------------------------------------------


def extract_profile_views(page) -> int | None:
    nums = page.evaluate(_TEXTNODES_JS)
    # Profile views 90d is usually the only 3-4 digit standalone number on this page
    candidates = [r["val"] for r in nums if 100 < r["val"] < 50_000]
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# SQLite storage
# ---------------------------------------------------------------------------


def init_db(path: Path) -> sqlite_utils.Database:
    db = sqlite_utils.Database(path)
    schemas = {
        "profile_snapshots": {
            "date": str,
            "impressions": int,
            "members_reached": int,
            "followers_total": int,
            "new_followers_period": int,
            "profile_views_90d": int,
            "search_appearances": int,
            "engagements_total": int,
            "engagements_reactions": int,
            "engagements_comments": int,
            "engagements_clicks": int,
            "newsletter_new_subscribers": int,
            "newsletter_article_views": int,
        },
        "post_metrics": {
            "post_urn": str,
            "scraped_date": str,
            "period": str,
            "snippet": str,
            "impressions": int,
            "members_reached": int,
            "reactions": int,
            "comments": int,
            "reposts": int,
        },
        "daily_impressions": {"date": str, "impressions": int},
        "daily_engagements": {"date": str, "engagements": int},
        "daily_followers": {"date": str, "new_followers": int},
        "demographics": {"date": str, "type": str, "data": str},
    }
    pks = {
        "profile_snapshots": "date",
        "post_metrics": ("post_urn", "scraped_date", "period"),
        "daily_impressions": "date",
        "daily_engagements": "date",
        "daily_followers": "date",
        "demographics": ("date", "type"),
    }
    for tbl, schema in schemas.items():
        if tbl not in db.table_names():
            db[tbl].create(schema, pk=pks[tbl])
    # One-time migration: add period column + rebuild composite PK
    if "post_metrics" in db.table_names():
        existing_cols = {c.name for c in db["post_metrics"].columns}
        if "period" not in existing_cols:
            db["post_metrics"].add_column("period", str)
            db.execute("UPDATE post_metrics SET period = 'past_7_days' WHERE period IS NULL")
            db.conn.commit()
            db["post_metrics"].transform(pk=("post_urn", "scraped_date", "period"))
    return db


def upsert(db: sqlite_utils.Database, table: str, row: dict, pk):
    db[table].upsert(row, pk=pk, alter=True)


def save_snapshot(db, data: dict, date: str):
    upsert(db, "profile_snapshots", {"date": date, **data}, pk="date")


def save_daily_series(db, table: str, field: str, daily: list[dict]):
    for row in daily:
        upsert(db, table, {"date": row["date"], field: row["value"]}, pk="date")


def save_posts(db, posts: list[dict], date: str, period: str = "past_7_days"):
    """Upsert post records into post_metrics.
    Accepts both Voyager-style dicts (key: 'urn') and
    top-posts-style dicts (key: 'post_urn').
    Only non-None values are written so merging two sources
    for the same (post_urn, scraped_date) works correctly.
    """
    for p in posts:
        # Support both key conventions
        urn = p.get("post_urn") or p.get("urn")
        if not urn:
            continue
        row: dict = {"post_urn": urn, "scraped_date": date, "period": period}
        for k in [
            "snippet",
            "post_url",
            "post_text",
            "image_url",
            "published_at",
            "impressions",
            "members_reached",
            "engagements",
            "reactions",
            "comments",
            "reposts",
        ]:
            v = p.get(k)
            if v is not None:
                row[k] = v
        upsert(db, "post_metrics", row, pk=("post_urn", "scraped_date", "period"))


def save_demographics(db, demo: dict, date: str):
    for dtype, items in demo.items():
        if dtype.startswith("_") or not items:
            continue
        upsert(
            db,
            "demographics",
            {
                "date": date,
                "type": dtype,
                "data": json.dumps(items, ensure_ascii=False),
            },
            pk=("date", "type"),
        )


# ---------------------------------------------------------------------------
# Per-metric run functions
# ---------------------------------------------------------------------------


def run_overview(page, today_date: datetime.date) -> dict:
    print("  [1/6] dashboard overview")
    nav(page, BASE_URL + URLS["dashboard"])
    return extract_dashboard(page)


def run_impressions(page, period: str, today_date: datetime.date) -> tuple[dict, list]:
    print("  [2/6] content / impressions")
    nav(page, BASE_URL + URLS["impressions"].format(period=period), wait_ms=8000)
    charts = extract_highcharts(page)
    daily = hc_to_daily(charts, period, today_date) if charts else []
    posts = extract_posts(page)
    # Pull total impressions + members_reached from top stat cards
    vals = []
    for _attempt in range(2):
        try:
            nums = page.evaluate(_TEXTNODES_JS)
        except Exception:
            nums = []
        vals = sorted({r["val"] for r in nums if 1_000 < r["val"] < 2_000_000})
        if len(vals) >= 2:
            break
        if _attempt == 0:
            page.wait_for_timeout(3000)
    total = vals[-1] if vals else None
    reached = vals[-2] if len(vals) >= 2 else None
    return {"impressions": total, "members_reached": reached, "daily": daily}, posts


def run_engagements(page, period: str, today_date: datetime.date) -> dict:
    print("  [3/6] content / engagements")
    nav(page, BASE_URL + URLS["engagements"].format(period=period), wait_ms=8000)
    charts = extract_highcharts(page)
    daily = hc_to_daily(charts, period, today_date) if charts else []
    try:
        texts = page.evaluate(_ENGAGEMENTS_JS)
    except Exception:
        texts = []
    breakdown = parse_engagements(texts)
    return {**breakdown, "daily": daily}


def run_followers(page, period: str, today_date: datetime.date) -> dict:
    print("  [4/6] audience / followers")
    nav(page, BASE_URL + URLS["followers"].format(period=period), wait_ms=8000)
    charts = extract_highcharts(page)
    daily = hc_to_daily(charts, period, today_date) if charts else []
    new_total = sum(d["value"] for d in daily) if daily else None
    # Total follower count
    total = None
    for _attempt in range(2):
        try:
            nums = page.evaluate(_TEXTNODES_JS)
        except Exception:
            nums = []
        total = next((r["val"] for r in nums if 500 < r["val"] < 500_000), None)
        if total is not None:
            break
        if _attempt == 0:
            page.wait_for_timeout(3000)
    return {"followers_total": total, "new_followers_period": new_total, "daily": daily}


def run_demographics(page) -> dict:
    print("  [5/6] demographics")
    nav(page, BASE_URL + URLS["demographics"], wait_ms=6000)
    return extract_demographics(page)


def run_profile_views(page) -> dict:
    print("  [6/6] profile views")
    nav(page, BASE_URL + URLS["profile_views"])
    count = extract_profile_views(page)
    return {"profile_views_90d": count} if count else {}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="LinkedIn Analytics Extractor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Available metrics: {', '.join(ALL_METRICS)}",
    )
    parser.add_argument(
        "--metrics", default="all", help="Comma-separated list or 'all'"
    )
    parser.add_argument(
        "--period", default="past_7_days", choices=list(PERIOD_DAYS.keys())
    )
    parser.add_argument("--output", default="both", choices=["json", "sqlite", "both"])
    parser.add_argument("--db-path", default=str(DEFAULT_DB))
    parser.add_argument("--cdp-port", type=int, default=9222)
    parser.add_argument(
        "--dry-run", action="store_true", help="Print plan without scraping"
    )
    parser.add_argument(
        "--no-dashboard",
        action="store_true",
        help="Skip auto-regenerating dashboard.html after sqlite save",
    )
    args = parser.parse_args()

    requested = (
        ALL_METRICS
        if args.metrics.strip().lower() == "all"
        else [m.strip() for m in args.metrics.split(",")]
    )
    bad = [m for m in requested if m not in ALL_METRICS]
    if bad:
        print(f"Unknown metrics: {bad}. Valid: {ALL_METRICS}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        print(f"Plan: {requested} | period={args.period} | db={args.db_path}")
        return

    # Ensure Chrome is running
    if not chrome_running(args.cdp_port):
        print(
            f"Chrome not found on port {args.cdp_port}. Launching...", file=sys.stderr
        )
        if not launch_chrome(args.cdp_port):
            print(
                f"Failed. Start Chrome manually:\n"
                f'  "{CHROME_APP}" --remote-debugging-port={args.cdp_port} '
                f"--user-data-dir={CHROME_DEBUG_DIR}",
                file=sys.stderr,
            )
            sys.exit(1)

    today_date = datetime.date.today()
    today = today_date.isoformat()
    snapshot = {}
    daily_data = {}
    posts = []
    top_posts_by_period: list = []
    demo_data = {}

    with sync_playwright() as p:
        page, _browser = get_page(p, args.cdp_port)
        try:
            print(f"\nLinkedIn Analytics  date={today}  period={args.period}")
            print(f"Metrics: {requested}\n")

            if "overview" in requested:
                snapshot.update(run_overview(page, today_date))

            if "impressions" in requested:
                data, post_list = run_impressions(page, args.period, today_date)
                snapshot.update({k: v for k, v in data.items() if k != "daily" and v is not None})
                daily_data["impressions"] = data.get("daily", [])
                posts.extend(post_list)

            if "engagements" in requested:
                data = run_engagements(page, args.period, today_date)
                snapshot.update({k: v for k, v in data.items() if k != "daily" and v is not None})
                daily_data["engagements"] = data.get("daily", [])

            if "followers" in requested:
                data = run_followers(page, args.period, today_date)
                snapshot.update({k: v for k, v in data.items() if k != "daily" and v is not None})
                daily_data["followers"] = data.get("daily", [])

            if "demographics" in requested:
                demo_data = run_demographics(page)

            if "profile_views" in requested:
                snapshot.update(run_profile_views(page))

            if "top_posts" in requested:
                top_posts_by_period = []
                for tp_period in ["past_7_days", "past_28_days", "past_90_days"]:
                    tp_list = run_top_posts(page, tp_period)
                    if tp_list:
                        top_posts_by_period.append((tp_list, tp_period))
                # also expose first period in shared posts list for JSON output
                if top_posts_by_period:
                    posts.extend(top_posts_by_period[0][0])

        finally:
            page.close()

    output = {
        "captured_at": datetime.datetime.now().isoformat(),
        "period": args.period,
        "snapshot": {"date": today, **snapshot},
        "daily": daily_data,
        "posts": posts,
        "demographics": {k: v for k, v in demo_data.items() if not k.startswith("_")},
    }

    if args.output in ("json", "both"):
        print(json.dumps(output, indent=2, ensure_ascii=False))

    if args.output in ("sqlite", "both"):
        db = init_db(Path(args.db_path))

        if snapshot:
            save_snapshot(db, snapshot, today)

        if "impressions" in daily_data:
            save_daily_series(
                db, "daily_impressions", "impressions", daily_data["impressions"]
            )
        if "engagements" in daily_data:
            save_daily_series(
                db, "daily_engagements", "engagements", daily_data["engagements"]
            )
        if "followers" in daily_data:
            save_daily_series(
                db, "daily_followers", "new_followers", daily_data["followers"]
            )

        if posts:
            save_posts(db, posts, today, period=args.period)
        for tp_list, tp_period in top_posts_by_period:
            save_posts(db, tp_list, today, period=tp_period)
            save_posts(db, tp_list, today, period=tp_period)
        if demo_data:
            save_demographics(db, demo_data, today)

        print(f"\nSaved → {args.db_path}", file=sys.stderr)
        if not args.no_dashboard:
            print("\n[dashboard] Regenerating dashboard.html…", file=sys.stderr)
            _script_dir = Path(__file__).parent
            _ = subprocess.run(
                [
                    sys.executable,
                    str(_script_dir / "dashboard_gen.py"),
                    "--no-open",
                    "--db", args.db_path,
                    "--out", str(_script_dir / "dashboard.html"),
                ],
            )


if __name__ == "__main__":
    main()
