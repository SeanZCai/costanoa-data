---
name: granola-sync-all
description: Backfill the full history of the current user's Granola meeting notes into Costanoa's Supabase knowledge base. Use when the user says "sync everything", "do a full backfill", "import all my Granola history", "sync all my meetings", or runs /granola-sync-all. Same authentication, exclusion, and idempotency semantics as /granola-sync. The only difference is the time window — this one pulls everything since the user's Granola account started.
---

# /granola-sync-all

Pull every Granola meeting in the connected user's history into Supabase, in one go. Same auth path, same exclusion filter, same first-uploader-wins attribution as `/granola-sync`. The only difference is no 30-day window — this fetches from the start of the account.

## When this vs `/granola-sync`

- `/granola-sync` — incremental, default last 30 days. What you run daily (or schedule).
- `/granola-sync-all` — full historical backfill. Run once when you first install the plugin, or after a long gap. After it finishes, the daily auto-sync via `/schedule` keeps things current going forward.

It is safe to run more than once. The dedup via `--list-synced` and the no-overwrite policy in `sync.py` mean you'll never duplicate or clobber existing rows.

## Paths

Same as `/granola-sync`:
- Sync script: `${CLAUDE_PLUGIN_ROOT}/scripts/sync.py`
- Per-user venv: `~/.costanoa-data/.venv/bin/python`
- Per-user session: `~/.costanoa-data/session.json`
- Transient payload: `/tmp/granola_sync_all_payload.json`

If `${CLAUDE_PLUGIN_ROOT}` isn't set (dev mode), fall back to `/Users/seancai/CostanoaData`.

## Step 0 — Bootstrap (auth + venv)

Identical to `/granola-sync` Step 0. Check that `~/.costanoa-data/.venv` exists and that the user is authenticated (`sync.py --auth-status` reports `"authenticated": true`). If either is missing, walk through venv creation and Email OTP exactly as the `granola-sync` skill documents.

## Step 1 — Identify the VC + collect exclusions

Call `mcp__claude_ai_Granola__get_account_info`. Capture `email` and `active_workspace.id` (the workspace_id).

Read the user's persistent exclusions from `~/.costanoa-data/.env` (the `EXCLUDE_PHRASES=` line if present). Parse the user's invocation for any inline exclusions of the form `excluding "X", "Y"`. Merge into a single list, lowercased.

## Step 2 — Diff against what's already in Supabase

```bash
"$HOME/.costanoa-data/.venv/bin/python" "${CLAUDE_PLUGIN_ROOT:-/Users/seancai/CostanoaData}/scripts/sync.py" --list-synced
```

Output is a JSON array of Granola meeting UUIDs already in Supabase. Hold as `synced_ids`. If the user has previously run any sync (incremental or full), most of those IDs will already be in there and only the gaps will need fetching.

## Step 3 — List the entire Granola history

Call:
```
mcp__claude_ai_Granola__list_meetings(
  time_range='custom',
  custom_start='2018-01-01',
  custom_end=<today>
)
```

`2018-01-01` predates any plausible Granola account start, so this returns the full history.

If the result is large enough that the MCP framework auto-saves it to disk (over ~80k characters), the response will tell you the path. Read it from disk rather than parsing the in-context blob.

## Step 4 — Filter to survivors

Drop:
- Anything whose `id` is in `synced_ids` (already in Supabase).
- Placeholders: title is exactly `Busy`, starts with `Busy ` (case-insensitive), or is one of `hold`, `amy hold`, `hold for kaizen`, or starts with `hold `.
- Anything whose title (case-insensitive substring) contains any phrase in the merged exclusion list.

Count the survivors. Also note the date range (earliest and latest meeting_date among survivors).

If survivors is zero, report "Everything's already synced. Nothing new to do." and stop.

## Step 5 — Decide inline vs subagent

If survivors **≤ 20**, do the work inline. Follow Steps 4-8 of the `/granola-sync` skill (fetch in batches of up to 10 via `get_meetings`, parse `known_participants`, run LLM enrichment on single-participant meetings, build the JSON payload, run `sync.py`).

If survivors **> 20**, delegate to a subagent. This is the path that handled Sean's 250+ meeting backfill cleanly — putting all those fetches into the main conversation's context would burn through it.

### Subagent dispatch

