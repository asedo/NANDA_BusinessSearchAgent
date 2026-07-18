"""Export a town's businesses to Excel (.xlsx) or CSV.

    python export.py Concord MA
    python export.py Concord MA --naics 722
    python export.py Concord MA --csv
    python export.py Concord MA -o my_file.xlsx

Writes a real .xlsx with no third-party dependency: the format is a zip of XML
parts, which the standard library can produce directly.

Why not just CSV: Excel silently reformats CSV values it thinks are numbers.
Massachusetts postcodes start with a zero, so "01742" is read back as 1742, and
phone numbers and NAICS codes suffer the same way. Writing .xlsx lets each
column declare its type, so identifiers stay strings. CSV remains available via
--csv (with a UTF-8 BOM so Excel reads accents correctly), but it carries that
caveat.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import pathlib
import sys
import zipfile

import agent as agent_mod
import ingest_osm

# (header, key, type) - "s" forces text, preserving leading zeros.
COLUMNS: list[tuple[str, str, str]] = [
    ("Business Name",   "name",              "s"),
    ("Active Web Query Description", "active_web_query_description", "s"),
    ("Description Confidence",       "description_confidence",       "s"),
    ("Description Is Chain Page",    "description_is_chain_page",    "b"),
    ("NAICS",           "naics",             "s"),
    ("NAICS Industry",  "naics_title",       "s"),
    ("NAICS Sector",    "naics_sector",      "s"),
    ("Sector Name",     "naics_sector_name", "s"),
    ("NAICS Source",    "naics_source",      "s"),
    ("OSM Category",    "osm_category",      "s"),
    ("Storefront",      "has_storefront",    "b"),
    ("Restaurant",      "is_restaurant",     "b"),
    ("Address",         "address",           "s"),
    ("City",            "city",              "s"),
    ("Postcode",        "postcode",          "s"),
    ("Latitude",        "lat",               "n"),
    ("Longitude",       "lon",               "n"),
    ("Phone",           "phone",             "s"),
    ("Website",         "website",           "s"),
    ("Opening Hours",   "opening_hours",     "s"),
    ("Cuisine",         "cuisine",           "s"),
    # When OSM was fetched for this town - not the export date. Repeated on
    # every row so a row stays self-describing when copied out of the file.
    ("Data Updated",    "data_updated",      "s"),
]

WIDTHS = [34, 78, 12, 12, 9, 38, 8, 34, 14, 22, 10, 10, 30, 16, 11, 11, 11, 22, 42, 34, 24, 13]


# --------------------------------------------------------------- xlsx writer

def _esc(v: str) -> str:
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _col_letter(idx: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _cell(ref: str, value, kind: str, style: int = 0) -> str:
    if value is None or value == "":
        return ""
    st = f' s="{style}"' if style else ""
    if kind == "n" and isinstance(value, (int, float)):
        return f'<c r="{ref}"{st}><v>{value}</v></c>'
    if kind == "b":
        value = "Yes" if value else "No"
    return f'<c r="{ref}"{st} t="inlineStr"><is><t>{_esc(value)}</t></is></c>'


def _sheet_xml(headers: list[str], rows: list[list], kinds: list[str],
               widths: list[int] | None = None) -> str:
    cols = ""
    if widths:
        cols = "<cols>" + "".join(
            f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>'
            for i, w in enumerate(widths)) + "</cols>"

    body = ['<row r="1">' + "".join(
        _cell(f"{_col_letter(i)}1", h, "s", style=1) for i, h in enumerate(headers)
    ) + "</row>"]

    for r, row in enumerate(rows, start=2):
        cells = "".join(
            _cell(f"{_col_letter(i)}{r}", v, kinds[i]) for i, v in enumerate(row))
        body.append(f'<row r="{r}">{cells}</row>')

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        "</sheetView></sheetViews>"
        f"{cols}<sheetData>{''.join(body)}</sheetData></worksheet>"
    )


def write_xlsx(path: pathlib.Path, sheets: list[tuple[str, list[str], list[list], list[str], list[int] | None]]) -> None:
    """sheets: [(name, headers, rows, kinds, widths)]"""
    n = len(sheets)
    ct = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
          + "".join(
              f'<Override PartName="/xl/worksheets/sheet{i+1}.xml" '
              f'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
              for i in range(n))
          + "</Types>")

    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>")

    wb = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
          + "".join(f'<sheet name="{_esc(s[0])}" sheetId="{i+1}" r:id="rId{i+1}"/>'
                    for i, s in enumerate(sheets))
          + "</sheets></workbook>")

    wb_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
               + "".join(
                   f'<Relationship Id="rId{i+1}" '
                   f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                   f'Target="worksheets/sheet{i+1}.xml"/>' for i in range(n))
               + f'<Relationship Id="rId{n+1}" '
                 f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                 f'Target="styles.xml"/>'
               + "</Relationships>")

    # Two styles: 0 = default, 1 = bold (the header row).
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
              '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
              '<borders count="1"><border/></borders>'
              '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
              '<cellXfs count="2"><xf xfId="0"/><xf xfId="0" fontId="1" applyFont="1"/></cellXfs>'
              "</styleSheet>")

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/styles.xml", styles)
        for i, (_, headers, rows, kinds, widths) in enumerate(sheets):
            z.writestr(f"xl/worksheets/sheet{i+1}.xml",
                       _sheet_xml(headers, rows, kinds, widths))


# ------------------------------------------------------------------- export

def _updated_date(res: dict) -> str:
    """Date (not time) the town's data was last fetched from OSM."""
    return (res["provenance"]["observed"] or "")[:10]


