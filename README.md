# Costanoa Data — VC Knowledge Base

Shared Supabase-backed knowledge base for Costanoa Ventures. Ingests meeting
notes from disparate sources (starting with Granola) and routes them into
relational `companies` / `individuals` / `meetings` so downstream agents can
answer questions like _"which cybersecurity founders has John met?"_.

## What's in v0

- Supabase schema: `team_members`, `companies`, `individuals`, `meetings`,
  `meeting_attendees`, `meeting_companies`, `sync_state` (see
  `supabase/schema.sql`).
- `/granola-sync` Claude Code skill — each VC runs this against their own
  Granola account; new notes are attributed to them via
  `meetings.created_by_team_member_id`.
- Two recurring upload paths: `/schedule` (cloud) primary, `launchd` (local)
  fallback.

Future sources (Google Docs ingestion, image attachments, RLS) are deferred.

---

## One-time setup

### Admin-side: apply the schema (once, ever)

Open the Supabase SQL editor for project `lgnejbduwpytgbvtnhpr` and run
`supabase/schema.sql`. Then apply each file in `supabase/migrations/` in
order. Both are idempotent — safe to re-run.

This is a one-time step for the team. New teammates do NOT need to do this.

### Teammate-side: install the plugin (~2 minutes)

In Claude Code, run:

```
/plugin marketplace add SeanZCai/costanoa-data
/plugin install costanoa-data@costanoa-data
```

That's it. No keys to share. The skills (`/granola-sync`, `/team-onboard`)
auto-load. The first time you run `/granola-sync`, the skill:

1. Creates a local Python venv at `~/.costanoa-data/.venv` and installs deps.
2. Asks for your `@costanoa.vc` email and sends a 6-digit code via Supabase
   Email OTP.
3. You paste the code; the resulting session is saved to
   `~/.costanoa-data/session.json` (chmod 600). The session auto-refreshes;
   you only authenticate once.
4. Sync proceeds.

Row Level Security policies (see `supabase/migrations/0002_rls.sql`) enforce
that you can only insert meetings attributed to your own `team_members` row,
and that only authenticated Costanoa users can read anything.

Prerequisites: Claude Code installed, the Granola integration connected
(Settings → Integrations → Granola), and Python 3.10+ on the path.

---

## Daily automation

### Option A (recommended): `/schedule`

Inside Claude Code, run:

```
> /schedule create daily at 7am: /granola-sync
```

This registers a cloud cron that runs the skill against your Granola account
every morning. No laptop required. Manage with `/schedule list`,
`/schedule delete`.

### Option B (fallback): `launchd`

For VCs who don't keep Claude Code open. Requires the Claude Code CLI
(`brew install claude`) on the path.

```bash
cp automation/launchd/vc.costanoa.granola-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/vc.costanoa.granola-sync.plist
```

This fires daily at 7am local. Logs to
`automation/launchd/sync.log` and `sync.err.log`. To stop:

```bash
launchctl unload ~/Library/LaunchAgents/vc.costanoa.granola-sync.plist
```

Edit the plist's `ProgramArguments` if your `claude` binary lives elsewhere
(check with `which claude`).

---

## Query examples

```sql
-- All cybersecurity-tagged companies Sean has met
select c.name, m.title, m.meeting_date
from meetings m
join meeting_companies mc on mc.meeting_id = m.id
join companies c on c.id = mc.company_id
where 'cybersecurity' = any(c.tags)
  and m.created_by_team_member_id = (select id from team_members where email='sean@costanoa.vc')
order by m.meeting_date desc;

-- Full-text search across all summaries
select title, meeting_date,
       ts_rank(to_tsvector('english', summary_md), plainto_tsquery('english', 'agent infra')) as rank
from meetings
where to_tsvector('english', summary_md) @@ plainto_tsquery('english', 'agent infra')
order by rank desc
limit 20;

-- Who has met Costanoa's portfolio company "Globex"?
select distinct tm.name, count(m.*) as meetings
from meetings m
join meeting_companies mc on mc.meeting_id = m.id
join companies c on c.id = mc.company_id
join team_members tm on tm.id = m.created_by_team_member_id
where c.name ilike 'globex'
group by tm.name
order by meetings desc;
```

---

## Repo layout

```
costanoa-data/
├── .claude-plugin/
│   ├── plugin.json            # plugin manifest
│   └── marketplace.json       # marketplace declaring this single plugin
├── .claude/
│   └── skills/
│       ├── granola-sync/SKILL.md
│       └── team-onboard/SKILL.md
├── scripts/
│   ├── sync.py                # idempotent upserter (runs in per-user venv)
│   └── requirements.txt
├── supabase/
│   ├── schema.sql             # initial DDL + seed (admin-only, applied once)
│   └── migrations/
│       └── 0001_team_member_aliases.sql
├── automation/
│   └── launchd/
│       └── vc.costanoa.granola-sync.plist
├── setup-guide.html           # html guide for teammates (link-send)
└── README.md                  # this file
```

Per-user state lives under `~/.costanoa-data/` (created by the bootstrap):
- `~/.costanoa-data/.env` — their Supabase keys (chmod 600)
- `~/.costanoa-data/.venv` — Python venv with `supabase` + `python-dotenv`

## Onboarding new VCs

When a new teammate joins Costanoa, they appear in `team_members` automatically
via one of three paths — no schema edits required.

**Path 1 — their first `/granola-sync`.** Whenever someone with a
`@costanoa.vc` (or `@costanoavc.com`) email runs `/granola-sync`, the script
checks the `TEAM_DOMAINS` allowlist and creates their `team_members` row if
absent. Subsequent runs are idempotent.

**Path 2 — auto-promotion from attendee data.** When `tony@costanoa.vc` shows
up as an attendee in Sean's meeting (even before Tony has synced anything
himself), Tony's `team_members` row is created on the spot. Sean's historical
meetings where Tony attended also get `is_team_member=true` flipped on for
those attendee rows — backfilled in one indexed update per newly-recognized
email.

**Path 3 — manual `/team-onboard`.** For new hires who haven't yet connected
Granola or Claude:

```
/team-onboard newhire@costanoa.vc "Full Name"
```

This is the same skill internally — calls `python scripts/sync.py
--add-teammate <email> [name]`.

### Email aliasing

A single VC can have multiple Costanoa emails (e.g. `tony@costanoa.vc` AND
`tony@costanoavc.com`). When the auto-promotion path sees the second email,
it does an exact case-insensitive name match against existing `team_members`
rows. If "Tony Liu" already exists at one address, the second email is
appended to that row's `aliases` array — no duplicate row. All existing
attendee rows under the new alias also flip to `is_team_member=true`.

**Limitation**: if Granola parses different names from the two emails (e.g.
"Tony Liu" vs just "Tony"), the name match fails and a duplicate row is
created. A future `/merge-teammate <canonical_email> <duplicate_email>` skill
will consolidate manually.

### Defensive gating

A run of `/granola-sync` from a non-Costanoa Granola account (e.g. someone
signed in with a personal email) exits with a clear error rather than
silently polluting `team_members`. The allowlist lives in `TEAM_DOMAINS`.

---

## Extending to new sources

The `meetings` table already has `source` and `external_id` columns. Adding
Google Docs ingestion later means: build a parallel `/gdoc-sync` skill that
fetches docs, runs a foundation-model parse to extract attendees + topics +
date, and writes payloads in the same shape that `sync.py` already consumes
(with `source='gdoc'`). No schema changes required.
