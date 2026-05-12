---
name: team-onboard
description: Manually register a Costanoa teammate in the knowledge base by email. Use when a new VC joins and they haven't yet connected Granola/Claude themselves, or to add an alias email to an existing teammate. Validates the email is on a Costanoa domain.
---

# /team-onboard

Register a Costanoa teammate in the `team_members` table without waiting for them to run `/granola-sync` themselves. Used for new hires before they're set up, or to add a second email (e.g. `tony@costanoavc.com`) as an alias to an existing row (e.g. `tony@costanoa.vc`, name "Tony Liu").

## Usage

The user provides an email and optionally a full name:

> /team-onboard newhire@costanoa.vc "New Hire Name"
> /team-onboard tony@costanoavc.com "Tony Liu"

## Step 1 — Validate the input

Confirm the email's domain is in `costanoa.vc` or `costanoavc.com` (whatever `TEAM_DOMAINS` in `.env` allows; default is both). If not, refuse and tell the user.

If the user didn't pass a name, ask them for one. Names matter: when an email's domain matches but its primary email doesn't exist yet, the script tries to find an existing teammate by exact (case-insensitive) name and append the new email as an alias. So a missing or mismatched name will create a duplicate row instead of merging.

## Step 2 — Run the helper

```bash
"$HOME/.costanoa-data/.venv/bin/python" "${CLAUDE_PLUGIN_ROOT:-/Users/seancai/CostanoaData}/scripts/sync.py" --add-teammate "<email>" "<name>"
```

(If you see "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY required", the user hasn't run `/granola-sync` yet to complete first-run bootstrap. Have them do that first, then retry.)

The script prints a JSON object like:

```json
{
  "team_member_id": "6e1a573d-...",
  "email": "tony@costanoavc.com",
  "name": "Tony Liu",
  "created": false,
  "alias_added": true,
  "attendee_rows_backfilled": 4
}
```

Three outcomes:
- `created: true` — brand new teammate row.
- `alias_added: true` — name matched an existing row; the email was appended to that row's `aliases`. Historical attendee rows for this email flipped to `is_team_member=true`.
- both `false` — the email or alias already existed; no-op.

## Step 3 — Report

Translate the JSON into a one-line summary for the user. Examples:

> Added New Hire Name (newhire@costanoa.vc) to the team.
> Merged tony@costanoavc.com into existing teammate Tony Liu. Backfilled 4 historical attendee rows.
> tony@costanoa.vc is already on the team — nothing to do.
