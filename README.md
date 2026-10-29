# BCS Outbound Board

The TV page rotates: **Weekly Outbound Board (30s) → 30-Day Outbound Board (20s) → Leads (25s) → Outbound meetings booked this week (25s) → New opportunities: latest one on top + rolling 7 days by rep (25s) → Closed-won celebration (20s) → Quota attainment (20s) → This-quarter pipeline (25s) → Next-quarter pipeline (25s)**. Arrow keys step through pages, space pauses. Add `#board`, `#30d`, `#leads`, `#outbound`, `#newdeals`, `#wins`, `#quota`, `#thisq` or `#nextq` to the URL to pin one page.

**Leads page:** HubSpot **Lead records** (Leads object) created in the last 30 days, by **lead owner** (roster reps, other HubSpot users, and **Unassigned** in red). Three charts: **leads by type per week** (Lead Type: SQL, Freemium, Re-engaged, Upsell, Re-attempting; Monday-start weeks), **leads by rep** (stacked by Lead Type, shared scale), and **lead status by rep** (bar length = lead count, split by Lead pipeline stage New → Attempting → Connected → Qualified, plus Disqualified) with "% connected+" (connected or qualified). Needs `crm.objects.leads.read` on the service key. Chart palettes were checked for color-blind separation on the dark panel.


**Pipeline pages** show every rep as a row, open deals broken out by stage (Qualified → Contract Review, plus Delayed), with total and weighted pipeline. The this-quarter page also has an Overdue column: open deals whose close date passed before the quarter started.

**30-Day board** uses the same metrics over today and the previous 29 days, with weekly bars; targets scale to the business days in the window.

Office-TV dashboard of this week's outbound activity per rep, pulled from HubSpot every 15 minutes (Mon–Fri, ~6am–7pm MT) by GitHub Actions and published to GitHub Pages. The page reloads itself every 5 minutes.

## Metrics
| Column | Definition |
|---|---|
| Dials per day | Calls owned by the rep with direction ≠ Inbound, bucketed Mon–Fri in Mountain time. Week resets Monday 12:00 AM MT. |
| Pace target | 60 dials/day × business days elapsed (today prorated across 8am–5pm). Green ≥100%, amber ≥75%, red below. |
| Talk time | Minutes on connected calls (outbound, 2–15 min, contact not on an open deal). |
| Connect rate | Outbound calls of 2–15 minutes ÷ dials (over 15 min = a demo, not a cold-call connect). HubSpot's call-outcome tag is ignored. Calls to contacts/companies with an open deal or that are existing clients (any closed-won deal) are excluded from both. |
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

Service key scopes needed: `crm.objects.contacts.read`, `crm.objects.owners.read`, `crm.objects.deals.read`, `crm.objects.companies.read`. If a scope is missing that column shows "—" instead of breaking the board.

## Outbound meetings page
Celebrates every **outbound** meeting booked this week (Mon–now, by booked date), newest first, using the same rule as the board's Outbound mtgs column. Each card shows the rep, the prospect (name, title, company), the meeting title, when it was booked and when it meets. A tally per rep vs weekly goal sits at the top (gold when the goal is hit). Up to 12 cards; extras show as "+N more".

## Outbound vs inbound meetings
**Outbound mtgs** (colored vs target): none of the meeting's contacts or companies had a deal (any stage) when it was booked, AND no contact on it is, or has ever been, in the **SQL or Freemium** lifecycle stage. Weekly targets (`MEETING_TARGETS` in `scripts/build.py`): Rick and Thomas 2.5/wk; everyone else (SMB team) 5/wk; prorated by business days elapsed, ×30-day business days on the 30-day board. **Inbound mtgs** (own column, not color-coded): a contact on the meeting is/was SQL or Freemium. Meetings with an existing deal show as "+N on deals"; meetings with no linked contact/company fall back to the meeting type (Discovery Demo = new) and otherwise show as "unlinked". Without deals + companies read access, the board shows all meetings instead.


## New Deals page
Every deal created in the last 7 days (rolling), one column per rep: deal name, amount and current stage, largest first.

## Stage colors
Pipeline stages are colored from red (Contract Review, closest to signature) through orange to yellow (Qualified). Delayed is grey.

## Calls on live deals are excluded
Dials, connects and talk time count outbound prospecting only: a call is dropped if its contact or company has an open deal or is an existing client (any closed-won deal), whatever the owner. The board footer shows how many calls were excluded.

## Quota attainment
Quotas live in `QUOTAS` at the top of `scripts/build.py` (annual amount + the date it starts; quarterly = annual ÷ 4). Current setup: Rick $690K/yr; David $200K/yr; Wyatt and Hunter $500K/yr starting Q1 2027; Thomas $1M/yr starting Q2 2027. Before a rep's start date the row shows "Ramping" with won and pipeline only.
Each row: closed-won this quarter vs quarterly quota (bar with a white pace line for how far through the quarter we are), attainment %, gap, pipeline coverage (open deals this quarter + overdue ÷ gap), forecast (won + weighted pipeline) and year-to-date vs annual quota.

## Celebration pages
**New opportunity**: the single most recently created deal (no confetti — it is not closed yet) by anyone on the roster: rep, deal name, amount, stage, how long ago.
**Closed won**: the most recent closed-won deal as the hero (gold, confetti), the last-30-day tally, and other recent wins underneath (or the previous three wins if it's been quiet).

## Refresh loop
GitHub's scheduler is throttled on small repos, so the workflow re-triggers itself: build + deploy, wait ~14 minutes, dispatch the next run (business hours Mon–Fri 6am–7pm MT; hourly check otherwise). The cron schedule only restarts the loop if it breaks.
