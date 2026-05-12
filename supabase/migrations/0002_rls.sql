-- Migration 0002: Row Level Security + Costanoa-team auth policies
-- v0.2.0: shift from "service-role distributed to all VCs" to
-- "anon key + Supabase Email OTP + per-user JWT".
--
-- After this migration:
--   - Service role (admin) bypasses RLS entirely. Used by Sean's machine
--     for migrations, backfills, ad-hoc fixes. Stays out of teammate plugins.
--   - Anon role (no JWT) is denied everywhere.
--   - Authenticated users whose JWT email ends in @costanoa.vc or
--     @costanoavc.com get full read access and scoped write access.
--
-- Safe to re-run.

------------------------------------------------------------------
-- Helper functions
------------------------------------------------------------------

create or replace function is_costanoa_user() returns boolean
  language sql stable security definer
  set search_path = public
as $$
  select (auth.jwt() ->> 'email') is not null
    and split_part(lower(auth.jwt() ->> 'email'), '@', 2)
        in ('costanoa.vc', 'costanoavc.com');
$$;

create or replace function my_team_member_id() returns uuid
  language sql stable security definer
  set search_path = public
as $$
  select id from team_members
   where lower(email) = lower(auth.jwt() ->> 'email')
      or lower(auth.jwt() ->> 'email') = any (
           select lower(unnest(aliases))
         )
   limit 1;
$$;

------------------------------------------------------------------
-- Enable RLS
------------------------------------------------------------------

alter table team_members       enable row level security;
alter table companies          enable row level security;
alter table individuals        enable row level security;
alter table meetings           enable row level security;
alter table meeting_attendees  enable row level security;
alter table meeting_companies  enable row level security;
alter table sync_state         enable row level security;

------------------------------------------------------------------
-- Drop any existing policies (idempotent re-run)
------------------------------------------------------------------

do $$
declare p record;
begin
  for p in
    select schemaname, tablename, policyname
      from pg_policies
     where schemaname = 'public'
       and tablename in (
         'team_members','companies','individuals','meetings',
         'meeting_attendees','meeting_companies','sync_state'
       )
  loop
    execute format('drop policy if exists %I on %I.%I',
                   p.policyname, p.schemaname, p.tablename);
  end loop;
end $$;

------------------------------------------------------------------
-- team_members
------------------------------------------------------------------
-- Read: any authenticated Costanoa user can see the roster.
create policy tm_select on team_members for select to authenticated
  using (is_costanoa_user());

-- Insert: any Costanoa user can register a new teammate row, as long as
-- the new row's email is also on a Costanoa domain. Powers /team-onboard
-- and the auto-promotion path.
create policy tm_insert on team_members for insert to authenticated
  with check (
    is_costanoa_user()
    and split_part(lower(email), '@', 2) in ('costanoa.vc', 'costanoavc.com')
  );

-- Update: any Costanoa user can update any team_members row (we trust
-- the firm with roster maintenance — alias merges, name fixes). Tightens
-- if the team ever exceeds ~15 people.
create policy tm_update on team_members for update to authenticated
  using (is_costanoa_user())
  with check (is_costanoa_user());

------------------------------------------------------------------
-- companies (shared, collaborative dedup)
------------------------------------------------------------------
create policy co_all on companies for all to authenticated
  using (is_costanoa_user())
  with check (is_costanoa_user());

------------------------------------------------------------------
-- individuals (shared, collaborative dedup)
------------------------------------------------------------------
create policy ind_all on individuals for all to authenticated
  using (is_costanoa_user())
  with check (is_costanoa_user());

------------------------------------------------------------------
-- meetings (attribution-locked)
------------------------------------------------------------------
create policy m_select on meetings for select to authenticated
  using (is_costanoa_user());

-- Insert: you can only create meetings attributed to YOUR team_members row.
create policy m_insert on meetings for insert to authenticated
  with check (
    is_costanoa_user()
    and created_by_team_member_id = my_team_member_id()
  );

-- Update: only on rows you own. (No-overwrite policy in sync.py means
-- this rarely fires, but the DB enforces it regardless.)
create policy m_update_own on meetings for update to authenticated
  using (created_by_team_member_id = my_team_member_id())
  with check (created_by_team_member_id = my_team_member_id());

------------------------------------------------------------------
-- meeting_attendees (scoped to meeting ownership)
------------------------------------------------------------------
create policy ma_select on meeting_attendees for select to authenticated
  using (is_costanoa_user());

create policy ma_insert on meeting_attendees for insert to authenticated
  with check (
    is_costanoa_user()
    and meeting_id in (
      select id from meetings
       where created_by_team_member_id = my_team_member_id()
    )
  );

create policy ma_update_own on meeting_attendees for update to authenticated
  using (
    meeting_id in (
      select id from meetings
       where created_by_team_member_id = my_team_member_id()
    )
  )
  with check (
    meeting_id in (
      select id from meetings
       where created_by_team_member_id = my_team_member_id()
    )
  );

------------------------------------------------------------------
-- meeting_companies (scoped to meeting ownership)
------------------------------------------------------------------
create policy mc_select on meeting_companies for select to authenticated
  using (is_costanoa_user());

create policy mc_insert on meeting_companies for insert to authenticated
  with check (
    is_costanoa_user()
    and meeting_id in (
      select id from meetings
       where created_by_team_member_id = my_team_member_id()
    )
  );

------------------------------------------------------------------
-- sync_state (private to each user)
------------------------------------------------------------------
create policy ss_all_own on sync_state for all to authenticated
  using (team_member_id = my_team_member_id())
  with check (team_member_id = my_team_member_id());
