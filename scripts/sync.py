#!/usr/bin/env python3
"""Costanoa Granola → Supabase sync.

Consumes a JSON payload produced by the /granola-sync skill and idempotently
upserts team_members, companies, individuals, meetings, and the
meeting_attendees + meeting_companies junctions.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from supabase import Client, create_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.de", "yahoo.fr", "yahoo.co.uk", "yahoo.co.jp",
    "hotmail.com", "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me",
    "aol.com",
    "duck.com",
    "fastmail.com",
}


def is_personal_domain(domain: str | None) -> bool:
    if not domain:
        return True
    d = domain.lower()
    if d in PERSONAL_DOMAINS:
        return True
    if d.endswith(".edu") or d.endswith(".ac.uk") or d.endswith(".edu.au"):
        return True
    return False


def domain_of(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[1].lower()


USER_CONFIG_ENV = Path.home() / ".costanoa-data" / ".env"


def load_env() -> tuple[str, str]:
    # Per-user config (used when installed as a Claude Code plugin) wins.
    # Falls back to the project-local .env for dev mode in the source repo.
    if USER_CONFIG_ENV.exists():
        load_dotenv(USER_CONFIG_ENV)
    else:
        load_dotenv(PROJECT_ROOT / ".env")
    url = os.environ.get("SUPABASE_URL")
    key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
    )
    if not url or not key:
        sys.exit(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY required. "
            f"Looked at {USER_CONFIG_ENV} and {PROJECT_ROOT / '.env'}. "
            "If you just installed the plugin, run /granola-sync — it bootstraps the config on first run."
        )
    return url, key


def team_domains_from_env() -> list[str]:
    raw = os.environ.get("TEAM_DOMAINS", "costanoa.vc,costanoavc.com")
    return [d.strip().lower() for d in raw.split(",") if d.strip()]


def exclude_phrases_from_env() -> list[str]:
    """Default exclusion phrases applied to every sync (automated or manual).
    Configured via EXCLUDE_PHRASES in .env, comma-separated."""
    raw = os.environ.get("EXCLUDE_PHRASES", "")
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def should_exclude(meeting: dict, phrases: list[str]) -> str | None:
    """Return a human-readable reason if any phrase appears in the meeting's
    title, summary_md, or transcript (case-insensitive substring match)."""
    if not phrases:
        return None
    for field in ("title", "summary_md", "transcript"):
        text = (meeting.get(field) or "").lower()
        if not text:
            continue
        for p in phrases:
            if p and p in text:
                return f"matched '{p}' in {field}"
    return None


_ALIASES_SUPPORTED: bool | None = None


def aliases_supported(sb: Client) -> bool:
    """Cached one-time probe: does team_members have the `aliases` column?
    Lets the script run before/after the 0001 migration is applied."""
    global _ALIASES_SUPPORTED
    if _ALIASES_SUPPORTED is None:
        try:
            sb.table("team_members").select("aliases").limit(1).execute()
            _ALIASES_SUPPORTED = True
        except Exception:
            _ALIASES_SUPPORTED = False
    return _ALIASES_SUPPORTED


def ensure_team_member(sb: Client, email: str, name: str) -> tuple[str, bool, bool]:
    """Resolve a Costanoa email to a team_members row.

    Resolution order: primary email → alias → exact case-insensitive name → insert.
    When name matches an existing row, the email is appended to that row's
    `aliases` array (this is how `tony@costanoa.vc` and `tony@costanoavc.com`
    collapse into one entity).

    Returns (team_member_id, was_created, alias_added).
    Never overwrites `name` on an existing row — preserves seeded/manual values.
    """
    email = email.lower()
    name_clean = (name or "").strip()

    has_aliases = aliases_supported(sb)

    # 1. Primary email
    res = sb.table("team_members").select("id").eq("email", email).limit(1).execute()
    if res.data:
        return res.data[0]["id"], False, False

    # 2. Existing alias (skipped silently if migration 0001 hasn't been applied)
    if has_aliases:
        res = sb.table("team_members").select("id").contains("aliases", [email]).limit(1).execute()
        if res.data:
            return res.data[0]["id"], False, False

    # 3. Name match (case-insensitive exact). Names assumed unique within the firm.
    if name_clean and has_aliases:
        res = sb.table("team_members").select("id, aliases").ilike("name", name_clean).limit(1).execute()
        if res.data:
            tm = res.data[0]
            current_aliases = tm.get("aliases") or []
            if email not in (a.lower() for a in current_aliases):
                new_aliases = list({*current_aliases, email})
                sb.table("team_members").update({"aliases": new_aliases}).eq("id", tm["id"]).execute()
            return tm["id"], False, True

    # 4. Brand-new team member
    row: dict = {"email": email}
    if name_clean:
        row["name"] = name_clean
    inserted = sb.table("team_members").insert(row).execute()
    return inserted.data[0]["id"], True, False


def backfill_is_team_member(sb: Client, email: str) -> int:
    """Flip is_team_member=true on any meeting_attendees row whose individual
    has this email. Run after a team_members row is created or an alias is added,
    so historical attribution catches up automatically."""
    email = email.lower()
    ind = sb.table("individuals").select("id").eq("email", email).execute()
    if not ind.data:
        return 0
    individual_ids = [r["id"] for r in ind.data]
    res = (
        sb.table("meeting_attendees")
        .update({"is_team_member": True})
        .in_("individual_id", individual_ids)
        .eq("is_team_member", False)
        .execute()
    )
    return len(res.data or [])


def set_workspace_id_if_missing(sb: Client, team_member_id: str, workspace_id: str | None) -> None:
    if not workspace_id:
        return
    existing = sb.table("team_members").select("granola_workspace_id").eq("id", team_member_id).single().execute()
    if not existing.data.get("granola_workspace_id"):
        sb.table("team_members").update({"granola_workspace_id": workspace_id}).eq("id", team_member_id).execute()


def upsert_company(sb: Client, name: str, domain: str | None) -> str:
    name = (name or "").strip()
    if domain:
        existing = sb.table("companies").select("id").eq("domain", domain).limit(1).execute()
        if existing.data:
            return existing.data[0]["id"]
    if not name:
        # Last-resort fallback so we never insert a NULL name.
        name = domain or "Unknown"
    row = {"name": name}
    if domain:
        row["domain"] = domain
    sb.table("companies").upsert(row, on_conflict="name").execute()
    res = sb.table("companies").select("id").eq("name", name).single().execute()
    return res.data["id"]


def upsert_individual(sb: Client, name: str, email: str | None, company_id: str | None) -> str:
    if email:
        email = email.lower()
        existing = sb.table("individuals").select("id, name, current_company_id").eq("email", email).limit(1).execute()
        if existing.data:
            ind_id = existing.data[0]["id"]
            updates: dict = {}
            if name and len(name) > len((existing.data[0].get("name") or "")):
                updates["name"] = name
            if company_id and not existing.data[0].get("current_company_id"):
                updates["current_company_id"] = company_id
            if updates:
                sb.table("individuals").update(updates).eq("id", ind_id).execute()
            return ind_id
    row = {"name": name or (email or "Unknown")}
    if email:
        row["email"] = email
    if company_id:
        row["current_company_id"] = company_id
    inserted = sb.table("individuals").insert(row).execute()
    return inserted.data[0]["id"]


def insert_meeting_if_new(sb: Client, meeting: dict, team_member_id: str) -> tuple[str, bool]:
    """Insert a meeting row only if its (source, external_id) hasn't been seen.

    Returns (meeting_id, is_new). When the row already exists we return its id
    untouched — preserving the first uploader as `created_by_team_member_id`
    and avoiding any churn on `ingested_at`, `summary_md`, `transcript`, etc.
    This is the duplicate-prevention contract: re-running, or two VCs syncing
    the same meeting, cannot overwrite the canonical row.
    """
    external_id = meeting["external_id"]
    existing = (
        sb.table("meetings")
        .select("id")
        .eq("source", "granola")
        .eq("external_id", external_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        return existing.data[0]["id"], False
    row = {
        "source": "granola",
        "external_id": external_id,
        "title": meeting["title"],
        "meeting_date": meeting["meeting_date"],
        "summary_md": meeting.get("summary_md"),
        "transcript": meeting.get("transcript"),
        "created_by_team_member_id": team_member_id,
        "raw_payload": meeting.get("raw_payload", {}),
    }
    inserted = sb.table("meetings").insert(row).execute()
    return inserted.data[0]["id"], True


def attach_attendee(sb, meeting_id, individual_id, *, is_team_member, role, source, confidence=1.0):
    sb.table("meeting_attendees").upsert({
        "meeting_id": meeting_id,
        "individual_id": individual_id,
        "is_team_member": is_team_member,
        "role": role,
        "source": source,
        "confidence": confidence,
    }, on_conflict="meeting_id,individual_id").execute()


def attach_company(sb, meeting_id, company_id, *, relation_type, source, confidence=1.0):
    sb.table("meeting_companies").upsert({
        "meeting_id": meeting_id,
        "company_id": company_id,
        "relation_type": relation_type,
        "source": source,
        "confidence": confidence,
    }, on_conflict="meeting_id,company_id").execute()


def get_team_member_emails(sb: Client) -> set[str]:
    """Union of primary emails and all aliases — every email that should be
    treated as a teammate. Falls back to email-only if the aliases column
    hasn't been added yet."""
    if aliases_supported(sb):
        res = sb.table("team_members").select("email, aliases").execute()
        out: set[str] = set()
        for row in (res.data or []):
            out.add(row["email"].lower())
            for alias in (row.get("aliases") or []):
                out.add(alias.lower())
        return out
    res = sb.table("team_members").select("email").execute()
    return {row["email"].lower() for row in (res.data or [])}


