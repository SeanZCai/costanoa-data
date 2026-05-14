---
name: costanoa-search
description: Search the Costanoa Ventures shared meeting knowledge base on Supabase. Use when the user asks questions about meetings, founders, companies, or attendees that any team member has met — e.g. "which cybersecurity founders has Tony met this quarter?", "when did I last talk to Alex?", "list all meetings with Globex", "stats on the knowledge base". Mirrors what the Granola connector does for personal Granola notes, but spans every VC's data across the partnership.
---

# /costanoa-search

Query the team's shared Supabase knowledge base. Returns structured JSON; you should then read it and answer the user in plain English.

## Paths

- Search script: `${CLAUDE_PLUGIN_ROOT}/scripts/search.py`
- Python venv (per user): `~/.costanoa-data/.venv/bin/python`

If `${CLAUDE_PLUGIN_ROOT}` isn't set (dev mode), fall back to `/Users/seancai/CostanoaData`.

If the user isn't authenticated yet (`~/.costanoa-data/session.json` missing), divert to `/granola-sync` first — they need to be logged in via Supabase Email OTP before any query will work. The first sync handles that bootstrap.

## How to use this skill

The user will ask a natural-language question. Your job is to:

1. Decide which `search.py` subcommand fits.
2. Translate names, dates, and tags into the right CLI flags.
3. Run the command.
4. Read the JSON output and answer the user's question conversationally. Don't dump raw JSON at them — that's worse than useless. Quote specific meetings, names, and dates from the results.

## Subcommands

### `search.py meetings [--vc EMAIL] [--company NAME] [--attendee NAME] [--since DATE] [--until DATE] [--text QUERY] [--tag TAG] [--limit N]`

Filters compose with AND. All filters except `--text` pre-filter at the database level (no risk of dropping older matches past the limit).

- `--vc`: filter to one VC's synced meetings. Accepts email or alias. e.g. `--vc sean@costanoa.vc`.
- `--company`: meetings linked to a company whose name contains this substring (case-insensitive). e.g. `--company Veridian`.
- `--attendee`: meetings where an attendee's name or email matches. e.g. `--attendee Alex` or `--attendee alex@veridianvp.com`.
- `--since YYYY-MM-DD`, `--until YYYY-MM-DD`: meeting_date range.
- `--text`: substring match against title + summary. Cheap fallback when the user's question doesn't map to a known entity.
- `--tag`: company tag, e.g. `--tag cybersecurity`. (Tags are user-curated and currently sparse.)
- `--limit N`: default 20. Bump it if the user wants "all" or implies a large set.

Output shape:
```json
{
  "query_type": "meetings",
  "filters": {...},
  "count": 4,
  "results": [
    {
      "id": "...", "title": "Globex <> Sean", "meeting_date": "2026-05-09T...",
      "synced_by": "sean@costanoa.vc", "synced_by_name": "Sean Cai",
      "summary_snippet": "first ~240 chars of summary_md",
      "companies": [{"name": "Globex", "tags": [], ...}],
      "attendees": [{"name": "Sean Cai", "email": "sean@costanoa.vc", ...}]
    }
  ]
}
```

### `search.py companies [--name NAME] [--domain DOMAIN] [--tag TAG] [--with-meeting-count] [--limit N]`

Look up companies. `--with-meeting-count` adds a `meeting_count` field per company and sorts by it descending (useful for "what are the most-mentioned companies?").

### `search.py people [--name NAME] [--email EMAIL] [--company NAME] [--limit N]`

Find an individual. Always returns their `current_company_id` and a hydrated `company_name`.

### `search.py meeting <id-or-external-id>`

Fetch one meeting with the full (non-truncated) summary, every attendee, and every linked company. Use when the user wants details on a specific meeting they already identified.

### `search.py stats`

High-level counts (meetings, companies, individuals, team_members), top 10 companies by meeting count, and meetings synced per VC.

## Example translations

| User says | You run |
|-----------|---------|
| "Which cybersecurity founders has Sean met this quarter?" | `search.py meetings --vc sean@costanoa.vc --tag cybersecurity --since 2026-04-01 --limit 50` |
| "When did Tony last meet anyone from Acme?" | `search.py meetings --vc tony@costanoa.vc --company Acme --limit 5` |
| "What did I discuss with Alex at Veridian?" | `search.py meetings --attendee Alex --company Veridian --limit 5` |
| "List all meetings with Globex" | `search.py meetings --company Globex --limit 50` |
| "How many companies are in the DB?" | `search.py stats` (use the `totals.companies` field) |
| "Find Sam Cooper's profile" | `search.py people --name "Sam Cooper"` |
| "Show me meetings about post-training" | `search.py meetings --text "post-training" --limit 20` |
| "What's the most-discussed company on the team?" | `search.py stats` (use the `top_companies` field) |
| "Pull up the Initech followup" | `search.py meeting 00000000-0000-0000-0000-000000000000` if you have the id; otherwise `search.py meetings --company Initech --text "followup" --limit 5` and then `search.py meeting <id>` once you've identified the right one |

## Calling pattern

```bash
"$HOME/.costanoa-data/.venv/bin/python" "${CLAUDE_PLUGIN_ROOT:-/Users/seancai/CostanoaData}/scripts/search.py" meetings --company Globex --limit 10
```

## After the result lands

Read the JSON. Answer the user's actual question. Concretely:

- If they asked a counting question, give the number first, then briefly cite which meetings drove it.
- If they asked for a list, give 3-5 named items with dates, not a wall of every match.
- If they asked about a specific person or company, lead with the most recent interaction, then summarize the pattern.
- Quote specific snippets from `summary_snippet` when the user asks "what did we talk about?" — they're already truncated to ~240 chars.

If a query returns zero results, don't fail silently. Suggest one of:
- A different spelling (Granola's parsing is messy, especially for solo meetings)
- Dropping a filter
- A broader date range

## Auth failures

If `search.py` reports a 401 or "JWT expired" error, the teammate's session needs refresh. Tell them to run `/granola-sync` once — its Step 0 will re-authenticate.
