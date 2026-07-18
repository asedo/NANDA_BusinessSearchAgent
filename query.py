"""Agent-facing query surface.

Every result carries its provenance (source, license, as_of). That is deliberate:
this database does not claim to be a complete census of any town, so a consumer
needs to know what backs each fact and when it was observed.

  python query.py stats
  python query.py search "bakery"
  python query.py naics 72          # all food service (prefix match)
  python query.py naics 722511      # full-service restaurants exactly
  python query.py restaurants --with-website
  python query.py storefronts
  python query.py show 42
"""
from __future__ import annotations

import argparse
import json
import sys

import db
from naics import SECTORS as NAICS_SECTORS  # single source of truth

FACT_COLS = """id, name, osm_category, naics, naics_title, naics_vintage,
               naics_source, has_storefront, is_restaurant, street_address,
               addr_city, addr_postcode, lat, lon, phone, website,
               opening_hours, cuisine, source_name, source_license,
               attribution, as_of"""


def rows_to_json(rows) -> str:
    return json.dumps([dict(r) for r in rows], indent=2)


def print_table(rows, cols: tuple[str, ...]) -> None:
    if not rows:
        print("(no results)")
        return
    widths = {c: max(len(c), max(len(str(r[c] or "")) for r in rows)) for c in cols}
    widths = {c: min(w, 46) for c, w in widths.items()}
    print("  ".join(c.upper().ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r[c] or "")[:widths[c]].ljust(widths[c]) for c in cols))
    print(f"\n{len(rows)} result(s)")


def cmd_stats(conn, _args) -> None:
    q = conn.execute
    total = q("SELECT COUNT(*) n FROM business").fetchone()["n"]
    print(f"businesses            : {total}")
    for label, sql in [
        ("with NAICS code", "SELECT COUNT(*) n FROM business WHERE naics IS NOT NULL"),
        ("storefront", "SELECT COUNT(*) n FROM business WHERE has_storefront=1"),
        ("restaurants", "SELECT COUNT(*) n FROM business WHERE is_restaurant=1"),
        ("with website", "SELECT COUNT(*) n FROM business WHERE website IS NOT NULL"),
        ("with phone", "SELECT COUNT(*) n FROM business WHERE phone IS NOT NULL"),
        ("with street address", "SELECT COUNT(*) n FROM business WHERE addr_street IS NOT NULL"),
        ("with opening hours", "SELECT COUNT(*) n FROM business WHERE opening_hours IS NOT NULL"),
    ]:
        n = q(sql).fetchone()["n"]
        pct = f"{100 * n / total:5.1f}%" if total else "  n/a"
        print(f"  {label:20s}: {n:4d}  {pct}")

    print("\nNAICS sectors (2-digit):")
    for r in q("""SELECT SUBSTR(naics,1,2) sector, COUNT(*) n
                  FROM business WHERE naics IS NOT NULL
                  GROUP BY sector ORDER BY n DESC"""):
        name = NAICS_SECTORS.get(r["sector"], "(unknown sector)")
        print(f"  {r['sector']}  {r['n']:4d}  {name}")

    print("\nunclassified OSM categories (no NAICS mapping):")
    for r in q("""SELECT primary_tag_key||'='||primary_tag_val cat, COUNT(*) n
                  FROM business WHERE naics IS NULL AND primary_tag_key IS NOT NULL
                  GROUP BY cat ORDER BY n DESC LIMIT 10"""):
        print(f"  {r['n']:4d}  {r['cat']}")

    run = q("""SELECT place, finished_at, element_count, endpoint
               FROM ingest_run ORDER BY id DESC LIMIT 1""").fetchone()
    if run:
        print(f"\nlast ingest: {run['place']}")
        print(f"  {run['element_count']} elements at {run['finished_at']} via "
              f"{(run['endpoint'] or '').split('/')[2] if run['endpoint'] else '?'}")


def cmd_search(conn, args) -> None:
    rows = conn.execute(
        f"SELECT {FACT_COLS} FROM business_fact WHERE name LIKE ? ORDER BY name",
        (f"%{args.term}%",)).fetchall()
    _emit(rows, args, ("id", "name", "naics", "naics_title", "street_address"))


def cmd_naics(conn, args) -> None:
    rows = conn.execute(
        f"""SELECT {FACT_COLS} FROM business_fact
            WHERE naics LIKE ?||'%' ORDER BY naics, name""",
        (args.code,)).fetchall()
    _emit(rows, args, ("id", "name", "naics", "naics_title", "street_address"))


def cmd_restaurants(conn, args) -> None:
    sql = f"SELECT {FACT_COLS} FROM business_fact WHERE is_restaurant=1"
    if args.with_website:
        sql += " AND website IS NOT NULL"
    rows = conn.execute(sql + " ORDER BY name").fetchall()
    _emit(rows, args, ("id", "name", "naics_title", "cuisine", "website"))


def cmd_storefronts(conn, args) -> None:
    rows = conn.execute(
        f"SELECT {FACT_COLS} FROM business_fact WHERE has_storefront=1 ORDER BY name"
    ).fetchall()
    _emit(rows, args, ("id", "name", "osm_category", "naics_title", "street_address"))


def cmd_show(conn, args) -> None:
    row = conn.execute(
        f"SELECT {FACT_COLS} FROM business_fact WHERE id=?", (args.id,)).fetchone()
    if not row:
        sys.exit(f"no business with id {args.id}")
    print(json.dumps(dict(row), indent=2))
    print("\n-- provenance --")
    for r in conn.execute(
            """SELECT ir.place, ir.endpoint, sr.fetched_at
               FROM source_record sr JOIN ingest_run ir ON ir.id = sr.ingest_run_id
               WHERE sr.business_id=? ORDER BY sr.id DESC""", (args.id,)):
        print(f"  observed {r['fetched_at']} in {r['place']}")


def _emit(rows, args, cols) -> None:
    print(rows_to_json(rows) if args.json else "", end="")
    if not args.json:
        print_table(rows, cols)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="emit JSON (agent-facing)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats").set_defaults(fn=cmd_stats)

    s = sub.add_parser("search"); s.add_argument("term"); s.set_defaults(fn=cmd_search)
    n = sub.add_parser("naics"); n.add_argument("code"); n.set_defaults(fn=cmd_naics)
    r = sub.add_parser("restaurants")
    r.add_argument("--with-website", action="store_true"); r.set_defaults(fn=cmd_restaurants)
    sub.add_parser("storefronts").set_defaults(fn=cmd_storefronts)
    sh = sub.add_parser("show"); sh.add_argument("id", type=int); sh.set_defaults(fn=cmd_show)

    args = p.parse_args()
    args.fn(db.connect(), args)


if __name__ == "__main__":
    main()
