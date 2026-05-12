"""Committed Supabase config for the costanoa-data plugin.

The anon (publishable) key is safe to commit. RLS policies in
`supabase/migrations/0002_rls.sql` are what actually protect the data —
the anon key alone can read/write nothing without a valid user JWT.

Service role key (admin) is NEVER committed. It only lives in the admin's
local `.env` and is read by sync.py when present, used to bypass RLS for
migrations + backfills + cleanup.
"""

SUPABASE_URL = "https://lgnejbduwpytgbvtnhpr.supabase.co"

# Anon / publishable key — safe to commit per Supabase's documented model.
# Acts as the API key (apikey header) but carries no row-level authorization;
# row access is gated by RLS using the authenticated user's JWT.
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxnbmVqYmR1d3B5dGdidnRuaHByIiwic"
    "m9sZSI6ImFub24iLCJpYXQiOjE3Nzg1NDQ1NTMsImV4cCI6MjA5NDEyMDU1M30."
    "BAj9YlUebnwd9qlooOfM96z18VugUmFTs0AqF5z_eqM"
)

TEAM_DOMAINS_DEFAULT = "costanoa.vc,costanoavc.com"
