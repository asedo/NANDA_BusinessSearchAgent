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

import config
import db
import derive

UA = {"User-Agent": config.user_agent()}

NOMINATIM = "https://nominatim.openstreetmap.org/search"
# Full-planet mirrors only. Two removals, each a lesson:
# - overpass.osm.jp: TLS certificate fails hostname validation, and working
#   around that would mean disabling certificate verification — never worth it.
# - overpass.osm.ch: the Swiss OSM association's REGIONAL instance. It hosts
#   only Switzerland, so it answered a Massachusetts query with HTTP 200 and
#   zero elements, which got cached as a valid "0 businesses" (Somerville,
#   2026-07-18). Speed probing found it fastest; coverage was never probed.
_DEFAULT_MIRRORS = (
    "https://overpass-api.de/api/interpreter",    # canonical
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
# BSA_OVERPASS_ENDPOINT pins a single endpoint; blank uses the fallback list.
MIRRORS = ((config.get("BSA_OVERPASS_ENDPOINT"),) if config.get("BSA_OVERPASS_ENDPOINT")
           else _DEFAULT_MIRRORS)

OSM_ATTRIBUTION = "© OpenStreetMap contributors"
OSM_LICENSE = "ODbL-1.0"

# ".a out ids;" makes the area itself part of the response. A mirror that
# hosts the region always returns at least that one element, so an empty
# response means "this mirror does not have the region" — distinguishable
# from a town that genuinely has no businesses (area present, nothing else).
QUERY_TMPL = """
[out:json][timeout:180];
area({area})->.a;
.a out ids;
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


class ResolveError(RuntimeError):
    """Nominatim could not resolve the place to an administrative boundary."""


class UnsupportedCountryError(ResolveError):
    """The place resolved outside the United States, which is out of scope
    for now. Subclasses ResolveError so existing handlers surface it."""


class OverpassError(RuntimeError):
    """Every Overpass mirror failed. Transient far more often than not."""


def resolve_area(place: str) -> dict:
    """Nominatim place name -> {area_id, display_name, osm_relation_id, lat, lon}.

    Overpass can only build an area from a *relation* (an administrative
    boundary). A town that resolves to a node or way has no boundary polygon to
    query inside, so we fail with a clear message rather than a confusing
    empty result set.

    US-only for now: Nominatim drops query parts it cannot match, so even a
    query ending in ", USA" can resolve abroad (e.g. "Paris, France, USA" ->
    Paris, France). addressdetails gives us the country code to filter on.
    """
    url = NOMINATIM + "?" + urllib.parse.urlencode(
        {"q": place, "format": "json", "limit": 5, "addressdetails": 1})
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=45) as r:
        hits = json.load(r)
    if not hits:
        raise ResolveError(f"could not resolve place: {place!r}")

    relations = [h for h in hits if h.get("osm_type") == "relation"]
    if not relations:
        kinds = ", ".join(sorted({h.get("osm_type", "?") for h in hits}))
        raise ResolveError(
            f"{place!r} resolved only to {kinds}, not a relation. Overpass needs "
            f"an administrative boundary; try a more specific query such as "
            f"'Town, County, State, USA'.")

    us = [h for h in relations
          if (h.get("address") or {}).get("country_code") == "us"]
    if not us:
        countries = ", ".join(sorted(
            {(h.get("address") or {}).get("country") or "unknown"
             for h in relations}))
        raise UnsupportedCountryError(
            f"{place!r} resolved to a place in {countries}. This agent "
            f"currently supports United States towns only; workflows for "
            f"countries outside the US will be added at a later time.")

    hit = us[0]
    return {
        "area_id": int(hit["osm_id"]) + 3_600_000_000,
        "display_name": hit["display_name"],
        "osm_relation_id": int(hit["osm_id"]),
        "lat": float(hit["lat"]) if hit.get("lat") else None,
        "lon": float(hit["lon"]) if hit.get("lon") else None,
    }


def upsert_place(conn, town: str, state: str, resolved: dict) -> int:
    """Record the resolved place, leaving last_ingested untouched.

    last_ingested is set only by mark_ingested(), after Overpass has actually
    answered. Setting it here would mean a failed fetch leaves behind a place
    that looks freshly ingested with zero businesses - and the agent would then
    serve "0 businesses" as a cached fact rather than reporting the failure.
    """
    conn.execute(
        """INSERT INTO place (query_town, query_state, display_name,
                              osm_relation_id, area_id, lat, lon)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(area_id) DO UPDATE SET
             query_town=excluded.query_town, query_state=excluded.query_state,
             display_name=excluded.display_name""",
        (town, state, resolved["display_name"], resolved["osm_relation_id"],
         resolved["area_id"], resolved["lat"], resolved["lon"]))
    conn.commit()
    return conn.execute("SELECT id FROM place WHERE area_id=?",
                        (resolved["area_id"],)).fetchone()["id"]


def mark_ingested(conn, place_id: int, ts: str) -> None:
    """Called only on a successful fetch. This is what makes the cache valid."""
    conn.execute(
        """UPDATE place SET last_ingested=?,
             first_ingested=COALESCE(first_ingested, ?)
           WHERE id=?""", (ts, ts, place_id))
    conn.commit()


def run_overpass(query: str, quiet: bool = False,
                 expect_area: int | None = None) -> tuple[dict, str]:
    """Try each mirror once, then a second pass with backoff.

    Two passes rather than two consecutive attempts per mirror: a 504 means that
    instance is loaded right now, so the next mirror is a better bet than an
    immediate retry against the same one.

    expect_area is the coverage guard. Regional mirrors answer HTTP 200 with
    zero elements for regions they do not host, which is indistinguishable from
    an empty town. When set, a response lacking that area element is treated as
    a mirror failure and the next mirror is tried.
    """
    errors: list[str] = []
    for pass_no in (1, 2):
        for mirror in MIRRORS:
            host = mirror.split("/")[2]
            try:
                req = urllib.request.Request(
                    mirror, data=urllib.parse.urlencode({"data": query}).encode(),
                    headers=UA)
                with urllib.request.urlopen(req, timeout=240) as r:
                    data = json.load(r)
            except Exception as exc:  # noqa: BLE001 - try the next mirror
                errors.append(f"{host}: {type(exc).__name__}")
                if not quiet:
                    print(f"  {host} (pass {pass_no}): {type(exc).__name__}",
                          file=sys.stderr)
                continue
            if expect_area is not None and not any(
                    el.get("type") == "area" and el.get("id") == expect_area
                    for el in data.get("elements", [])):
                errors.append(f"{host}: answered but does not host area {expect_area}")
                if not quiet:
                    print(f"  {host} (pass {pass_no}): answered but does not "
                          f"host this region - trying next mirror", file=sys.stderr)
                continue
            return data, mirror
        if pass_no == 1:
            time.sleep(5)
    raise OverpassError(
        "all Overpass mirrors failed - this is usually transient load; retry "
        "in a minute.\n  " + "\n  ".join(errors))


def ingest(conn, place: str, town: str | None = None,
           state: str | None = None, quiet: bool = False) -> dict:
    """Ingest one town. Returns a summary dict for programmatic callers."""
    def log(*a):
        if not quiet:
            print(*a)

    source_id = db.upsert_source(conn, "openstreetmap", "https://www.openstreetmap.org",
                                 OSM_LICENSE, OSM_ATTRIBUTION)
    resolved = resolve_area(place)
    area_id, display = resolved["area_id"], resolved["display_name"]
    log(f"resolved: {display}\n  overpass area {area_id}")

    place_id = upsert_place(conn, town or place, state or "", resolved)

    query = QUERY_TMPL.format(area=area_id)
    started = db.now()
    cur = conn.execute(
        """INSERT INTO ingest_run (source_id, place_id, place, area_id, started_at, query)
           VALUES (?,?,?,?,?,?)""",
        (source_id, place_id, display, area_id, started, query))
    run_id = cur.lastrowid
    conn.commit()

    log("querying overpass ...")
    data, endpoint = run_overpass(query, quiet=quiet, expect_area=area_id)
    # Drop the area element the query emits as coverage proof; it is not a
    # business. run_overpass has already verified it was present.
    elements = [el for el in data.get("elements", []) if el.get("type") != "area"]
    log(f"  {len(elements)} elements via {endpoint.split('/')[2]}")

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
            place_id, el["type"], el["id"], name, lat, lon,
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
              place_id, osm_type, osm_id, name, lat, lon,
              addr_housenumber, addr_street, addr_city, addr_state, addr_postcode,
              phone, website, opening_hours, brand, cuisine,
              primary_tag_key, primary_tag_val,
              naics, naics_title, naics_vintage, naics_source,
              has_storefront, is_restaurant, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(osm_type, osm_id) DO UPDATE SET
              place_id=excluded.place_id,
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
    # Only now is the cache valid: Overpass answered and rows are committed.
    mark_ingested(conn, place_id, ts)
    conn.commit()

    named = inserted + updated
    log(f"\n  inserted {inserted}, updated {updated}, skipped {skipped} (unnamed)")
    if named:
        log(f"  NAICS assigned to {classified}/{named} ({100 * classified / named:.0f}%)")

    return {
        "place_id": place_id,
        "display_name": display,
        "area_id": area_id,
        "elements": len(elements),
        "inserted": inserted,
        "updated": updated,
        "skipped_unnamed": skipped,
        "classified": classified,
        "endpoint": endpoint,
        "as_of": ts,
    }


if __name__ == "__main__":
    place = sys.argv[1] if len(sys.argv) > 1 else \
        "Concord, Middlesex County, Massachusetts, USA"
    conn = db.connect()
    db.init(conn)
    try:
        ingest(conn, place)
    except (ResolveError, OverpassError) as exc:
        sys.exit(str(exc))