def update_sync_state(sb: Client, team_member_id: str, last_meeting_date: str | None):
    row = {
        "team_member_id": team_member_id,
        "source": "granola",
        "last_synced_at": datetime.now(timezone.utc).isoformat(),
    }
    if last_meeting_date:
        row["last_meeting_date"] = last_meeting_date
    sb.table("sync_state").upsert(row, on_conflict="team_member_id,source").execute()


def process_meeting(
    sb: Client,
    meeting: dict,
    team_member_id: str,
    team_emails: set[str],
    team_domains: list[str],
) -> dict:
    meeting_id, is_new = insert_meeting_if_new(sb, meeting, team_member_id)
    if not is_new:
        # First uploader wins. Don't re-process attendees/companies on a row
        # that's already canonical — the original sync set those correctly,
        # and any manual fixes since then would get clobbered.
        return {
            "meeting_id": meeting_id,
            "is_new": False,
            "skipped": "already_synced",
            "individuals": 0,
            "companies": 0,
        }

    company_ids_seen: set[str] = set()
    individual_ids_seen: set[str] = set()

    for p in meeting.get("known_participants", []):
        name = (p.get("name") or "").strip()
        email = (p.get("email") or "").strip().lower() or None
        company_hint = p.get("company_hint")
        is_note_creator = bool(p.get("is_note_creator"))

        # Auto-promote any Costanoa-domain attendee we haven't seen yet.
        if email and domain_of(email) in team_domains and email not in team_emails:
            _, was_created, alias_added = ensure_team_member(sb, email, name)
            if was_created or alias_added:
                backfill_is_team_member(sb, email)
            team_emails.add(email)

        company_id = None
        domain = domain_of(email)
        if email and domain and not is_personal_domain(domain):
            company_name = company_hint or domain.rsplit(".", 1)[0].split(".")[-1].title()
            company_id = upsert_company(sb, company_name, domain)
        elif company_hint:
            company_id = upsert_company(sb, company_hint, None)

        is_team_member_attendee = bool(email and email in team_emails)
        ind_id = upsert_individual(sb, name or (email or "Unknown"), email, company_id)
        role = "note_creator" if is_note_creator else "attendee"
        attach_attendee(
            sb, meeting_id, ind_id,
            is_team_member=is_team_member_attendee,
            role=role,
            source="granola_known_participant",
        )
        individual_ids_seen.add(ind_id)

        # Link company to meeting unless it's the team-member's own firm
        # (we don't want every Costanoa meeting tagged with "Costanoa").
        if company_id and not is_team_member_attendee and company_id not in company_ids_seen:
            attach_company(sb, meeting_id, company_id, relation_type="co_attendee", source="granola_known_participant")
            company_ids_seen.add(company_id)

    inferred = meeting.get("title_inferred")
    if inferred:
        confidence = float(inferred.get("confidence", 0.6))
        c_name = (inferred.get("counterparty_name") or "").strip()
        c_company = (inferred.get("counterparty_company") or "").strip()
        company_id = None
        if c_company:
            company_id = upsert_company(sb, c_company, None)
            if company_id not in company_ids_seen:
                attach_company(sb, meeting_id, company_id, relation_type="subject", source="title_inferred", confidence=confidence)
                company_ids_seen.add(company_id)
        if c_name:
            ind_id = upsert_individual(sb, c_name, None, company_id)
            if ind_id not in individual_ids_seen:
                attach_attendee(
                    sb, meeting_id, ind_id,
                    is_team_member=False,
                    role="attendee",
                    source="title_inferred",
                    confidence=confidence,
                )
                individual_ids_seen.add(ind_id)

    return {
        "meeting_id": meeting_id,
        "is_new": is_new,
        "individuals": len(individual_ids_seen),
        "companies": len(company_ids_seen),
    }


