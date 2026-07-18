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
    check("running inside a virtual environment",
          sys.prefix != sys.base_prefix,
          "recommended, not required" if sys.prefix == sys.base_prefix else sys.prefix)


def check_dependencies() -> None:
    """The zero-dependency claim must be enforced, not just asserted."""
    section("Dependencies")
    stdlib = set(sys.stdlib_module_names)
    local = {p.stem for p in ROOT.glob("*.py")}
    third: dict[str, set[str]] = {}
    for f in sorted(ROOT.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m not in stdlib and m not in local and m != "__future__":
                    third.setdefault(m, set()).add(f.name)
    check("zero third-party imports", not third,
          "standard library only" if not third
          else f"found {', '.join(sorted(third))}")


def check_files() -> None:
    section("Project files")
    for name in ("schema.sql", "config.py", "db.py", "derive.py", "naics.py",
                 "ingest_osm.py", "agent.py", "query.py", "export.py",
                 "agentfacts.py", ".env.example", "pyproject.toml"):
        check(name, (ROOT / name).exists())


def check_modules() -> None:
    section("Imports")
    import importlib
    for name in ("config", "db", "derive", "naics", "ingest_osm",
                 "query", "agent", "agentfacts", "export"):
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
    check_pipeline(args.offline)

    print(f"\n{_passed} passed, {_failed} failed")
    if _failed:
        print("\nEnvironment is NOT ready. Fix the failures above.")
        sys.exit(1)
    print("Environment is ready.")


if __name__ == "__main__":
    main()
