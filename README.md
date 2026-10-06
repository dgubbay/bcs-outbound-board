# BCS Outbound Board

The TV page rotates: **Weekly Outbound Board (30s) → 30-Day Outbound Board (20s) → one pipeline page per rep (15s each)**. Arrow keys step through pages, space pauses. Add `#board`, `#30d`, `#rick`, etc. to the URL to pin one page.

**Pipeline pages** show each rep's open deals (BCS Sales + Expansion pipelines) in stage columns, largest first, with open/weighted/this-quarter totals and flags for past-due close dates and deals with no notes in 14+ days. Delayed deals are shown dimmed and excluded from totals.

**30-Day board** uses the same metrics over today and the previous 29 days, with weekly bars; targets scale to the business days in the window.

Office-TV dashboard of this week's outbound activity per rep, pulled from HubSpot every 15 minutes (Mon–Fri, ~6am–7pm MT) by GitHub Actions and published to GitHub Pages. The page reloads itself every 5 minutes.

## Metrics
| Column | Definition |
|---|---|
| Dials per day | Calls owned by the rep with direction ≠ Inbound, bucketed Mon–Fri in Mountain time. Week resets Monday 12:00 AM MT. |
| Pace target | 60 dials/day × business days elapsed (today prorated across 8am–5pm). Green ≥100%, amber ≥75%, red below. |
| Talk time | Sum of `hs_call_duration` on calls with outcome **Connected**. |
| Connect rate | Calls tagged Connected **and** lasting 2–15 min ÷ dials. |
| Meetings booked | Meetings created this week by the rep, booked ahead of their start time, excluding titles containing interview / round / role discussion / training / test / hold: / internal / 1:1, and auto-logged Zoom/Teams records. |

## Setup
1. Repo secret `HUBSPOT_TOKEN` = HubSpot private app token (scopes: `crm.objects.contacts.read`, `crm.objects.owners.read`).
2. Settings → Pages → Source: **GitHub Actions**.
3. Actions → *Build outbound board* → **Run workflow** to publish immediately.

## Changing the roster
Edit `REPS` (HubSpot owner ID → name) at the top of `scripts/build.py`. Find owner IDs in HubSpot under Settings → Users & Teams.

## On the TV
Open the Pages URL in the TV browser, full screen. Turn off the TV's screensaver / auto-sleep. A red bar appears at the bottom if data is >45 min old during business hours.

| New deals | Deals (any pipeline) owned by the rep with create date this week; count + total amount. |
| SQLs routed | Contacts owned by the rep whose "Date entered Sales Qualified Lead" is this week. |
| Freemiums routed | Contacts owned by the rep whose Freemium Sign Up Date is this week. |

Service key scopes needed: `crm.objects.contacts.read`, `crm.objects.owners.read`, `crm.objects.deals.read`. If a scope is missing that column shows "—" instead of breaking the board.
