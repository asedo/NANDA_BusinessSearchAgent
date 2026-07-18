"""Stage 3 — read each business's website and generate a one-sentence description.

    python enrich_web.py Concord MA                 # enrich that town
    python enrich_web.py Concord MA --limit 5       # try a handful first
    python enrich_web.py Concord MA --dry-run       # fetch + extract text, no API calls
    python enrich_web.py Concord MA --refresh       # regenerate existing descriptions
    python enrich_web.py --all                      # every town in the database

Result is stored as `active_web_query_description` and surfaced on the
`business_fact` view, in `agent.py` output, and in spreadsheet exports.

This is the one place an LLM earns its keep: turning unstructured HTML into a
structured field is a judgement task. Everything else in this project is
deterministic and stays that way.

Being a polite crawler is not optional:
  - robots.txt is fetched and honoured per-host
  - one request per second per run, single-threaded
  - identifying User-Agent, with BSA_CONTACT appended when set
  - response body capped, redirects bounded, timeouts everywhere

Snapshots are stored separately from extractions, so a better prompt can be
re-run over saved HTML without re-crawling anybody's site.
"""
from __future__ import annotations

import argparse
import hashlib
import html.parser
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser

import config
import db

SCHEMA_VERSION = "awqd-1"
MAX_BYTES = 600_000        # generous for a homepage, bounded against surprises
MAX_TEXT_CHARS = 12_000    # what we hand the model
REQUEST_DELAY = 1.0        # seconds between requests, per politeness
FETCH_TIMEOUT = 25

SYSTEM_PROMPT = """You write factual one-sentence descriptions of local businesses \
based on text scraped from their own website.

Rules:
- Exactly one sentence. No preamble, no trailing commentary.
- Describe what the business actually does and who it serves. Prefer concrete \
detail (what they sell, what service they provide, notable specialities) over \
adjectives.
- Use neutral, factual language. Do not copy marketing slogans, and do not \
invent facts that are not supported by the page text.
- If the page is a corporate store-locator or franchise landing page for a \
chain rather than an independent business's own site, describe the chain and \
set is_chain_page to true.
- If the page text is too sparse, broken, or off-topic to support a real \
description, say so plainly in the description and set confidence to "low"."""


# ------------------------------------------------------------------ HTML

class _TextExtractor(html.parser.HTMLParser):
    """Minimal HTML -> text. Avoids a BeautifulSoup dependency."""

    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self.title = ""
        self._in_title = False
        self.meta_description = ""

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            a = dict(attrs)
            if a.get("name", "").lower() in ("description", "og:description") or \
               a.get("property", "").lower() == "og:description":
                if a.get("content") and not self.meta_description:
                    self.meta_description = a["content"].strip()

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        # <title> sits inside <head>, which is skipped wholesale, so the title
        # check has to come BEFORE the skip check or it never fires.
        if self._in_title:
            self.title += text
            return
        if self._skip_depth:
            return
        self.parts.append(text)

    def text(self) -> str:
        seen, out = set(), []
        for p in self.parts:
            if p not in seen:          # pages repeat nav labels endlessly
                seen.add(p)
                out.append(p)
        return " ".join(out)


def html_to_text(raw: str) -> tuple[str, str, str]:
    """-> (title, meta_description, body_text)"""
    p = _TextExtractor()
    try:
        p.feed(raw)
    except Exception:  # noqa: BLE001 - malformed markup is common; keep what we got
        pass
    return p.title.strip(), p.meta_description.strip(), p.text()


# ----------------------------------------------------------------- fetch

_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def robots_allows(url: str, ua: str) -> tuple[bool, str]:
    """Honour robots.txt. Unreachable robots.txt is treated as permitted,
    which is the conventional reading, but a 5xx is treated as a refusal."""
    parts = urllib.parse.urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(f"{origin}/robots.txt")
        try:
            rp.read()
            _robots_cache[origin] = rp
        except urllib.error.HTTPError as exc:
            _robots_cache[origin] = None if exc.code < 500 else False  # type: ignore[assignment]
        except Exception:  # noqa: BLE001
            _robots_cache[origin] = None
    rp = _robots_cache[origin]
    if rp is False:
        return False, "robots.txt returned a server error"
    if rp is None:
        return True, "no robots.txt"
    return (rp.can_fetch(ua, url), "robots.txt")


def fetch(url: str, ua: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
        ctype = r.headers.get("Content-Type", "")
        if "html" not in ctype and "text" not in ctype:
            raise ValueError(f"not HTML (Content-Type: {ctype or 'unknown'})")
        raw = r.read(MAX_BYTES)
        charset = r.headers.get_content_charset() or "utf-8"
        return r.status, raw.decode(charset, "replace")


# ------------------------------------------------------------------- LLM

DESCRIPTION_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {
            "type": "string",
            "description": "Exactly one factual sentence about the business.",
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "How well the page text supported the description.",
        },
        "is_chain_page": {
            "type": "boolean",
            "description": "True if this is a corporate store-locator or "
                           "franchise page rather than an independent site.",
        },
    },
    "required": ["description", "confidence", "is_chain_page"],
    "additionalProperties": False,
}


