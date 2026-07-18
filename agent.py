"""BusinessSearchAgent — ask for a town and state, get back its businesses.

    python agent.py Concord MA
    python agent.py Lexington MA --json
    python agent.py Concord MA --naics 72        # food service only
    python agent.py Concord MA --refresh         # force re-fetch from OSM

Programmatic:
    from agent import lookup
    result = lookup("Concord", "MA")

This is a deterministic agent: structured input (town, state) -> structured
output (businesses + NAICS). There is no LLM in the path, because there is
nothing here for one to decide - it would add cost, latency, and nondeterminism
to a lookup. An LLM belongs at the stage-3 boundary, where unstructured website
HTML has to become structured fields.

Every response carries provenance and a coverage note. The agent does not claim
to know every business in a town, and says so in-band rather than leaving a
consuming agent to assume completeness.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys

import db
import ingest_osm
import naics as naics_mod

# How long a town's data is served from cache before we re-query OSM.
CACHE_TTL_DAYS = 7

COVERAGE_NOTE = (
    "OpenStreetMap maps what is physically visible, so coverage skews toward "
    "storefront retail and food service and under-represents home-based and "
    "professional service businesses. This is a partial inventory, not a "
    "complete business census."
)


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        then = _dt.datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=_dt.timezone.utc)
    return (_dt.datetime.now(_dt.timezone.utc) - then).total_seconds() / 86400


def find_place(conn, town: str, state: str):
    return conn.execute(
        """SELECT * FROM place
           WHERE LOWER(query_town)=LOWER(?) AND LOWER(query_state)=LOWER(?)
           ORDER BY last_ingested DESC LIMIT 1""",
        (town, state)).fetchone()


def lookup(town: str, state: str, *, naics_prefix: str | None = None,
           refresh: bool = False, quiet: bool = True) -> dict:
    """Return businesses for a town, ingesting from OSM if not cached.

    Raises ingest_osm.ResolveError if the town cannot be resolved to an
    administrative boundary.
    """
    conn = db.connect()
    db.init(conn)

    if not conn.execute("SELECT 1 FROM osm_naics_crosswalk LIMIT 1").fetchone():
        if not quiet:
            print("NAICS crosswalk empty - building it first ...", file=sys.stderr)
        naics_mod.build(conn)

    place = find_place(conn, town, state)
    age = _age_days(place["last_ingested"]) if place else None
    stale = age is None or age > CACHE_TTL_DAYS

    ingested = None
    if refresh or place is None or stale:
        query = f"{town}, {state}, USA"
        if not quiet:
            print(f"fetching {query} from OpenStreetMap ...", file=sys.stderr)
        ingested = ingest_osm.ingest(conn, query, town=town, state=state, quiet=quiet)
        place = find_place(conn, town, state)
        age = 0.0

    if place is None:
        raise ingest_osm.ResolveError(f"no data for {town}, {state}")

    sql = "SELECT * FROM business_fact WHERE place_id=?"
    params: list = [place["id"]]
    if naics_prefix:
        sql += " AND naics LIKE ?||'%'"
        params.append(naics_prefix)
    rows = conn.execute(sql + " ORDER BY naics IS NULL, naics, name", params).fetchall()

    businesses = []
    for r in rows:
        sector_code, sector_name = naics_mod.sector_of(r["naics"])
        businesses.append({
            "name": r["name"],
            "naics": r["naics"],
            "naics_title": r["naics_title"],
            "naics_sector": sector_code,
            "naics_sector_name": sector_name,
            "naics_vintage": r["naics_vintage"],
            "naics_source": r["naics_source"],
            "osm_category": r["osm_category"],
            "has_storefront": bool(r["has_storefront"]) if r["has_storefront"] is not None else None,
            "is_restaurant": bool(r["is_restaurant"]),
            "address": r["street_address"] or None,
            "city": r["addr_city"],
            "postcode": r["addr_postcode"],
            "lat": r["lat"],
            "lon": r["lon"],
            "phone": r["phone"],
            "website": r["website"],
            "opening_hours": r["opening_hours"],
            "cuisine": r["cuisine"],
            # Stage 3: generated from the business's own website by enrich_web.py.
            # None until that has been run for this business.
            "active_web_query_description": r["active_web_query_description"],
            "description_confidence": r["description_confidence"],
            "description_is_chain_page": (
                bool(r["description_is_chain_page"])
                if r["description_is_chain_page"] is not None else None),
            "description_model": r["description_model"],
            "description_generated_at": r["description_generated_at"],
        })

    with_naics = sum(1 for b in businesses if b["naics"])
    sectors: dict[str, int] = {}
    for b in businesses:
        if b["naics_sector_name"]:
            sectors[b["naics_sector_name"]] = sectors.get(b["naics_sector_name"], 0) + 1

    return {
        "query": {"town": town, "state": state,
                  "naics_prefix": naics_prefix},
        "place": {
            "resolved_name": place["display_name"],
            "osm_relation_id": place["osm_relation_id"],
            "lat": place["lat"], "lon": place["lon"],
        },
        "counts": {
            "businesses": len(businesses),
            "with_naics": with_naics,
            "storefronts": sum(1 for b in businesses if b["has_storefront"]),
            "restaurants": sum(1 for b in businesses if b["is_restaurant"]),
            "with_website": sum(1 for b in businesses if b["website"]),
        },
        "naics_sectors": dict(sorted(sectors.items(), key=lambda kv: -kv[1])),
        "provenance": {
            "source": "OpenStreetMap via Overpass API",
            "license": "ODbL-1.0",
            "attribution": "© OpenStreetMap contributors",
            "naics_crosswalk": "OpenStreetMap Wiki NAICS/2022 (CC BY-SA 2.0)",
            "naics_vintage": "2022",
            "observed": place["last_ingested"],
            "cache_age_days": round(age, 2) if age is not None else None,
            "refetched": ingested is not None,
        },
        "coverage_note": COVERAGE_NOTE,
        "businesses": businesses,
    }


def _print_human(res: dict) -> None:
    p, c = res["place"], res["counts"]
    print(f"\n{p['resolved_name']}")
    print(f"  {c['businesses']} businesses | {c['with_naics']} with NAICS | "
          f"{c['storefronts']} storefronts | {c['restaurants']} restaurants")
    prov = res["provenance"]
    freshness = ("freshly fetched" if prov["refetched"]
                 else f"cached, {prov['cache_age_days']}d old")
    print(f"  observed {prov['observed']} ({freshness})")

    if res["naics_sectors"]:
        print("\n  NAICS sectors:")
        for name, n in res["naics_sectors"].items():
            print(f"    {n:4d}  {name}")

    print(f"\n  {'NAICS':8s} {'BUSINESS':34s} {'INDUSTRY':40s} SF")
    print(f"  {'-'*8} {'-'*34} {'-'*40} --")
    for b in res["businesses"]:
        sf = "Y" if b["has_storefront"] else ("n" if b["has_storefront"] is False else "?")
        print(f"  {(b['naics'] or '--'):8s} {b['name'][:34]:34s} "
              f"{(b['naics_title'] or '(unclassified)')[:40]:40s} {sf}")

    print(f"\n  {res['provenance']['attribution']} | "
          f"{res['provenance']['license']} | NAICS {res['provenance']['naics_vintage']}")
    print(f"  note: {res['coverage_note'][:100]}...")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Return businesses and NAICS classification for a US town. "
                    "(US only for now; other countries will be added later.)")
    ap.add_argument("town")
    ap.add_argument("state", help="two-letter US state code or full name, e.g. MA")
    ap.add_argument("--naics", metavar="PREFIX",
                    help="filter by NAICS prefix, e.g. 72 or 722511")
    ap.add_argument("--refresh", action="store_true",
                    help="force re-fetch from OSM, ignoring cache")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    try:
        res = lookup(args.town, args.state, naics_prefix=args.naics,
                     refresh=args.refresh, quiet=args.json)
    except (ingest_osm.ResolveError, ingest_osm.OverpassError) as exc:
        sys.exit(f"error: {exc}")

    print(json.dumps(res, indent=2)) if args.json else _print_human(res)


if __name__ == "__main__":
    main()