Save the survivor list to disk first so the subagent doesn't inherit it through context:

```bash
mkdir -p /tmp
python3 -c "
import json
survivors = [
  # list of {id, title, date} dicts you filtered down to in Step 4
]
json.dump(survivors, open('/tmp/granola_sync_all_survivors.json', 'w'))
"
```

Then use the `Agent` tool with `subagent_type=general-purpose` and this prompt template (fill in the bracketed values):

> You are running a bulk Granola → Supabase backfill for the Costanoa Data plugin. The user has already authenticated (session at `~/.costanoa-data/session.json`); use that, not service role.
>
> Inputs:
> - Survivor list: `/tmp/granola_sync_all_survivors.json` — JSON array of `{id, title, date}` for the meetings to fetch and sync. There are [N] of them.
> - Exclusion phrases: `[list]`
> - Team member: `{email: '[email]', name: '[derived from email]', granola_workspace_id: '[workspace_id]'}`
> - Sync script: `~/.costanoa-data/.venv/bin/python ${CLAUDE_PLUGIN_ROOT:-/Users/seancai/CostanoaData}/scripts/sync.py`
>
> Procedure:
> 1. Process the survivor list in batches of 10 via `mcp__claude_ai_Granola__get_meetings(meeting_ids=[...])`.
> 2. Do NOT call `get_meeting_transcript` — for the bulk backfill we keep transcripts null to stay within MCP and context budgets. Summaries carry the searchable content.
> 3. Parse each meeting's `known_participants` (comma-separated `Name [from Company] <email>` format). Skip Zoom-room emails (`@resource.calendar.google.com`).
> 4. For meetings where only the note creator appears in `known_participants`, LLM-infer the counterparty name and/or company from the title + summary. Confidence 0.9 if title+summary both confirm, 0.7 if only the title hints, 0.5 if guessing. Skip the field if you genuinely can't tell.
> 5. Accumulate everything into a single payload at `/tmp/granola_sync_all_payload.json` with the shape:
>    ```json
>    {
>      "team_member": {"email": ..., "name": ..., "granola_workspace_id": ...},
>      "exclude_phrases": [...],
>      "meetings": [
>        {"external_id": ..., "title": ..., "meeting_date": ISO, "summary_md": ..., "transcript": null, "known_participants": [...], "title_inferred": {...}}
>      ]
>    }
>    ```
> 6. Run the sync:
>    ```bash
>    "$HOME/.costanoa-data/.venv/bin/python" "${CLAUDE_PLUGIN_ROOT:-/Users/seancai/CostanoaData}/scripts/sync.py" /tmp/granola_sync_all_payload.json
>    ```
> 7. Honor the exclusion filter at every layer — drop by title pre-fetch, by summary post-fetch, and rely on `sync.py`'s defensive recheck at insert.
>
> Return a short status report under 200 words:
> - Total processed, new inserted, already-synced skipped, excluded count.
> - Date range covered (earliest → latest).
> - Number of new companies, individuals, attendee links created (read from the sync.py output JSON).
> - Any failures or anomalies.
> - Do NOT paste payload contents, summaries, or full meeting details. Just the numbers and any specific errors.

### After the subagent returns

The subagent's report is short. Pass the numbers through to the user verbatim. Don't elaborate beyond what's reported — those counts came from a privileged read of the data; speculating about content is not your job here.

## Step 6 — Report to the user

A one-liner is enough:

> Backfilled N new meetings into Supabase (M already synced, K excluded). Earliest synced meeting: YYYY-MM-DD. Daily auto-sync via `/schedule` will keep things current from here on.

## Notes

- **Transcripts stay null** in this path. The shared summaries carry enough signal for the `/costanoa-search` skill to answer most questions. If a teammate later wants verbatim transcripts on specific meetings, that's a future per-meeting backfill operation.
- **Very large histories** (1000+ meetings) may exceed a single subagent's comfort. If the survivor count is over ~250, split the survivor list into chunks of 200 and dispatch sequential subagents, each handling one chunk. Each finishes and writes its own payload to `/tmp/granola_sync_all_payload_chunkN.json`; each runs `sync.py` against its chunk.
- **Idempotency**: re-running this skill after a partial run picks up the gap. The `--list-synced` diff in Step 2 sees what's already there.
