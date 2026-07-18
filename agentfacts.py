"""Generate an AgentFacts document describing this agent for the NANDA Index.

    python agentfacts.py              # print to stdout
    python agentfacts.py -o agentfacts.json

The document is generated from live database state - capabilities, coverage,
and served places are read from the database rather than hand-maintained, so it
cannot drift from what the agent actually does.

-------------------------------------------------------------------------------
SCHEMA STATUS: PROVISIONAL - NOT VALIDATED AGAINST THE CANONICAL SPEC
-------------------------------------------------------------------------------
The AgentFacts @context URL cited across the NANDA literature,
https://spec.projectnanda.org/agentfacts/v1, does not currently resolve in DNS
(checked 2026-07-18; projectnanda.org and index.projectnanda.org both resolve,
spec.projectnanda.org does not).

Field names below are drawn from the NANDA paper "Beyond DNS: Unlocking the
Internet of AI Agents via the NANDA Index and Verified AgentFacts"
(arXiv:2507.14263), Table 5, cross-referenced with published examples. Treat
them as a best-effort reconstruction. Validate and adjust before registering.

Two things are known to be required and NOT done here:
  1. `id` must be a real DID. The placeholder below is not resolvable.
  2. The document must be signed as a W3C Verifiable Credential v2. This
     generator emits the unsigned payload only; signing needs a key this
     repository deliberately does not hold.
-------------------------------------------------------------------------------
"""
from __future__ import annotations

import argparse
import json
import sys

import config
import db

SPEC_CONTEXT = "https://spec.projectnanda.org/agentfacts/v1"
SCHEMA_VERSION = "0.1-provisional"

# Identity is deployment-specific. Override via environment or .env.
DEFAULTS = {
    "NANDA_HANDLE": "@example:business-search",
    "NANDA_AGENT_DID": "did:nanda:PLACEHOLDER-not-yet-issued",
    "NANDA_OWNER_DID": "did:nanda:PLACEHOLDER-owner",
    "NANDA_ENDPOINT": "https://example.invalid/business-search",
}


def _cfg(key: str) -> str:
    return config.get(key) or DEFAULTS[key]


def _placeholders() -> list[str]:
    return [k for k in DEFAULTS if not config.get(k)]


