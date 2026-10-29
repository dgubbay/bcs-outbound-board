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
REPS = {
    "99808543": "Wyatt Ison",
    "99960123": "Hunter Wooten",
    "99808544": "Thomas Gubbay",
    "135846867": "Rick Smith",
    "14324253": "David Gubbay",
}

CONNECTED = "f240bbac-87c9-4f6e-bf70-924b57d47db7"
REAL_CONNECT_MIN_MS = 120_000   # 2 min — screens out gatekeepers / hang-ups / short voicemails
REAL_CONNECT_MAX_MS = 900_000   # 15 min — longer = scheduled demo, not a cold-call connect
DIALS_PER_DAY = 60              # 300 / week
MEETINGS_PER_WEEK = 5          # default outbound-meeting target per rep per week (SMB team)
# Per-rep overrides (per David, 2026-10-27): Mid-Market/Enterprise reps carry a lower outbound target.
MEETING_TARGETS = {"99808544": 2.5,   # Thomas Gubbay
                   "135846867": 2.5}  # Rick Smith
FREEMIUM_STAGE = "94805561"
# Internal / test contacts: never count as a prospect on a meeting.
INTERNAL_DOMAINS = ("getbcs.com", "bcsaudit.com")
INTERNAL_EMAILS = ("thomasgubbay@gmail.com", "djp.gubbay@gmail.com")


def is_internal_email(e):
    e = (e or "").strip().lower()
    if not e:
        return False
    local, _, dom = e.rpartition("@")
    base = local.split("+")[0] + "@" + dom
    return dom in INTERNAL_DOMAINS or e in INTERNAL_EMAILS or base in INTERNAL_EMAILS    # BCS custom lifecycle stage "Freemium"
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
SLIDE_SECONDS_NEWDEALS = 25
SLIDE_SECONDS_QUOTA = 20
SLIDE_SECONDS_LEADS = 25
SLIDE_SECONDS_OBM = 25
OBM_MAX_CARDS = 12
# HubSpot Leads object pipeline (default "Lead pipeline"): stage id → label, in funnel order.
LEAD_STAGES = {"new-stage-id": "New", "attempting-stage-id": "Attempting", "connected-stage-id": "Connected",
               "qualified-stage-id": "Qualified", "unqualified-stage-id": "Disqualified"}
LEAD_STAGE_ORDER = ["New", "Attempting", "Connected", "Qualified", "Disqualified"]
LEAD_STAGE_CLS = {"New": "st-new", "Attempting": "st-att", "Connected": "st-con", "Qualified": "st-q",
                  "Disqualified": "st-dq"}
# Lead Type (hs_lead_type) internal value → BCS label. NEW_BUSINESS is relabeled "SQL" in HubSpot.
LEAD_TYPES = {"NEW_BUSINESS": "SQL", "Freemium": "Freemium", "Re-engaged": "Re-engaged",
              "UPSELL": "Upsell", "RE_ATTEMPTING": "Re-attempting"}
LEAD_TYPE_ORDER = ["SQL", "Freemium", "Re-engaged", "Upsell", "Re-attempting", "Other"]
LEAD_TYPE_CLS = {"SQL": "k-sql", "Freemium": "k-free", "Re-engaged": "k-reeng", "Upsell": "k-up",
                 "Re-attempting": "k-reatt", "Other": "k-other"}

# Annual quotas and the date each one starts (per David, 2026-10-09). Quarterly quota = annual ÷ 4.
# Before a rep's start date they're ramping: no quota, the row shows closed-won and pipeline only.
QUOTAS = {
    "135846867": [(date(2026, 1, 1), 690_000)],      # Rick Smith
    "99808543": [(date(2027, 1, 1), 500_000)],       # Wyatt Ison — quota starts Q1 2027
    "99960123": [(date(2027, 1, 1), 500_000)],       # Hunter Wooten — quota starts Q1 2027
    "99808544": [(date(2027, 4, 1), 1_000_000)],     # Thomas Gubbay — quota starts Q2 2027
    "14324253": [(date(2026, 1, 1), 200_000)],       # David Gubbay — $200K/yr (per David, 2026-10-13)
}


def annual_quota(oid, on: date):
    q = None
    for start, amount in QUOTAS.get(oid, []):
        if on >= start:
            q = amount
    return q


def quota_start(oid):
    return min((s for s, _ in QUOTAS.get(oid, [])), default=None)
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


