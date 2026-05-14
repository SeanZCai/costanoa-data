---
name: granola-sync
description: Sync the current user's net-new Granola meeting notes into Costanoa's Supabase knowledge base. Use when the user says "sync granola", "upload meeting notes", "refresh the knowledge base", or runs /granola-sync. Supports excluding meetings by keyword (e.g. "/granola-sync excluding 'board meeting'"). Pulls summaries + transcripts, resolves attendees and companies, and idempotently upserts into Supabase. First-uploader wins on attribution — meetings already in the DB are never overwritten.
---

# /granola-sync

Sync net-new Granola meeting notes into the Costanoa Supabase knowledge base. Each note is attributed to the VC whose Granola account is connected.

## Duplicate-prevention contract

The sync script only INSERTS meetings; it never overwrites. If a meeting is already in Supabase (from any team member's earlier sync), `process_meeting` short-circuits and the row stays exactly as it was — preserving the original `created_by_team_member_id`, `ingested_at`, and any manual edits. This means:
- Re-running `/granola-sync` is always safe.
- If two VCs are both at a meeting, whoever syncs first becomes the canonical uploader.
- The `--list-synced` diff (Step 2) makes this fast, but the DB-level guard catches anything that slips through.

## Exclusion phrases

Users can ask the sync to skip meetings that contain certain phrases — useful for confidential meetings, internal-only sessions, or noisy entries. Sources, applied as a union:

1. **`EXCLUDE_PHRASES` env var** in `.env` (comma-separated). Applies to every sync, including scheduled/automated runs.
2. **User invocation**: parse the user's message for phrases like "excluding 'X', 'Y'" or "skip anything about Z". Lowercase + trim each. Pass them through to the payload as `exclude_phrases: ["x", "y", "z"]`.

Apply filtering at three layers to short-circuit fetches:
- After Step 3 (list_meetings): drop any meeting whose **title** contains any phrase (case-insensitive substring). These skip both `get_meetings` and `get_meeting_transcript` calls.
- After Step 4 (get_meetings): drop any meeting whose **summary_md** contains any phrase. These skip the transcript fetch.
- After fetching the transcript: drop any meeting whose **transcript** contains any phrase.

Always pass the merged exclusion list into the payload's `exclude_phrases` field — `sync.py` re-checks at insertion time as defense-in-depth.

Report excluded meetings in the final summary, e.g. "Synced 12 new meetings; excluded 4 (matched 'standard data' in title)."

## Paths

When installed as a Claude Code plugin, the skill's own files live under `${CLAUDE_PLUGIN_ROOT}`. The per-user config (env vars, venv) lives under `~/.costanoa-data/`.

- Sync script: `${CLAUDE_PLUGIN_ROOT}/scripts/sync.py`
- Python venv (per user): `~/.costanoa-data/.venv/bin/python`
- Auth session (per user): `~/.costanoa-data/session.json`
- Payload file (transient): `/tmp/granola_sync_payload.json`

Connection details (URL + anon key) ship inside the plugin at `${CLAUDE_PLUGIN_ROOT}/scripts/supabase_config.py` — no env config required of teammates. Teammates authenticate as themselves via Supabase Email OTP; Row Level Security enforces per-VC scope at the database.

If `${CLAUDE_PLUGIN_ROOT}` isn't set (e.g. running from the source repo for development), fall back to `/Users/seancai/CostanoaData` as the plugin root.

## Step 0 — First-run bootstrap (venv + auth)

Two sub-checks, in order. Skip whichever is already done.

### 0a. Venv exists?

```bash
test -d "$HOME/.costanoa-data/.venv" && echo OK || echo VENV_NEEDED
```

If `VENV_NEEDED`, create it:

```bash
mkdir -p "$HOME/.costanoa-data"
python3 -m venv "$HOME/.costanoa-data/.venv"
"$HOME/.costanoa-data/.venv/bin/pip" install --quiet -r "${CLAUDE_PLUGIN_ROOT:-/Users/seancai/CostanoaData}/scripts/requirements.txt"
```

### 0b. Authenticated?

```bash
"$HOME/.costanoa-data/.venv/bin/python" "${CLAUDE_PLUGIN_ROOT:-/Users/seancai/CostanoaData}/scripts/sync.py" --auth-status
```

If the output reports `"authenticated": false`, walk the user through Email OTP:

1. Ask: **"What's your Costanoa email (`@costanoa.vc` or `@costanoavc.com`)?"** Capture as `$EMAIL`.

2. Send the OTP:
   ```bash
   "$HOME/.costanoa-data/.venv/bin/python" "${CLAUDE_PLUGIN_ROOT:-/Users/seancai/CostanoaData}/scripts/sync.py" --auth-start "$EMAIL"
   ```
   Tell the user: "Check your email — Supabase just sent a 6-digit code (subject usually 'Your code is …'). Paste it here."

3. Capture the code as `$CODE`, verify:
   ```bash
   "$HOME/.costanoa-data/.venv/bin/python" "${CLAUDE_PLUGIN_ROOT:-/Users/seancai/CostanoaData}/scripts/sync.py" --auth-verify "$EMAIL" "$CODE"
   ```
   Success looks like `"status": "ready"`. On failure (wrong/expired code), surface the error and have the user re-trigger `--auth-start`.

After both 0a and 0b are satisfied, proceed to Step 1. The session persists in `~/.costanoa-data/session.json` and refreshes automatically — no re-auth needed on subsequent runs (refresh tokens are long-lived).

If the user's email isn't on `@costanoa.vc` or `@costanoavc.com`, the OTP send succeeds but every DB call will be denied by RLS with a clear error. Surface that and ask whether they're using the right Costanoa email.

## Step 1 — Identify the VC

Call `mcp__claude_ai_Granola__get_account_info`. Capture:
- `email` (e.g. `sean@costanoa.vc`)
- `active_workspace.id` → `granola_workspace_id`
- `name`: derive from the email local-part, title-cased (`sean@costanoa.vc` → `Sean`). The DB seed already has the correct full name; this is only used on first sync for never-seen VCs.

## Step 2 — Get already-synced meeting IDs

Critical: do this BEFORE fetching transcripts (transcripts are ~100KB each and would blow context).

```bash
"$HOME/.costanoa-data/.venv/bin/python" "${CLAUDE_PLUGIN_ROOT:-/Users/seancai/CostanoaData}/scripts/sync.py" --list-synced
```

Output is a JSON array of Granola meeting UUIDs already in Supabase. Hold as `synced_ids`.

## Step 3 — List recent meetings

Call `mcp__claude_ai_Granola__list_meetings` with `time_range='last_30_days'` by default. If the user explicitly asked for a wider window (e.g. "sync the last 6 months"), use `time_range='custom'` with appropriate dates.

Filter the returned meetings:
- Drop meetings whose `id` is in `synced_ids`.
- Drop meetings whose title is exactly `"Busy"`, starts with `"Busy "`, or is `"hold"` / `"amy hold"` / similar calendar placeholder.
- **Drop meetings whose `title` (case-insensitive) contains any exclusion phrase** (see "Exclusion phrases" above).

Call the survivors `new_meetings`. If empty, report "0 new meetings to sync" and stop.

## Step 4 — Fetch full meeting details

In batches of up to 10 UUIDs, call `mcp__claude_ai_Granola__get_meetings(meeting_ids=[...])` for each batch.

After this call, **drop any meeting whose `summary_md` contains an exclusion phrase** (case-insensitive). Skip the transcript fetch for those.

For each survivor, call `mcp__claude_ai_Granola__get_meeting_transcript(meeting_id=...)` to fetch the verbatim transcript. Transcripts may be empty for short meetings — that's fine; store as `null`. After fetching, **drop any meeting whose `transcript` contains an exclusion phrase**.

## Step 5 — Parse `known_participants`

Each meeting's `known_participants` is a comma-separated string. Per-attendee format:

    Name [(note creator)] [from Company] <email@domain>

Examples (from real data):
- `Sean (note creator) from Costanoa <sean@costanoa.vc>` → `{name:"Sean", is_note_creator:true, company_hint:"Costanoa", email:"sean@costanoa.vc"}`
- `Antonibertel <antonibertel@gmail.com>` → `{name:"Antonibertel", company_hint:null, email:"antonibertel@gmail.com"}`
- `Alex from Veridianvp <alex@veridianvp.com>` → `{name:"Alex", company_hint:"Veridianvp", email:"alex@veridianvp.com"}`

Skip any "attendee" whose email contains `@resource.calendar.google.com` — those are Zoom-room calendar resources, not people.

## Step 6 — LLM enrichment for single-participant meetings

For each meeting where the parsed `known_participants` resolves to exactly 1 real person (just the note creator), read the title + summary and infer the counterparty. Add `title_inferred` to the meeting record:

```json
{
  "counterparty_name": "Sam Cooper",
  "counterparty_company": null,
  "confidence": 0.7
}
```

Heuristics:
- `"Globex <> Sean"` → counterparty_company="Globex"
- `"Sam Cooper <> Sean"` → counterparty_name="Sam Cooper"
- `"Alex (Veridian) <> Sean (Costanoa)"` → counterparty_name="Alex", counterparty_company="Veridian"
- `"Sean / Antoni"` → counterparty_name="Antoni"
- Use the summary body to fill missing fields (e.g. if summary says "Antoni's company X", set company="X").
- `confidence`: ~0.9 when title+summary both confirm; ~0.6 when only title hints; ~0.4 when guessing.
- If you genuinely can't infer, OMIT `title_inferred` — don't fabricate.

## Step 7 — Build the payload

```json
{
  "team_member": {
    "email": "sean@costanoa.vc",
    "name": "Sean",
    "granola_workspace_id": "..."
  },
  "synced_at": "2026-05-11T19:00:00Z",
  "exclude_phrases": ["standard data"],
  "meetings": [
    {
      "external_id": "<uuid>",
      "title": "...",
      "meeting_date": "2026-05-10T19:00:00+00:00",
      "summary_md": "...",
      "transcript": "...",
      "known_participants": [
        {"name":"Sean","email":"sean@costanoa.vc","company_hint":"Costanoa","is_note_creator":true}
      ],
      "raw_payload": { /* whatever the MCP returned */ },
      "title_inferred": { "counterparty_name":"...", "counterparty_company":"...", "confidence":0.7 }
    }
  ]
}
```

`exclude_phrases` is the merged list (env defaults + user invocation). `sync.py` re-applies it at insertion time.

For `meeting_date`, convert the human-readable date (e.g. "May 10, 2026 12:00 PM PDT") to ISO 8601 with timezone offset.

Write the payload to `/tmp/granola_sync_payload.json` using the Write tool.

## Step 8 — Run the upsert

```bash
"$HOME/.costanoa-data/.venv/bin/python" "${CLAUDE_PLUGIN_ROOT:-/Users/seancai/CostanoaData}/scripts/sync.py" /tmp/granola_sync_payload.json
```

(If you want to pass exclusions via flag instead of payload, append `--exclude 'phrase1,phrase2'`.)

The script prints a JSON summary:
```json
{
  "team_member_email": "sean@costanoa.vc",
  "new_meetings": 12,
  "updated_meetings": 0,
  "total_processed": 12,
  "latest_meeting_date": "2026-05-10T19:00:00+00:00"
}
```

## Step 9 — Report

Report a one-line summary to the user. Example:

> Synced 12 new Granola meetings into Supabase (latest: May 10, 2026).

## Failure mode

If any step errors, surface the error and stop — do NOT continue with partial data.
