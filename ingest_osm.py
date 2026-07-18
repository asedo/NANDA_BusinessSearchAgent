"""Ingest businesses for a town from OpenStreetMap via the Overpass API.

Idempotent: re-running updates existing rows and bumps last_seen. Each run also
writes an immutable source_record with the raw upstream payload, so extraction
logic can be revised later without re-querying Overpass.

Run:  python ingest_osm.py "Concord, Middlesex County, Massachusetts, USA"
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

import db
import derive

UA = {"User-Agent": "BusinessSearchAgent/0.1 (+https://projectnanda.org)"}

NOMINATIM = "https://nominatim.openstreetmap.org/search"
MIRRORS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
)

OSM_ATTRIBUTION = "© OpenStreetMap contributors"
OSM_LICENSE = "ODbL-1.0"

QUERY_TMPL = """
[out:json][timeout:180];
area({area})->.a;
(
  nwr["shop"](area.a);
  nwr["amenity"~"^(restaurant|cafe|bar|pub|fast_food|ice_cream|food_court|bank|pharmacy|fuel|veterinary|car_wash|car_rental|cinema|theatre|library|post_office)$"](area.a);
  nwr["office"](area.a);
  nwr["craft"](area.a);
  nwr["tourism"~"^(hotel|motel|guest_house|museum|gallery)$"](area.a);
  nwr["healthcare"](area.a);
  nwr["leisure"~"^(fitness_centre|sports_centre|golf_course)$"](area.a);
);
out center tags;
"""


def resolve_area(place: str) -> tuple[int, str]:
    """Nominatim place name -> Overpass area id."""
    url = NOMINATIM + "?" + urllib.parse.urlencode(
        {"q": place, "format": "json", "limit": 1})
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=45) as r:
        hits = json.load(r)
    if not hits:
        sys.exit(f"could not resolve place: {place!r}")
    hit = hits[0]
    if hit["osm_type"] != "relation":
        sys.exit(f"{place!r} resolved to a {hit['osm_type']}, need a relation "
                 f"(an administrative boundary)")
    return int(hit["osm_id"]) + 3_600_000_000, hit["display_name"]


def run_overpass(query: str) -> tuple[dict, str]:
    """Try mirrors in order. The public instance 504s under load routinely."""
    last = None
    for mirror in MIRRORS:
        for attempt in (1, 2):
            try:
                req = urllib.request.Request(
                    mirror, data=urllib.parse.urlencode({"data": query}).encode(),
                    headers=UA)
                with urllib.request.urlopen(req, timeout=240) as r:
                    return json.load(r), mirror
            except Exception as exc:  # noqa: BLE001 - report and try next mirror
                last = exc
                print(f"  {mirror.split('/')[2]} attempt {attempt}: "
                      f"{type(exc).__name__}", file=sys.stderr)
                time.sleep(4)
    sys.exit(f"all Overpass mirrors failed; last error: {last}")


def ingest(conn, place: str) -> None:
    source_id = db.upsert_source(conn, "openstreetmap", "https://www.openstreetmap.org",
                                 OSM_LICENSE, OSM_ATTRIBUTION)
    area_id, display = resolve_area(place)
    print(f"resolved: {display}\n  overpass area {area_id}")

    query = QUERY_TMPL.format(area=area_id)
    started = db.now()
    cur = conn.execute(
        """INSERT INTO ingest_run (source_id, place, area_id, started_at, query)
           VALUES (?,?,?,?,?)""",
        (source_id, display, area_id, started, query))
    run_id = cur.lastrowid
    conn.commit()

    print("querying overpass ...")
    data, endpoint = run_overpass(query)
    elements = data.get("elements", [])
    print(f"  {len(elements)} elements via {endpoint.split('/')[2]}")

    crosswalk = {
        r["osm_tag"]: (r["naics"], r["naics_title"])
        for r in conn.execute(
            "SELECT osm_tag, naics, naics_title FROM osm_naics_crosswalk WHERE vintage='2022'")
    }
    if not crosswalk:
        sys.exit("crosswalk is empty - run `python naics.py` first")

    ts = db.now()
    inserted = updated = skipped = classified = 0

    for el in elements:
        tags = el.get("tags") or {}
        name = tags.get("name")
        if not name:
            skipped += 1
            continue

        key, val = derive.primary_tag(tags)
        naics = naics_title = naics_src = None
        if key and val:
            hit = crosswalk.get(f"{key}={val}") or crosswalk.get(f"{key}=*")
            if hit:
                naics, naics_title = hit
                naics_src = "osm_crosswalk"
                classified += 1

        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")

        row = (
            el["type"], el["id"], name, lat, lon,
            tags.get("addr:housenumber"), tags.get("addr:street"),
            tags.get("addr:city"), tags.get("addr:state"), tags.get("addr:postcode"),
            derive.first(tags, "phone", "contact:phone"),
            derive.first(tags, "website", "contact:website", "url"),
            tags.get("opening_hours"), tags.get("brand"), tags.get("cuisine"),
            key, val,
            naics, naics_title, "2022" if naics else None, naics_src,
            derive.has_storefront(key, val), derive.is_restaurant(key, val),
            ts, ts,
        )

        existed = conn.execute(
            "SELECT id FROM business WHERE osm_type=? AND osm_id=?",
            (el["type"], el["id"])).fetchone()

        conn.execute("""
            INSERT INTO business (
              osm_type, osm_id, name, lat, lon,
              addr_housenumber, addr_street, addr_city, addr_state, addr_postcode,
              phone, website, opening_hours, brand, cuisine,
              primary_tag_key, primary_tag_val,
              naics, naics_title, naics_vintage, naics_source,
              has_storefront, is_restaurant, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(osm_type, osm_id) DO UPDATE SET
              name=excluded.name, lat=excluded.lat, lon=excluded.lon,
              addr_housenumber=excluded.addr_housenumber,
              addr_street=excluded.addr_street, addr_city=excluded.addr_city,
              addr_state=excluded.addr_state, addr_postcode=excluded.addr_postcode,
              phone=excluded.phone, website=excluded.website,
              opening_hours=excluded.opening_hours, brand=excluded.brand,
              cuisine=excluded.cuisine,
              primary_tag_key=excluded.primary_tag_key,
              primary_tag_val=excluded.primary_tag_val,
              naics=excluded.naics, naics_title=excluded.naics_title,
              naics_vintage=excluded.naics_vintage, naics_source=excluded.naics_source,
              has_storefront=excluded.has_storefront,
              is_restaurant=excluded.is_restaurant,
              last_seen=excluded.last_seen
        """, row)

        bid = conn.execute("SELECT id FROM business WHERE osm_type=? AND osm_id=?",
                           (el["type"], el["id"])).fetchone()["id"]
        conn.execute(
            """INSERT INTO source_record (business_id, ingest_run_id, raw_json, fetched_at)
               VALUES (?,?,?,?)""",
            (bid, run_id, json.dumps(el, sort_keys=True), ts))

        if existed:
            updated += 1
        else:
            inserted += 1

    conn.execute(
        "UPDATE ingest_run SET finished_at=?, endpoint=?, element_count=? WHERE id=?",
        (db.now(), endpoint, len(elements), run_id))
    conn.commit()

    named = inserted + updated
    print(f"\n  inserted {inserted}, updated {updated}, skipped {skipped} (unnamed)")
    print(f"  NAICS assigned to {classified}/{named} "
          f"({100 * classified / named:.0f}%)" if named else "")


if __name__ == "__main__":
    place = sys.argv[1] if len(sys.argv) > 1 else \
        "Concord, Middlesex County, Massachusetts, USA"
    conn = db.connect()
    db.init(conn)
    ingest(conn, place)