def cmd_list_synced(sb: Client):
    """Print JSON list of Granola external_ids already in the meetings table.

    Used by the /granola-sync skill to diff before fetching transcripts —
    avoids re-fetching ~100KB of transcript text per already-synced meeting.
    """
    res = sb.table("meetings").select("external_id").eq("source", "granola").execute()
    ids = [row["external_id"] for row in (res.data or [])]
    print(json.dumps(ids))


def cmd_add_teammate(sb: Client, args: list[str]):
    """Manually register a teammate by email + optional name.

    usage: sync.py --add-teammate <email> [name]
    """
    if not args:
        sys.exit("usage: sync.py --add-teammate <email> [name]")
    email = args[0]
    name = " ".join(args[1:]) if len(args) > 1 else ""

    team_domains = team_domains_from_env()
    domain = domain_of(email)
    if domain not in team_domains:
        sys.exit(
            f"Email {email} is not a Costanoa domain "
            f"({','.join(team_domains)}). Refusing to add."
        )

    tm_id, was_created, alias_added = ensure_team_member(sb, email, name)
    backfilled = backfill_is_team_member(sb, email) if (was_created or alias_added) else 0

    print(json.dumps({
        "team_member_id": tm_id,
        "email": email.lower(),
        "name": name,
        "created": was_created,
        "alias_added": alias_added,
        "attendee_rows_backfilled": backfilled,
    }, indent=2))


