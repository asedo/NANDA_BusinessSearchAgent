# BusinessSearchAgent

A queryable database of verifiable facts about businesses in a town, built for
[Project NANDA](https://projectnanda.org/) so agents can ask structured
questions and get answers with provenance attached.

Initial test case: **Concord, Massachusetts.**

Standard library only — no pip install required.

## Quick start

```bash
python naics.py                                            # build the NAICS crosswalk
python ingest_osm.py "Concord, Middlesex County, Massachusetts, USA"
python query.py stats
```

## Current state (Concord, MA)

| Metric | Count | Share |
|---|---:|---:|
| Businesses | 156 | — |
| With NAICS code | 141 | 90% |
| Storefront | 140 | 90% |
| Restaurants | 31 | 20% |
| With website | 48 | 31% |
| With street address | 91 | 58% |
| With opening hours | 46 | 30% |

## Querying

```bash
python query.py stats                      # coverage summary + unclassified tags
python query.py search "bakery"            # name substring
python query.py naics 72                   # prefix: all accommodation & food service
python query.py naics 722511               # exact: full-service restaurants
python query.py restaurants --with-website # stage-3 scrape candidates
python query.py storefronts
python query.py show 84                    # full record + provenance
python query.py naics 44 --json            # agent-facing JSON
```

NAICS is hierarchical, so prefix matching gives you every level for free:

```
722511  Full-Service Restaurants
  72251   Restaurants and Other Eating Places
    7225    Restaurants and Other Eating Places
      722     Food Services and Drinking Places
        72      Accommodation and Food Services
```

## Design principles

**1. Every fact is attributable.** `source` → `ingest_run` → `source_record`
means any row can answer *where did this come from, and when?* The
`business_fact` view carries source, license, and `as_of` on every result. This
matters more than usual here: the database does **not** claim to be a complete
census, so a consumer needs to know what backs each fact.

**2. Raw payloads are retained.** `source_record.raw_json` holds the original
upstream element. Re-parsing never requires re-querying. Likewise `web_snapshot`
is kept separate from `extraction`, so extraction can be re-run with a better
prompt without re-crawling anyone's website.

**3. Inferred and declared facts are distinguishable.** `naics_source` is
`osm_crosswalk` (we concluded it) or `registry` (the business declared it).
Never flatten those together.

## Sources

| Source | Role | Status | Cost |
|---|---|---|---|
| [OpenStreetMap](https://www.openstreetmap.org) via [Overpass](https://overpass-api.de) | Discovery, geo, storefront flag | **Live** | Free, no key |
| [OSM NAICS/2022 crosswalk](https://wiki.openstreetmap.org/wiki/NAICS/2022) | Industry classification | **Live** | Free |
| [MA Corporations Division](https://corp.sec.state.ma.us/corpweb/CorpSearch/CorpSearch.aspx) | Legal status, formation date, officers | Planned (manual) | Free |
| MassGIS assessor parcels | Coverage beyond OSM | Unverified lead | Free |
| Business websites | Menus, hours, detail | Planned | ~$1–2/town via LLM |

### Attribution requirement

OSM data is **ODbL-1.0**, which requires attribution. Any NANDA agent serving
these facts must credit *© OpenStreetMap contributors* — the string is stored in
`source.attribution` and surfaced on every `business_fact` row.

## Known limitations

**Coverage is partial, and that is inherent.** Concord has roughly 400–600
actual establishments; OSM yields 156. Because OSM maps what is physically
visible, coverage skews toward storefront retail and food, and misses
home-based and professional service businesses. This is not a bug to fix but a
property to disclose — hence provenance on every fact.

**The NAICS crosswalk is community-maintained and imperfect:**

- `amenity=cafe` → 722515 (*Snack and Nonalcoholic Beverage Bars*) is
  defensible for a coffee chain, wrong for a sit-down café. Concord has 11.
- `shop=hairdresser` → 812111 (*Barber Shops*) vs `shop=beauty` → 812112
  (*Beauty Salons*): NAICS splits along a line OSM does not encode.
- `shop=dry_cleaning` and `shop=laundry` collapse to the same code.
- `shop=clothes` and `leisure=sports_centre` had no wiki entry; supplied via
  `MANUAL_OVERRIDES` in `naics.py` and validated against the real NAICS list.

Caveats are stored per-mapping in `osm_naics_crosswalk.note`.

**15 businesses remain unclassified** — mostly `office=*` subtypes and
`amenity=theatre`. `python query.py stats` lists them; extending
`MANUAL_OVERRIDES` is an afternoon's work.

**The MA registry cannot be enumerated.** Its search accepts entity name,
individual name, ID, or filing number — **not city**. It also 403s plain HTTP
clients and uses session-scoped opaque tokens rather than stable permalinks. It
is therefore an *enrichment* source keyed off names OSM already gave us, not a
discovery source. No public bulk download exists.

**Privacy note:** registry records include officer and resident-agent addresses,
which are frequently home addresses. They are public record, but republishing
them into an agent-queryable index is a different act than a state website
requiring a manual per-entity lookup. Recommend storing officer *names* and
*roles* only, or gating addresses behind an explicit AgentFacts capability.

## Files

```
schema.sql       tables, indexes, business_fact view
db.py            connection + schema bootstrap
derive.py        storefront / restaurant inference rules (audit these here)
naics.py         builds OSM-tag -> NAICS crosswalk from the OSM wiki
ingest_osm.py    Overpass -> database, idempotent, mirror fallback
query.py         agent-facing query surface
businesses.db    SQLite database (generated)
```

## Roadmap

1. **Extend `MANUAL_OVERRIDES`** to classify the remaining 15 businesses.
2. **Registry enrichment** — 156 targeted lookups adding legal name, entity
   type, standing, and formation date. Highest-value facts available; nothing
   else free answers *"is this business currently in good standing?"*
3. **Verify MassGIS parcels** for coverage beyond OSM.
4. **AgentFacts document** publishing the handle, endpoint, capabilities, and
   OSM attribution to the NANDA Index.
5. **Website extraction** last — only ~5 Concord restaurants have a real
   independent site (4 of the 9 with URLs are chain store-locator pages).

## Operational notes

- Overpass's public instance returns **504 under load**; `ingest_osm.py`
  retries and falls back across three mirrors.
- Ingest is idempotent: re-running updates rows and bumps `last_seen`.
- `place` must resolve to an OSM **relation** (an administrative boundary).
  The script exits with a clear message if it resolves to a node or way.
