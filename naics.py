"""Build the OSM-tag -> NAICS crosswalk.

Source: https://wiki.openstreetmap.org/wiki/NAICS/2022 (community-maintained).

The wiki table is many-to-many by its own admission, and it has gaps. We load it
verbatim with confidence='exact', then fill known gaps with MANUAL_OVERRIDES at
confidence='manual'. Every manual code is validated against the NAICS code list
parsed from the same page, so a typo'd code fails loudly instead of landing in
the database.

Run:  python naics.py
"""
from __future__ import annotations

import re
import sys
import urllib.request

import db

VINTAGE = "2022"
WIKI = "https://wiki.openstreetmap.org/w/index.php?title=NAICS/2022&action=raw"
UA = {"User-Agent": "BusinessSearchAgent/0.1 (+https://projectnanda.org)"}

# Gaps and corrections found by inspecting the crosswalk against real Concord data.
# code -> (naics, note). Applied only where the wiki has no entry, unless forced.
MANUAL_OVERRIDES: dict[str, tuple[str, str]] = {
    "shop=clothes":          ("458110", "wiki crosswalk has no entry for shop=clothes"),
    "leisure=sports_centre": ("713940", "wiki crosswalk has no entry; fitness/rec centers"),
}

# Tags where the wiki mapping is present but arguably wrong. We keep the wiki
# value (community consensus) and record the caveat rather than silently diverge.
KNOWN_CAVEATS: dict[str, str] = {
    "amenity=cafe": "wiki maps to 722515 (snack/beverage bars); a sit-down cafe is "
                    "arguably 722511. Review before relying on cafe counts.",
    "shop=hairdresser": "NAICS splits barber shops (812111) from beauty salons "
                        "(812112); OSM does not encode that distinction.",
    "shop=laundry": "shares 8123 with shop=dry_cleaning; granularity is lost.",
}


def _clean(s: str) -> str:
    """Normalise wiki markup: {{tag|amenity|restaurant}} -> amenity=restaurant."""
    s = re.sub(r"\{\{tag\|([^|}]+)\|([^|}]+?)(\|[^}]*)?\}\}", r"\1=\2", s)
    s = re.sub(r"\{\{tag\|([^|}]+)\}\}", r"\1=*", s)
    s = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[\[([^\]]*)\]\]", r"\1", s)
    s = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", s)
    return re.sub(r"<[^>]+>", "", s).strip()


def fetch_wikitext() -> str:
    req = urllib.request.Request(WIKI, headers=UA)
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read().decode("utf-8", "replace")


def parse(wikitext: str) -> tuple[dict[str, str], list[dict]]:
    """Return (naics_code -> title, [rows carrying OSM tags])."""
    titles: dict[str, str] = {}
    tagged: list[dict] = []
    for block in wikitext.split("\n|-"):
        cells = [c.strip() for c in block.split("||")]
        if len(cells) < 3:
            continue
        m = re.search(r"details=(\d{2,6})\s+(\d{2,6})\]", cells[1]) or \
            re.search(r"(\d{2,6})\]", cells[1])
        if not m:
            continue
        code = m.group(len(m.groups()))
        title = _clean(cells[2])
        titles.setdefault(code, title)
        osm = _clean(cells[3]) if len(cells) > 3 else ""
        if osm:
            tagged.append({"naics": code, "title": title, "osm": osm})
    return titles, tagged


def best_for_tag(tag: str, tagged: list[dict]) -> dict | None:
    """Most specific (longest) NAICS code whose OSM expression mentions `tag`."""
    pat = re.compile(rf"(?<![\w:]){re.escape(tag)}(?![\w])")
    cands = [r for r in tagged if pat.search(r["osm"])]
    return max(cands, key=lambda r: len(r["naics"])) if cands else None


def build(conn) -> None:
    print("fetching OSM NAICS/2022 crosswalk ...")
    titles, tagged = parse(fetch_wikitext())
    print(f"  {len(titles)} NAICS codes, {len(tagged)} rows carrying OSM tags")

    # Every distinct osm tag mentioned anywhere in the table.
    seen: set[str] = set()
    for row in tagged:
        seen.update(re.findall(r"[a-z_]+=[A-Za-z0-9_:*-]+", row["osm"]))

    rows, exact = [], 0
    for tag in sorted(seen):
        hit = best_for_tag(tag, tagged)
        if hit:
            exact += 1
            rows.append((tag, VINTAGE, hit["naics"], hit["title"],
                         "exact", KNOWN_CAVEATS.get(tag)))

    have = {r[0] for r in rows}
    manual = 0
    for tag, (code, note) in MANUAL_OVERRIDES.items():
        if tag in have:
            continue
        if code not in titles:
            sys.exit(f"FATAL: manual override {tag} -> {code} is not a valid "
                     f"{VINTAGE} NAICS code")
        manual += 1
        rows.append((tag, VINTAGE, code, titles[code], "manual", note))

    conn.executemany(
        """INSERT INTO osm_naics_crosswalk
             (osm_tag, vintage, naics, naics_title, confidence, note)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(osm_tag, vintage) DO UPDATE SET
             naics=excluded.naics, naics_title=excluded.naics_title,
             confidence=excluded.confidence, note=excluded.note""",
        rows,
    )
    conn.commit()
    print(f"  loaded {len(rows)} mappings ({exact} exact, {manual} manual)")
    print(f"  {sum(1 for r in rows if r[5])} carry a caveat note")


if __name__ == "__main__":
    conn = db.connect()
    db.init(conn)
    build(conn)