def build(conn) -> dict:
    places = conn.execute(
        """SELECT query_town, query_state, display_name, last_ingested,
                  (SELECT COUNT(*) FROM business b WHERE b.place_id=place.id) n
           FROM place WHERE last_ingested IS NOT NULL
           ORDER BY query_state, query_town""").fetchall()

    total = conn.execute("SELECT COUNT(*) n FROM business").fetchone()["n"]
    classified = conn.execute(
        "SELECT COUNT(*) n FROM business WHERE naics IS NOT NULL").fetchone()["n"]
    sectors = [r["s"] for r in conn.execute(
        """SELECT DISTINCT SUBSTR(naics,1,2) s FROM business
           WHERE naics IS NOT NULL ORDER BY s""")]

    return {
        "@context": SPEC_CONTEXT,
        "id": _cfg("NANDA_AGENT_DID"),
        "handle": _cfg("NANDA_HANDLE"),
        "agent_name": "BusinessSearchAgent",
        "description": (
            "Returns businesses in a US town with NAICS 2022 industry "
            "classification, storefront status, geolocation, and per-fact "
            "provenance. Built on OpenStreetMap."
        ),
        "owner": {"id": _cfg("NANDA_OWNER_DID")},

        # Paper describes static / rotating / adaptive endpoint classes with TTLs.
        "endpoints": {
            "static": [{
                "url": _cfg("NANDA_ENDPOINT"),
                "protocol": "https",
                "ttl_seconds": 3600,
            }],
        },

        "capabilities": [
            {
                "urn": "urn:nanda:cap:business-directory-lookup:v1",
                "name": "lookup_businesses_by_town",
                "description": "List businesses in a US town, given town and state.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "town": {"type": "string", "description": "Town name, e.g. Concord"},
                        "state": {"type": "string", "description": "State code or name, e.g. MA"},
                        "naics_prefix": {
                            "type": "string",
                            "description": "Optional NAICS prefix filter; 72 matches "
                                           "all accommodation and food service.",
                        },
                    },
                    "required": ["town", "state"],
                },
                "output_schema": {
                    "type": "object",
                    "properties": {
                        "counts": {"type": "object"},
                        "naics_sectors": {"type": "object"},
                        "provenance": {"type": "object"},
                        "coverage_note": {"type": "string"},
                        "businesses": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "naics": {"type": "string"},
                                    "naics_title": {"type": "string"},
                                    "naics_sector": {"type": "string"},
                                    "has_storefront": {"type": ["boolean", "null"]},
                                    "is_restaurant": {"type": "boolean"},
                                    "address": {"type": ["string", "null"]},
                                    "lat": {"type": ["number", "null"]},
                                    "lon": {"type": ["number", "null"]},
                                    "website": {"type": ["string", "null"]},
                                },
                            },
                        },
                    },
                },
            },
            {
                "urn": "urn:nanda:cap:industry-classification:v1",
                "name": "filter_by_naics",
                "description": "Filter results by NAICS 2022 code or prefix. Codes are "
                               "hierarchical, so a prefix selects a whole sector.",
            },
        ],

        "usage_format": {
            "input": "application/json",
            "output": "application/json",
            "classification_standard": "NAICS",
            "classification_vintage": "2022",
        },

        "discovery": {
            "keywords": ["business directory", "local business", "NAICS",
                         "industry classification", "storefront", "restaurants",
                         "OpenStreetMap", "town", "municipality"],
            "geographic_scope": "United States",
            "coverage": {
                "places_served": [
                    {"town": p["query_town"], "state": p["query_state"],
                     "resolved": p["display_name"], "businesses": p["n"],
                     "observed": p["last_ingested"]}
                    for p in places
                ],
                "businesses_total": total,
                "businesses_classified": classified,
                "naics_sectors_present": sectors,
                "on_demand": True,
                "note": "Towns not yet cached are fetched from OpenStreetMap on "
                        "first request.",
            },
        },

        # Stated plainly so a consuming agent does not infer completeness.
        "limitations": {
            "completeness": "PARTIAL",
            "detail": (
                "OpenStreetMap records what is physically visible, so coverage "
                "skews toward storefront retail and food service and "
                "under-represents home-based and professional service "
                "businesses. Observed coverage is roughly a quarter to a third "
                "of actual establishments in a town. This is a partial "
                "inventory, not a business census."
            ),
            "classification": (
                "NAICS codes are inferred from OpenStreetMap tags via a "
                "community-maintained crosswalk, not self-declared by the "
                "business. Each record carries naics_source so inferred and "
                "declared values remain distinguishable. Known imprecision: "
                "amenity=cafe maps to 722515 (snack and nonalcoholic beverage "
                "bars), which suits a coffee chain better than a sit-down cafe."
            ),
        },

        # ODbL requires attribution to reach downstream consumers. Carrying it
        # in-band is what makes that possible for agent-to-agent use.
        "provenance": {
            "sources": [
                {
                    "name": "OpenStreetMap",
                    "url": "https://www.openstreetmap.org",
                    "license": "ODbL-1.0",
                    "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
                    "attribution": "© OpenStreetMap contributors",
                    "role": "business discovery, geolocation, attributes",
                },
                {
                    "name": "OpenStreetMap Wiki NAICS/2022 crosswalk",
                    "url": "https://wiki.openstreetmap.org/wiki/NAICS/2022",
                    "license": "CC-BY-SA-2.0",
                    "attribution": "OpenStreetMap Wiki contributors",
                    "role": "OSM tag to NAICS mapping",
                },
                {
                    "name": "NAICS 2022",
                    "url": "https://www.census.gov/naics/",
                    "license": "public-domain",
                    "attribution": "US Census Bureau",
                    "role": "industry code definitions",
                },
            ],
            "attribution_required": True,
            "attribution_notice": "© OpenStreetMap contributors (ODbL-1.0)",
        },

        "security": {
            "transport": "TLS",
            "auth": {"type": "none", "note": "Read-only public reference data."},
            "pii": {
                "contains_pii": False,
                "note": "Business records only. Registry officer and agent "
                        "addresses are deliberately excluded, as they are "
                        "frequently home addresses.",
            },
        },

        "certification": {
            "type": "self-asserted",
            "verifiable_credential": None,
            "note": "Unsigned. Requires a W3C Verifiable Credential v2 signature "
                    "before registration.",
        },

        "ttl_seconds": 3600,

        "meta": {
            "schema_version": SCHEMA_VERSION,
            "schema_status": "PROVISIONAL - spec.projectnanda.org did not resolve "
                             "at generation time; field names reconstructed from "
                             "arXiv:2507.14263 Table 5 and published examples",
            "published": db.now(),
            "generator": "agentfacts.py",
            "source_repository": "https://github.com/asedo/NANDA_BusinessSearchAgent",
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate an AgentFacts document.")
    ap.add_argument("-o", "--output", help="write to file instead of stdout")
    args = ap.parse_args()

    conn = db.connect()
    db.init(conn)
    doc = build(conn)
    text = json.dumps(doc, indent=2, ensure_ascii=False)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.output} ({len(text)} bytes)", file=sys.stderr)
    else:
        print(text)

    missing = _placeholders()
    if missing:
        print("\nPLACEHOLDERS STILL PRESENT - not registrable as-is:",
              file=sys.stderr)
        for key in missing:
            print(f"  {key:20s} using {DEFAULTS[key]!r}", file=sys.stderr)
        print("  Set these in .env, then regenerate.", file=sys.stderr)
    print("  Document is UNSIGNED; W3C VC v2 signature required before "
          "registration.", file=sys.stderr)


if __name__ == "__main__":
    main()
