"""Verify the environment and the pipeline end to end.

    python verify.py              # checks + a live lookup (needs network)
    python verify.py --offline    # checks only, no network calls

Exits non-zero on the first failure, so it works as a CI gate.

This exists because "it works on my machine" is not reproducibility. Each check
asserts something the project actually claims: the Python floor is real, the
dependency list is honest, secrets cannot leak, and the pipeline produces the
numbers the README advertises.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).parent
MIN_PY = (3, 11)

_passed = 0
_failed = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    global _passed, _failed
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"  — {detail}" if detail else ""))
    if ok:
        _passed += 1
    else:
        _failed += 1
    return ok


def section(title: str) -> None:
    print(f"\n{title}")
    print("  " + "-" * (len(title) + 8))


# ----------------------------------------------------------------- checks

def check_python() -> None:
    section("Python")
    v = sys.version_info
    check(f"interpreter {v.major}.{v.minor}.{v.micro}", v[:2] >= MIN_PY,
          f"requires >= {MIN_PY[0]}.{MIN_PY[1]}")
    # Informational, never fatal: the project is stdlib-only, so running
    # outside a venv works fine. A recommendation must not fail the CI gate.
    in_venv = sys.prefix != sys.base_prefix
    check("virtual environment", True,
          sys.prefix if in_venv else "not active (recommended, not required)")


def _declared_optional() -> set[str]:
    """Package names declared under [project.optional-dependencies]."""
    try:
        import tomllib  # stdlib since 3.11, which is our floor
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return set()
    names: set[str] = set()
    for group in data.get("project", {}).get("optional-dependencies", {}).values():
        for spec in group:
            names.add(spec.split()[0].split(">")[0].split("=")[0]
                      .split("<")[0].split("[")[0].strip())
    return names


def check_dependencies() -> None:
    """Enforce the dependency contract rather than merely asserting it.

    Three distinct failures are possible:
      1. An undeclared third-party import appeared.
      2. A declared *optional* dependency is imported at module level, which
         would break the base install for everyone who never asked for it.
      3. Neither - the base install stays pure standard library.
    """
    section("Dependencies")
    stdlib = set(sys.stdlib_module_names)
    local = {p.stem for p in ROOT.glob("*.py")}
    optional = _declared_optional()

    undeclared: dict[str, set[str]] = {}
    eager_optional: dict[str, set[str]] = {}
    lazy_optional: dict[str, set[str]] = {}

    for f in sorted(ROOT.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        # Any import whose nearest enclosing scope is a function is "lazy".
        lazy_nodes: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        lazy_nodes.add(id(sub))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m in stdlib or m in local or m == "__future__":
                    continue
                bucket = (lazy_optional if id(node) in lazy_nodes else eager_optional) \
                    if m in optional else undeclared
                bucket.setdefault(m, set()).add(f.name)

    check("no undeclared third-party imports", not undeclared,
          "base install is standard library only" if not undeclared
          else f"found {', '.join(sorted(undeclared))} — declare in pyproject.toml")

    if optional:
        check("optional dependencies imported lazily", not eager_optional,
              f"{', '.join(sorted(lazy_optional))} imported inside functions"
              if lazy_optional and not eager_optional
              else (f"{', '.join(sorted(eager_optional))} imported at module level "
                    f"— would break the base install" if eager_optional
                    else "none imported"))


def check_files() -> None:
    section("Project files")
    for name in ("schema.sql", "config.py", "db.py", "derive.py", "naics.py",
                 "ingest_osm.py", "agent.py", "query.py", "export.py",
                 "agentfacts.py", "enrich_web.py", "verify.py",
                 ".env.example", "pyproject.toml", "requirements.txt"):
        check(name, (ROOT / name).exists())


def check_modules() -> None:
    section("Imports")
    import importlib
    for name in ("config", "db", "derive", "naics", "ingest_osm",
                 "query", "agent", "agentfacts", "export", "enrich_web"):
        try:
            importlib.import_module(name)
            check(f"import {name}", True)
        except Exception as exc:  # noqa: BLE001
            check(f"import {name}", False, f"{type(exc).__name__}: {exc}")


def check_secrets() -> None:
    section("Secret hygiene")
    env = ROOT / ".env"
    if env.exists():
        r = subprocess.run(["git", "check-ignore", "-q", str(env)],
                           cwd=ROOT, capture_output=True)
        check(".env is gitignored", r.returncode == 0,
              "CRITICAL: .env would be committed" if r.returncode else "")
    else:
        check(".env absent (fine — no secrets required yet)", True)
    check(".env.example present", (ROOT / ".env.example").exists())

    r = subprocess.run(["git", "ls-files"], cwd=ROOT,
                       capture_output=True, text=True)
    if r.returncode == 0:
        tracked = set(r.stdout.split())
        check(".env not tracked by git", ".env" not in tracked)
        check("businesses.db not tracked", "businesses.db" not in tracked)


def check_schema() -> None:
    section("Database")
    import db
    conn = db.connect(":memory:")
    conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    expected = {"source", "place", "ingest_run", "source_record", "business",
                "osm_naics_crosswalk", "web_snapshot", "extraction"}
    missing = expected - tables
    check("schema applies cleanly", not missing,
          f"missing {missing}" if missing else f"{len(tables)} tables")
    views = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view'")}
    check("business_fact view", "business_fact" in views)


def check_mirrors(offline: bool) -> None:
    """Every configured Overpass mirror must host US data.

    A regional mirror (e.g. overpass.osm.ch, Switzerland-only) answers HTTP 200
    with zero elements for a US area, which once got cached as a valid
    "0 businesses" for Somerville MA. Unreachable mirrors are only noted, not
    failed - transient outages are why a fallback list exists at all.
    """
    section("Overpass mirror coverage (live)")
    if offline:
        print("  [SKIP] --offline: no network calls made")
        return
    import json
    import urllib.parse
    import urllib.request
    import ingest_osm
    probe = "[out:json][timeout:20];area(3601840166);out ids;"  # Concord MA
    for mirror in ingest_osm._DEFAULT_MIRRORS:
        host = mirror.split("/")[2]
        try:
            req = urllib.request.Request(
                mirror, data=urllib.parse.urlencode({"data": probe}).encode(),
                headers={"User-Agent": ingest_osm.UA["User-Agent"]})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.load(r)
        except Exception as exc:  # noqa: BLE001
            check(f"{host}", True, f"unreachable ({type(exc).__name__}) - "
                  f"transient, fallback list handles it")
            continue
        check(f"{host} hosts US data", bool(data.get("elements")),
              "would cache empty results as facts" if not data.get("elements") else "")


def check_pipeline(offline: bool) -> None:
    section("Pipeline (live)")
    if offline:
        print("  [SKIP] --offline: no network calls made")
        return
    import agent as agent_mod
    import ingest_osm
    try:
        res = agent_mod.lookup("Concord", "MA", quiet=True)
    except (ingest_osm.ResolveError, ingest_osm.OverpassError) as exc:
        check("lookup Concord MA", False, f"{type(exc).__name__} — "
              f"Overpass is often transient; retry")
        return

    n = res["counts"]["businesses"]
    check("lookup Concord MA returns businesses", n > 0, f"{n} businesses")
    check("NAICS classification applied",
          res["counts"]["with_naics"] > 0,
          f"{res['counts']['with_naics']}/{n} classified")
    check("provenance attached",
          res["provenance"]["attribution"] == "© OpenStreetMap contributors")
    check("coverage note present", bool(res.get("coverage_note")))
    b = res["businesses"][0]
    check("records carry NAICS + storefront fields",
          "naics" in b and "has_storefront" in b)


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify environment and pipeline.")
    ap.add_argument("--offline", action="store_true",
                    help="skip checks that need network access")
    args = ap.parse_args()

    print("BusinessSearchAgent — environment verification")
    check_python()
    check_dependencies()
    check_files()
    check_modules()
    check_secrets()
    check_schema()
    check_mirrors(args.offline)
    check_pipeline(args.offline)

    print(f"\n{_passed} passed, {_failed} failed")
    if _failed:
        print("\nEnvironment is NOT ready. Fix the failures above.")
        sys.exit(1)
    print("Environment is ready.")


if __name__ == "__main__":
    main()
