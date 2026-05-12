-- Migration: add aliases column to team_members so a single VC with multiple
-- Costanoa emails (e.g. tony@costanoa.vc + tony@costanoavc.com) collapses
-- into one row, matched by name when the email is unknown.
--
-- Safe to re-run.

alter table team_members
  add column if not exists aliases citext[] not null default '{}';

create index if not exists team_members_aliases_idx
  on team_members using gin (aliases);
