create table submissions (
    id uuid primary key default gen_random_uuid(),
    raw_input text,
    input_type text check (input_type in ('text','upload','url')),
    created_at timestamptz default now()
);

create table claims (
    id uuid primary key default gen_random_uuid(),
    submission_id uuid references submissions(id),
    text text not null,
    topic text,
    specificity text,
    verifiability_score float
);

create table verdicts (
    id uuid primary key default gen_random_uuid(),
    claim_id uuid references claims(id),
    label text check (label in ('TRUE','FALSE','PARTIALLY_TRUE','MISLEADING','UNVERIFIABLE','OUTDATED','SATIRE')),
    rationale text,
    confidence_score float,
    citations jsonb,
    created_at timestamptz default now()
);

create table source_credibility (
    domain_pattern text primary key,
    category text,
    weight float check (weight >= 0 and weight <= 1)
);

-- Seed: 8-tier source credibility table (weights per PRD v1 appendix / Sprint 1 spec).
-- A handful of real domains per tier to start; the Evidence Ranker (Sprint 5) grows this list.
insert into source_credibility (domain_pattern, category, weight) values
    ('who.int', 'Primary Gov/IGO', 0.95),
    ('imf.org', 'Primary Gov/IGO', 0.95),
    ('worldbank.org', 'Primary Gov/IGO', 0.95),
    ('un.org', 'Primary Gov/IGO', 0.95),
    ('cdc.gov', 'Primary Gov/IGO', 0.95),
    ('europa.eu', 'Primary Gov/IGO', 0.95),

    ('pubmed.ncbi.nlm.nih.gov', 'Peer-Reviewed Academic', 0.90),
    ('nature.com', 'Peer-Reviewed Academic', 0.90),
    ('sciencedirect.com', 'Peer-Reviewed Academic', 0.90),
    ('thelancet.com', 'Peer-Reviewed Academic', 0.90),
    ('nejm.org', 'Peer-Reviewed Academic', 0.90),

    ('snopes.com', 'Established Fact-Checkers', 0.88),
    ('politifact.com', 'Established Fact-Checkers', 0.88),
    ('factcheck.org', 'Established Fact-Checkers', 0.88),
    ('fullfact.org', 'Established Fact-Checkers', 0.88),

    ('reuters.com', 'Major Wire Services', 0.82),
    ('apnews.com', 'Major Wire Services', 0.82),
    ('afp.com', 'Major Wire Services', 0.82),

    ('nytimes.com', 'Major National Newspapers', 0.75),
    ('washingtonpost.com', 'Major National Newspapers', 0.75),
    ('theguardian.com', 'Major National Newspapers', 0.75),
    ('wsj.com', 'Major National Newspapers', 0.75),
    ('bbc.com', 'Major National Newspapers', 0.75),

    ('wikipedia.org', 'Wikipedia', 0.55),

    ('medium.com', 'Blogs/Opinion', 0.30),
    ('substack.com', 'Blogs/Opinion', 0.30),
    ('blogspot.com', 'Blogs/Opinion', 0.30),

    ('twitter.com', 'Social Media', 0.10),
    ('x.com', 'Social Media', 0.10),
    ('facebook.com', 'Social Media', 0.10),
    ('instagram.com', 'Social Media', 0.10),
    ('tiktok.com', 'Social Media', 0.10)
on conflict (domain_pattern) do nothing;

-- Sprint 18: Full Phase 2 Integration + Supabase Storage. Run this section
-- against an existing database that already has the tables above (they
-- aren't recreated here) - it's additive, not a fresh-install script.
--
-- storage_path/storage_expires_at: the downloaded/uploaded video file's
-- location in Supabase Storage and when it should be deleted (24h retention -
-- see app/db/client.cleanup_expired_media, checked on each Phase 2 request
-- rather than a cron job, per the sprint's own "not required yet" allowance).
-- extracted_text/extracted_text_expires_at: the combined transcript +
-- visual_context + caption text actually fed into the Phase 1 pipeline (90-day
-- retention) - kept separately from storage_path's much shorter window
-- because the extracted text is cheap to keep and useful for audits/debugging
-- long after the (large, storage-costly) video file itself is gone.
alter table submissions add column if not exists storage_path text;
alter table submissions add column if not exists storage_expires_at timestamptz;
alter table submissions add column if not exists extracted_text text;
alter table submissions add column if not exists extracted_text_expires_at timestamptz;

-- Private bucket for temporarily-stored uploaded/downloaded video files.
insert into storage.buckets (id, name, public)
values ('media', 'media', false)
on conflict (id) do nothing;

-- Storage has RLS on by default, unlike the plain tables above (which have
-- never had RLS enabled at all in this project - no auth system yet, so
-- every table so far is wide open to the anon key). These three policies
-- match that same all-open posture, just scoped to the one 'media' bucket
-- Storage itself requires policies for.
create policy "media bucket: anon can upload" on storage.objects
    for insert to anon
    with check (bucket_id = 'media');

create policy "media bucket: anon can read" on storage.objects
    for select to anon
    using (bucket_id = 'media');

create policy "media bucket: anon can delete" on storage.objects
    for delete to anon
    using (bucket_id = 'media');
