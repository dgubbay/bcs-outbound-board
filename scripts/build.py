#!/usr/bin/env python3
"""BCS Outbound TV Board — pulls this week's calls + meetings from HubSpot and
renders a static, self-contained dist/index.html for the office TV.

Env: HUBSPOT_TOKEN (private app token, read scopes: crm.objects.contacts.read,
crm.objects.owners.read). Optional: FIXTURE=path.json to render from saved data.
"""
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("America/Denver")
API = "https://api.hubapi.com"

# Display order is re-sorted by dials; this is just the roster.
REPS = {
    "99808543": "Wyatt Ison",
    "99960123": "Hunter Wooten",
    "99808544": "Thomas Gubbay",
    "135846867": "Rick Smith",
    "14324253": "David Gubbay",
}

CONNECTED = "f240bbac-87c9-4f6e-bf70-924b57d47db7"
REAL_CONNECT_MIN_MS = 120_000   # 2 min — screens out gatekeepers / hang-ups
REAL_CONNECT_MAX_MS = 900_000   # 15 min — longer = scheduled demo, counted as meeting
DIALS_PER_DAY = 60              # 300 / week
MEETINGS_PER_WEEK = 4
MEETING_EXCLUDE = ("interview", "round", "role discussion", "training", "test", "hold:", "internal", "1:1", "1 on 1")