def main():
    args = list(sys.argv[1:])

    # Parse --exclude before mode dispatch so it can be combined with payload mode.
    cli_exclude_phrases: list[str] = []
    if "--exclude" in args:
        i = args.index("--exclude")
        if i + 1 >= len(args):
            sys.exit("--exclude requires a comma-separated argument")
        cli_exclude_phrases = [p.strip().lower() for p in args[i + 1].split(",") if p.strip()]
        args = args[:i] + args[i + 2:]

    url, key = load_env()
    sb: Client = create_client(url, key)

    if args and args[0] == "--list-synced":
        cmd_list_synced(sb)
        return
    if args and args[0] == "--add-teammate":
        cmd_add_teammate(sb, args[1:])
        return

    if len(args) != 1:
        sys.exit(
            "usage: sync.py <payload.json> [--exclude 'phrase1,phrase2']  |  "
            "sync.py --list-synced  |  "
            "sync.py --add-teammate <email> [name]"
        )
    payload = json.loads(Path(args[0]).read_text())

    tm = payload["team_member"]
    team_domains = team_domains_from_env()

    # Gate: refuse to register a non-Costanoa account as a team member.
    domain = domain_of(tm["email"])
    if domain not in team_domains:
        sys.exit(
            f"Email {tm['email']} is not a Costanoa domain "
            f"({','.join(team_domains)}). Refusing to create team_members row."
        )

    team_member_id, was_created, alias_added = ensure_team_member(
        sb, tm["email"], tm.get("name", "")
    )
    if was_created or alias_added:
        backfill_is_team_member(sb, tm["email"])
    set_workspace_id_if_missing(sb, team_member_id, tm.get("granola_workspace_id"))

    team_emails = get_team_member_emails(sb)

    # Merge exclusion phrases from every source. Lowercased for case-insensitive match.
    payload_excludes = [p.lower() for p in (payload.get("exclude_phrases") or [])]
    env_excludes = exclude_phrases_from_env()
    exclude_phrases = sorted({*payload_excludes, *env_excludes, *cli_exclude_phrases})

    meetings = payload.get("meetings", [])
    new_count = 0
    already_synced_count = 0
    excluded_count = 0
    excluded_log: list[dict] = []
    latest_meeting_date: str | None = None

    for m in meetings:
        reason = should_exclude(m, exclude_phrases)
        if reason:
            excluded_count += 1
            excluded_log.append({
                "external_id": m.get("external_id"),
                "title": m.get("title"),
                "reason": reason,
            })
            continue

        result = process_meeting(sb, m, team_member_id, team_emails, team_domains)
        if result["is_new"]:
            new_count += 1
        else:
            already_synced_count += 1
        d = m.get("meeting_date")
        if d and (not latest_meeting_date or d > latest_meeting_date):
            latest_meeting_date = d

    update_sync_state(sb, team_member_id, latest_meeting_date)

    print(json.dumps({
        "team_member_email": tm["email"],
        "team_member_created": was_created,
        "alias_added": alias_added,
        "new_meetings": new_count,
        "already_synced_skipped": already_synced_count,
        "excluded_meetings": excluded_count,
        "excluded_log": excluded_log,
        "exclude_phrases_active": exclude_phrases,
        "total_processed": len(meetings),
        "latest_meeting_date": latest_meeting_date,
    }, indent=2))


if __name__ == "__main__":
    main()