def build_sheets(res: dict):
    kinds = [k for _, _, k in COLUMNS]
    headers = [h for h, _, _ in COLUMNS]
    updated = _updated_date(res)
    rows = [[{**b, "data_updated": updated}.get(key) for _, key, _ in COLUMNS]
            for b in res["businesses"]]

    c, prov, place = res["counts"], res["provenance"], res["place"]
    summary = [
        ["Town", f"{res['query']['town']}, {res['query']['state']}"],
        ["Resolved", place["resolved_name"]],
        ["Businesses", str(c["businesses"])],
        ["With NAICS code", str(c["with_naics"])],
        ["Storefronts", str(c["storefronts"])],
        ["Restaurants", str(c["restaurants"])],
        ["With website", str(c["with_website"])],
        ["", ""],
        ["NAICS sector", "Count"],
        *[[k, str(v)] for k, v in res["naics_sectors"].items()],
        ["", ""],
        ["Data updated (OSM fetch)", prov["observed"]],
        ["Exported", _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")],
    ]

    provenance = [
        ["Source", prov["source"]],
        ["License", prov["license"]],
        ["Attribution", prov["attribution"]],
        ["NAICS crosswalk", prov["naics_crosswalk"]],
        ["NAICS vintage", prov["naics_vintage"]],
        ["Observed", prov["observed"]],
        ["", ""],
        ["Coverage note", res["coverage_note"]],
        ["", ""],
        ["NAICS source", "'osm_crosswalk' means the code was inferred from an "
                         "OpenStreetMap tag, not declared by the business."],
    ]

    return [
        ("Businesses", headers, rows, kinds, WIDTHS),
        ("Summary", ["Field", "Value"], summary, ["s", "s"], [26, 62]),
        ("Provenance", ["Field", "Value"], provenance, ["s", "s"], [20, 96]),
    ]


def write_csv(path: pathlib.Path, res: dict) -> None:
    # utf-8-sig: Excel needs the BOM to read UTF-8 correctly on Windows.
    updated = _updated_date(res)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([h for h, _, _ in COLUMNS])
        for b in res["businesses"]:
            b = {**b, "data_updated": updated}
            row = []
            for _, key, kind in COLUMNS:
                v = b.get(key)
                row.append("" if v is None else
                           ("Yes" if v else "No") if kind == "b" else v)
            w.writerow(row)


def export_town(res: dict, out_dir: pathlib.Path | str = "exports") -> pathlib.Path:
    """Write one xlsx per town into out_dir; stable name, overwritten on
    refresh so the file always mirrors the latest data. Used by agent.py to
    export automatically after every query."""
    q = res["query"]
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{q['town']}_{q['state']}_businesses.xlsx".replace(" ", "_")
    write_xlsx(path, build_sheets(res))
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="Export a town's businesses to Excel or CSV.")
    ap.add_argument("town")
    ap.add_argument("state")
    ap.add_argument("--naics", metavar="PREFIX", help="filter by NAICS prefix")
    ap.add_argument("--csv", action="store_true",
                    help="write CSV instead of xlsx (Excel may mangle postcodes)")
    ap.add_argument("-o", "--output", help="output path")
    args = ap.parse_args()

    try:
        res = agent_mod.lookup(args.town, args.state,
                               naics_prefix=args.naics, quiet=False)
    except (ingest_osm.ResolveError, ingest_osm.OverpassError) as exc:
        sys.exit(f"error: {exc}")

    if not res["businesses"]:
        sys.exit(f"no businesses found for {args.town}, {args.state}"
                 + (f" with NAICS prefix {args.naics}" if args.naics else ""))

    ext = "csv" if args.csv else "xlsx"
    suffix = f"_naics{args.naics}" if args.naics else ""
    default = f"{args.town}_{args.state}{suffix}_businesses.{ext}".replace(" ", "_")
    if args.output:
        path = pathlib.Path(args.output)
    else:
        # Same folder agent.py auto-exports into: one file per town.
        path = pathlib.Path("exports") / default
        path.parent.mkdir(parents=True, exist_ok=True)

    if args.csv:
        write_csv(path, res)
    else:
        write_xlsx(path, build_sheets(res))

    print(f"\nwrote {path}  ({path.stat().st_size:,} bytes)")
    print(f"  {len(res['businesses'])} businesses"
          + (f", NAICS prefix {args.naics}" if args.naics else ""))
    if not args.csv:
        print("  sheets: Businesses, Summary, Provenance")
    else:
        print("  NOTE: Excel may strip leading zeros from postcodes in CSV. "
              "Use xlsx to avoid that.")


if __name__ == "__main__":
    main()
