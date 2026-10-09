#!/usr/bin/env python3
"""BCS Outbound TV Board — pulls outbound activity, inbound routing and open
pipeline from HubSpot and renders a self-contained, auto-rotating
dist/index.html for the office TV.

Slides: Weekly Outbound Board → 30-Day Outbound Board → one pipeline page per rep.

Env: HUBSPOT_TOKEN (HubSpot service key; read scopes: crm.objects.contacts.read,
crm.objects.owners.read, crm.objects.deals.read). Optional: FIXTURE=path.json to
render from saved data instead of calling HubSpot.
"""
import html
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("America/Denver")
API = "https://api.hubapi.com"

# Roster (HubSpot owner ID → name). Also the order of the pipeline pages.
# David Gubbay (14324253) is the sales manager — intentionally not on the board.
REPS = {
    "99808543": "Wyatt Ison",
    "99960123": "Hunter Wooten",
    "99808544": "Thomas Gubbay",
    "135846867": "Rick Smith",
}

CONNECTED = "f240bbac-87c9-4f6e-bf70-924b57d47db7"
REAL_CONNECT_MIN_MS = 120_000   # 2 min — screens out gatekeepers / hang-ups
REAL_CONNECT_MAX_MS = 900_000   # 15 min — longer = scheduled demo, counted as meeting
DIALS_PER_DAY = 60              # 300 / week
MEETINGS_PER_WEEK = 4
MEETING_EXCLUDE = ("interview", "round", "role discussion", "training", "test", "hold:",
                   "internal", "1:1", "1 on 1")
ROLLING_DAYS = 30

SLIDE_SECONDS_BOARD = 30
SLIDE_SECONDS_30D = 20
SLIDE_SECONDS_PIPE = 25

# Pipeline columns, left → right. Expansion/Upsell pipeline stages fold into the equivalent column.
STAGES = [
    ("Qualified", {"2213233a-87bf-4515-9f74-3cc6352bb30d", "1296150575"}),
    ("Demo", {"7ca6506e-73ec-4c63-bd22-b8d46cfad227", "1296150576"}),
    ("Proposal Sent", {"a3cb6831-d42b-442b-b53c-9e3f07cf09b2", "1296150577"}),
    ("Decision Making", {"210467244", "1296150578"}),
    ("Contract Review", {"a2ebaf9d-9fa7-4e07-9ec7-32194337add3"}),
    ("Delayed", {"1299512469"}),
]
EXPANSION_PIPELINE = "866294330"
STAGE_RGB = {                       # hottest (closest to signature) → coolest
    "Contract Review": (255, 77, 94),
    "Decision Making": (255, 118, 64),
    "Proposal Sent": (255, 156, 52),
    "Demo": (254, 192, 60),
    "Qualified": (253, 225, 75),
    "Delayed": (140, 134, 180),
    "Won": (61, 220, 132),
    "Lost": (140, 134, 180),
}
CLOSED_STAGES = {
    "Won": {"242932c5-7c92-4aea-92ab-d2bbab06d462", "1296150580", "27199892"},
    "Lost": {"25636d46-96b1-49d3-b3cc-b031632ef786", "3810ebed-c181-442b-a564-01c1f7d53bb7", "1296150581"},
}
SLIDE_SECONDS_NEWDEALS = 20
NEW_DEALS_DAYS = 7
NEW_DEALS_PER_REP = 9


def stage_label(stage_id):
    for lbl, ids in STAGES:
        if stage_id in ids:
            return lbl
    for lbl, ids in CLOSED_STAGES.items():
        if stage_id in ids:
            return lbl
    return "Other"


def rgb(lbl, a=1.0):
    r, g, b = STAGE_RGB.get(lbl, (169, 163, 214))
    return f"rgba({r},{g},{b},{a})"
CARDS_PER_STAGE = 6
STALE_DAYS = 14


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


