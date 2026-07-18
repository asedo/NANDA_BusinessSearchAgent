-- BusinessSearchAgent — schema
--
-- Design principles:
--   1. Every fact is attributable. `source` + `ingest_run` + `source_record`
--      let any row answer "where did this come from, and when?"
--   2. Raw payloads are kept. Re-parsing never requires re-fetching.
--   3. Derived fields (has_storefront, naics) record HOW they were derived,
--      so an inferred value is never mistaken for a declared one.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- provenance

CREATE TABLE IF NOT EXISTS source (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,   -- 'openstreetmap'
    url         TEXT,
    license     TEXT,                   -- 'ODbL-1.0'
    attribution TEXT                    -- required by ODbL; served in AgentFacts
);

CREATE TABLE IF NOT EXISTS ingest_run (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES source(id),
    place         TEXT NOT NULL,        -- 'Concord, Middlesex County, Massachusetts, USA'
    area_id       INTEGER,              -- Overpass area id
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    endpoint      TEXT,                 -- which Overpass mirror answered
    element_count INTEGER,
    query         TEXT                  -- exact query, for reproducibility
);

-- Raw upstream payload, one row per business per run. Never mutated.
CREATE TABLE IF NOT EXISTS source_record (
    id            INTEGER PRIMARY KEY,
    business_id   INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    ingest_run_id INTEGER NOT NULL REFERENCES ingest_run(id),
    raw_json      TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_record_business ON source_record(business_id);

-- ------------------------------------------------------------------ business

CREATE TABLE IF NOT EXISTS business (
    id               INTEGER PRIMARY KEY,

    -- identity (OSM is the spine; osm_type+osm_id is the natural key)
    osm_type         TEXT NOT NULL,     -- node | way | relation
    osm_id           INTEGER NOT NULL,
    name             TEXT NOT NULL,

    -- location
    lat              REAL,
    lon              REAL,
    addr_housenumber TEXT,
    addr_street      TEXT,
    addr_city        TEXT,
    addr_state       TEXT,
    addr_postcode    TEXT,

    -- contact / attributes as published upstream
    phone            TEXT,
    website          TEXT,
    opening_hours    TEXT,
    brand            TEXT,
    cuisine          TEXT,

    -- the OSM tag that classified this business, e.g. amenity=restaurant
    primary_tag_key  TEXT,
    primary_tag_val  TEXT,

    -- industry classification
    naics            TEXT,              -- 6-digit where available; prefix-queryable
    naics_title      TEXT,
    naics_vintage    TEXT,              -- '2022' — codes are revised ~every 5 years
    naics_source     TEXT,              -- 'osm_crosswalk' (inferred) | 'registry' (declared)

    -- derived flags; see derive.py for the rules
    has_storefront   INTEGER,           -- 0/1/NULL
    is_restaurant    INTEGER,

    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,

    UNIQUE (osm_type, osm_id)
);

CREATE INDEX IF NOT EXISTS idx_business_name       ON business(name);
CREATE INDEX IF NOT EXISTS idx_business_naics      ON business(naics);
CREATE INDEX IF NOT EXISTS idx_business_storefront ON business(has_storefront);
CREATE INDEX IF NOT EXISTS idx_business_city       ON business(addr_city);

-- --------------------------------------------------------------------- naics

CREATE TABLE IF NOT EXISTS osm_naics_crosswalk (
    osm_tag     TEXT NOT NULL,          -- 'amenity=restaurant'
    vintage     TEXT NOT NULL,          -- '2022'
    naics       TEXT NOT NULL,
    naics_title TEXT,
    confidence  TEXT,                   -- 'exact' | 'approximate' | 'manual'
    note        TEXT,
    PRIMARY KEY (osm_tag, vintage)
);

-- ------------------------------------------------- stage 3: web enrichment
-- Populated later. Snapshot and extraction are deliberately separate so
-- extraction can be re-run with a better prompt without re-crawling anyone.

CREATE TABLE IF NOT EXISTS web_snapshot (
    id          INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    url         TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    http_status INTEGER,
    content     TEXT,
    content_sha TEXT
);

CREATE TABLE IF NOT EXISTS extraction (
    id              INTEGER PRIMARY KEY,
    web_snapshot_id INTEGER NOT NULL REFERENCES web_snapshot(id) ON DELETE CASCADE,
    business_id     INTEGER NOT NULL REFERENCES business(id) ON DELETE CASCADE,
    extracted_at    TEXT NOT NULL,
    model           TEXT,
    schema_version  TEXT,
    payload_json    TEXT
);

-- ---------------------------------------------------------------------- view
-- The agent-facing projection: only facts we can attribute.

CREATE VIEW IF NOT EXISTS business_fact AS
SELECT
    b.id,
    b.name,
    b.primary_tag_key || '=' || b.primary_tag_val AS osm_category,
    b.naics,
    b.naics_title,
    b.naics_vintage,
    b.naics_source,
    b.has_storefront,
    b.is_restaurant,
    TRIM(COALESCE(b.addr_housenumber, '') || ' ' || COALESCE(b.addr_street, '')) AS street_address,
    b.addr_city,
    b.addr_postcode,
    b.lat,
    b.lon,
    b.phone,
    b.website,
    b.opening_hours,
    b.cuisine,
    s.name       AS source_name,
    s.license    AS source_license,
    s.attribution,
    b.last_seen  AS as_of
FROM business b
JOIN source_record sr ON sr.business_id = b.id
JOIN ingest_run  ir ON ir.id = sr.ingest_run_id
JOIN source       s ON s.id = ir.source_id
GROUP BY b.id;
