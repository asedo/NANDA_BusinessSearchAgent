# BusinessSearchAgent

A queryable database of verifiable facts about businesses in a town, built for
[Project NANDA](https://projectnanda.org/) so agents can ask structured
questions and get answers with provenance attached.

Initial test case: **Concord, Massachusetts.**

## Setup

```powershell
.\setup.ps1          # Windows
```
```bash
./setup.sh           # macOS / Linux / WSL / Git Bash
```

Creates `.venv` and runs 33 verification checks. Add `--offline` / `-Offline`
to skip the checks that need network.

Then:

```powershell
.\.venv\Scripts\Activate.ps1     # or: source .venv/bin/activate
python agent.py Concord MA
```

**Python 3.11+.** Verified against 3.11.9 and developed on 3.14.5 — the floor is
set where the project has actually been tested, not guessed.

### There are no dependencies

`requirements.txt` is empty on purpose. Every data source is keyless and
reachable with `urllib`; SQLite ships with Python; the `.xlsx` exporter writes
the format directly with `zipfile`, since xlsx is a zip of XML parts.

This is enforced rather than asserted — `python verify.py` walks the AST of every
module and **fails** if a third-party import appears, so the claim cannot quietly
rot. The virtual environment exists to pin the interpreter and isolate from
system Python, not to install anything.

Stage 3 (website extraction) will need the Anthropic SDK. It is declared as an
optional extra so the default install stays empty:

```bash
pip install -e ".[extract]"
```

### Verifying an existing checkout

```bash
python verify.py             # environment + live pipeline
python verify.py --offline   # no network calls
```

Exits non-zero on the first failure, so it works as a CI gate. It checks the
Python floor, the zero-dependency claim, that every module imports, that `.env`
is gitignored and untracked, that the schema applies cleanly, and that a live
lookup returns classified businesses with provenance attached.

## Quick start — ask the agent about a town

```bash
python agent.py Concord MA                # businesses + NAICS for a US town
python agent.py Lexington MA --json       # agent-to-agent JSON (no file written)
python agent.py Concord MA --naics 72     # food service only (NAICS prefix)
python agent.py Concord MA --refresh      # force re-fetch, ignore cache
python agent.py Concord MA --no-export    # skip the xlsx side effect
```

Everything bootstraps on first call: the NAICS crosswalk builds itself, the town
is resolved against OpenStreetMap, and results are cached for 7 days. US towns
only for now; other countries will be added later.

Every query also writes (or refreshes) one workbook per town at
`exports/<Town>_<ST>_businesses.xlsx`. A **Data Updated** column on every row
carries the date the town's data was fetched from OSM — not the export date —
so a row stays self-describing when copied out of the file.

```python
from agent import lookup
result = lookup("Concord", "MA", naics_prefix="722511")
```

**No credentials are required.** OpenStreetMap, Overpass, and Nominatim are all
keyless — this runs with no `.env` file at all.

### Verified across four towns

| Query | Resolved | Businesses | With NAICS | Storefronts | Restaurants |
|---|---|---:|---:|---:|---:|
| `Concord MA` | Middlesex County, MA | 156 | 141 (90%) | 140 | 31 |
| `Lexington MA` | Middlesex County, MA | 195 | 181 (93%) | 167 | 42 |
| `Concord NH` | Merrimack County, NH | 588 | 519 (88%) | 491 | 123 |
| `Somerville MA` | Middlesex County, MA | 776 | 705 (91%) | 697 | 229 |

Concord MA and Concord NH resolve independently, which is the point of the
`place` table — `addr_city` is only ~57% populated in OSM and cannot scope a town.

### Why there is no LLM in this path

Structured input (town, state) produces structured output (businesses + NAICS).
Nothing in that requires judgement, so a model would add cost, latency, and
nondeterminism for no gain. The LLM belongs at the stage-3 boundary, where
unstructured website HTML has to become structured fields.

## Website descriptions (stage 3)

For every business that lists a URL, `enrich_web.py` reads the site and generates
a one-sentence factual description, stored as **`active_web_query_description`**.

```bash
pip install -e ".[extract]"          # installs the Anthropic SDK
# set ANTHROPIC_API_KEY in .env

python enrich_web.py Concord MA --dry-run --limit 5   # fetch only, no API calls
python enrich_web.py Concord MA --limit 5             # try a few for real
python enrich_web.py Concord MA                       # the whole town
python enrich_web.py --all                            # every town
python enrich_web.py Concord MA --refresh             # regenerate existing
```

The column appears in `agent.py` output, in `business_fact`, and as a
spreadsheet column named *Active Web Query Description*. Alongside it:

| Field | Meaning |
|---|---|
| `active_web_query_description` | One factual sentence about the business |
| `description_confidence` | `high` / `medium` / `low`, as judged by the model |
| `description_is_chain_page` | True when the URL is a corporate store-locator rather than the business's own site |
| `description_model` | Which model produced it |
| `description_generated_at` | When |

**Why `is_chain_page` exists:** of Concord's nine restaurant URLs, four point at
Dunkin'/Starbucks store-locator pages. A description generated from those
describes the chain, not the local outlet. Flagging it beats silently mixing the
two.

**Costs money.** Roughly $1–2 per town on `claude-opus-4-8` (Concord MA has 48
sites; 342 across all three towns). Override with `ANTHROPIC_MODEL` in `.env`.

### Crawling conduct

- `robots.txt` fetched and honoured per host, cached per run
- one request per second, single-threaded
- identifying User-Agent, with `BSA_CONTACT` appended when set
- response bodies capped at 600 KB, 25-second timeouts
- non-HTML content types rejected before download

Raw HTML is saved to `web_snapshot` **before** extraction runs, so a revised
prompt can be re-run over stored pages without re-crawling anyone's site.

Expect some failures — around 1 in 8 sites in testing had TLS problems, served
no server-rendered text, or timed out. One bad site never stops the run.

## Configuration and secrets

Secrets are loaded by `config.py`, with this precedence:

1. Real process environment variables
2. Values in a local `.env` file
3. Defaults in `config.py`

Real env vars win, so production, CI, and containers work without a `.env` ever
existing on disk. `.env` is gitignored and optional — the core pipeline needs
no credentials at all. Create one by hand only if you need optional settings:

```bash
echo "ANTHROPIC_API_KEY=sk-..." >> .env   # only if you use enrich_web.py
python config.py         # self-check — reports set/unset, never prints values
```

| Variable | Required | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | Stage 3 only | Website extraction (not yet implemented) |
| `NANDA_REGISTRY_TOKEN` | Not yet | Reserved for AgentFacts publication |
| `BSA_DB_PATH` | No | Database location |
| `BSA_CONTACT` | No | Contact appended to the OSM User-Agent |
| `BSA_OVERPASS_ENDPOINT` | No | Pin one endpoint instead of the mirror list |
| `ANTHROPIC_MODEL` | No | Model for stage 3 (default `claude-opus-4-8`) |

`python config.py` masks secret values (`sk-a************00`) and verifies that
`.env` is actually gitignored — an unignored `.env` is the single most damaging
misconfiguration here, so it is checked rather than assumed.

Use a role address for `BSA_CONTACT`, not a personal one: it is transmitted to
OSM services in the User-Agent header.

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
agent.py         THE AGENT — lookup(town, state) -> businesses + NAICS
verify.py        environment + pipeline verification (CI gate)
export.py        export a town to .xlsx or CSV (one file per town, in exports/)
enrich_web.py    stage 3 — website -> one-sentence description (needs API key)
setup.ps1 / .sh  create .venv and verify
schema.sql       tables, indexes, business_fact view
config.py        env/.env loading, secret masking, self-check
db.py            connection + schema bootstrap
derive.py        storefront / restaurant inference rules (audit these here)
naics.py         OSM-tag -> NAICS crosswalk + 2-digit sector names
ingest_osm.py    Overpass -> database, idempotent, mirror fallback
query.py         low-level SQL query surface (single-town, debugging)
agentfacts.py    generates the NANDA AgentFacts descriptor (provisional schema)
.env             optional local settings (gitignored, never commit)
businesses.db    SQLite database (generated, gitignored)
```

## NANDA registration (AgentFacts)

```bash
python agentfacts.py -o agentfacts.json
```

Generates the AgentFacts descriptor from **live database state** — capabilities,
places served, business counts, and NAICS sectors are read from the database
rather than hand-maintained, so the document cannot drift from what the agent
actually does.

It also carries **ODbL attribution in-band**, under `provenance.sources`. That is
the mechanism by which the OpenStreetMap credit reaches a consuming agent instead
of dying at the API boundary.

> ### ⚠️ Schema is provisional — not validated against the canonical spec
>
> The `@context` URL cited across the NANDA literature,
> `https://spec.projectnanda.org/agentfacts/v1`, **does not resolve in DNS**
> (checked 2026-07-18; `projectnanda.org` and `index.projectnanda.org` both
> resolve, `spec.projectnanda.org` does not).
>
> Field names are reconstructed from *Beyond DNS: Unlocking the Internet of AI
> Agents via the NANDA Index and Verified AgentFacts*
> ([arXiv:2507.14263](https://arxiv.org/abs/2507.14263)), Table 5, cross-checked
> against published examples. Validate before registering.

Two things are required and deliberately **not** done here:

1. **`id` must be a real DID.** The placeholder is not resolvable.
2. **The document must be signed** as a W3C Verifiable Credential v2. This
   generator emits the unsigned payload only — signing needs a key this
   repository does not hold and should not.

Set identity via `.env` (`NANDA_HANDLE`, `NANDA_AGENT_DID`, `NANDA_OWNER_DID`,
`NANDA_ENDPOINT`); `agentfacts.py` warns about any that are still placeholders.

## Operational hazards handled

**A failed fetch must never look like an empty town.** `place.last_ingested` is
written only by `mark_ingested()`, after Overpass has actually answered. An
earlier version set it when the place was resolved, so a failed fetch left a
place that appeared freshly ingested with zero businesses — and the agent then
served "0 businesses" as a cached fact. Silently wrong answers are worse than
errors, especially for a consuming agent that cannot tell the difference.

**Overpass mirrors fail routinely.** `run_overpass()` tries each mirror once,
then makes a second pass with backoff — a 504 means that instance is loaded, so
the next mirror beats an immediate retry. `overpass.osm.jp` was removed from the
list: its TLS certificate fails hostname validation, and the only workaround
would be disabling certificate verification.

**Mirrors must be full-planet, and the query proves it.** `overpass.osm.ch` was
removed after it answered a Somerville MA query with HTTP 200 and zero elements
— it is the Swiss OSM association's regional instance and hosts only
Switzerland, so the empty answer was cached as a valid "0 businesses". The
ingest query now emits the area itself (`.a out ids;`) as a coverage proof: a
mirror whose response lacks the area element is treated as failed and the next
mirror is tried, making "no coverage" distinguishable from "town with no
businesses". `verify.py` probes every configured mirror for US coverage.

**US towns only, for now.** Nominatim drops query parts it cannot match, so
even a query ending in ", USA" can resolve abroad. `resolve_area()` requests
`addressdetails` and rejects any place whose country code is not `us` with a
clear message; workflows for other countries will be added later.

**Towns must resolve to an OSM relation.** Overpass builds query areas from
administrative boundaries; a town resolving only to a node or way has no polygon
to search inside. `resolve_area()` scans up to five Nominatim hits for a relation
and raises a clear error rather than silently returning nothing.

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