# ---------------------------------------------------------------- HubSpot
def search(token, obj, filters, props):
    out, after = [], None
    while True:
        body = {"filterGroups": [{"filters": filters}], "properties": props, "limit": 200}
        if after:
            body["after"] = after
        for attempt in range(5):
            r = requests.post(f"{API}/crm/v3/objects/{obj}/search", json=body,
                              headers={"Authorization": f"Bearer {token}"}, timeout=30)
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            r.raise_for_status()
            break
        data = r.json()
        out.extend(x["properties"] for x in data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            return out
        time.sleep(0.25)  # search API: ~4 req/s


def fetch(token, week_start_utc):
    owners = {"propertyName": "hubspot_owner_id", "operator": "IN", "values": list(REPS)}
    iso = week_start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    calls = search(token, "calls",
                   [owners, {"propertyName": "hs_timestamp", "operator": "GTE", "value": iso}],
                   ["hubspot_owner_id", "hs_timestamp", "hs_call_duration",
                    "hs_call_disposition", "hs_call_direction"])
    meetings = search(token, "meetings",
                      [owners, {"propertyName": "hs_createdate", "operator": "GTE", "value": iso}],
                      ["hubspot_owner_id", "hs_createdate", "hs_meeting_start_time",
                       "hs_meeting_title", "hs_meeting_source"])
    return calls, meetings


# ---------------------------------------------------------------- math
def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def week_window(now_local):
    monday = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return monday


def is_prospect_meeting(m):
    title = (m.get("hs_meeting_title") or "").lower()
    if any(k in title for k in MEETING_EXCLUDE):
        return False
    if m.get("hs_meeting_source") == "INTEGRATION":  # auto-logged Zoom/Teams after the fact
        return False
    start, created = ts(m.get("hs_meeting_start_time")), ts(m.get("hs_createdate"))
    return bool(start and created and start > created)  # booked ahead, not logged after


def compute(calls, meetings, monday, now_local):
    rows = {oid: {"name": n, "days": [0] * 5, "dials": 0, "talk_ms": 0,
                  "connects": 0, "tagged": 0, "meetings": 0} for oid, n in REPS.items()}
    for c in calls:
        r = rows.get(c.get("hubspot_owner_id"))
        t = ts(c.get("hs_timestamp"))
        if not r or not t or c.get("hs_call_direction") == "INBOUND":
            continue
        d = (t.astimezone(TZ).date() - monday.date()).days
        if 0 <= d < 5:
            r["days"][d] += 1
        r["dials"] += 1
        dur = int(float(c.get("hs_call_duration") or 0))
        if c.get("hs_call_disposition") == CONNECTED:
            r["tagged"] += 1
            r["talk_ms"] += dur
            if REAL_CONNECT_MIN_MS <= dur <= REAL_CONNECT_MAX_MS:
                r["connects"] += 1
    for m in meetings:
        r = rows.get(m.get("hubspot_owner_id"))
        if r and is_prospect_meeting(m):
            r["meetings"] += 1
    if now_local.weekday() >= 5:
        biz_days = 5.0
    else:  # full days so far + share of today's 8am–5pm window
        frac = min(1.0, max(0.0, (now_local.hour + now_local.minute / 60 - 8) / 9))
        biz_days = max(0.25, now_local.weekday() + frac)
    return sorted(rows.values(), key=lambda r: (-r["dials"], r["name"])), biz_days


# ---------------------------------------------------------------- render
def pace_class(actual, target):
    if target <= 0:
        return "ok"
    p = actual / target
    return "good" if p >= 1 else "warn" if p >= 0.75 else "bad"


def hm(ms):
    m = int(ms // 60000)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def render(rows, biz_days, monday, now_local):
    today_idx = now_local.weekday() if now_local.weekday() < 5 else -1
    dial_target = round(DIALS_PER_DAY * biz_days)
    mtg_target = round(MEETINGS_PER_WEEK * biz_days / 5, 1)
    max_day = max([1] + [d for r in rows for d in r["days"]])
    labels = ["M", "T", "W", "T", "F"]

    trs = []
    for r in rows:
        bars = []
        for i, v in enumerate(r["days"]):
            h = round(100 * v / max_day) if v else 0
            cls = "bar today" if i == today_idx else "bar future" if today_idx != -1 and i > today_idx else "bar"
            bars.append(f'<div class="{cls}"><span class="v">{v if (today_idx == -1 or i <= today_idx) else ""}</span>'
                        f'<i style="height:{h}%"></i><span class="d">{labels[i]}</span></div>')
        pct = round(100 * r["connects"] / r["dials"]) if r["dials"] else 0
        trs.append(f"""
<div class="row">
  <div class="name">{html.escape(r['name'])}</div>
  <div class="dials"><div class="bars">{''.join(bars)}</div>
    <div class="total {pace_class(r['dials'], dial_target)}"><b>{r['dials']}</b><small>pace target {dial_target}</small></div></div>
  <div class="metric"><b>{hm(r['talk_ms'])}</b><small>talk time</small></div>
  <div class="metric"><b>{pct}%</b><small>{r['connects']} real connect{'' if r['connects'] == 1 else 's'}</small></div>
  <div class="metric {pace_class(r['meetings'], mtg_target)}"><b>{r['meetings']}</b><small>of {MEETINGS_PER_WEEK} / wk</small></div>
</div>""")

    tot_d = sum(r["dials"] for r in rows)
    tot_c = sum(r["connects"] for r in rows)
    tot_m = sum(r["meetings"] for r in rows)
    tot_t = sum(r["talk_ms"] for r in rows)
    updated = now_local.strftime("%-I:%M %p")
    week = f"Week of {monday.strftime('%b %-d')}"
    gen_epoch = int(now_local.timestamp())

    tpl = Path(__file__).with_name("template.html").read_text()
    return (tpl.replace("{{ROWS}}", "".join(trs))
               .replace("{{WEEK}}", week)
               .replace("{{UPDATED}}", updated)
               .replace("{{GEN}}", str(gen_epoch))
               .replace("{{TOT_DIALS}}", str(tot_d))
               .replace("{{TOT_TALK}}", hm(tot_t))
               .replace("{{TOT_PCT}}", f"{round(100 * tot_c / tot_d) if tot_d else 0}%")
               .replace("{{TOT_MTG}}", str(tot_m)))


def main():
    now_local = datetime.now(TZ)
    monday = week_window(now_local)
    if os.environ.get("FIXTURE"):
        data = json.loads(Path(os.environ["FIXTURE"]).read_text())
        calls, meetings = data["calls"], data["meetings"]
    else:
        token = os.environ.get("HUBSPOT_TOKEN")
        if not token:
            sys.exit("HUBSPOT_TOKEN not set")
        calls, meetings = fetch(token, monday.astimezone(timezone.utc))
    rows, biz_days = compute(calls, meetings, monday, now_local)
    out = Path("dist")
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(render(rows, biz_days, monday, now_local))
    (out / ".nojekyll").write_text("")
    print(json.dumps({"calls": len(calls), "meetings": len(meetings),
                      "rows": [{k: r[k] for k in ("name", "dials", "connects", "talk_ms", "meetings")} for r in rows]},
                     indent=1))


if __name__ == "__main__":
    main()
