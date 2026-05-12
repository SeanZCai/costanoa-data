#!/usr/bin/env python3
"""Costanoa knowledge base search.

Provides structured queries over the Supabase tables for the /costanoa-search
skill. Reuses the auth setup from sync.py (anon key + user JWT for teammates,
service role for admin).

Subcommands:
  meetings    Search meetings by VC, company, attendee, date range, text, tag.
  companies   Search companies by name/domain/tag.
  people      Search individuals by name/email/company.
  meeting     Fetch one meeting with its attendees and companies.
  stats       High-level counts (total meetings, top companies, top attendees).

All commands emit JSON to stdout by default; pass --format table for plain text.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync import build_client  # noqa: E402


def _meeting_select(deep: bool = True) -> str:
    base = "id, title, meeting_date, summary_md, source, external_id, ingested_at"
    if not deep:
        return base
    return (
        base
        + ", team_members!meetings_created_by_team_member_id_fkey(email, name)"
        + ", meeting_companies(company_id, relation_type, confidence, companies(name, domain, tags))"
        + ", meeting_attendees(individual_id, role, is_team_member, confidence, individuals(name, email))"
    )


def _shrink_meeting(row: dict, snippet_chars: int = 240) -> dict:
    summary = row.get("summary_md") or ""
    if snippet_chars and len(summary) > snippet_chars:
        summary = summary[:snippet_chars].rstrip() + "…"
    companies = []
    for mc in (row.get("meeting_companies") or []):
        c = mc.get("companies") or {}
        companies.append({
            "name": c.get("name"),
            "domain": c.get("domain"),
            "tags": c.get("tags") or [],
            "relation": mc.get("relation_type"),
            "confidence": mc.get("confidence"),
        })
    attendees = []
    for ma in (row.get("meeting_attendees") or []):
        ind = ma.get("individuals") or {}
        attendees.append({
            "name": ind.get("name"),
            "email": ind.get("email"),
            "is_team_member": ma.get("is_team_member"),
            "role": ma.get("role"),
        })
    tm = row.get("team_members") or {}
    return {
        "id": row.get("id"),
        "title": row.get("title"),
        "meeting_date": row.get("meeting_date"),
        "synced_by": tm.get("email") or None,
        "synced_by_name": tm.get("name"),
        "summary_snippet": summary,
        "companies": companies,
        "attendees": attendees,
    }


def cmd_meetings(args):
    sb = build_client()

    # Pre-filter: build a set of candidate meeting_ids from entity filters
    # BEFORE applying limit, so older matches aren't dropped at the edge.
    candidate_ids: set[str] | None = None

    def _restrict(new_ids: set[str]) -> set[str] | None:
        return new_ids if candidate_ids is None else (candidate_ids & new_ids)

    if args.company:
        cs = sb.table("companies").select("id").ilike("name", f"%{args.company}%").execute().data or []
        if not cs:
            return _emit({"query_type": "meetings", "count": 0, "results": []}, args.format)
        mc = sb.table("meeting_companies").select("meeting_id").in_("company_id", [c["id"] for c in cs]).execute().data or []
        candidate_ids = _restrict({row["meeting_id"] for row in mc})

    if args.attendee:
        like = f"%{args.attendee}%"
        inds = sb.table("individuals").select("id").or_(f"name.ilike.{like},email.ilike.{like}").execute().data or []
        if not inds:
            return _emit({"query_type": "meetings", "count": 0, "results": []}, args.format)
        ma = sb.table("meeting_attendees").select("meeting_id").in_("individual_id", [i["id"] for i in inds]).execute().data or []
        candidate_ids = _restrict({row["meeting_id"] for row in ma})

    if args.tag:
        cs = sb.table("companies").select("id").contains("tags", [args.tag]).execute().data or []
        if not cs:
            return _emit({"query_type": "meetings", "count": 0, "results": []}, args.format)
        mc = sb.table("meeting_companies").select("meeting_id").in_("company_id", [c["id"] for c in cs]).execute().data or []
        candidate_ids = _restrict({row["meeting_id"] for row in mc})

    q = sb.table("meetings").select(_meeting_select())

    if candidate_ids is not None:
        if not candidate_ids:
            return _emit({"query_type": "meetings", "count": 0, "results": []}, args.format)
        q = q.in_("id", list(candidate_ids))

    if args.since:
        q = q.gte("meeting_date", args.since)
    if args.until:
        q = q.lte("meeting_date", args.until)

    if args.vc:
        tm = sb.table("team_members").select("id, email, aliases").execute()
        matched = None
        target = args.vc.lower()
        for row in (tm.data or []):
            if row["email"].lower() == target:
                matched = row["id"]
                break
            if target in [a.lower() for a in (row.get("aliases") or [])]:
                matched = row["id"]
                break
        if not matched:
            print(json.dumps({"error": f"No team member found for {args.vc}"}))
            return
        q = q.eq("created_by_team_member_id", matched)

    if args.text:
        like = f"%{args.text}%"
        q = q.or_(f"title.ilike.{like},summary_md.ilike.{like}")

    q = q.order("meeting_date", desc=True).limit(args.limit)
    rows = q.execute().data or []

    out = {
        "query_type": "meetings",
        "filters": {
            k: v for k, v in vars(args).items()
            if v is not None and k not in ("func", "format", "limit")
        },
        "count": len(rows),
        "results": [_shrink_meeting(r) for r in rows],
    }
    _emit(out, args.format)


def cmd_companies(args):
    sb = build_client()
    q = sb.table("companies").select("id, name, domain, description, tags, created_at, updated_at")
    if args.name:
        q = q.ilike("name", f"%{args.name}%")
    if args.domain:
        q = q.eq("domain", args.domain.lower())
    if args.tag:
        q = q.contains("tags", [args.tag])
    q = q.order("name").limit(args.limit)
    rows = q.execute().data or []

    # Optionally count meetings per company.
    if args.with_meeting_count and rows:
        ids = [r["id"] for r in rows]
        mc = sb.table("meeting_companies").select("company_id").in_("company_id", ids).execute()
        counts = {}
        for row in (mc.data or []):
            counts[row["company_id"]] = counts.get(row["company_id"], 0) + 1
        for r in rows:
            r["meeting_count"] = counts.get(r["id"], 0)
        rows.sort(key=lambda r: r.get("meeting_count", 0), reverse=True)

    _emit({"query_type": "companies", "count": len(rows), "results": rows}, args.format)


def cmd_people(args):
    sb = build_client()
    q = sb.table("individuals").select("id, name, email, current_company_id, tags, created_at")
    if args.name:
        q = q.ilike("name", f"%{args.name}%")
    if args.email:
        q = q.ilike("email", f"%{args.email}%")
    rows = q.limit(args.limit).execute().data or []

    if args.company and rows:
        company = sb.table("companies").select("id, name").ilike("name", f"%{args.company}%").execute().data
        company_ids = {c["id"] for c in (company or [])}
        rows = [r for r in rows if r.get("current_company_id") in company_ids]

    # Hydrate company name for context.
    if rows:
        company_ids = list({r["current_company_id"] for r in rows if r.get("current_company_id")})
        if company_ids:
            cs = sb.table("companies").select("id, name").in_("id", company_ids).execute().data or []
            cmap = {c["id"]: c["name"] for c in cs}
            for r in rows:
                r["company_name"] = cmap.get(r.get("current_company_id"))

    _emit({"query_type": "people", "count": len(rows), "results": rows}, args.format)


def cmd_meeting(args):
    sb = build_client()
    # Accept either internal UUID or Granola external_id.
    res = sb.table("meetings").select(_meeting_select()).eq("id", args.id).limit(1).execute()
    if not res.data:
        res = sb.table("meetings").select(_meeting_select()).eq("external_id", args.id).limit(1).execute()
    if not res.data:
        print(json.dumps({"error": f"No meeting matches {args.id}"}))
        return
    detail = _shrink_meeting(res.data[0], snippet_chars=0)  # full summary, not snippet
    detail["summary_md"] = res.data[0].get("summary_md")
    detail.pop("summary_snippet", None)
    _emit({"query_type": "meeting", "result": detail}, args.format)


def cmd_stats(args):
    sb = build_client()
    total_meetings = sb.table("meetings").select("id", count="exact").eq("source", "granola").execute().count
    total_companies = sb.table("companies").select("id", count="exact").execute().count
    total_individuals = sb.table("individuals").select("id", count="exact").execute().count
    total_team = sb.table("team_members").select("id", count="exact").execute().count

    # Top companies by meeting count.
    mc = sb.table("meeting_companies").select("company_id").execute().data or []
    counts = {}
    for row in mc:
        counts[row["company_id"]] = counts.get(row["company_id"], 0) + 1
    top_ids = sorted(counts, key=lambda k: counts[k], reverse=True)[:10]
    top_companies = []
    if top_ids:
        cs = sb.table("companies").select("id, name").in_("id", top_ids).execute().data or []
        cmap = {c["id"]: c["name"] for c in cs}
        top_companies = [{"name": cmap.get(cid), "meeting_count": counts[cid]} for cid in top_ids]

    # Meetings per VC.
    mt = sb.table("meetings").select("created_by_team_member_id").eq("source", "granola").execute().data or []
    per_vc = {}
    for row in mt:
        per_vc[row["created_by_team_member_id"]] = per_vc.get(row["created_by_team_member_id"], 0) + 1
    by_vc = []
    if per_vc:
        tms = sb.table("team_members").select("id, email, name").in_("id", list(per_vc)).execute().data or []
        tmap = {t["id"]: t for t in tms}
        for tid, n in sorted(per_vc.items(), key=lambda kv: kv[1], reverse=True):
            tm = tmap.get(tid, {})
            by_vc.append({
                "email": tm.get("email"),
                "name": tm.get("name"),
                "meeting_count": n,
            })

    _emit({
        "query_type": "stats",
        "totals": {
            "meetings": total_meetings,
            "companies": total_companies,
            "individuals": total_individuals,
            "team_members": total_team,
        },
        "top_companies": top_companies,
        "meetings_by_vc": by_vc,
    }, args.format)


def _emit(payload: dict, fmt: str):
    if fmt == "json":
        print(json.dumps(payload, indent=2, default=str))
        return
    # Plain text fallback for humans reading directly.
    print(json.dumps(payload, indent=2, default=str))


def main():
    p = argparse.ArgumentParser(prog="search.py")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("meetings", help="Search meetings")
    m.add_argument("--vc", help="Filter to one VC's synced meetings (email)")
    m.add_argument("--company", help="Filter to meetings linked to this company (fuzzy)")
    m.add_argument("--attendee", help="Filter to meetings with an attendee matching this name/email")
    m.add_argument("--since", help="Earliest meeting_date (YYYY-MM-DD)")
    m.add_argument("--until", help="Latest meeting_date (YYYY-MM-DD)")
    m.add_argument("--text", help="Substring match on title + summary")
    m.add_argument("--tag", help="Filter by company tag (e.g. cybersecurity)")
    m.add_argument("--limit", type=int, default=20)
    m.add_argument("--format", choices=["json", "table"], default="json")
    m.set_defaults(func=cmd_meetings)

    c = sub.add_parser("companies", help="Search companies")
    c.add_argument("--name", help="Fuzzy name match")
    c.add_argument("--domain", help="Exact domain match")
    c.add_argument("--tag", help="Tag containment")
    c.add_argument("--with-meeting-count", action="store_true")
    c.add_argument("--limit", type=int, default=20)
    c.add_argument("--format", choices=["json", "table"], default="json")
    c.set_defaults(func=cmd_companies)

    pp = sub.add_parser("people", help="Search individuals")
    pp.add_argument("--name", help="Fuzzy name match")
    pp.add_argument("--email", help="Fuzzy email match")
    pp.add_argument("--company", help="Fuzzy match on their current_company")
    pp.add_argument("--limit", type=int, default=20)
    pp.add_argument("--format", choices=["json", "table"], default="json")
    pp.set_defaults(func=cmd_people)

    md = sub.add_parser("meeting", help="Get one meeting with all related rows")
    md.add_argument("id", help="Internal UUID or Granola external_id")
    md.add_argument("--format", choices=["json", "table"], default="json")
    md.set_defaults(func=cmd_meeting)

    s = sub.add_parser("stats", help="High-level counts and top entities")
    s.add_argument("--format", choices=["json", "table"], default="json")
    s.set_defaults(func=cmd_stats)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
