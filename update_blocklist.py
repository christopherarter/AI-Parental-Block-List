#!/usr/bin/env python3
"""Build the AI blocklist files published to a public GitHub repo.

Fetches the HPT Full AI blocklist as the baseline, merges in any custom entries
from ``custom-entries.txt`` (git-versioned, additive), then writes two combined
files (each deduped + sorted):

* ``ai-blocklist.txt`` — HOSTS format (``0.0.0.0 <domain>``). For AdGuard Home,
  Pi-hole, dnsmasq, and anything else that loads a remote HOSTS list. Includes
  every entry from the source, even path-style ones like
  ``adobe.com/products/firefly``.
* ``ai-blocklist-domains.txt`` — plain domains, one per line. For the GL-iNet
  Parental Controls app, whose "Detect" parser only accepts bare domain names.
  Path-style and otherwise-malformed entries are dropped here, because a
  domain-list filter can only match whole hostnames and reducing such an entry
  to its parent (``adobe.com``) would over-block a legitimate site.

CI commits both files so a router / DNS resolver can pull them from the public
raw URLs.

The output is deterministic (sorted, no timestamps/comments), so each file only
changes when its domain set actually changes — CI commits nothing on a no-op run.

Pure standard library: no third-party dependencies.

Usage:
    python update_blocklist.py             # fetch, merge custom, write both files
    python update_blocklist.py --dry-run   # fetch + merge only, print summary, write nothing
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

# HOSTS-format output (AdGuard Home, Pi-hole, dnsmasq, ...). Keeps every entry.
OUTPUT_HOSTS_FILENAME = "ai-blocklist.txt"

# Plain-domain output (GL-iNet Parental Controls). Valid bare hostnames only.
OUTPUT_DOMAINS_FILENAME = "ai-blocklist-domains.txt"

# A correctly-fetched baseline has ~1000+ active entries. If far fewer parse,
# treat it as a fetch failure (outage / error page / rate limit) and refuse to
# write, so a bad fetch never overwrites the last-good published files. Checked
# against the REMOTE source only — custom entries can't mask a broken fetch.
MIN_ENTRIES = 100

# Matches an active HOSTS entry: "0.0.0.0 <domain>".
ENTRY_RE = re.compile(r"^0\.0\.0\.0\s+(\S+)")

# Loose IPv4 check used to skip an IP prefix in custom lines like "127.0.0.1 x".
IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")

# A bare, blockable hostname: dot-separated labels of [a-z0-9-], each label not
# starting/ending with a hyphen, at least two labels (must have a TLD). Used to
# keep the plain-domain list clean — path-style entries ("adobe.com/firefly")
# and single-label tokens ("artbreeder") fail this and are dropped from it.
HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$"
)


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


def is_blockable_domain(domain: str) -> bool:
    """True if ``domain`` is a bare hostname a domain-list filter can match.

    The GL-iNet Parental Controls "Detect" parser rejects anything that isn't a
    plain domain, so path-style entries (``adobe.com/products/firefly``) and
    malformed single-label tokens (``artbreeder``) are excluded from the
    plain-domain file. They stay in the HOSTS file untouched.
    """
    return len(domain) <= 253 and bool(HOSTNAME_RE.match(domain))


def format_hosts(domains: list[str]) -> str:
    return "\n".join(f"0.0.0.0 {domain}" for domain in domains) + "\n"


def format_domains(domains: list[str]) -> str:
    return "\n".join(domains) + "\n"


def write_output(path: Path, body: str) -> None:
    """Write atomically so a crash can't leave a half-written file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the AI blocklist files (HPT baseline + custom entries)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch + merge only; print a summary and write nothing",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="directory for the output files (default: next to this script)",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent
    source_url = os.environ.get("SOURCE_URL", DEFAULT_SOURCE_URL)
    output_dir = Path(args.output_dir) if args.output_dir else script_dir
    hosts_path = output_dir / OUTPUT_HOSTS_FILENAME
    domains_path = output_dir / OUTPUT_DOMAINS_FILENAME

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

    combined = sorted(source_domains | custom_domains)
    hosts_body = format_hosts(combined)

    domain_only = [d for d in combined if is_blockable_domain(d)]
    domains_body = format_domains(domain_only)
    dropped = len(combined) - len(domain_only)

    print(f"Combined total: {len(combined)} unique entries.")
    print(
        f"  {OUTPUT_HOSTS_FILENAME}: {len(combined)} entries (HOSTS format)."
    )
    print(
        f"  {OUTPUT_DOMAINS_FILENAME}: {len(domain_only)} entries "
        f"(plain domains; {dropped} non-hostname entries dropped)."
    )

    if args.dry_run:
        print("--- dry run: writing nothing. ---")
        print(f"First 5 of {OUTPUT_HOSTS_FILENAME}:")
        for line in hosts_body.splitlines()[:5]:
            print(f"  {line}")
        print(f"First 5 of {OUTPUT_DOMAINS_FILENAME}:")
        for line in domains_body.splitlines()[:5]:
            print(f"  {line}")
        return 0

    write_output(hosts_path, hosts_body)
    print(f"Wrote {hosts_path}")
    write_output(domains_path, domains_body)
    print(f"Wrote {domains_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
