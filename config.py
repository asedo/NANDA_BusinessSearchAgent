"""Configuration and secret loading.

Precedence (highest first):
  1. Real process environment variables
  2. Values in the local .env file
  3. Defaults defined here

Real env vars win so that production, CI, and container deployments work
without a .env file ever existing on disk. The .env file is a local developer
convenience only, and is gitignored.

No dependency on python-dotenv - this project is standard library only.

Self-check (never prints secret values):
    python config.py
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).parent
ENV_PATH = ROOT / ".env"

# Names treated as secret: never printed, only ever reported as set/unset.
SECRET_KEYS = frozenset({
    "ANTHROPIC_API_KEY",
    "NANDA_REGISTRY_TOKEN",
})

DEFAULTS: dict[str, str] = {
    "BSA_DB_PATH": str(ROOT / "businesses.db"),
    "BSA_CONTACT": "",          # optional contact string for the User-Agent
    "BSA_OVERPASS_ENDPOINT": "",  # blank => use the built-in mirror list
    "ANTHROPIC_MODEL": "claude-opus-4-8",
}

_loaded = False


def _parse_env_file(path: pathlib.Path) -> dict[str, str]:
    """Minimal .env parser: KEY=VALUE, # comments, optional quotes/export."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def load() -> None:
    """Populate os.environ from .env WITHOUT overriding real env vars."""
    global _loaded
    if _loaded:
        return
    for key, val in _parse_env_file(ENV_PATH).items():
        os.environ.setdefault(key, val)
    _loaded = True


def get(key: str, default: str | None = None) -> str | None:
    load()
    return os.environ.get(key, DEFAULTS.get(key, default))


def require(key: str) -> str:
    """Fetch a mandatory secret, failing with guidance rather than a traceback."""
    val = get(key)
    if not val:
        raise RuntimeError(
            f"{key} is not set.\n"
            f"  Set it in the environment, or create a {ENV_PATH.name} file "
            f"containing a line like: {key}=your-value\n"
            f"  .env is gitignored and must never be committed."
        )
    return val


def user_agent() -> str:
    """OSM etiquette: identify the client. A contact is optional but courteous."""
    contact = get("BSA_CONTACT") or ""
    suffix = f"; {contact}" if contact else ""
    return f"BusinessSearchAgent/0.1 (+https://projectnanda.org{suffix})"


def _mask(val: str) -> str:
    """Show only enough to confirm which key is loaded. Never the whole value."""
    if len(val) <= 8:
        return "*" * len(val)
    return f"{val[:4]}{'*' * 12}{val[-2:]}"


def selfcheck() -> None:
    load()
    print(f".env file: {'found' if ENV_PATH.exists() else 'not present (fine - none required yet)'}")
    print(f"           {ENV_PATH}\n")

    print("secrets:")
    for key in sorted(SECRET_KEYS):
        raw = os.environ.get(key)
        if raw:
            src = "env" if key not in _parse_env_file(ENV_PATH) else ".env"
            print(f"  {key:24s} SET     ({_mask(raw)}, from {src})")
        else:
            print(f"  {key:24s} unset   (not required yet)")

    print("\nsettings:")
    for key in sorted(DEFAULTS):
        val = get(key) or "(empty)"
        print(f"  {key:24s} {val}")

    print(f"\nuser-agent: {user_agent()}")

    # A .env that is not ignored is the single most dangerous misconfiguration
    # here, so check it explicitly rather than trusting the file exists.
    import subprocess
    if ENV_PATH.exists():
        r = subprocess.run(["git", "check-ignore", "-q", str(ENV_PATH)],
                           cwd=ROOT, capture_output=True)
        print(f"\n.env gitignored: {'YES' if r.returncode == 0 else '*** NO - DO NOT COMMIT ***'}")


if __name__ == "__main__":
    selfcheck()
