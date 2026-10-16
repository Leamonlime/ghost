#!/usr/bin/env python3
"""Generate CHANGELOG.md from the vault "Ghost — Build Log" session headers.

Headers look like:  ## Session 94 — 11/10/2026 — <title>
Only the session number, date, and title are published (never the body, which
is internal/operational). Any title containing a publish-denylist term is
withheld rather than leaked, so a stray identifier can never block publishing.

Paths default to $HOME-relative so nothing host-specific is hard-coded.
Usage: gen_changelog.py [--build-log PATH] [--out PATH] [--denylist PATH]
"""
import argparse
import os
import re
import sys

HEADER_RE = re.compile(r"^#{2,}\s*Session\s+(\d+)\s*—\s*(.+?)\s*—\s*(.+?)\s*$")


def load_denylist(path):
    terms = []
    if not path or not os.path.exists(path):
        return terms
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    return terms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-log",
                    default=os.environ.get("GHOST_BUILD_LOG")
                    or os.path.expanduser(
                        "~/CentralVault/Projects/Tech/Ghost/Ghost — Build Log.md"))
    ap.add_argument("--out",
                    default=os.path.join(os.path.dirname(os.path.dirname(
                        os.path.abspath(__file__))), "CHANGELOG.md"))
    ap.add_argument("--denylist",
                    default=os.environ.get("GHOST_PUBLISH_DENYLIST")
                    or os.path.expanduser("~/.ghost-runner/publish-denylist.txt"))
    args = ap.parse_args()

    if not os.path.exists(args.build_log):
        sys.stderr.write(f"gen_changelog: build log not found: {args.build_log}\n")
        return 1

    deny = [re.compile(t, re.IGNORECASE) for t in load_denylist(args.denylist)]

    entries = []
    with open(args.build_log, encoding="utf-8") as fh:
        for line in fh:
            m = HEADER_RE.match(line.rstrip("\n"))
            if not m:
                continue
            num, date, title = int(m.group(1)), m.group(2).strip(), m.group(3).strip()
            if any(rx.search(title) for rx in deny):
                title = "(title withheld)"
            entries.append((num, date, title))

    entries.sort(key=lambda e: e[0], reverse=True)

    lines = [
        "# Changelog",
        "",
        "Auto-generated from the project build log; titles only. "
        "Newest session first.",
        "",
        "| Session | Date | Summary |",
        "| ------: | :--- | :------ |",
    ]
    for num, date, title in entries:
        title = title.replace("|", "\\|")
        lines.append(f"| {num} | {date} | {title} |")
    lines.append("")

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    sys.stderr.write(f"gen_changelog: wrote {len(entries)} entries to {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