def latest_deal(token):
    body = {"filterGroups": [{"filters": [{"propertyName": "hubspot_owner_id", "operator": "IN", "values": list(REPS)}]}],
            "properties": ["hubspot_owner_id", "dealname", "amount", "dealstage", "createdate", "pipeline"],
            "sorts": [{"propertyName": "createdate", "direction": "DESCENDING"}], "limit": 1}
    r = requests.post(f"{API}/crm/v3/objects/deals/search", json=body,
                      headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code >= 400:
        return None
    res = r.json().get("results", [])
    return res[0]["properties"] if res else None


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


def deal_flags(token, ids):
    """{deal_id: (is_open, is_won)}"""
    out = {}
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 100):
        r = requests.post(f"{API}/crm/v3/objects/deals/batch/read",
                          json={"properties": ["hs_is_closed", "hs_is_closed_won"], "inputs": [{"id": x} for x in ids[i:i + 100]]},
                          headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        for d in r.json().get("results", []):
            p = d["properties"]
            out[str(d["id"])] = ((p.get("hs_is_closed") or "false") != "true", (p.get("hs_is_closed_won") or "false") == "true")
    return out


def calls_on_live_deals(token, calls):
    """Ids of calls that are deal or client work, not outbound prospecting: the call's contact OR company
    has an open deal, or is an existing customer (any closed-won deal). Per David, 2026-10-20 — e.g. a
    62-min pricing call with an existing client whose expansion deal was marked Lost slipped through
    the open-deal-only rule."""
    ids = [c["hs_object_id"] for c in calls if c.get("hs_object_id")]
    if not ids:
        return []
    try:
        c2ct = assoc(token, "calls", "contacts", ids)
        c2co = assoc(token, "calls", "companies", ids)
        contacts = {x for v in c2ct.values() for x in v}
        companies = {x for v in c2co.values() for x in v}
        ct2d = assoc(token, "contacts", "deals", contacts) if contacts else {}
        co2d = assoc(token, "companies", "deals", companies) if companies else {}
        flags = deal_flags(token, {d for v in list(ct2d.values()) + list(co2d.values()) for d in v})
    except requests.HTTPError as e:
        print(f"WARN: live-deal call filter unavailable ({e.response.status_code})", file=sys.stderr)
        return None
    def busy(deals):
        return any(flags.get(d, (False, False))[0] or flags.get(d, (False, False))[1] for d in deals)
    out = []
    for cid in ids:
        cts, cos = c2ct.get(cid, []), c2co.get(cid, [])
        if any(busy(ct2d.get(ct, [])) for ct in cts) or any(busy(co2d.get(co, [])) for co in cos):
            out.append(cid)
    return out


NEW_TYPES = {"Discovery Demo"}                                   # meeting-type fallback when nothing is linked
EXISTING_TYPES = {"Follow-up Demo", "Proposal Review", "Follow up Call", "Follow-up Call"}


def sql_contacts(token, ids):
    """(inbound, internal): inbound = lifecycle stage is, or has ever been, SQL or Freemium;
    internal = BCS staff / test contacts (see INTERNAL_DOMAINS)."""
    out, internal = set(), set()
    ids = list(dict.fromkeys(ids))
    for i in range(0, len(ids), 100):
        r = requests.post(f"{API}/crm/v3/objects/contacts/batch/read",
                          json={"properties": ["lifecyclestage", "email", "hs_v2_date_entered_salesqualifiedlead",
                                               f"hs_v2_date_entered_{FREEMIUM_STAGE}"],
                                "inputs": [{"id": x} for x in ids[i:i + 100]]},
                          headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        for c in r.json().get("results", []):
            p = c["properties"]
            if is_internal_email(p.get("email")):
                internal.add(str(c["id"]))
            if (p.get("lifecyclestage") in ("salesqualifiedlead", FREEMIUM_STAGE)
                    or p.get("hs_v2_date_entered_salesqualifiedlead") or p.get(f"hs_v2_date_entered_{FREEMIUM_STAGE}")):
                out.add(str(c["id"]))
    return out, internal


def classify_meetings(token, meetings):
    """meeting id → "existing" (a deal already existed with that contact/company when it was booked),
    "inbound" (a contact on the meeting is, or has been, an SQL or Freemium lifecycle stage — per David,
    2026-10-20 / 2026-10-27; how the contact was created doesn't matter), "new" (outbound prospect meeting) or
    "unlinked" (no contact/company to check).
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
        sqls, internal = sql_contacts(token, contacts) if contacts else (set(), set())
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
        ext = [c for c in cs if c not in internal]
        if cs and not ext:
            out[mid] = "internal"      # only BCS staff / test contacts on it: not a prospect meeting
            continue
        inbound = any(c in sqls for c in ext)
        out[mid] = "existing" if prior else "inbound" if inbound else "new"
    return out


def owner_names(token, ids):
    """Names for lead owners who aren't on the roster (e.g. CS or ops users)."""
    out = {}
    for oid in ids:
        try:
            r = requests.get(f"{API}/crm/v3/owners/{oid}", headers={"Authorization": f"Bearer {token}"}, timeout=20)
            r.raise_for_status()
            o = r.json()
            out[oid] = f'{o.get("firstName") or ""} {o.get("lastName") or ""}'.strip() or o.get("email") or f"Owner {oid}"
        except requests.RequestException:
            out[oid] = f"Owner {oid}"
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
        "won": safe("deals", [owners, {"propertyName": "hs_is_closed_won", "operator": "EQ", "value": "true"},
                               {"propertyName": "closedate", "operator": "GTE",
                                "value": datetime(since_local.year if since_local.month > 1 else since_local.year - 1, 1, 1,
                                                  tzinfo=TZ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}],
                     ["hubspot_owner_id", "dealname", "amount", "closedate"]),
        # Leads page: HubSpot Lead records created in the window, whoever owns them (or nobody).
        "lead_objs": safe("leads", [{"propertyName": "hs_createdate", "operator": "GTE", "value": iso}],
                          ["hs_lead_name", "hubspot_owner_id", "hs_lead_type", "hs_pipeline_stage", "hs_createdate"]),
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
    rows = {oid: {"oid": oid, "name": n, "bars": [0] * buckets, "dials": 0, "talk_ms": 0, "connects": 0,
                  "meetings": 0, "mtg_existing": 0, "mtg_inbound": 0, "mtg_unlinked": 0, "deal_amt": 0.0, "deal_calls": 0,
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
        # Connect = any outbound call of 2–15 minutes (per David, 2026-10-20) — the HubSpot outcome tag
        # is ignored. Calls to contacts on open deals were already skipped above.
        if REAL_CONNECT_MIN_MS <= dur <= REAL_CONNECT_MAX_MS:
            r["connects"] += 1
            r["talk_ms"] += dur
    for m in data["meetings"]:
        r = rows.get(m.get("hubspot_owner_id"))
        if r and keep(local_day(m.get("hs_createdate"))) and is_prospect_meeting(m):
            kind = "new" if mclass is None else mclass.get(m.get("hs_object_id"), "unlinked")
            if kind == "new":
                r["meetings"] += 1          # the headline number: brand-new prospect meetings only
            elif kind == "existing":
                r["mtg_existing"] += 1
            elif kind == "inbound":
                r["mtg_inbound"] += 1
            elif kind != "internal":
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
    if n == 1:
        return f"{n} {word}"
    return f"{n} {word[:-1] + 'ies' if word.endswith('y') and word[-2:-1] not in 'aeiou' else word + 's'}"


# ---------------------------------------------------------------- outbound board slide
def mtg_note(r, base):
    extra = []
    if r["mtg_existing"]:
        extra.append(f"+{r['mtg_existing']} on deals")
    if r["mtg_unlinked"]:
        extra.append(f"{r['mtg_unlinked']} unlinked")
    return base + ("<br>" + " · ".join(extra) if extra else "")


def mtg_goal(oid):
    return MEETING_TARGETS.get(oid, MEETINGS_PER_WEEK)


def fmt_n(x):
    return f"{x:.1f}".rstrip("0").rstrip(".")


def render_board(rows, *, classified=True, filtered=True, key, label, dur, title, period, labels, current_idx, dial_target,
                 mtg_scale, weekly, updated):
    """mtg_scale = business days elapsed ÷ 5; each rep's outbound-meeting target = their weekly goal × mtg_scale."""
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
  <div class="metric"><b>{pct}%</b><small>{plural(r['connects'], 'connect')}</small></div>
  <div class="metric {pace_class(r['meetings'], mtg_goal(r['oid']) * mtg_scale)}"><b>{r['meetings']}</b><small>{mtg_note(r, f"of {fmt_n(mtg_goal(r['oid']))} / wk" if weekly else f"target {fmt_n(round(mtg_goal(r['oid']) * mtg_scale * 2) / 2)}")}</small></div>
  <div class="metric"><b>{r['mtg_inbound'] if classified else '—'}</b><small>SQL / freemium</small></div>
  <div class="metric pipe"><b>{num(r['deals'])}</b><small>{money(r['deal_amt']) if r['deals'] else 'no new deals'}</small></div>
  <div class="metric inb"><b>{num(r['sqls'])}</b><small>SQLs</small></div>
  <div class="metric inb"><b>{num(r['freemiums'])}</b><small>freemiums</small></div>
</div>""")
    td = sum(r["dials"] for r in rows)
    tc = sum(r["connects"] for r in rows)
    return f"""<section class="slide" data-dur="{dur}" data-key="{key}" data-label="{label}">
<header><h1><span>BCS</span> {title}</h1>
  <div class="meta">{period} · updated <b>{updated}</b> MT</div></header>
<div class="grid head"><div>Rep</div><div>Dials · total</div><div>Talk time</div><div>Connect rate</div><div>{'Outbound mtgs' if classified else 'Meetings booked'}</div><div>Inbound mtgs</div><div class="hpipe">New deals</div><div class="hinb">SQLs routed</div><div class="hinb">Freemiums routed</div></div>
<div class="rows">{''.join(trs)}
  <div class="row totals">
    <div class="name">TEAM</div>
    <div class="metric"><b>{td}</b><small>dials</small></div>
    <div class="metric"><b>{hm(sum(r['talk_ms'] for r in rows))}</b><small>talk time</small></div>
    <div class="metric"><b>{round(100 * tc / td) if td else 0}%</b><small>connect rate</small></div>
    <div class="metric"><b>{sum(r['meetings'] for r in rows)}</b><small>{'outbound mtgs' if classified else 'meetings'}</small></div>
    <div class="metric"><b>{sum(r['mtg_inbound'] for r in rows) if classified else '—'}</b><small>inbound mtgs</small></div>
    <div class="metric pipe"><b>{num(tot(rows, 'deals'))}</b><small>{money(sum(r['deal_amt'] for r in rows))}</small></div>
    <div class="metric inb"><b>{num(tot(rows, 'sqls'))}</b><small>SQLs</small></div>
    <div class="metric inb"><b>{num(tot(rows, 'freemiums'))}</b><small>freemiums</small></div>
  </div>
</div>
<footer>
  <div>Connect = outbound call of 2–15 min (outcome tag ignored) · Talk time = minutes on those calls · {'Outbound mtg = no contact is/was SQL or Freemium, no deal yet · Inbound mtg = an SQL/Freemium contact' if classified else 'Prospect-vs-deal meeting split needs deals + companies read access'} · Deals created · SQLs & freemiums by owner</div>
  <div>{f"Excludes {plural(sum(r['deal_calls'] for r in rows), 'call')} to open deals & existing clients" if filtered else "Target 60 dials/day"}</div>
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


def render_quota(won, open_deals, now_local, updated):
    today = now_local.date()
    qs, qe, qn = quarter_bounds(today)
    ys, ye = date(today.year, 1, 1), date(today.year + 1, 1, 1)
    pace = (today - qs).days / (qe - qs).days            # share of the quarter elapsed
    head = f'''<header><h1><span>BCS</span> Quota Attainment · {qn} {today.year}</h1>
  <div class="meta">{round(pace * 100)}% of the quarter elapsed · updated <b>{updated}</b> MT</div></header>'''
    tag = f'<section class="slide" data-dur="{SLIDE_SECONDS_QUOTA}" data-key="quota" data-label="Quota">'
    if won is None or open_deals is None:
        return tag + head + '<div class="empty"><b>Quota data unavailable</b><small>The HubSpot key needs deals read access.</small></div></section>'
    amt = lambda d: float(d.get("amount") or 0)
    prob = lambda d: float(d.get("hs_deal_stage_probability") or 0)
    delayed_ids = STAGES[-1][1]
    rows = []
    for oid, name in REPS.items():
        w = [d for d in won if d.get("hubspot_owner_id") == oid]
        q_won = sum(amt(d) for d in w if local_day(d.get("closedate")) and qs <= local_day(d.get("closedate")) < qe)
        y_won = sum(amt(d) for d in w if local_day(d.get("closedate")) and ys <= local_day(d.get("closedate")) < ye)
        q_open = [d for d in open_deals if d.get("hubspot_owner_id") == oid and d.get("dealstage") not in delayed_ids
                  and local_day(d.get("closedate")) and local_day(d.get("closedate")) < qe]   # this quarter + overdue
        pipe = sum(amt(d) for d in q_open)
        weighted = sum(amt(d) * prob(d) for d in q_open)
        annual = annual_quota(oid, qs)
        qquota = annual / 4 if annual else None
        rows.append(dict(name=name, oid=oid, q_won=q_won, y_won=y_won, pipe=pipe, weighted=weighted,
                         annual=annual, qquota=qquota, ystart=annual_quota(oid, ys)))
    rows.sort(key=lambda r: (r["qquota"] is None, -(r["q_won"] / r["qquota"] if r["qquota"] else r["q_won"])))

    def pct_class(p):
        return "good" if p >= pace else "warn" if p >= pace * 0.75 else "bad"

    out = []
    for r in rows:
        if r["qquota"]:
            p = r["q_won"] / r["qquota"]
            gap = max(0.0, r["qquota"] - r["q_won"])
            cov = (r["pipe"] / gap) if gap else None
            fill = min(100, p * 100)
            bar = f'''<div class="qbar"><i class="{pct_class(p)}" style="width:{fill:.1f}%"></i>
  <span class="pace" style="left:{pace * 100:.1f}%"></span></div>
  <div class="qsub">{money(r["q_won"])} won of {money(r["qquota"])} · pace line = where they should be today</div>'''
            pct = f'<div class="metric {pct_class(p)}"><b>{round(p * 100)}%</b><small>of {qn} quota</small></div>'
            gapc = f'<div class="metric"><b>{money(gap) if gap else "✓"}</b><small>{"left to close" if gap else "quota hit"}</small></div>'
            covc = (f'<div class="metric {"good" if cov >= 3 else "warn" if cov >= 1 else "bad"}"><b>{cov:.1f}×</b>'
                    f'<small>{money(r["pipe"])} open</small></div>') if cov is not None else '<div class="metric"><b>—</b><small>no gap</small></div>'
            fc = r["q_won"] + r["weighted"]
            fcc = f'<div class="metric {"good" if fc >= r["qquota"] else "warn" if fc >= .75 * r["qquota"] else "bad"}"><b>{money(fc)}</b><small>forecast · {round(100 * fc / r["qquota"])}%</small></div>'
        else:
            st = quota_start(r["oid"])
            sq = f"Q{(st.month - 1) // 3 + 1} {st.year}" if st else "TBD"
            bar = f'<div class="ramp">Ramping — quota starts {sq}</div><div class="qsub">{money(r["q_won"])} won this quarter · {money(r["pipe"])} open pipeline</div>'
            pct = '<div class="metric"><b>—</b><small>no quota yet</small></div>'
            gapc = f'<div class="metric"><b>{money(r["q_won"]) if r["q_won"] else "$0"}</b><small>won in {qn}</small></div>'
            covc = f'<div class="metric"><b>{money(r["pipe"]) if r["pipe"] else "$0"}</b><small>open pipeline</small></div>'
            fcc = f'<div class="metric"><b>{money(r["weighted"]) if r["weighted"] else "$0"}</b><small>weighted pipeline</small></div>'
        if r["ystart"]:
            yp = r["y_won"] / r["ystart"]
            ycell = f'<div class="metric"><b>{round(yp * 100)}%</b><small>{today.year}: {money(r["y_won"])} of {money(r["ystart"])}</small></div>'
        else:
            ycell = f'<div class="metric"><b>{money(r["y_won"]) if r["y_won"] else "$0"}</b><small>won in {today.year}</small></div>'
        out.append(f'<div class="qrow"><div class="name">{html.escape(r["name"])}</div><div class="qmain">{bar}</div>{pct}{gapc}{covc}{fcc}{ycell}</div>')
    hdr = ('<div class="qrow qhead"><div>Rep</div><div>Closed-won vs quarter quota</div><div>Attainment</div>'
           '<div>Gap</div><div>Pipeline coverage</div><div>Forecast</div><div>Year to date</div></div>')
    foot = ('<footer><div>Won = closed-won amount by close date · coverage = open pipeline (this quarter + overdue, excl. Delayed) ÷ gap · '
            'forecast = won + pipeline × stage probability</div><div>Green = at/above pace</div></footer>')
    return tag + head + hdr + f'<div class="qrows">{"".join(out)}</div>' + foot + "</section>"


CONFETTI_COLORS = ["#ED1F82", "#00B7D1", "#FDE14B", "#6B4FA0", "#3DDC84", "#ffffff"]
SLIDE_SECONDS_CELEBRATE = 15
WINS_DAYS = 30


def confetti(n=70, seed=7):
    import random
    rnd = random.Random(seed)
    bits = []
    for i in range(n):
        bits.append(f'<i style="left:{rnd.uniform(0, 100):.1f}%;background:{CONFETTI_COLORS[i % len(CONFETTI_COLORS)]};'
                    f'width:{rnd.uniform(.6, 1.4):.2f}vh;height:{rnd.uniform(1.2, 2.6):.2f}vh;'
                    f'animation-duration:{rnd.uniform(3.5, 7):.2f}s;animation-delay:-{rnd.uniform(0, 7):.2f}s;'
                    f'transform:rotate({rnd.randint(0, 360)}deg)"></i>')
    return f'<div class="confetti">{"".join(bits)}</div>'


def ago(t, now_local):
    mins = int((now_local - t).total_seconds() // 60)
    if mins < 60:
        return f"{max(mins, 1)} min ago"
    if mins < 24 * 60:
        return plural(mins // 60, "hour") + " ago"
    return plural(mins // (24 * 60), "day") + " ago"


def render_latest_deal(d, now_local, updated):
    tag = f'<section class="slide spotlight" data-dur="{SLIDE_SECONDS_CELEBRATE}" data-key="newdeal" data-label="New opportunity">'
    if not d:
        return tag + '<div class="empty"><b>No opportunities yet</b></div></section>'
    rep = REPS.get(d.get("hubspot_owner_id"), "")
    lbl = stage_label(d.get("dealstage"))
    created = ts(d.get("createdate"))
    amt = float(d.get("amount") or 0)
    return tag + f'''<div class="cel">
  <div class="kicker">New opportunity</div>
  <div class="who">{html.escape(rep)}</div>
  <div class="what">{html.escape((d.get("dealname") or "Untitled").strip())}</div>
  <div class="big">{money(amt) if amt else "Amount TBD"}</div>
  <div class="tags">{f'<em style="background:{rgb(lbl)}">{lbl}</em>' if lbl != "Other" else ""}<span>created {ago(created, now_local) if created else ""}</span></div>
</div>
<footer><div>Most recently created opportunity on the team · not closed yet</div><div>updated <b>{updated}</b> MT</div></footer></section>'''


def render_wins(won, now_local, updated):
    tag = f'<section class="slide celebrate wins" data-dur="{SLIDE_SECONDS_CELEBRATE + 5}" data-key="wins" data-label="Closed won">'
    if won is None:
        return tag + '<div class="empty"><b>Wins unavailable</b><small>The HubSpot key needs deals read access.</small></div></section>'
    amt = lambda d: float(d.get("amount") or 0)
    wins = sorted([d for d in won if ts(d.get("closedate"))], key=lambda d: ts(d.get("closedate")), reverse=True)
    if not wins:
        return tag + '<div class="empty"><b>The next win goes here</b></div></section>'
    since = now_local - timedelta(days=WINS_DAYS)
    recent = [d for d in wins if ts(d["closedate"]) >= since]
    hero = wins[0]
    hero_ts = ts(hero["closedate"])
    fresh = hero_ts >= since
    kicker = "CLOSED WON" if fresh else "Most recent win"
    rest = recent[1:7] if len(recent) > 1 else wins[1:4]
    strip_label = "Also closed recently" if len(recent) > 1 else "Earlier wins"
    strip = "".join(f'''<div class="wcard"><div class="wrep">{html.escape(REPS.get(d.get("hubspot_owner_id"), ""))}</div>
  <div class="wname">{html.escape((d.get("dealname") or "").strip())}</div><b>{money(amt(d))}</b>
  <span>{ts(d["closedate"]).astimezone(TZ).strftime("%b %-d")}</span></div>''' for d in rest)
    tally = (f'{plural(len(recent), "deal")} · {money(sum(amt(d) for d in recent))} closed in the last {WINS_DAYS} days'
             if recent else f"No closed-won deals in the last {WINS_DAYS} days — who's next?")
    return tag + confetti(110, seed=3) + f'''<div class="cel">
  <div class="kicker gold">{kicker}</div>
  <div class="who">{html.escape(REPS.get(hero.get("hubspot_owner_id"), ""))}</div>
  <div class="what">{html.escape((hero.get("dealname") or "").strip())}</div>
  <div class="big gold">{money(amt(hero))}</div>
  <div class="tags"><span>closed {hero_ts.astimezone(TZ).strftime("%b %-d")} · {ago(hero_ts, now_local)}</span></div>
</div>
<div class="tally">{tally}</div>
<div class="wlabel">{strip_label if strip else ""}</div><div class="wstrip">{strip}</div>
<footer><div>Closed-won deals, newest first</div><div>updated <b>{updated}</b> MT</div></footer></section>'''


def latest_banner(d, now_local):
    if not d:
        return ""
    lbl = stage_label(d.get("dealstage"))
    created = ts(d.get("createdate"))
    amt = float(d.get("amount") or 0)
    return f'''<div class="oppbanner">
  <div class="ob-left"><div class="ob-kicker">Latest opportunity</div>
    <div class="ob-who">{html.escape(REPS.get(d.get("hubspot_owner_id"), ""))}</div>
    <div class="ob-what">{html.escape((d.get("dealname") or "Untitled").strip())}</div></div>
  <div class="ob-right"><b>{money(amt) if amt else "Amount TBD"}</b>
    <div class="tags">{f'<em style="background:{rgb(lbl)}">{lbl}</em>' if lbl != "Other" else ""}<span>created {ago(created, now_local) if created else ""}</span></div></div>
</div>'''


def render_new_deals(deals, now_local, updated, latest=None):
    since = now_local - timedelta(days=NEW_DEALS_DAYS)
    head = f'''<header><h1><span>BCS</span> New Opportunities · last {NEW_DEALS_DAYS} days</h1>
  <div class="meta">created {since.strftime("%b %-d")} – {now_local.strftime("%b %-d")} · updated <b>{updated}</b> MT</div></header>'''
    tag = f'<section class="slide" data-dur="{SLIDE_SECONDS_NEWDEALS}" data-key="newdeals" data-label="New opportunities">'
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
  <div class="nda"><b>{money(amt(d)) if amt(d) else "no amount"}</b>{f'<em style="background:{rgb(lbl)}">{lbl}</em>' if lbl != "Other" else ""}</div></div>''')
        if len(mine) > NEW_DEALS_PER_REP:
            rest = mine[NEW_DEALS_PER_REP:]
            items.append(f'<div class="more">+{len(rest)} more · {money(sum(amt(d) for d in rest))}</div>')
        cols.append(f'''<div class="ndcol"><div class="ndh"><span>{html.escape(name)}</span>
  <b>{len(mine)}</b><small>{money(v) if mine else "none yet"}</small></div>
  <div class="ndlist">{"".join(items) or '<div class="none">—</div>'}</div></div>''')
    legend = " ".join(f'<em style="background:{rgb(l)}">{l}</em>' for l in
                      ["Qualified", "Demo", "Proposal Sent", "Decision Making", "Contract Review"])
    foot = (f'<footer><div>Team: {plural(team_n, "new opportunity")} · {money(team_v)} · current stage shown · {legend}</div>'
            f'<div>Rolling {NEW_DEALS_DAYS} days</div></footer>')
    return (tag + head + latest_banner(latest, now_local) +
            f'<div class="ndgrid" style="grid-template-columns:repeat({len(REPS)},minmax(0,1fr))">{"".join(cols)}</div>' +
            foot + "</section>")


def render_leads(lead_rows, owners, *, start, first_mon, week_labels, now_local, updated):
    """Leads page built on HubSpot Lead records (Leads object): owner, Lead Type, pipeline stage, created date."""
    tag = f'<section class="slide" data-dur="{SLIDE_SECONDS_LEADS}" data-key="leads" data-label="Leads">'
    head = (f'<header><h1><span>BCS</span> Leads · last {ROLLING_DAYS} days</h1>'
            f'<div class="meta">{start.strftime("%b %-d")} – {now_local.strftime("%b %-d")} · HubSpot Lead records'
            f' · updated <b>{updated}</b> MT</div></header>')
    if lead_rows is None:
        return (tag + head + '<div class="empty"><b>Leads unavailable</b>'
                '<small>The HubSpot key needs Leads read access (crm.objects.leads.read).</small></div></section>')
    leads = []
    for p in lead_rows:
        d = local_day(p.get("hs_createdate"))
        if not d or d < start:
            continue
        leads.append({"owner": p.get("hubspot_owner_id") or "", "day": d,
                      "type": LEAD_TYPES.get(p.get("hs_lead_type"), "Other"),
                      "stage": LEAD_STAGES.get(p.get("hs_pipeline_stage"), "New")})
    n_weeks = len(week_labels)
    names = dict(REPS)
    names.update(owners or {})
    others = {L["owner"] for L in leads} - set(REPS) - {""}
    order = list(REPS) + sorted(others, key=lambda o: names.get(o, o)) + [""]
    esc = html.escape
    types = [t for t in LEAD_TYPE_ORDER if any(L["type"] == t for L in leads)] or ["SQL"]

    def legend(items):
        return '<div class="lleg">' + "".join(f'<span><i class="{c}"></i>{t}</span>' for c, t in items) + "</div>"

    type_leg = legend([(LEAD_TYPE_CLS[t], t) for t in types])
    tally = lambda ls: {t: sum(1 for L in ls if L["type"] == t) for t in types}

    # ---- 1. Leads by type, per week (stacked columns, first type at the base)
    def rng(i):
        a = max(start, first_mon + timedelta(weeks=i))
        b = min(now_local.date(), first_mon + timedelta(weeks=i, days=6))
        if a == b:
            return a.strftime("%b %-d")
        return f'{a.strftime("%b %-d")}–{b.strftime("%-d" if a.month == b.month else "%b %-d")}'
    wk = lambda L: (L["day"] - first_mon).days // 7
    weeks = [(rng(i), [L for L in leads if wk(L) == i]) for i in range(n_weeks)]
    wmax = max([len(ls) for _, ls in weeks] + [1])
    cols = []
    for i, (lbl, ls) in enumerate(weeks):
        cur = " cur" if i == n_weeks - 1 else ""
        t = tally(ls)
        segs = "".join(f'<i class="{LEAD_TYPE_CLS[k]}" style="flex:{v} 0 0">{v if v / wmax >= .12 else ""}</i>'
                       for k, v in reversed(list(t.items())) if v)
        tip = ", ".join(f"{v} {k}" for k, v in t.items() if v) or "none"
        cols.append(f'<div class="wcol{cur}" title="{lbl}: {tip}"><div class="wplot"><b>{len(ls)}</b>'
                    f'<div class="wstack" style="height:{len(ls) / wmax * 88:.2f}%">{segs}</div></div>'
                    f'<span>{"This week" if cur else lbl}</span></div>')
    c1 = (f'<div class="lcard"><div class="lct"><h2>Leads by type · per week</h2>{type_leg}</div>'
          f'<div class="wchart">{"".join(cols)}</div></div>')

    # ---- 2. Leads by rep (stacked by type, shared scale)
    rows = []
    for oid in order:
        mine = [L for L in leads if L["owner"] == oid]
        if oid not in REPS and not mine:
            continue
        rows.append((oid, names.get(oid, f"Owner {oid}") if oid else "Unassigned", mine))
    rmax = max([len(m) for *_, m in rows] + [1])
    bars = []
    for oid, nm, mine in rows:
        t = tally(mine)
        segs = "".join(f'<i class="{LEAD_TYPE_CLS[k]}" style="width:{v / rmax * 100:.2f}%">{v if v / rmax >= .08 else ""}</i>'
                       for k, v in t.items() if v)
        tip = ", ".join(f"{v} {k}" for k, v in t.items() if v) or "no leads"
        bars.append(f'<div class="hrow{" un" if not oid else ""}" title="{esc(nm)}: {tip}"><span class="hn">{esc(nm)}</span>'
                    f'<div class="hbar">{segs}</div><b>{len(mine)}</b></div>')
    c2 = (f'<div class="lcard"><div class="lct"><h2>Leads by rep</h2>{type_leg}</div>'
          f'<div class="hchart">{"".join(bars)}</div></div>')

    # ---- 3. Lead status by rep (bar length = lead count, split by pipeline stage)
    stage_leg = legend([(LEAD_STAGE_CLS[k], k) for k in LEAD_STAGE_ORDER])
    srows = []
    for oid, nm, mine in rows:
        n = len(mine)
        cnt = {k: sum(1 for L in mine if L["stage"] == k) for k in LEAD_STAGE_ORDER}
        if not n:
            bar = '<div class="sbar empty">no leads</div>'
        else:
            segs = "".join(f'<i class="{LEAD_STAGE_CLS[k]}" style="flex:{v} 0 0">{v if v / rmax >= .05 else ""}</i>'
                           for k, v in cnt.items() if v)
            bar = f'<div class="strack"><div class="sbar" style="width:{n / rmax * 100:.2f}%">{segs}</div></div>'
        worked = cnt["Connected"] + cnt["Qualified"]
        tip = ", ".join(f"{v} {k}" for k, v in cnt.items() if v) or "no leads"
        srows.append(f'<div class="srow{" un" if not oid else ""}" title="{esc(nm)}: {tip}"><span class="hn">{esc(nm)}</span>{bar}'
                     f'<span class="sn"><b>{n}</b>{f" · {round(worked / n * 100)}% connected+" if n else ""}</span></div>')
    c3 = (f'<div class="lcard wide"><div class="lct"><h2>Lead status by rep</h2>{stage_leg}</div>'
          f'<div class="schart">{"".join(srows)}</div></div>')

    un = sum(1 for L in leads if not L["owner"])
    unb = f' · <b class="bad">{un} unassigned</b>' if un else ""
    mix = " · ".join(f"{v} {k}" for k, v in tally(leads).items() if v)
    foot = (f'<footer><div>{plural(len(leads), "lead")}{" · " + mix if mix else ""}{unb}'
            f' · lead records created in the window, by lead owner · connected+ = connected or qualified</div>'
            f'<div>Weeks start Monday</div></footer>')
    return tag + head + f'<div class="lcharts">{c1}{c2}</div>{c3}' + foot + "</section>"


def meeting_people(token, mids):
    """{meeting_id: {"contacts": [(name, title)], "company": str}} for the given meetings."""
    mids = list(dict.fromkeys(mids))
    if not mids:
        return {}
    m2c = assoc(token, "meetings", "contacts", mids)
    m2co = assoc(token, "meetings", "companies", mids)

    def batch(obj, ids, props):
        out = {}
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), 100):
            r = requests.post(f"{API}/crm/v3/objects/{obj}/batch/read",
                              json={"properties": props, "inputs": [{"id": x} for x in ids[i:i + 100]]},
                              headers={"Authorization": f"Bearer {token}"}, timeout=30)
            r.raise_for_status()
            for o in r.json().get("results", []):
                out[str(o["id"])] = o["properties"]
        return out

    contacts = batch("contacts", [c for v in m2c.values() for c in v], ["firstname", "lastname", "email", "jobtitle", "company"])
    companies = batch("companies", [c for v in m2co.values() for c in v], ["name"])
    out = {}
    for mid in mids:
        people, co = [], ""
        for cid in m2c.get(mid, []):
            p = contacts.get(cid) or {}
            nm = f'{p.get("firstname") or ""} {p.get("lastname") or ""}'.strip() or p.get("email") or ""
            if nm and not is_internal_email(p.get("email")):
                people.append((nm, (p.get("jobtitle") or "").strip()))
            co = co or (p.get("company") or "").strip()
        for coid in m2co.get(mid, []):
            if (companies.get(coid) or {}).get("name"):
                co = companies[coid]["name"].strip()
                break
        out[mid] = {"contacts": people, "company": co}
    return out


def week_outbound_meetings(data, monday):
    """This week's outbound prospect meetings (same rule as the board's Outbound mtgs column), newest booked first."""
    mclass = data.get("meeting_class")
    out = []
    for m in data.get("meetings") or []:
        if m.get("hubspot_owner_id") not in REPS or not is_prospect_meeting(m):
            continue
        d = local_day(m.get("hs_createdate"))
        if not d or d < monday:
            continue
        kind = "new" if mclass is None else mclass.get(m.get("hs_object_id"), "unlinked")
        if kind == "new":
            out.append(m)
    return sorted(out, key=lambda m: ts(m.get("hs_createdate")), reverse=True)


def render_outbound_mtgs(mtgs, details, *, monday, now_local, updated):
    tag = (f'<section class="slide celebrate obm" data-dur="{SLIDE_SECONDS_OBM}" data-key="outbound" '
           f'data-label="Outbound meetings">')
    head = (f'<header><h1><span>BCS</span> Outbound Meetings Booked</h1>'
            f'<div class="meta">Week of {monday.strftime("%b %-d")} · updated <b>{updated}</b> MT</div></header>')
    details = details or {}
    # Per-rep tally vs weekly target
    chips = []
    for oid, nm in REPS.items():
        n = sum(1 for m in mtgs if m.get("hubspot_owner_id") == oid)
        goal = mtg_goal(oid)
        hit = " hit" if n >= goal else ""
        chips.append(f'<div class="obchip{hit}"><span>{html.escape(nm.split()[0])}</span><b>{n}</b>'
                     f'<small>of {fmt_n(goal)}</small></div>')
    team_goal = sum(mtg_goal(o) for o in REPS)
    tally = (f'<div class="obtally"><div class="obbig"><b>{len(mtgs)}</b><small>{"outbound meeting" if len(mtgs) == 1 else "outbound meetings"} booked this week'
             f'<i>team goal {fmt_n(team_goal)}</i></small></div><div class="obchips">{"".join(chips)}</div></div>')
    if not mtgs:
        return (tag + head + tally + '<div class="empty"><b>First outbound meeting of the week goes here</b>'
                '<small>Book one and it shows up on this screen.</small></div>' + obm_foot(updated) + "</section>")
    shown = mtgs[:OBM_MAX_CARDS]
    cards = []
    for i, m in enumerate(shown):
        mid = m.get("hs_object_id")
        det = details.get(mid) or {}
        people = det.get("contacts") or []
        who = people[0][0] if people else (m.get("hs_meeting_title") or "Prospect meeting").strip()
        more = f' +{len(people) - 1}' if len(people) > 1 else ""
        title = people[0][1] if people else ""
        co = det.get("company") or ""
        sub = " · ".join(x for x in (title, co) if x)
        booked = ts(m.get("hs_createdate"))
        start = ts(m.get("hs_meeting_start_time"))
        when = f'meets {start.astimezone(TZ).strftime("%a %b %-d · %-I:%M %p")}' if start else ""
        new = " fresh" if i == 0 else ""
        cards.append(f'''<div class="obcard{new}"><div class="obrep">{html.escape(REPS.get(m.get("hubspot_owner_id"), ""))}</div>
  <div class="obwho">{html.escape(who)}<em>{more}</em></div>
  <div class="obsub">{html.escape(sub) or "&nbsp;"}</div>
  <div class="obmt">{html.escape((m.get("hs_meeting_title") or "").strip())}</div>
  <div class="obwhen"><span>booked {ago(booked, now_local) if booked else ""}</span><span>{when}</span></div></div>''')
    extra = (f'<div class="obmore">+{len(mtgs) - len(shown)} more this week</div>' if len(mtgs) > len(shown) else "")
    size = "lg" if len(shown) <= 4 else "md" if len(shown) <= 9 else "sm"
    return (tag + confetti(60, seed=11) + head + tally +
            f'<div class="obgrid {size}">{"".join(cards)}</div>{extra}' + obm_foot(updated) + "</section>")


def obm_foot(updated):
    return ('<footer><div>Meetings booked this week with outbound prospects: no contact is/was SQL or Freemium, '
            'no deal on the contact or company when booked · newest first</div>'
            f'<div>Goals: SMB 5/wk · Mid-Market/Enterprise 2.5/wk</div></footer>')


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
        dial_target=round(DIALS_PER_DAY * wk_days), mtg_scale=wk_days / 5, weekly=True, updated=updated)

    # Rolling 30 days: today and the 29 days before it, bars per Monday-start week
    start = today - timedelta(days=ROLLING_DAYS - 1)
    first_mon = start - timedelta(days=start.weekday())
    n_weeks = (monday - first_mon).days // 7 + 1
    week_labels = [(first_mon + timedelta(weeks=i)).strftime("%b %-d") for i in range(n_weeks)]
    week_labels[0] = max(start, first_mon).strftime("%b %-d")  # partial first week starts on `start`
    r30 = compute(data, start, n_weeks, lambda d: (d - first_mon).days // 7)
    d30 = business_days(start, now_local)
    rolling = render_board(
        r30, classified=classified, filtered=filtered, key="30d", label="30 days", dur=SLIDE_SECONDS_30D, title="30-Day Outbound Board",
        period=f"{start.strftime('%b %-d')} – {today.strftime('%b %-d')} · bars by week",
        labels=week_labels, current_idx=n_weeks - 1,
        dial_target=round(DIALS_PER_DAY * d30), mtg_scale=d30 / 5, weekly=False, updated=updated)

    qs, qe, qn = quarter_bounds(today)
    nqs, nqe, nqn = quarter_bounds(qe)
    reps = (render_pipeline_quarter(data.get("open_deals"), key="thisq", label=f"{qn} pipeline", qs=qs, qe=qe,
                                    qlabel=f"{qn} {qs.year}", today=today, include_overdue=True, updated=updated) +
            render_pipeline_quarter(data.get("open_deals"), key="nextq", label=f"{nqn} pipeline", qs=nqs, qe=nqe,
                                    qlabel=f"{nqn} {nqs.year}", today=today, include_overdue=False, updated=updated))
    tpl = Path(__file__).with_name("template.html").read_text()
    newdeals = render_new_deals(data.get("deals"), now_local, updated, data.get("latest_deal"))
    quota = render_quota(data.get("won"), data.get("open_deals"), now_local, updated)
    wins = render_wins(data.get("won"), now_local, updated)
    leads = render_leads(data.get("lead_objs"), data.get("lead_owners"),
                         start=start, first_mon=first_mon, week_labels=week_labels, now_local=now_local, updated=updated)
    obm = render_outbound_mtgs(week_outbound_meetings(data, monday), data.get("outbound_details"),
                               monday=monday, now_local=now_local, updated=updated)
    page = (tpl.replace("{{SLIDES}}", weekly + rolling + leads + obm + newdeals + wins + quota + reps)
               .replace("{{GEN}}", str(int(now_local.timestamp()))))
    summary = {k: (None if v is None else len(v)) for k, v in data.items()}
    summary["week"] = [{k: r[k] for k in ("name", "dials", "deal_calls", "connects", "meetings", "mtg_existing", "mtg_inbound", "mtg_unlinked", "deals", "sqls", "freemiums")}
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
        data.setdefault("won", None); data.setdefault("latest_deal", None)
        data.setdefault("lead_objs", None); data.setdefault("lead_owners", {})
        data.setdefault("outbound_details", {})
    else:
        token = os.environ.get("HUBSPOT_TOKEN")
        if not token:
            sys.exit("HUBSPOT_TOKEN not set")
        data = fetch(token, since)
        data["latest_deal"] = latest_deal(token)
        others = {c.get("hubspot_owner_id") for c in data["lead_objs"] or []}
        data["lead_owners"] = owner_names(token, sorted(o for o in others if o and o not in REPS))
        data["excluded_calls"] = calls_on_live_deals(token, [c for c in data["calls"]
                                                             if c.get("hs_call_direction") != "INBOUND"])
        data["meeting_class"] = classify_meetings(token, [m for m in data["meetings"] if is_prospect_meeting(m)])
        try:
            monday = now_local.date() - timedelta(days=now_local.weekday())
            data["outbound_details"] = meeting_people(
                token, [m["hs_object_id"] for m in week_outbound_meetings(data, monday) if m.get("hs_object_id")])
        except requests.HTTPError as e:
            print(f"WARN: outbound meeting details unavailable ({e.response.status_code})", file=sys.stderr)
            data["outbound_details"] = {}
    page, summary = build(data, now_local)
    out = Path("dist")
    out.mkdir(exist_ok=True)
    (out / "index.html").write_text(page)
    (out / ".nojekyll").write_text("")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
