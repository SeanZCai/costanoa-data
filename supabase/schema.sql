-- Costanoa VC knowledge base schema (v0)
-- Apply via Supabase SQL Editor.

create extension if not exists citext;
create extension if not exists pg_trgm;
create extension if not exists pgcrypto;

create or replace function set_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create table if not exists team_members (
  id uuid primary key default gen_random_uuid(),
  email citext unique not null,
  name text,
  granola_workspace_id text,
  created_at timestamptz not null default now()
);
alter table team_members
  add column if not exists aliases citext[] not null default '{}';
create index if not exists team_members_aliases_idx
  on team_members using gin (aliases);

create table if not exists companies (
  id uuid primary key default gen_random_uuid(),
  name citext unique not null,
  domain citext,
  description text,
  tags text[] not null default '{}',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists companies_domain_idx on companies (domain);
create index if not exists companies_tags_idx on companies using gin (tags);

drop trigger if exists companies_set_updated_at on companies;
create trigger companies_set_updated_at
  before update on companies
  for each row execute function set_updated_at();

create table if not exists individuals (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  email citext unique,
  current_company_id uuid references companies(id) on delete set null,
  tags text[] not null default '{}',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists individuals_company_idx on individuals (current_company_id);
create index if not exists individuals_name_trgm_idx on individuals using gin (name gin_trgm_ops);

drop trigger if exists individuals_set_updated_at on individuals;
create trigger individuals_set_updated_at
  before update on individuals
  for each row execute function set_updated_at();

create table if not exists meetings (
  id uuid primary key default gen_random_uuid(),
  source text not null default 'granola',
  external_id text not null,
  title text not null,
  meeting_date timestamptz not null,
  summary_md text,
  transcript text,
  created_by_team_member_id uuid not null references team_members(id) on delete restrict,
  raw_payload jsonb not null default '{}'::jsonb,
  ingested_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (source, external_id)
);
create index if not exists meetings_created_by_idx on meetings (created_by_team_member_id);
create index if not exists meetings_date_idx on meetings (meeting_date desc);
create index if not exists meetings_summary_fts_idx on meetings using gin (to_tsvector('english', coalesce(summary_md, '')));
create index if not exists meetings_transcript_fts_idx on meetings using gin (to_tsvector('english', coalesce(transcript, '')));

drop trigger if exists meetings_set_updated_at on meetings;
create trigger meetings_set_updated_at
  before update on meetings
  for each row execute function set_updated_at();

create table if not exists meeting_attendees (
  meeting_id uuid not null references meetings(id) on delete cascade,
  individual_id uuid not null references individuals(id) on delete cascade,
  is_team_member boolean not null default false,
  role text not null default 'attendee',
  source text not null default 'granola_known_participant',
  confidence float not null default 1.0,
  primary key (meeting_id, individual_id)
);
create index if not exists meeting_attendees_individual_idx on meeting_attendees (individual_id);

create table if not exists meeting_companies (
  meeting_id uuid not null references meetings(id) on delete cascade,
  company_id uuid not null references companies(id) on delete cascade,
  relation_type text not null default 'co_attendee',
  source text not null default 'granola_known_participant',
  confidence float not null default 1.0,
  primary key (meeting_id, company_id)
);
create index if not exists meeting_companies_company_idx on meeting_companies (company_id);

create table if not exists sync_state (
  team_member_id uuid not null references team_members(id) on delete cascade,
  source text not null,
  last_synced_at timestamptz,
  last_meeting_date timestamptz,
  updated_at timestamptz not null default now(),
  primary key (team_member_id, source)
);

drop trigger if exists sync_state_set_updated_at on sync_state;
create trigger sync_state_set_updated_at
  before update on sync_state
  for each row execute function set_updated_at();

-- Seed Costanoa team members observed in real Granola data.
insert into team_members (email, name) values
  ('sean@costanoa.vc',    'Sean Cai'),
  ('tony@costanoa.vc',    'Tony Liu'),
  ('greg@costanoa.vc',    'Greg Sands'),
  ('mark@costanoa.vc',    'Mark Selcow'),
  ('nicole@costanoa.vc',  'Nicole Seah'),
  ('mkappaz@costanoa.vc', 'Mkappaz')
on conflict (email) do nothing;

-- Seed Costanoa as a company so attendee rows can reference it.
insert into companies (name, domain, tags)
values ('Costanoa Ventures', 'costanoa.vc', '{vc, internal}')
on conflict (name) do nothing;
