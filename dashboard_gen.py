"""
LinkedIn Analytics — Dynamic Dashboard Generator
Reads all historical data from linkedin.db and generates dashboard.html
Run: python dashboard_gen.py [--db linkedin.db] [--out dashboard.html] [--no-open]
"""

import argparse
import base64
import datetime
import json
import os
import sqlite3
import subprocess
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(PROJECT_DIR, "linkedin.db")
DEFAULT_OUT = os.path.join(PROJECT_DIR, "dashboard.html")
PROFILE_PIC = os.path.join(
    os.path.expanduser("~"),
    "Obsidian/Laibyte/_default/_attachments/profile-pic.jpeg",
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_data(db_path: str) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    def q(sql, params=()):
        return [dict(r) for r in con.execute(sql, params).fetchall()]

    snap_rows = q("SELECT * FROM profile_snapshots ORDER BY date DESC LIMIT 1")
    snap = snap_rows[0] if snap_rows else {}

    # For each KPI field, fill NULLs with the most recent non-null value
    _kpi_fields = [
        "impressions", "members_reached", "followers_total",
        "new_followers_period", "engagements_total", "profile_views_90d",
    ]
    for _field in _kpi_fields:
        if snap.get(_field) is None:
            _fb = q(
                f"SELECT {_field} FROM profile_snapshots "
                f"WHERE {_field} IS NOT NULL ORDER BY date DESC LIMIT 1"
            )
            if _fb:
                snap[_field] = _fb[0][_field]

    snapshots = q("SELECT date, followers_total FROM profile_snapshots ORDER BY date")
    imp_daily = q("SELECT date, impressions     FROM daily_impressions  ORDER BY date")
    eng_daily = q("SELECT date, engagements     FROM daily_engagements  ORDER BY date")
    fol_daily = q("SELECT date, new_followers   FROM daily_followers    ORDER BY date")
    demo_raw = q("""
        SELECT type, data FROM demographics
        WHERE date=(SELECT MAX(date) FROM demographics)
    """)
    posts_raw = q("""
        SELECT post_urn, post_url, post_text, snippet, image_url,
               published_at, impressions, engagements,
               reactions, comments, reposts, scraped_date, period
        FROM post_metrics
        WHERE scraped_date = (SELECT MAX(scraped_date) FROM post_metrics)
        ORDER BY period, COALESCE(impressions, 0) DESC
    """)
    con.close()

    # Parse demographics
    demo = {}
    for row in demo_raw:
        try:
            demo[row["type"]] = json.loads(row["data"])
        except Exception:
            pass

    # Reconstruct cumulative follower total from known snapshots + daily deltas
    cumulative = _reconstruct_cumulative(fol_daily, snapshots)

    # Derive available periods and default
    periods_available = sorted({r['period'] for r in posts_raw if r.get('period')})
    default_period = ('past_28_days' if 'past_28_days' in periods_available
                      else (periods_available[0] if periods_available else 'past_7_days'))

    return {
        "snapshot": snap,
        "snapshots": snapshots,
        "imp_daily": imp_daily,
        "eng_daily": eng_daily,
        "fol_daily": fol_daily,
        "cumulative": cumulative,
        "demo": demo,
        "posts": posts_raw,
        "periods_available": periods_available,
        "default_period": default_period,
    }


def _reconstruct_cumulative(fol_daily: list, snapshots: list) -> list:
    """
    Build a list of {date, total} by anchoring on known followers_total
    values from profile_snapshots and filling gaps using daily deltas.
    Works backwards and forwards from each anchor.
    """
    if not fol_daily:
        return []

    # Build a map of date -> new_followers
    delta = {r["date"]: (r["new_followers"] or 0) for r in fol_daily}
    # Build a map of date -> known total
    known = {r["date"]: r["followers_total"] for r in snapshots if r["followers_total"]}

    all_dates = sorted(delta.keys())
    if not known:
        return []  # no anchor, can't reconstruct

    # Pick the most recent anchor and walk backwards + forwards
    anchor_date = max(known.keys())
    anchor_val = known[anchor_date]

    # Build ordered list with running total
    result = {}
    # Forward from anchor
    total = anchor_val
    for d in sorted([x for x in all_dates if x >= anchor_date]):
        if d == anchor_date:
            result[d] = total
        else:
            total += delta.get(d, 0)
            result[d] = total
    # Backward from anchor
    total = anchor_val
    prev_date = anchor_date
    for d in sorted([x for x in all_dates if x < anchor_date], reverse=True):
        total -= delta.get(prev_date, 0)  # subtract the FROM day's gain, not the TO day's
        result[d] = total
        prev_date = d

    return [{"date": d, "total": result[d]} for d in sorted(result)]


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

DEMO_LABELS = {
    "job_title": "Job Title",
    "seniority": "Seniority",
    "location": "Location",
    "industry": "Industry",
    "function": "Function",
    "company_size": "Company Size",
    "company": "Company",
}


def generate_html(data: dict, pic_b64: str) -> str:
    snap = data["snapshot"]
    imp_daily = data["imp_daily"]
    eng_daily = data["eng_daily"]
    fol_daily = data["fol_daily"]
    cumulative = data["cumulative"]
    demo = data["demo"]

    # Align all daily series to a shared date axis
    all_dates = sorted(
        set(
            [r["date"] for r in imp_daily]
            + [r["date"] for r in eng_daily]
            + [r["date"] for r in fol_daily]
        )
    )

    def series(rows, field):
        lookup = {r["date"]: r[field] for r in rows}
        return [lookup.get(d) for d in all_dates]

    cum_lookup = {r["date"]: r["total"] for r in cumulative}
    cum_series = [cum_lookup.get(d) for d in all_dates]

    # Format date labels nicely (Feb 24, Mar 1, …)
    def fmt_date(d: str) -> str:
        try:
            dt = datetime.date.fromisoformat(d)
            return dt.strftime("%-d %b")
        except Exception:
            return d

    labels_js = json.dumps([fmt_date(d) for d in all_dates])
    imp_js = json.dumps(series(imp_daily, "impressions"))
    eng_js = json.dumps(series(eng_daily, "engagements"))
    fol_new_js = json.dumps(series(fol_daily, "new_followers"))
    fol_total_js = json.dumps(cum_series)

    # KPI helpers
    def kpi(label, value, sub="", accent=False):
        cls = "kpi-value accent" if accent else "kpi-value"
        val_str = (
            f"{value:,}"
            if isinstance(value, (int, float)) and value is not None
            else (str(value) if value else "—")
        )
        return f"""<div class="kpi-card">
      <div class="kpi-label">{label}</div>
      <div class="{cls}">{val_str}</div>
      <div class="kpi-sub">{sub}</div>
    </div>"""

    # Engagement rate
    impr = snap.get("impressions") or 0
    eng = snap.get("engagements_total") or 0
    er = f"{eng / impr * 100:.2f}%" if impr else "—"

    # Period label
    if all_dates:
        period_label = f"{fmt_date(all_dates[0])} – {fmt_date(all_dates[-1])} · {len(all_dates)} days"
    else:
        period_label = snap.get("date", "")

    # Demo panels
    demo_panels_html = ""
    for key, label in DEMO_LABELS.items():
        rows = demo.get(key, [])
        if not rows:
            continue
        top_pct = rows[0]["pct"] if rows else 1
        bars = ""
        for row in rows[:6]:
            w = int(row["pct"] / top_pct * 100)
            bars += f"""<div class="demo-row">
        <div class="demo-row-header">
          <span class="demo-name">{row["label"]}</span>
          <span class="demo-pct">{row["pct"]}%</span>
        </div>
        <div class="demo-bar-bg"><div class="demo-bar-fill" style="width:{w}%"></div></div>
      </div>"""
        demo_panels_html += f"""<div class="demo-card">
    <div class="demo-title">{label}</div>
    {bars}
  </div>"""

    # Top posts grid
    import json as _json
    posts_rows = data.get('posts', [])
    if posts_rows:
        def _fmt_num(v):
            if v is None:
                return None
            n = int(v)
            return f'{n/1000:.1f}K' if n >= 1000 else str(n)

        def _fmt_dt(s):
            if not s:
                return ''
            try:
                return datetime.datetime.strptime(s, '%Y-%m-%d %H:%M:%S').strftime('%-d %b %Y')
            except Exception:
                return s[:10]

        def _esc(s):
            if not s:
                return ''
            return (s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                      .replace('"', '&quot;'))

        def _fmt_period_label(p):
            return p.replace('past_', 'Past ').replace('_days', ' Days').replace('_', ' ').title()

        posts_json_list = []
        for row in posts_rows:
            raw_text = (row.get('post_text') or row.get('snippet') or '').replace('\n', ' ').strip()
            trunc = (raw_text[:120].rsplit(' ', 1)[0] + '\u2026') if len(raw_text) > 120 else raw_text
            posts_json_list.append({
                'url':         row.get('post_url') or '',
                'text':        _esc(trunc),
                'date':        _fmt_dt(row.get('published_at') or ''),
                'image_url':   row.get('image_url') or '',
                'period':      row.get('period') or 'past_7_days',
                'impressions': row.get('impressions'),
                'engagements': row.get('engagements'),
                'reactions':   row.get('reactions'),
                'comments':    row.get('comments'),
            })
        posts_json = _json.dumps(posts_json_list)

        periods_available = data.get('periods_available', [])
        default_period    = data.get('default_period', 'past_7_days')

        _period_labels = {
            'past_7_days':  '7 Days',
            'past_28_days': '28 Days',
            'past_90_days': '90 Days',
        }
        period_opts_html = ''
        for _p in ['past_7_days', 'past_28_days', 'past_90_days']:
            if _p in periods_available:
                _selected = ' selected' if _p == default_period else ''
                _lbl = _period_labels.get(_p, _p)
                period_opts_html += f'<option value="{_p}"{_selected}>{_lbl}</option>'

        posts_section_html = f'''<div class="section-label">Top Posts</div>
<div class="posts-section">
  <div class="posts-controls">
    <div class="ctrl-group">
      <label class="ctrl-label">Period</label>
      <select id="period-select" class="ctrl-select">{period_opts_html}</select>
    </div>
    <div class="ctrl-group">
      <label class="ctrl-label">Sort by</label>
      <select id="sort-select" class="ctrl-select">
        <option value="impressions" selected>Impressions</option>
        <option value="engagements">Engagements</option>
        <option value="reactions">Reactions</option>
        <option value="comments">Comments</option>
      </select>
    </div>
    <div class="ctrl-group">
      <label class="ctrl-label">View</label>
      <select id="view-select" class="ctrl-select">
        <option value="grid" selected>Grid</option>
        <option value="list">List</option>
      </select>
    </div>
  </div>
  <div id="posts-grid"></div>
</div>
<script>
(function(){{
  var POSTS_DATA = {posts_json};
  var state = {{ period: '{default_period}', sort: 'impressions', view: 'grid' }};
  function fmtNum(n) {{
    if (n == null) return '\u2014';
    return n >= 1000 ? (n/1000).toFixed(1) + 'K' : String(n);
  }}
  function renderPosts() {{
    var filtered = POSTS_DATA.filter(function(p){{ return p.period === state.period; }});
    var sorted   = filtered.slice().sort(function(a,b){{ return (b[state.sort]||0)-(a[state.sort]||0); }});
    var top25    = sorted.slice(0, 25);
    var el = document.getElementById('posts-grid');
    if (state.view === 'list') {{
      el.className = 'posts-list';
      el.innerHTML = top25.map(function(p, i) {{
        return '<a class="post-list-item" href="'+p.url+'" target="_blank" rel="noopener">'
          + '<span class="list-rank">'+(i+1)+'</span>'
          + '<span class="list-text">'+p.text+'</span>'
          + '<span class="list-date">'+p.date+'</span>'
          + '<span class="list-metric"><span class="list-metric-val">'+fmtNum(p.impressions)+'</span><span class="list-metric-lbl">Impr</span></span>'
          + '<span class="list-metric"><span class="list-metric-val">'+fmtNum(p.engagements)+'</span><span class="list-metric-lbl">Eng</span></span>'
          + '<span class="list-metric"><span class="list-metric-val">'+fmtNum(p.reactions)+'</span><span class="list-metric-lbl">React</span></span>'
          + '<span class="list-metric"><span class="list-metric-val">'+fmtNum(p.comments)+'</span><span class="list-metric-lbl">Cmnt</span></span>'
          + '</a>';
      }}).join('');
    }} else {{
      el.className = 'posts-grid';
      el.innerHTML = top25.map(function(p) {{
        var thumb = p.image_url
          ? '<div class="post-card-thumb"><img src="'+p.image_url+'" loading="lazy"></div>'
          : '<div class="post-card-thumb"></div>';
        var breakdown = (p.reactions!=null||p.comments!=null)
          ? '<span class="post-metric-breakdown">'+fmtNum(p.reactions)+' reactions \u00b7 '+fmtNum(p.comments)+' comments</span>'
          : '';
        return '<a class="post-card" href="'+p.url+'" target="_blank" rel="noopener">'
          + thumb
          + '<div class="post-card-body">'
          +   '<div class="post-card-text">'+p.text+'</div>'
          +   '<div class="post-card-date">'+p.date+'</div>'
          +   '<div class="post-card-metrics">'
          +     '<div class="post-metric-primary">'
          +       '<span class="post-metric-val">'+fmtNum(p.impressions)+'</span>'
          +       '<span class="post-metric-lbl">Impressions</span>'
          +     '</div>'
          +     '<div class="post-metric-secondary">'
          +       '<span class="post-metric-val">'+fmtNum(p.engagements)+'</span>'
          +       '<span class="post-metric-lbl">Engagements</span>'
          +       breakdown
          +     '</div>'
          +   '</div>'
          + '</div>'
          + '</a>';
      }}).join('');
    }}
  }}
  document.getElementById('period-select').addEventListener('change', function(){{
    state.period = this.value;
    renderPosts();
  }});
  document.getElementById('sort-select').addEventListener('change', function(){{
    state.sort = this.value;
    renderPosts();
  }});
  document.getElementById('view-select').addEventListener('change', function(){{
    state.view = this.value;
    renderPosts();
  }});
  renderPosts();
}})()\n</script>'''
    else:
        posts_section_html = ''
    scraped = snap.get("date", datetime.date.today().isoformat())

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>LinkedIn Analytics — Ali Alfredji</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  :root {{
    --bg:      #0D0D0D; --surface: #141414; --border: #242424;
    --text:    #F0EDE8; --muted:   #555;
    --accent:  #E8D5B0; --green:   #6FCF97; --blue:   #5B9CF6;
  }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: 'IBM Plex Sans', sans-serif;
    padding: 44px 48px; max-width: 1200px; margin: 0 auto; line-height: 1.5;
  }}

  /* HEADER */
  .header {{
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 48px; padding-bottom: 28px; border-bottom: 1px solid var(--border);
  }}
  .header-left {{ display: flex; align-items: center; gap: 18px; }}
  .avatar {{ width: 48px; height: 48px; border-radius: 50%; border: 2px solid var(--accent); object-fit: cover; }}
  .header-name {{ font-family: 'IBM Plex Mono', monospace; font-size: 17px; font-weight: 600; }}
  .header-sub  {{ font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--muted); margin-top: 3px; }}
  .header-meta {{ font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--muted); text-align: right; line-height: 2; }}
  .header-meta span {{ color: var(--accent); }}

  /* SECTION LABEL */
  .section-label {{
    font-family: 'IBM Plex Mono', monospace; font-size: 10px; font-weight: 500;
    letter-spacing: 2.5px; text-transform: uppercase; color: var(--muted); margin-bottom: 16px;
  }}

  /* KPI */
  .kpi-grid {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin-bottom: 44px; }}
  .kpi-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 18px 16px; }}
  .kpi-label {{ font-size: 10px; color: var(--muted); font-family: 'IBM Plex Mono', monospace; letter-spacing: 0.4px; text-transform: uppercase; margin-bottom: 10px; }}
  .kpi-value {{ font-family: 'IBM Plex Mono', monospace; font-size: 24px; font-weight: 600; line-height: 1; }}
  .kpi-value.accent {{ color: var(--accent); }}
  .kpi-sub {{ font-size: 10px; color: var(--muted); margin-top: 5px; }}

  /* CHART CARDS */
  .chart-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 24px; }}
  .chart-title {{ font-family: 'IBM Plex Mono', monospace; font-size: 12px; font-weight: 500; margin-bottom: 3px; }}
  .chart-sub   {{ font-size: 11px; color: var(--muted); margin-bottom: 20px; }}
  .chart-wrap  {{ position: relative; height: 200px; }}
  .chart-wrap.tall {{ height: 260px; }}

  .row-full {{ margin-bottom: 14px; }}
  .row-half {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-bottom: 44px; }}

  /* DEMOGRAPHICS */
  .demo-grid {{
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 14px; margin-bottom: 44px;
  }}
  .demo-card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }}
  .demo-title {{ font-family: 'IBM Plex Mono', monospace; font-size: 10px; font-weight: 500; letter-spacing: 1.5px; text-transform: uppercase; color: var(--muted); margin-bottom: 16px; }}
  .demo-row {{ margin-bottom: 11px; }}
  .demo-row-header {{ display: flex; justify-content: space-between; margin-bottom: 4px; }}
  .demo-name {{ font-size: 12px; }}
  .demo-pct  {{ font-family: 'IBM Plex Mono', monospace; font-size: 12px; color: var(--accent); font-weight: 500; }}
  .demo-bar-bg   {{ height: 2px; background: var(--border); border-radius: 2px; overflow: hidden; }}
  .demo-bar-fill {{ height: 100%; background: var(--accent); opacity: 0.65; border-radius: 2px; }}

  /* TOP POSTS GRID */
  .posts-section {{ margin-bottom: 44px; }}
  .posts-controls {{ display: flex; gap: 16px; margin-bottom: 20px; align-items: flex-end; flex-wrap: wrap; }}
  .ctrl-group {{ display: flex; flex-direction: column; gap: 5px; }}
  .ctrl-label {{ font-family: 'IBM Plex Mono', monospace; font-size: 9px; letter-spacing: 1.5px; text-transform: uppercase; color: var(--muted); }}
  .ctrl-select {{
    font-family: 'IBM Plex Mono', monospace; font-size: 11px; font-weight: 500;
    padding: 6px 28px 6px 10px; border-radius: 4px; cursor: pointer;
    background: var(--surface); border: 1px solid var(--border);
    color: var(--text); appearance: none; -webkit-appearance: none;
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23555'/%3E%3C/svg%3E");
    background-repeat: no-repeat; background-position: right 8px center;
    transition: border-color 150ms;
  }}
  .ctrl-select:hover {{ border-color: var(--accent); }}
  .ctrl-select:focus {{ outline: none; border-color: var(--blue); }}
  .posts-grid {{
    display: grid; grid-template-columns: repeat(3, 1fr);
    gap: 12px; margin-bottom: 44px;
  }}
  .posts-list {{
    display: flex; flex-direction: column; gap: 6px; margin-bottom: 44px;
  }}
  .post-list-item {{
    display: flex; align-items: center; gap: 12px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; padding: 10px 14px;
    text-decoration: none; color: inherit;
    transition: border-color 150ms;
  }}
  .post-list-item:hover {{ border-color: var(--accent); }}
  .list-rank {{
    font-family: 'IBM Plex Mono', monospace; font-size: 11px; color: var(--muted);
    width: 20px; flex-shrink: 0; text-align: right;
  }}
  .list-text {{
    font-size: 12px; flex: 1; min-width: 0;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    color: var(--text);
  }}
  .list-date {{
    font-family: 'IBM Plex Mono', monospace; font-size: 10px; color: var(--muted);
    flex-shrink: 0; width: 80px; text-align: right;
  }}
  .list-metric {{
    flex-shrink: 0; text-align: right; min-width: 52px;
    display: flex; flex-direction: column; align-items: flex-end; gap: 1px;
  }}
  .list-metric-val {{
    font-family: 'IBM Plex Mono', monospace; font-size: 12px; font-weight: 600;
    color: var(--accent);
  }}
  .list-metric-lbl {{
    font-family: 'IBM Plex Mono', monospace; font-size: 9px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.5px;
  }}
  .post-card {{
    display: flex; flex-direction: column;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: 8px; overflow: hidden;
    text-decoration: none; color: inherit;
    cursor: pointer; transition: transform 180ms ease, border-color 180ms ease;
  }}
  .post-card:hover {{ transform: scale(1.02); border-color: var(--accent); }}
  .post-card-thumb {{ width: 100%; aspect-ratio: 16/9; overflow: hidden; background: #1a1a1a; }}
  .post-card-thumb img {{ width: 100%; height: 100%; object-fit: cover; display: block; }}
  .post-card-body {{ padding: 14px; display: flex; flex-direction: column; gap: 8px; flex: 1; }}
  .post-card-text {{
    font-size: 12px; color: var(--text); line-height: 1.5;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
  }}
  .post-card-date {{ font-family: 'IBM Plex Mono', monospace; font-size: 10px; color: var(--muted); }}
  .post-card-metrics {{
    display: flex; gap: 20px; margin-top: auto; padding-top: 10px;
    border-top: 1px solid var(--border);
  }}
  .post-metric-primary .post-metric-val {{ color: var(--accent); }}
  .post-metric-secondary .post-metric-val {{ color: var(--green); }}
  .post-metric-val {{
    font-family: 'IBM Plex Mono', monospace; font-size: 20px; font-weight: 600;
    display: block; line-height: 1.1;
  }}
  .post-metric-lbl {{
    font-family: 'IBM Plex Mono', monospace; font-size: 10px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.4px;
  }}
  .post-metric-breakdown {{ font-size: 10px; color: var(--muted); display: block; margin-top: 2px; }}
  .footer {{ border-top: 1px solid var(--border); padding-top: 24px; display: flex; align-items: center; gap: 12px; }}
  .footer img {{ width: 26px; height: 26px; border-radius: 50%; object-fit: cover; border: 1px solid var(--border); }}
  .footer-text {{ font-family: 'IBM Plex Mono', monospace; font-size: 10px; color: var(--muted); }}
  .footer-text span {{ color: var(--accent); }}
  /* FILTER BAR */
  .filter-bar {{ display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin-bottom:32px; }}
  .filter-btn {{
    font-family: 'IBM Plex Mono', monospace;
    font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase;
    padding: 6px 14px; border-radius: 20px;
    border: 1px solid var(--border);
    background: transparent; color: var(--muted);
    cursor: pointer; transition: all 0.15s ease;
  }}
  .filter-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
  .filter-btn.active {{ background: var(--accent); color: #0D0D0D; border-color: var(--accent); font-weight: 600; }}
</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <img class="avatar" src="data:image/jpeg;base64,{pic_b64}" alt="Ali">
    <div>
      <div class="header-name">Ali Alfredji</div>
      <div class="header-sub">linkedin.com/in/alialfredji</div>
    </div>
  </div>
  <div class="header-meta">
    <div>History <span>{period_label}</span></div>
    <div>Last scrape <span>{scraped}</span></div>
  </div>
</div>
<div class="filter-bar">
  <button class="filter-btn" onclick="setWindow(7)">7d</button>
  <button class="filter-btn" onclick="setWindow(14)">14d</button>
  <button class="filter-btn" onclick="setWindow(28)">28d</button>
  <button class="filter-btn" onclick="setWindow(90)">90d</button>
  <button class="filter-btn" onclick="setWindow(null)">All</button>
</div>

<div class="section-label">Snapshot — {scraped}</div>
<div class="kpi-grid">
  {kpi("Impressions", snap.get("impressions"), "7-day total", accent=True)}
  {kpi("Members Reached", snap.get("members_reached"), "unique viewers")}
  {kpi("Followers Total", snap.get("followers_total"), "running total")}
  {kpi("New Followers", snap.get("new_followers_period"), "this period")}
  {kpi("Engagements", snap.get("engagements_total"), f"ER {er}")}
  {kpi("Profile Views", snap.get("profile_views_90d"), "past 90 days")}
</div>

<div class="section-label">Follower Growth</div>
<div class="row-full">
  <div class="chart-card">
    <div class="chart-title">Follower Growth</div>
    <div class="chart-sub">Daily new followers (bars) · Running total (line) · All available history</div>
    <div class="chart-wrap tall"><canvas id="folChart"></canvas></div>
  </div>
</div>

<div class="row-half">
  <div class="chart-card">
    <div class="chart-title">Daily Impressions</div>
    <div class="chart-sub">All available history</div>
    <div class="chart-wrap"><canvas id="impChart"></canvas></div>
  </div>
  <div class="chart-card">
    <div class="chart-title">Daily Engagements</div>
    <div class="chart-sub">All available history</div>
    <div class="chart-wrap"><canvas id="engChart"></canvas></div>
  </div>
</div>

{posts_section_html}

<div class="section-label">Audience Demographics</div>
<div class="demo-grid">
  {demo_panels_html}
</div>

<div class="footer">
  <img src="data:image/jpeg;base64,{pic_b64}" alt="Ali">
  <div class="footer-text">Ali Alfredji · <span>linkedin-analytics</span> · Generated {scraped}</div>
</div>

<script>
const ALL_LABELS  = {labels_js};
const ALL_IMP     = {imp_js};
const ALL_ENG     = {eng_js};
const ALL_FOL_NEW = {fol_new_js};
const ALL_FOL_TOT = {fol_total_js};

const baseOpts = (yRight) => ({{
  responsive: true,
  maintainAspectRatio: false,
  interaction: {{ mode: 'index', intersect: false }},
  plugins: {{
    legend: {{ display: yRight, labels: {{ color: '#666', font: {{ family: 'IBM Plex Mono', size: 10 }}, boxWidth: 10, padding: 16 }} }},
    tooltip: {{
      backgroundColor: '#1A1A1A', borderColor: '#2A2A2A', borderWidth: 1,
      titleColor: '#F0EDE8', bodyColor: '#E8D5B0',
      titleFont: {{ family: 'IBM Plex Mono', size: 11 }},
      bodyFont:  {{ family: 'IBM Plex Mono', size: 11 }},
      padding: 10,
    }}
  }},
  scales: {{
    x: {{ grid: {{ color: '#1C1C1C' }}, ticks: {{ color: '#555', font: {{ family: 'IBM Plex Mono', size: 10 }} }} }},
    y: {{ grid: {{ color: '#1C1C1C' }}, ticks: {{ color: '#555', font: {{ family: 'IBM Plex Mono', size: 10 }} }}, border: {{ display: false }} }},
    ...(yRight ? {{
      y2: {{
        position: 'right',
        grid: {{ drawOnChartArea: false }},
        ticks: {{ color: '#6FCF97', font: {{ family: 'IBM Plex Mono', size: 10 }} }},
        border: {{ display: false }},
      }}
    }} : {{}})
  }}
}});

// Follower growth — dual axis (new followers bars + cumulative total line)
const chartFol = new Chart(document.getElementById('folChart'), {{
  data: {{
                labels: ALL_LABELS,
    datasets: [
      {{
        type: 'bar',
        label: 'New followers',
        data: ALL_FOL_NEW,
        backgroundColor: 'rgba(232,213,176,0.5)',
        borderColor: '#E8D5B0',
        borderWidth: 1,
        borderRadius: 3,
        borderSkipped: false,
        yAxisID: 'y',
        order: 2,
      }},
      {{
        type: 'line',
        label: 'Total followers',
        data: ALL_FOL_TOT,
        borderColor: '#6FCF97',
        backgroundColor: 'rgba(111,207,151,0.08)',
        borderWidth: 2,
        pointBackgroundColor: '#6FCF97',
        pointRadius: 4,
        tension: 0.3,
        fill: true,
        yAxisID: 'y2',
        order: 1,
        spanGaps: true,
      }},
    ]
  }},
  options: baseOpts(true),
}});

// Impressions
const chartImp = new Chart(document.getElementById('impChart'), {{
  type: 'bar',
  data: {{
                labels: ALL_LABELS,
    datasets: [{{
      data: ALL_IMP,
      backgroundColor: '#E8D5B0',
      borderRadius: 3,
      borderSkipped: false,
    }}]
  }},
  options: baseOpts(false),
}});

// Engagements
const chartEng = new Chart(document.getElementById('engChart'), {{
  type: 'line',
  data: {{
                labels: ALL_LABELS,
    datasets: [{{
      data: ALL_ENG,
      borderColor: '#5B9CF6',
      backgroundColor: 'rgba(91,156,246,0.08)',
      borderWidth: 2,
      pointBackgroundColor: '#5B9CF6',
      pointRadius: 4,
      tension: 0.3,
      fill: true,
    }}]
  }},
  options: baseOpts(false),
}});
function setWindow(n) {{
  const len = ALL_LABELS.length;
  const start = (n === null || n >= len) ? 0 : len - n;
  const sl = (arr) => arr ? arr.slice(start) : arr;
  const slicedLabels = ALL_LABELS.slice(start);
  [chartFol, chartImp, chartEng].forEach(c => {{ c.data.labels = slicedLabels; }});
  chartFol.data.datasets[0].data = sl(ALL_FOL_NEW);
  chartFol.data.datasets[1].data = sl(ALL_FOL_TOT);
  chartImp.data.datasets[0].data = sl(ALL_IMP);
  chartEng.data.datasets[0].data = sl(ALL_ENG);
  chartFol.update(); chartImp.update(); chartEng.update();
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  const lbl = n === null ? 'All' : n + 'd';
  const target = [...document.querySelectorAll('.filter-btn')].find(b => b.textContent === lbl);
  if (target) target.classList.add('active');
}}
// Default: last 7 days if available, else show all
setWindow(ALL_LABELS.length >= 7 ? 7 : null);
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description="Generate LinkedIn analytics dashboard from SQLite DB"
    )
    ap.add_argument("--db", default=DEFAULT_DB, help="Path to linkedin.db")
    ap.add_argument("--out", default=DEFAULT_OUT, help="Output HTML file path")
    ap.add_argument("--no-open", action="store_true", help="Don't auto-open in browser")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}", file=sys.stderr)
        print("Run: python extract.py --output sqlite", file=sys.stderr)
        sys.exit(1)

    print(f"Reading data from {args.db} …")
    data = load_data(args.db)

    snap = data["snapshot"]
    days = len(
        set(
            [r["date"] for r in data["imp_daily"]]
            + [r["date"] for r in data["fol_daily"]]
        )
    )
    print(f"  Latest snapshot : {snap.get('date', 'none')}")
    print(f"  History         : {days} days of time-series data")
    print(f"  Follower history: {len(data['cumulative'])} data points reconstructed")

    pic_b64 = ""
    if os.path.exists(PROFILE_PIC):
        with open(PROFILE_PIC, "rb") as f:
            pic_b64 = base64.b64encode(f.read()).decode()

    html = generate_html(data, pic_b64)

    with open(args.out, "w") as f:
        f.write(html)
    print(f"Dashboard written → {args.out}")

    if not args.no_open:
        subprocess.run(["open", args.out])


if __name__ == "__main__":
    main()
