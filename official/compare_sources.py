#!/usr/bin/env python3
"""Stop a refresh that would make the live tile set worse.

  python3 official/compare_sources.py LIVE_CURRENT_JSON NEW_SOURCES_JSON

Fails (exit 1) when a source of the live archive is missing in the new build, has no features, or
lost more than MAX_LOSS of them; prints a Markdown table for the run summary either way. A source
that is new in the build is fine. The live archive then stays as it was.
"""

from __future__ import annotations

import json
import sys

MAX_LOSS = 1 / 3


def check(live: dict, new: dict) -> list[str]:
    problems = []
    for source, before in (live.get("sources") or {}).items():
        old_count = int(before.get("count") or 0)
        now = new.get(source)
        if now is None:
            problems.append(f"{source}: missing in the new build")
            continue
        count = int(now.get("count") or 0)
        if count == 0:
            problems.append(f"{source}: no features")
        elif old_count and count < old_count * (1 - MAX_LOSS):
            problems.append(f"{source}: {count} features, live {old_count}")
    return problems


def main(live_path: str, new_path: str) -> None:
    live = json.load(open(live_path))
    new = json.load(open(new_path))
    print("| source | live | new | updated |\n|---|---|---|---|")
    for source in sorted(set(new) | set(live.get("sources") or {})):
        before = (live.get("sources") or {}).get(source, {})
        now = new.get(source, {})
        print(f"| {source} | {before.get('count', '-')} | {now.get('count', '-')} | {now.get('updated') or '-'} |")
    problems = check(live, new)
    for problem in problems:
        print(f"::error::{problem}", file=sys.stderr)
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main(*sys.argv[1:3])
