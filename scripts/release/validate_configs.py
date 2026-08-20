#!/usr/bin/env python3
"""Validate the repository's real release-controller configuration files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import resolve_versions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-config", type=Path, required=True)
    parser.add_argument("--platform-config", type=Path, required=True)
    parser.add_argument("--workflows-dir", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        release_config = resolve_versions.load_json(args.release_config)
        platform_config = resolve_versions.load_json(args.platform_config)
        resolve_versions.validate_release_config(release_config)
        resolve_versions.validate_platform_config(platform_config, args.workflows_dir)
        resolve_versions.validate_repository_platform_matrix(platform_config)
        print("Validated release controller configuration")
        return 0
    except resolve_versions.PlanError as exc:
        print(f"configuration validation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
