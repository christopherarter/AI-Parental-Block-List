#!/usr/bin/env python3
"""Build the AI blocklist file published to a public GitHub repo.

Fetches the HPT Full AI blocklist as the baseline, merges in any custom entries
from ``custom-entries.txt`` (git-versioned, additive), strips everything down to
``0.0.0.0 <domain>`` lines (deduped + sorted), and writes the single combined
file to ``ai-blocklist.txt``. CI commits that file to the repo so a router / DNS
resolver can pull it from the public raw URL.

The output is deterministic (sorted, no timestamps/comments), so it only changes
when the domain set actually changes — CI commits nothing on a no-op run.

Pure standard library: no third-party dependencies.

Usage:
    python update_blocklist.py            # fetch, merge custom, write ai-blocklist.txt
    python update_blocklist.py --dry-run  # fetch + merge only, print summary, write nothing
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.request
from pathlib import Path

DEFAULT_SOURCE_URL = (
    "https://codeberg.org/lumiworx/HPT-AI-Blocklist/raw/branch/main/HPT-Full-AI-List"
)

# Git-versioned file of your own additive entries, merged on top of the remote
# baseline. One domain per line (bare or "0.0.0.0 domain"); # comments allowed.
CUSTOM_FILENAME = "custom-entries.txt"

# The published file, committed to the repo and served via raw.githubusercontent.com.
OUTPUT_FILENAME = "ai-blocklist.txt"

# A correctly-fetched baseline has ~1000+ active entries. If far fewer parse,
# treat it as a fetch failure (outage / error page / rate limit) and refuse to
# write, so a bad fetch never overwrites the last-good published file. Checked
# against the REMOTE source only — custom entries can't mask a broken fetch.
MIN_ENTRIES = 100

# Matches an active HOSTS entry: "0.0.0.0 <domain>".
ENTRY_RE = re.compile(r"^0\.0\.0\.0\s+(\S+)")

# Loose IPv4 check used to skip an IP prefix in custom lines like "127.0.0.1 x".
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def fetch_source(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "ai-block-list/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (trusted URL)
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def parse_source_domains(raw: str) -> set[str]:
    """Domains from the remote baseline: only active ``0.0.0.0 <domain>`` lines.

    Drops comments, blank lines, and anything that isn't an active entry.
    Commented-out / "listed but not blocked" domains start with ``#`` and are
    excluded.
    """
    domains: set[str] = set()
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ENTRY_RE.match(stripped)
        if match:
            domains.add(match.group(1).lower())
    return domains


def parse_custom_domains(text: str) -> set[str]:
    """Domains from the custom file, parsed leniently.

    Each non-comment line may be a bare domain (``example.com``) or a HOSTS line
    (``0.0.0.0 example.com`` / ``127.0.0.1 example.com``). Blank lines and lines
    starting with ``#`` are ignored.
    """
    domains: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        if len(tokens) > 1 and IPV4_RE.match(tokens[0]):
            domain = tokens[1]
        else:
            domain = tokens[0]
        domains.add(domain.lower())
    return domains


def load_custom_domains(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return parse_custom_domains(path.read_text(encoding="utf-8"))


def format_entries(domains: set[str]) -> str:
    lines = [f"0.0.0.0 {domain}" for domain in sorted(domains)]
    return "\n".join(lines) + "\n"


def write_output(path: Path, body: str) -> None:
    """Write atomically so a crash can't leave a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the AI blocklist (HPT baseline + custom entries)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch + merge only; print a summary and write nothing",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=f"output path (default: {OUTPUT_FILENAME} next to this script)",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    source_url = os.environ.get("SOURCE_URL", DEFAULT_SOURCE_URL)
    output_path = Path(args.output) if args.output else script_dir / OUTPUT_FILENAME

    print(f"Fetching baseline: {source_url}")
    try:
        raw = fetch_source(source_url)
    except Exception as exc:  # noqa: BLE001 - clean message for any fetch failure
        print(f"Failed to fetch source: {exc}", file=sys.stderr)
        return 1

    source_domains = parse_source_domains(raw)
    print(f"Baseline: {len(source_domains)} unique remote entries.")

    if len(source_domains) < MIN_ENTRIES:
        print(
            f"Refusing to write: only {len(source_domains)} baseline entries "
            f"(< {MIN_ENTRIES}). Source may be down or returned an error page.",
            file=sys.stderr,
        )
        return 1

    custom_path = Path(
        os.environ.get("CUSTOM_ENTRIES_FILE", script_dir / CUSTOM_FILENAME)
    )
    custom_domains = load_custom_domains(custom_path)
    added = custom_domains - source_domains
    print(
        f"Custom file: {len(custom_domains)} entries from {custom_path.name} "
        f"({len(added)} not already in the baseline)."
    )

    combined = source_domains | custom_domains
    body = format_entries(combined)
    print(f"Combined total: {len(combined)} unique entries.")

    if args.dry_run:
        print("--- dry run: writing nothing. First 10 lines: ---")
        for line in body.splitlines()[:10]:
            print(line)
        print(f"... ({len(combined)} total)")
        return 0

    write_output(output_path, body)
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