def assoc(token, frm, to, ids):
    """{from_id: [to_id, ...]} via the v4 batch associations API."""
    out = {}
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 1000):
        r = requests.post(f"{API}/crm/v4/associations/{frm}/{to}/batch/read",
                          json={"inputs": [{"id": x} for x in ids[i:i + 1000]]},
                          headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        for row in r.json().get("results", []):
            out[str(row["from"]["id"])] = [str(t["toObjectId"]) for t in row.get("to", [])]
    return out


def deal_createdates(token, ids):
    out = {}
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 100):
        r = requests.post(f"{API}/crm/v3/objects/deals/batch/read",
                          json={"properties": ["createdate"], "inputs": [{"id": x} for x in ids[i:i + 100]]},
                          headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        for d in r.json().get("results", []):
            out[str(d["id"])] = d["properties"].get("createdate")
    return out


def open_deal_ids(token, ids):
    """Subset of deal ids that are still open (not won/lost)."""
    out = set()
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 100):
        r = requests.post(f"{API}/crm/v3/objects/deals/batch/read",
                          json={"properties": ["hs_is_closed"], "inputs": [{"id": x} for x in ids[i:i + 100]]},
                          headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        for d in r.json().get("results", []):
            if (d["properties"].get("hs_is_closed") or "false") != "true":
                out.add(str(d["id"]))
    return out


def calls_on_live_deals(token, calls):
    """Ids of calls whose contact has an open deal — that's deal work, not outbound prospecting."""
    ids = [c["hs_object_id"] for c in calls if c.get("hs_object_id")]
    if not ids:
        return []
    try:
        c2ct = assoc(token, "calls", "contacts", ids)
        contacts = {x for v in c2ct.values() for x in v}
        ct2d = assoc(token, "contacts", "deals", contacts) if contacts else {}
        live = open_deal_ids(token, {d for v in ct2d.values() for d in v})
    except requests.HTTPError as e:
        print(f"WARN: live-deal call filter unavailable ({e.response.status_code})", file=sys.stderr)
        return None
    live_contacts = {ct for ct, ds in ct2d.items() if any(d in live for d in ds)}
    return [cid for cid, cts in c2ct.items() if any(ct in live_contacts for ct in cts)]


NEW_TYPES = {"Discovery Demo"}                                   # meeting-type fallback when nothing is linked
EXISTING_TYPES = {"Follow-up Demo", "Proposal Review", "Follow up Call", "Follow-up Call"}


def classify_meetings(token, meetings):
    """meeting id → "new" (no deal existed with that contact/company when it was booked),
    "existing" (a deal already existed) or "unlinked" (no contact/company to check).
    Returns None if the key lacks the deals/companies scopes."""
    ids = [m["hs_object_id"] for m in meetings if m.get("hs_object_id")]
    if not ids:
        return {}
    try:
        m2c = assoc(token, "meetings", "contacts", ids)
        m2co = assoc(token, "meetings", "companies", ids)
        contacts = {c for v in m2c.values() for c in v}
        companies = {c for v in m2co.values() for c in v}
        c2d = assoc(token, "contacts", "deals", contacts) if contacts else {}
        co2d = assoc(token, "companies", "deals", companies) if companies else {}
        created = deal_createdates(token, {d for v in list(c2d.values()) + list(co2d.values()) for d in v})
    except requests.HTTPError as e:
        print(f"WARN: meeting classification unavailable ({e.response.status_code})", file=sys.stderr)
        return None
    out = {}
    for m in meetings:
        mid = m.get("hs_object_id")
        cs, cos = m2c.get(mid, []), m2co.get(mid, [])
        booked = ts(m.get("hs_createdate"))
        if not cs and not cos:
            t = m.get("hs_activity_type") or ""
            out[mid] = "new" if t in NEW_TYPES else "existing" if t in EXISTING_TYPES else "unlinked"
            continue
        deals = {d for c in cs for d in c2d.get(c, [])} | {d for c in cos for d in co2d.get(c, [])}
        prior = [d for d in deals if created.get(d) and booked and ts(created[d]) < booked]
        out[mid] = "existing" if prior else "new"
    return out


def fetch(token, since_local):
    """Everything since `since_local` (start of the 30-day window, which always covers this week)."""
    owners = {"propertyName": "hubspot_owner_id", "operator": "IN", "values": list(REPS)}
    iso = since_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    date_ms = str(int(datetime(since_local.year, since_local.month, since_local.day,
                               tzinfo=timezone.utc).timestamp() * 1000))

    def safe(obj, filters, props):
        # A missing scope shouldn't take the whole board down — that column shows "—".
        try:
            return search(token, obj, filters, props)
        except requests.HTTPError as e:
            print(f"WARN: {obj} query failed ({e.response.status_code})", file=sys.stderr)
            return None

    return {
        "calls": search(token, "calls",
                        [owners, {"propertyName": "hs_timestamp", "operator": "GTE", "value": iso}],
                        ["hubspot_owner_id", "hs_timestamp", "hs_call_duration",
                         "hs_call_disposition", "hs_call_direction"]),
        "meetings": search(token, "meetings",
                           [owners, {"propertyName": "hs_createdate", "operator": "GTE", "value": iso}],
                           ["hubspot_owner_id", "hs_createdate", "hs_meeting_start_time",
                            "hs_meeting_title", "hs_meeting_source", "hs_activity_type"]),
        "deals": safe("deals", [owners, {"propertyName": "createdate", "operator": "GTE", "value": iso}],
                      ["hubspot_owner_id", "createdate", "amount", "dealname", "dealstage", "pipeline"]),
        "sqls": safe("contacts", [owners, {"propertyName": "hs_v2_date_entered_salesqualifiedlead",
                                           "operator": "GTE", "value": iso}],
                     ["hubspot_owner_id", "hs_v2_date_entered_salesqualifiedlead"]),
        "freemiums": safe("contacts", [owners, {"propertyName": "freemium_sign_up_date",
                                                "operator": "GTE", "value": date_ms}],
                          ["hubspot_owner_id", "freemium_sign_up_date"]),
        "open_deals": safe("deals", [owners, {"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"}],
                           ["hubspot_owner_id", "dealname", "amount", "dealstage", "pipeline", "closedate",
                            "notes_last_updated", "hs_deal_stage_probability"]),
    }


# ---------------------------------------------------------------- math
def ts(s):
    if not s:
        return None
    if len(s) == 10:  # date-only property (e.g. freemium_sign_up_date)
        return datetime.fromisoformat(s).replace(tzinfo=TZ)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def local_day(s):
    t = ts(s)
    return t.astimezone(TZ).date() if t else None


def is_prospect_meeting(m):
    title = (m.get("hs_meeting_title") or "").lower()
    if any(k in title for k in MEETING_EXCLUDE):
        return False
    if m.get("hs_meeting_source") == "INTEGRATION":  # auto-logged Zoom/Teams after the fact
        return False
    start, created = ts(m.get("hs_meeting_start_time")), ts(m.get("hs_createdate"))
    return bool(start and created and start > created)  # booked ahead, not logged after


def business_days(start: date, now_local):
    """Weekdays from start through yesterday, plus today's share of an 8am–5pm day."""
    n, d = 0.0, start
    while d < now_local.date():
        n += d.weekday() < 5
        d += timedelta(days=1)
    if now_local.weekday() < 5:
        n += min(1.0, max(0.0, (now_local.hour + now_local.minute / 60 - 8) / 9))
    return max(0.25, n)


def compute(data, start: date, buckets, bucket_of):
    mclass = data.get("meeting_class")
    skip_calls = set(data.get("excluded_calls") or [])
    """Per-rep stats for records on/after `start`. `bucket_of(day)` → bar index or None."""
    def keep(day):
        return day is not None and day >= start

    nones = {k: data.get(k) is None for k in ("deals", "sqls", "freemiums")}
    rows = {oid: {"name": n, "bars": [0] * buckets, "dials": 0, "talk_ms": 0, "connects": 0,
                  "meetings": 0, "mtg_existing": 0, "mtg_unlinked": 0, "deal_amt": 0.0, "deal_calls": 0,
                  "deals": None if nones["deals"] else 0,
                  "sqls": None if nones["sqls"] else 0,
                  "freemiums": None if nones["freemiums"] else 0} for oid, n in REPS.items()}
    for c in data["calls"]:
        r, day = rows.get(c.get("hubspot_owner_id")), local_day(c.get("hs_timestamp"))
        if not r or not keep(day) or c.get("hs_call_direction") == "INBOUND":
            continue
        if c.get("hs_object_id") in skip_calls:
            r["deal_calls"] += 1
            continue
        b = bucket_of(day)
        if b is not None:
            r["bars"][b] += 1
        r["dials"] += 1
        dur = int(float(c.get("hs_call_duration") or 0))
        if c.get("hs_call_disposition") == CONNECTED:
            r["talk_ms"] += dur
            if REAL_CONNECT_MIN_MS <= dur <= REAL_CONNECT_MAX_MS:
                r["connects"] += 1
    for m in data["meetings"]:
        r = rows.get(m.get("hubspot_owner_id"))
        if r and keep(local_day(m.get("hs_createdate"))) and is_prospect_meeting(m):
            kind = "new" if mclass is None else mclass.get(m.get("hs_object_id"), "unlinked")
            if kind == "new":
                r["meetings"] += 1          # the headline number: brand-new prospect meetings only
            elif kind == "existing":
                r["mtg_existing"] += 1
            else:
                r["mtg_unlinked"] += 1
    for d in data.get("deals") or []:
        r = rows.get(d.get("hubspot_owner_id"))
        if r and keep(local_day(d.get("createdate"))):
            r["deals"] += 1
            r["deal_amt"] += float(d.get("amount") or 0)
    for key, prop in (("sqls", "hs_v2_date_entered_salesqualifiedlead"), ("freemiums", "freemium_sign_up_date")):
        for c in data.get(key) or []:
            r = rows.get(c.get("hubspot_owner_id"))
            if r and keep(local_day(c.get(prop))):
                r[key] += 1
    return sorted(rows.values(), key=lambda r: (-r["dials"], r["name"]))


# ---------------------------------------------------------------- render helpers
def pace_class(actual, target):
    if target <= 0:
        return "ok"
    p = actual / target
    return "good" if p >= 1 else "warn" if p >= 0.75 else "bad"


def money(v):
    if v >= 1_000_000:
        return f"${v/1e6:.2f}M"
    if v >= 100_000:
        return f"${v/1000:.0f}K"
    return f"${v/1000:.1f}K" if v >= 1000 else f"${v:,.0f}"


def num(v):
    return "—" if v is None else str(v)


def tot(rows, k):
    vals = [r[k] for r in rows]
    return None if any(v is None for v in vals) else sum(vals)


def hm(ms):
    m = int(ms // 60000)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


# ---------------------------------------------------------------- outbound board slide
def mtg_note(r, base):
    extra = []
    if r["mtg_existing"]:
        extra.append(f"+{r['mtg_existing']} on deals")
    if r["mtg_unlinked"]:
        extra.append(f"{r['mtg_unlinked']} unlinked")
    return base + ("<br>" + " · ".join(extra) if extra else "")


def render_board(rows, *, classified=True, filtered=True, key, label, dur, title, period, labels, current_idx, dial_target, mtg_target,
                 mtg_label, updated):
    peak = max([1] + [v for r in rows for v in r["bars"]])
    trs = []
    for r in rows:
        bars = []
        for i, v in enumerate(r["bars"]):
            h = round(100 * v / peak) if v else 0
            future = current_idx is not None and i > current_idx
            cls = "bar today" if i == current_idx else "bar future" if future else "bar"
            bars.append(f'<div class="{cls}"><span class="v">{"" if future else v}</span>'
                        f'<i style="height:{h}%"></i><span class="d">{labels[i]}</span></div>')
        pct = round(100 * r["connects"] / r["dials"]) if r["dials"] else 0
        trs.append(f"""
<div class="row">
  <div class="name">{html.escape(r['name'])}</div>
  <div class="dials"><div class="bars">{''.join(bars)}</div>
    <div class="total {pace_class(r['dials'], dial_target)}"><b>{r['dials']}</b><small>pace target {dial_target}</small></div></div>
  <div class="metric"><b>{hm(r['talk_ms'])}</b><small>talk time</small></div>
  <div class="metric"><b>{pct}%</b><small>{plural(r['connects'], 'real connect')}</small></div>
  <div class="metric {pace_class(r['meetings'], mtg_target)}"><b>{r['meetings']}</b><small>{mtg_note(r, mtg_label)}</small></div>
  <div class="metric pipe"><b>{num(r['deals'])}</b><small>{money(r['deal_amt']) if r['deals'] else 'no new deals'}</small></div>
  <div class="metric inb"><b>{num(r['sqls'])}</b><small>SQLs</small></div>
  <div class="metric inb"><b>{num(r['freemiums'])}</b><small>freemiums</small></div>
</div>""")
    td = sum(r["dials"] for r in rows)
    tc = sum(r["connects"] for r in rows)
    return f"""<section class="slide" data-dur="{dur}" data-key="{key}" data-label="{label}">
<header><h1><span>BCS</span> {title}</h1>
  <div class="meta">{period} · updated <b>{updated}</b> MT</div></header>
<div class="grid head"><div>Rep</div><div>Dials · total</div><div>Talk time</div><div>Connect rate</div><div>{'Prospect mtgs' if classified else 'Meetings booked'}</div><div class="hpipe">New deals</div><div class="hinb">SQLs routed</div><div class="hinb">Freemiums routed</div></div>
<div class="rows">{''.join(trs)}
  <div class="row totals">
    <div class="name">TEAM</div>
    <div class="metric"><b>{td}</b><small>dials</small></div>
    <div class="metric"><b>{hm(sum(r['talk_ms'] for r in rows))}</b><small>talk time</small></div>
    <div class="metric"><b>{round(100 * tc / td) if td else 0}%</b><small>connect rate</small></div>
    <div class="metric"><b>{sum(r['meetings'] for r in rows)}</b><small>{'prospect mtgs' if classified else 'meetings'}</small></div>
    <div class="metric pipe"><b>{num(tot(rows, 'deals'))}</b><small>{money(sum(r['deal_amt'] for r in rows))}</small></div>
    <div class="metric inb"><b>{num(tot(rows, 'sqls'))}</b><small>SQLs</small></div>
    <div class="metric inb"><b>{num(tot(rows, 'freemiums'))}</b><small>freemiums</small></div>
  </div>
</div>
<footer>
  <div>Connect = Connected call 2–15 min · {'Prospect mtg = booked with a contact/company that had no deal yet' if classified else 'Prospect-vs-deal meeting split needs deals + companies read access'} · Deals created · SQLs & freemiums by owner</div>
  <div>{f"Excludes {plural(sum(r['deal_calls'] for r in rows), 'call')} to contacts on open deals" if filtered else "Target 60 dials/day · 4 mtgs/wk"}</div>
</footer>
</section>"""


# ---------------------------------------------------------------- pipeline slide
def quarter_bounds(d):
    q0 = 3 * ((d.month - 1) // 3) + 1
    start = d.replace(month=q0, day=1)
    end = start.replace(year=start.year + 1, month=1) if q0 == 10 else start.replace(month=q0 + 3)
    return start, end, f"Q{(q0 - 1) // 3 + 1}"


def render_pipeline_quarter(deals, *, key, label, qs, qe, qlabel, today, include_overdue, updated):
    """One page, one row per rep, open deals closing in [qs, qe) broken out by stage.
    The current-quarter page also carries deals whose close date is before this quarter (overdue)."""
    head = f'''<header><h1><span>BCS</span> Pipeline · {qlabel}</h1>
  <div class="meta">{"closing this quarter" if include_overdue else "closing next quarter"} · {qs.strftime("%b %-d")} – {(qe - timedelta(days=1)).strftime("%b %-d")} · updated <b>{updated}</b> MT</div></header>'''
    tag = f'<section class="slide" data-dur="{SLIDE_SECONDS_PIPE}" data-key="{key}" data-label="{label}">'
    if deals is None:
        return tag + head + '<div class="empty"><b>Pipeline unavailable</b><small>The HubSpot key needs deals read access.</small></div></section>'
    amt = lambda d: float(d.get("amount") or 0)
    prob = lambda d: float(d.get("hs_deal_stage_probability") or 0)
    delayed_ids = STAGES[-1][1]
    open_stages = STAGES[:-1]

    def bucket(d):
        cd = local_day(d.get("closedate"))
        if cd and qs <= cd < qe:
            return "in"
        if include_overdue and cd and cd < qs:
            return "overdue"
        return None

    rows = []
    for oid, name in REPS.items():
        mine = [d for d in deals if d.get("hubspot_owner_id") == oid]
        inq = [d for d in mine if bucket(d) == "in"]
        cells = {lbl: [d for d in inq if d.get("dealstage") in ids] for lbl, ids in STAGES}
        active = [d for d in inq if d.get("dealstage") not in delayed_ids]
        overdue = [d for d in mine if bucket(d) == "overdue" and d.get("dealstage") not in delayed_ids]
        rows.append((name, cells, active, overdue))

    peak = max([1.0] + [sum(amt(d) for d in c) for _, cells, _, _ in rows for lbl, c in cells.items() if lbl != "Delayed"])

    def cell(ds, cls="", heat=True, stage=None):
        v = sum(amt(d) for d in ds)
        alpha = 1 if heat and v and stage else 0   # solid stage color: red (contract) → yellow (qualified)
        style = f' style="background:{rgb(stage, alpha)};color:#161041"' if alpha else ""
        if stage and not alpha and ds:
            style = f' style="color:{rgb(stage)}"'
        cls += " hot" if alpha else ""
        return (f'<div class="pc {cls}"{style}><b>{money(v) if ds else "—"}</b>'
                f'<small>{plural(len(ds), "deal") if ds else ""}</small></div>')

    cols = [lbl for lbl, _ in open_stages] + ["Delayed"]
    extra_head = '<div class="hod">Overdue<br><span>dated before {}</span></div>'.format(qlabel.split()[0]) if include_overdue else ""
    hdr = ('<div class="pgrid phead"><div>Rep</div>' +
           "".join(f'<div style="color:{rgb(c)}">{c}</div>' for c in cols) +
           '<div class="htot">Total</div><div class="htot">Weighted</div>' + extra_head + "</div>")
    body, team = [], {c: [] for c in cols}
    team_active, team_overdue = [], []
    for name, cells, active, overdue in rows:
        for c in cols:
            team[c] += cells[c]
        team_active += active; team_overdue += overdue
        body.append('<div class="pgrid prow"><div class="name">' + html.escape(name) + "</div>" +
                    "".join(cell(cells[c], "dly" if c == "Delayed" else "", c != "Delayed", c) for c in cols) +
                    cell(active, "tot", False) +
                    f'<div class="pc tot w"><b>{money(sum(amt(d) * prob(d) for d in active)) if active else "—"}</b><small>{"by stage odds" if active else ""}</small></div>' +
                    (cell(overdue, "od", False) if include_overdue else "") + "</div>")
    body.append('<div class="pgrid prow ptotals"><div class="name">TEAM</div>' +
                "".join(cell(team[c], "dly" if c == "Delayed" else "", False, c) for c in cols) +
                cell(team_active, "tot", False) +
                f'<div class="pc tot w"><b>{money(sum(amt(d) * prob(d) for d in team_active)) if team_active else "—"}</b><small>{"by stage odds" if team_active else ""}</small></div>' +
                (cell(team_overdue, "od", False) if include_overdue else "") + "</div>")
    note = ("Overdue = close date passed before this quarter — re-date or close · " if include_overdue else "")
    foot = (f'<footer><div>{note}Total & weighted exclude Delayed · weighted = amount × HubSpot stage probability · '
            f'Sales + Expansion pipelines</div><div>Red = closest to signature → yellow = earliest</div></footer>')
    grid_cls = "pgrid-wrap od-on" if include_overdue else "pgrid-wrap"
    return tag + head + f'<div class="{grid_cls}">' + hdr + '<div class="prows">' + "".join(body) + "</div></div>" + foot + "</section>"


def render_new_deals(deals, now_local, updated):
    since = now_local - timedelta(days=NEW_DEALS_DAYS)
    head = f'''<header><h1><span>BCS</span> New Deals · last {NEW_DEALS_DAYS} days</h1>
  <div class="meta">created {since.strftime("%b %-d")} – {now_local.strftime("%b %-d")} · updated <b>{updated}</b> MT</div></header>'''
    tag = f'<section class="slide" data-dur="{SLIDE_SECONDS_NEWDEALS}" data-key="newdeals" data-label="New deals">'
    if deals is None:
        return tag + head + '<div class="empty"><b>Deals unavailable</b><small>The HubSpot key needs deals read access.</small></div></section>'
    amt = lambda d: float(d.get("amount") or 0)
    recent = [d for d in deals if ts(d.get("createdate")) and ts(d.get("createdate")) >= since]
    cols, team_n, team_v = [], 0, 0.0
    for oid, name in REPS.items():
        mine = sorted([d for d in recent if d.get("hubspot_owner_id") == oid], key=lambda d: -amt(d))
        v = sum(amt(d) for d in mine)
        team_n += len(mine); team_v += v
        items = []
        for d in mine[:NEW_DEALS_PER_REP]:
            lbl = stage_label(d.get("dealstage"))
            items.append(f'''<div class="nd" style="border-left-color:{rgb(lbl)}">
  <div class="ndn">{html.escape((d.get("dealname") or "Untitled").strip())}</div>
  <div class="nda"><b>{money(amt(d)) if amt(d) else "no amount"}</b><em style="background:{rgb(lbl)}">{lbl}</em></div></div>''')
        if len(mine) > NEW_DEALS_PER_REP:
            rest = mine[NEW_DEALS_PER_REP:]
            items.append(f'<div class="more">+{len(rest)} more · {money(sum(amt(d) for d in rest))}</div>')
        cols.append(f'''<div class="ndcol"><div class="ndh"><span>{html.escape(name)}</span>
  <b>{len(mine)}</b><small>{money(v) if mine else "no new deals"}</small></div>
  <div class="ndlist">{"".join(items) or '<div class="none">—</div>'}</div></div>''')
    legend = " ".join(f'<em style="background:{rgb(l)}">{l}</em>' for l in
                      ["Qualified", "Demo", "Proposal Sent", "Decision Making", "Contract Review"])
    foot = (f'<footer><div>Team: {plural(team_n, "new deal")} · {money(team_v)} · current stage shown · {legend}</div>'
            f'<div>Rolling {NEW_DEALS_DAYS} days</div></footer>')
    return tag + head + f'<div class="ndgrid">{"".join(cols)}</div>' + foot + "</section>"


# ---------------------------------------------------------------- main
def build(data, now_local):
    updated = now_local.strftime("%-I:%M %p")
    today = now_local.date()

    # Weekly: Monday → now, bars per weekday
    monday = today - timedelta(days=today.weekday())
    week_rows = compute(data, monday, 5,
                        lambda d: (d - monday).days if 0 <= (d - monday).days < 5 else None)
    wk_days = 5.0 if today.weekday() >= 5 else business_days(monday, now_local)
    classified = data.get("meeting_class") is not None
    filtered = data.get("excluded_calls") is not None
    weekly = render_board(
        week_rows, classified=classified, filtered=filtered, key="board", label="This week", dur=SLIDE_SECONDS_BOARD, title="Outbound Board",
        period=f"Week of {monday.strftime('%b %-d')}", labels=["M", "T", "W", "T", "F"],
        current_idx=today.weekday() if today.weekday() < 5 else None,
        dial_target=round(DIALS_PER_DAY * wk_days), mtg_target=MEETINGS_PER_WEEK * wk_days / 5,
        mtg_label=f"of {MEETINGS_PER_WEEK} / wk", updated=updated)

    # Rolling 30 days: today and the 29 days before it, bars per Monday-start week
    start = today - timedelta(days=ROLLING_DAYS - 1)
    first_mon = start - timedelta(days=start.weekday())
    n_weeks = (monday - first_mon).days // 7 + 1
    week_labels = [(first_mon + timedelta(weeks=i)).strftime("%b %-d") for i in range(n_weeks)]
    week_labels[0] = max(start, first_mon).strftime("%b %-d")  # partial first week starts on `start`
    r30 = compute(data, start, n_weeks, lambda d: (d - first_mon).days // 7)
    d30 = business_days(start, now_local)
    m30 = MEETINGS_PER_WEEK * d30 / 5
    rolling = render_board(
        r30, classified=classified, filtered=filtered, key="30d", label="30 days", dur=SLIDE_SECONDS_30D, title="30-Day Outbound Board",
        period=f"{start.strftime('%b %-d')} – {today.strftime('%b %-d')} · bars by week",
        labels=week_labels, current_idx=n_weeks - 1,
        dial_target=round(DIALS_PER_DAY * d30), mtg_target=m30,
        mtg_label=f"target {round(m30)}", updated=updated)

    qs, qe, qn = quarter_bounds(today)
    nqs, nqe, nqn = quarter_bounds(qe)
    reps = (render_pipeline_quarter(data.get("open_deals"), key="thisq", label=f"{qn} pipeline", qs=qs, qe=qe,
                                    qlabel=f"{qn} {qs.year}", today=today, include_overdue=True, updated=updated) +
            render_pipeline_quarter(data.get("open_deals"), key="nextq", label=f"{nqn} pipeline", qs=nqs, qe=nqe,
                                    qlabel=f"{nqn} {nqs.year}", today=today, include_overdue=False, updated=updated))
    tpl = Path(__file__).with_name("template.html").read_text()
    newdeals = render_new_deals(data.get("deals"), now_local, updated)
    page = (tpl.replace("{{SLIDES}}", weekly + rolling + newdeals + reps)
               .replace("{{GEN}}", str(int(now_local.timestamp()))))
    summary = {k: (None if v is None else len(v)) for k, v in data.items()}
    summary["week"] = [{k: r[k] for k in ("name", "dials", "deal_calls", "connects", "meetings", "mtg_existing", "mtg_unlinked", "deals", "sqls", "freemiums")}
                       for r in week_rows]
    summary["30d"] = [{k: r[k] for k in ("name", "dials", "connects", "meetings", "deals", "sqls", "freemiums")}
                      for r in r30]
    return page, summary


def main():
    now_local = datetime.now(TZ)
    if os.environ.get("NOW"):  # testing: NOW=2026-10-06T12:00
        now_local = datetime.fromisoformat(os.environ["NOW"]).replace(tzinfo=TZ)
    since = datetime.combine(now_local.date() - timedelta(days=ROLLING_DAYS - 1), datetime.min.time(), TZ)
    if os.environ.get("FIXTURE"):
        data = json.loads(Path(os.environ["FIXTURE"]).read_text())
        data.setdefault("deals", None); data.setdefault("sqls", None)
        data.setdefault("freemiums", None); data.setdefault("open_deals", None)
        data.setdefault("meeting_class", None); data.setdefault("excluded_calls", [])
    else:
        token = os.environ.get("HUBSPOT_TOKEN")
        if not token:
            sys.exit("HUBSPOT_TOKEN not set")
        data = fetch(token, since)
        data["excluded_calls"] = calls_on_live_deals(token, [c for c in data["calls"]
                                                             if c.get("hs_call_direction") != "INBOUND"])
        data["meeting_class"] = classify_meetings(token, [m for m in data["meetings"] if is_prospect_meeting(m)])
    page, summary = build(data, now_local)
    out = Path("dist")
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(page)
    (out / ".nojekyll").write_text("")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
