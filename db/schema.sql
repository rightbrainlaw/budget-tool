-- Budget tool — database schema (Supabase / Postgres)
--
-- Run this in the Supabase SQL editor once the project exists. It defines the
-- data model for the enrichment/app layer described in SPEC-normalizer.md:
--
--   * Two people (Dan + son) log in with Google and share a "budget".
--   * EQUAL access: every member of a budget can read and write all of its
--     data. There is no role hierarchy. Accountability comes from audit_log,
--     not from restricting permissions.
--   * Ledger layer (immutable bank facts) and enrichment layer (user-assigned
--     merchant/category) are separate tables, joined by (budget_id, txn_id).
--   * Row-Level Security enforces "you can only touch budgets you belong to"
--     at the database, so the app never has to be trusted to get it right.
--
-- Auth users live in Supabase's built-in `auth.users`; we never store
-- passwords ourselves.

-- ===========================================================================
-- Profiles: one row per auth user, auto-created on signup
-- ===========================================================================
create table public.profile (
  id           uuid primary key references auth.users(id) on delete cascade,
  email        text,
  display_name text,
  created_at   timestamptz not null default now()
);

create function public.handle_new_user()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profile (id, email, display_name)
  values (new.id, new.email,
          coalesce(new.raw_user_meta_data->>'full_name', new.email));
  return new;
end; $$;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function public.handle_new_user();

-- ===========================================================================
-- Budgets and membership (the sharing model: users <-> budgets, many-to-many)
-- ===========================================================================
create table public.budget (
  id         uuid primary key default gen_random_uuid(),
  name       text not null,
  created_by uuid not null references auth.users(id),
  created_at timestamptz not null default now()
);

create table public.budget_member (
  budget_id uuid not null references public.budget(id) on delete cascade,
  user_id   uuid not null references auth.users(id) on delete cascade,
  -- role is reserved for a future hierarchy; today every member is equal.
  role      text not null default 'member',
  added_by  uuid references auth.users(id),
  added_at  timestamptz not null default now(),
  primary key (budget_id, user_id)
);

-- Membership test used by every RLS policy below. SECURITY DEFINER so the
-- policy check itself isn't subject to RLS recursion.
create function public.is_member(b uuid)
returns boolean language sql security definer stable set search_path = public as $$
  select exists (
    select 1 from public.budget_member
    where budget_id = b and user_id = auth.uid()
  );
$$;

-- When someone creates a budget, make them its first member automatically
-- (otherwise RLS would lock the creator out of their own budget).
create function public.add_creator_as_member()
returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.budget_member (budget_id, user_id, added_by)
  values (new.id, new.created_by, new.created_by);
  return new;
end; $$;

create trigger budget_creator_membership
  after insert on public.budget
  for each row execute function public.add_creator_as_member();

-- ===========================================================================
-- Enrichment vocab: saveable categories and merchants (budget-scoped)
-- ===========================================================================
create table public.category (
  id         uuid primary key default gen_random_uuid(),
  budget_id  uuid not null references public.budget(id) on delete cascade,
  name       text not null,
  created_at timestamptz not null default now(),
  unique (budget_id, name)
);

create table public.merchant (
  id                  uuid primary key default gen_random_uuid(),
  budget_id           uuid not null references public.budget(id) on delete cascade,
  name                text not null,
  default_category_id uuid references public.category(id) on delete set null,
  created_at          timestamptz not null default now(),
  unique (budget_id, name)
);

-- ===========================================================================
-- Ledger layer: immutable bank facts (output of normalizer.py), budget-scoped
-- ===========================================================================
-- txn_id is the content hash from normalizer.make_txn_id (bank facts only), so
-- (budget_id, txn_id) is the natural key and re-imports upsert onto it.
create table public.transaction (
  budget_id    uuid not null references public.budget(id) on delete cascade,
  txn_id       text not null,
  date         date not null,
  posted_date  date not null,
  amount       numeric(12,2) not null,
  description  text not null,
  account      text not null,
  pending      boolean not null,
  source_file  text,
  imported_by  uuid references auth.users(id),
  imported_at  timestamptz not null default now(),
  primary key (budget_id, txn_id)
);

-- ===========================================================================
-- Enrichment layer: user-assigned meaning, one row per transaction
-- ===========================================================================
create table public.enrichment (
  budget_id   uuid not null,
  txn_id      text not null,
  merchant_id uuid references public.merchant(id) on delete set null,
  category_id uuid references public.category(id) on delete set null,
  note        text,
  reviewed    boolean not null default false,
  updated_by  uuid references auth.users(id),
  updated_at  timestamptz not null default now(),
  primary key (budget_id, txn_id),
  foreign key (budget_id, txn_id)
    references public.transaction(budget_id, txn_id) on delete cascade
);

