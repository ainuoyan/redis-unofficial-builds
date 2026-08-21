#!/usr/bin/env python3
"""Fail closed unless an exact Redis version is eligible for publication."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import resolve_versions


class PolicyError(RuntimeError):
    """Raised when a requested release is outside the reviewed policy."""


def validate_publish_policy(
    config: dict[str, object], version: str, as_of: dt.date
) -> dict[str, object]:
    resolve_versions.validate_release_config(config)
    if type(as_of) is not dt.date:
        raise PolicyError("as_of must be a date")
    parsed = resolve_versions.parse_version(version)
    series = resolve_versions.series_text(parsed)
    entries = [entry for entry in config["series"] if entry["series"] == series]
    if len(entries) != 1:
        raise PolicyError(f"Redis {version} is not in exactly one tracked series")

    entry = entries[0]
    eol_text = entry["eol"]
    if config["policy"]["stop_after_eol"] and eol_text is not None:
        eol = dt.date.fromisoformat(eol_text)
        if as_of > eol:
            raise PolicyError(
                f"Redis {series} reached EOL on {eol.isoformat()} and may not be published"
            )
    return entry


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-config", type=Path, required=True)
    parser.add_argument("--redis-version", required=True)
    parser.add_argument("--as-of", default=dt.date.today().isoformat())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        config = resolve_versions.load_json(args.release_config)
        as_of = dt.date.fromisoformat(args.as_of)
        if args.as_of != as_of.isoformat():
            raise PolicyError(f"Noncanonical as-of date: {args.as_of}")
        entry = validate_publish_policy(config, args.redis_version, as_of)
        print(
            json.dumps(
                {
                    "version": args.redis_version,
                    "series": entry["series"],
                    "release_type": entry["release_type"],
                    "eol": entry["eol"],
                    "as_of": as_of.isoformat(),
                },
                separators=(",", ":"),
            )
        )
        return 0
    except (OSError, ValueError, resolve_versions.PlanError, PolicyError) as exc:
        print(f"release publication policy error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