def describe(client, model: str, business: dict, page: dict) -> dict:
    content = (
        f"Business name (from OpenStreetMap): {business['name']}\n"
        f"Category: {business.get('osm_category') or 'unknown'}\n"
        f"NAICS industry: {business.get('naics_title') or 'unclassified'}\n"
        f"Town: {business.get('town') or 'unknown'}\n"
        f"URL: {business['website']}\n\n"
        f"Page title: {page['title'] or '(none)'}\n"
        f"Meta description: {page['meta'] or '(none)'}\n\n"
        f"Page text:\n{page['text'][:MAX_TEXT_CHARS]}"
    )
    resp = client.messages.create(
        model=model,
        max_tokens=400,
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": DESCRIPTION_SCHEMA}},
        messages=[{"role": "user", "content": content}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)


# ------------------------------------------------------------------ main

def targets(conn, town, state, do_all, refresh, limit):
    sql = """
        SELECT b.id, b.name, b.website, b.naics_title,
               b.primary_tag_key || '=' || b.primary_tag_val AS osm_category,
               p.query_town || ', ' || p.query_state AS town
        FROM business b LEFT JOIN place p ON p.id = b.place_id
        WHERE b.website IS NOT NULL AND TRIM(b.website) <> ''
    """
    params: list = []
    if not do_all:
        sql += " AND LOWER(p.query_town)=LOWER(?) AND LOWER(p.query_state)=LOWER(?)"
        params += [town, state]
    if not refresh:
        sql += """ AND NOT EXISTS (SELECT 1 FROM extraction e
                    WHERE e.business_id = b.id
                      AND e.active_web_query_description IS NOT NULL)"""
    sql += " ORDER BY b.name"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, params).fetchall()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate one-sentence website descriptions for businesses.")
    ap.add_argument("town", nargs="?")
    ap.add_argument("state", nargs="?")
    ap.add_argument("--all", action="store_true", help="every town in the database")
    ap.add_argument("--limit", type=int, help="stop after N businesses")
    ap.add_argument("--refresh", action="store_true",
                    help="regenerate descriptions that already exist")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and extract text but make no API calls")
    args = ap.parse_args()

    if not args.all and not (args.town and args.state):
        ap.error("give a town and state, or --all")

    conn = db.connect()
    db.init(conn)

    rows = targets(conn, args.town, args.state, args.all, args.refresh, args.limit)
    if not rows:
        print("nothing to do — every matching business already has a description "
              "(use --refresh to regenerate)")
        return

    model = config.get("ANTHROPIC_MODEL") or "claude-opus-4-8"
    ua = config.user_agent()

    client = None
    if not args.dry_run:
        try:
            import anthropic
        except ImportError:
            sys.exit("the anthropic SDK is not installed.\n"
                     '  pip install -e ".[extract]"   (or: pip install anthropic)')
        try:
            config.require("ANTHROPIC_API_KEY")
        except RuntimeError as exc:
            sys.exit(f"{exc}\n\n  Or preview without any API calls: --dry-run")
        client = anthropic.Anthropic(api_key=config.require("ANTHROPIC_API_KEY"))

    print(f"{len(rows)} business(es) to process"
          f"{' (dry run — no API calls)' if args.dry_run else f', model {model}'}\n")

    ok = blocked = failed = 0
    for i, b in enumerate(rows, 1):
        name, url = b["name"], b["website"].strip()
        print(f"[{i}/{len(rows)}] {name[:40]:42s} ", end="", flush=True)

        allowed, why = robots_allows(url, ua)
        if not allowed:
            print(f"SKIP — disallowed by {why}")
            blocked += 1
            continue

        try:
            status, raw = fetch(url, ua)
        except Exception as exc:  # noqa: BLE001 - one bad site must not stop the run
            print(f"FAIL — {type(exc).__name__}: {str(exc)[:44]}")
            failed += 1
            time.sleep(REQUEST_DELAY)
            continue

        title, meta, text = html_to_text(raw)
        # Judge on everything we would actually send the model, not body text
        # alone: a JS-rendered page can have an empty body but a usable title
        # and meta description.
        usable = len(text) + len(meta) + len(title)
        if usable < 80:
            print(f"FAIL — only {usable} chars of usable text "
                  f"(likely JavaScript-rendered)")
            failed += 1
            time.sleep(REQUEST_DELAY)
            continue

        ts = db.now()
        cur = conn.execute(
            """INSERT INTO web_snapshot (business_id, url, fetched_at, http_status,
                                         content, content_sha)
               VALUES (?,?,?,?,?,?)""",
            (b["id"], url, ts, status, raw,
             hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()))
        snap_id = cur.lastrowid
        conn.commit()

        if args.dry_run:
            print(f"OK   — text {len(text):6,d}  meta {len(meta):4d}  title={title[:34]!r}")
            ok += 1
            time.sleep(REQUEST_DELAY)
            continue

        try:
            result = describe(client, model, dict(b),
                              {"title": title, "meta": meta, "text": text})
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL — model call: {type(exc).__name__}: {str(exc)[:40]}")
            failed += 1
            time.sleep(REQUEST_DELAY)
            continue

        conn.execute(
            """INSERT INTO extraction (web_snapshot_id, business_id, extracted_at,
                   model, schema_version, active_web_query_description,
                   confidence, is_chain_page, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (snap_id, b["id"], db.now(), model, SCHEMA_VERSION,
             result["description"], result["confidence"],
             int(result["is_chain_page"]), json.dumps(result)))
        conn.commit()

        flag = " [chain]" if result["is_chain_page"] else ""
        print(f"OK   — {result['confidence']:6s}{flag}")
        print(f"          {result['description'][:96]}")
        ok += 1
        time.sleep(REQUEST_DELAY)

    print(f"\n  described {ok}, blocked by robots {blocked}, failed {failed}")
    if args.dry_run:
        print("  dry run — HTML snapshots saved, no descriptions generated")


if __name__ == "__main__":
    main()