-- Remembered mappings: categorizing one row can create a rule so future
-- matching rows auto-fill. Table defined now; application logic comes later.
create table public.mapping_rule (
  id            uuid primary key default gen_random_uuid(),
  budget_id     uuid not null references public.budget(id) on delete cascade,
  match_pattern text not null,               -- substring matched against description
  merchant_id   uuid references public.merchant(id) on delete cascade,
  category_id   uuid references public.category(id) on delete cascade,
  created_by    uuid references auth.users(id),
  created_at    timestamptz not null default now()
);

-- ===========================================================================
-- Audit log: who added/edited/deleted enrichment. Tamper-resistant — only the
-- SECURITY DEFINER trigger writes it; no client INSERT policy exists.
-- ===========================================================================
create table public.audit_log (
  id          bigint generated always as identity primary key,
  budget_id   uuid not null,
  user_id     uuid,                          -- null for system/import actions
  action      text not null,                 -- 'insert' | 'update' | 'delete'
  entity_type text not null,                 -- e.g. 'enrichment'
  entity_id   text,                          -- e.g. the txn_id
  detail      jsonb,
  at          timestamptz not null default now()
);

create function public.log_enrichment_change()
returns trigger language plpgsql security definer set search_path = public as $$
declare
  v_action text := lower(tg_op);
  v_row    record := coalesce(new, old);
begin
  insert into public.audit_log (budget_id, user_id, action, entity_type, entity_id, detail)
  values (v_row.budget_id, auth.uid(), v_action, 'enrichment', v_row.txn_id,
          case when tg_op = 'DELETE' then to_jsonb(old) else to_jsonb(new) end);
  return v_row;
end; $$;

create trigger enrichment_audit
  after insert or update or delete on public.enrichment
  for each row execute function public.log_enrichment_change();

-- ===========================================================================
-- Row-Level Security
-- ===========================================================================
-- Pattern for budget-scoped tables: a row is visible/writable iff you are a
-- member of its budget. Equal access = same rule for select/insert/update/delete.

alter table public.profile        enable row level security;
alter table public.budget         enable row level security;
alter table public.budget_member  enable row level security;
alter table public.category       enable row level security;
alter table public.merchant       enable row level security;
alter table public.transaction    enable row level security;
alter table public.enrichment     enable row level security;
alter table public.mapping_rule   enable row level security;
alter table public.audit_log      enable row level security;

-- Profiles: see your own, plus co-members of any budget you share (for names).
create policy profile_read on public.profile for select using (
  id = auth.uid()
  or exists (
    select 1
    from public.budget_member me
    join public.budget_member them on me.budget_id = them.budget_id
    where me.user_id = auth.uid() and them.user_id = profile.id
  )
);
create policy profile_update_self on public.profile for update
  using (id = auth.uid()) with check (id = auth.uid());

-- Budget: members read/update/delete; any authenticated user may create one
-- (the trigger then adds them as the first member).
-- creator OR member can read: the `created_by` half lets someone see the budget
-- they just inserted during the INSERT...RETURNING, before the membership
-- trigger has finished making them a member.
create policy budget_read   on public.budget for select using (is_member(id) or created_by = auth.uid());
create policy budget_create on public.budget for insert with check (created_by = auth.uid());
create policy budget_update on public.budget for update using (is_member(id)) with check (is_member(id));
create policy budget_delete on public.budget for delete using (is_member(id));

-- Membership: members can see and manage who else is in their budget (invites).
create policy bm_read   on public.budget_member for select using (is_member(budget_id));
create policy bm_insert on public.budget_member for insert with check (is_member(budget_id));
create policy bm_delete on public.budget_member for delete using (is_member(budget_id));

-- Everything else: one "members do anything within their budget" policy each.
create policy category_all   on public.category    for all using (is_member(budget_id)) with check (is_member(budget_id));
create policy merchant_all   on public.merchant    for all using (is_member(budget_id)) with check (is_member(budget_id));
create policy txn_all        on public.transaction for all using (is_member(budget_id)) with check (is_member(budget_id));
create policy enrich_all     on public.enrichment  for all using (is_member(budget_id)) with check (is_member(budget_id));
create policy rule_all       on public.mapping_rule for all using (is_member(budget_id)) with check (is_member(budget_id));

-- Audit log: members read only. No write policy -> clients can't forge or erase
-- entries; the SECURITY DEFINER trigger (running as owner) writes them.
create policy audit_read on public.audit_log for select using (is_member(budget_id));
