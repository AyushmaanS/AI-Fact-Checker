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
