#!/usr/bin/env python3
"""Resolve Redis release lines into a deterministic, plan-only build matrix."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any


VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SERIES_RE = re.compile(r"^(\d+)\.(\d+)$")
HASH_LINE_RE = re.compile(
    r"^hash redis-(\d+)\.(\d+)\.(\d+)\.tar\.gz "
    r"(sha256) ([0-9a-f]{64}) (\S+)$"
)


class PlanError(RuntimeError):
    """Raised when release metadata cannot be trusted."""


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlanError(f"Unable to read JSON from {path}: {exc}") from exc


def parse_series(value: str) -> tuple[int, int]:
    match = SERIES_RE.fullmatch(value)
    if not match:
        raise PlanError(f"Invalid Redis series: {value}")
    return tuple(int(part) for part in match.groups())


def parse_version(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise PlanError(f"Invalid stable Redis version: {value}")
    return tuple(int(part) for part in match.groups())


def version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def series_text(version: tuple[int, int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def validate_release_config(config: dict[str, Any]) -> None:
    if config.get("schema") != 1:
        raise PlanError("release-lines.json must use schema 1")
    upstream = config.get("upstream")
    policy = config.get("policy")
    entries = config.get("series")
    if not isinstance(upstream, dict) or not isinstance(policy, dict):
        raise PlanError("Release configuration requires upstream and policy objects")
    if not isinstance(entries, list) or not entries:
        raise PlanError("Release configuration requires at least one series")
    if upstream.get("allow_prerelease") is not False:
        raise PlanError("The stable controller must keep allow_prerelease=false")
    if policy.get("controller_mode") != "plan_only":
        raise PlanError("This controller only supports controller_mode=plan_only")

    seen: set[str] = set()
    previous: tuple[int, int] | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            raise PlanError("Every series entry must be an object")
        name = entry.get("series")
        if not isinstance(name, str):
            raise PlanError("Every series entry requires a string series")
        parsed = parse_series(name)
        if name in seen:
            raise PlanError(f"Duplicate Redis series: {name}")
        if previous is not None and parsed <= previous:
            raise PlanError("Redis series must be unique and sorted ascending")
        seen.add(name)
        previous = parsed
        if entry.get("release_type") not in {"standard", "extended"}:
            raise PlanError(f"Invalid release_type for Redis {name}")
        eol = entry.get("eol")
        if eol is not None:
            try:
                dt.date.fromisoformat(eol)
            except (TypeError, ValueError) as exc:
                raise PlanError(f"Invalid EOL date for Redis {name}: {eol}") from exc

    floor = policy.get("new_series_floor")
    if not isinstance(floor, str) or parse_series(floor) not in {
        parse_series(entry["series"]) for entry in entries
    }:
        raise PlanError("new_series_floor must name a configured series")


def validate_platform_config(config: dict[str, Any]) -> None:
    if config.get("schema") != 1:
        raise PlanError("platforms.json must use schema 1")
    prefix = config.get("package_name_prefix")
    platforms = config.get("platforms")
    if not isinstance(prefix, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", prefix):
        raise PlanError("Invalid package_name_prefix")
    if not isinstance(platforms, list) or not platforms:
        raise PlanError("Platform configuration requires at least one platform")

    seen: set[str] = set()
    for platform in platforms:
        required = {
            "id",
            "variant",
            "os",
            "arch",
            "archive_extension",
            "status",
            "controller_enabled",
            "build_workflow",
        }
        if not isinstance(platform, dict) or set(platform) != required:
            raise PlanError(f"Platform entry must contain exactly {sorted(required)}")
        platform_id = platform["id"]
        if not isinstance(platform_id, str) or platform_id in seen:
            raise PlanError(f"Invalid or duplicate platform id: {platform_id}")
        seen.add(platform_id)
        for key in ("variant", "os", "arch"):
            if not isinstance(platform[key], str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9._-]*", platform[key]
            ):
                raise PlanError(f"Invalid {key} for platform {platform_id}")
        if platform["archive_extension"] not in {"tar.gz", "zip"}:
            raise PlanError(f"Invalid archive extension for platform {platform_id}")
        if platform["status"] not in {"implemented", "designed", "experimental"}:
            raise PlanError(f"Invalid status for platform {platform_id}")
        if not isinstance(platform["controller_enabled"], bool):
            raise PlanError(f"controller_enabled must be boolean for {platform_id}")
        if platform["controller_enabled"]:
            if platform["status"] != "implemented" or not platform["build_workflow"]:
                raise PlanError(
                    f"Enabled platform {platform_id} must be implemented and name a workflow"
                )
        elif platform["build_workflow"] is not None:
            raise PlanError(f"Disabled platform {platform_id} must not name a workflow")


def parse_hashes(path: Path) -> dict[tuple[int, int, int], dict[str, str]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PlanError(f"Unable to read Redis hashes from {path}: {exc}") from exc

    releases: dict[tuple[int, int, int], dict[str, str]] = {}
    for line in lines:
        match = HASH_LINE_RE.fullmatch(line.strip())
        if not match:
            continue
        version = tuple(int(part) for part in match.group(1, 2, 3))
        record = {
            "version": version_text(version),
            "series": series_text(version),
            "algorithm": match.group(4),
            "sha256": match.group(5),
            "hashes_source_url": match.group(6),
        }
        existing = releases.get(version)
        if existing is not None and existing != record:
            raise PlanError(f"Conflicting official hash entries for Redis {record['version']}")
        releases[version] = record
    if not releases:
        raise PlanError("No stable Redis SHA-256 entries were found")
    return releases


def index_releases(data: Any) -> dict[str, set[str]]:
    if data is None:
        return {}
    if not isinstance(data, list):
        raise PlanError("GitHub releases inventory must be a JSON array")
    indexed: dict[str, set[str]] = {}
    for release in data:
        if not isinstance(release, dict):
            raise PlanError("Invalid release object in GitHub inventory")
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            raise PlanError("GitHub release is missing tag_name")
        normalized = tag.removeprefix("redis-").removeprefix("v")
        if not VERSION_RE.fullmatch(normalized):
            continue
        assets = release.get("assets", [])
        if not isinstance(assets, list):
            raise PlanError(f"GitHub release {tag} has an invalid assets value")
        names: set[str] = set()
        for asset in assets:
            if isinstance(asset, dict) and isinstance(asset.get("name"), str):
                names.add(asset["name"])
        indexed.setdefault(normalized, set()).update(names)
    return indexed


def expected_assets(
    version: str, prefix: str, platform: dict[str, Any]
) -> tuple[str, str]:
    archive = (
        f"{prefix}-{version}-{platform['variant']}-{platform['arch']}."
        f"{platform['archive_extension']}"
    )
    return archive, f"{archive}.sha256"


def resolve(
    release_config: dict[str, Any],
    platform_config: dict[str, Any],
    hashes: dict[tuple[int, int, int], dict[str, str]],
    github_releases: dict[str, set[str]],
    as_of: dt.date,
    requested_series: set[str] | None = None,
    requested_version: str | None = None,
) -> dict[str, Any]:
    configured = {entry["series"]: entry for entry in release_config["series"]}
    selected = set(configured) if requested_series is None else requested_series
    unknown = sorted(selected - set(configured), key=parse_series)
    if unknown:
        raise PlanError(f"Requested untracked Redis series: {', '.join(unknown)}")

    exact: tuple[int, int, int] | None = None
    if requested_version is not None:
        exact = parse_version(requested_version)
        exact_series = series_text(exact)
        if exact_series not in configured:
            raise PlanError(f"Requested Redis version is not in a tracked series: {requested_version}")
        if exact not in hashes:
            raise PlanError(f"No official SHA-256 entry for Redis {requested_version}")
        selected = {exact_series}

    enabled_platforms = [
        item for item in platform_config["platforms"] if item["controller_enabled"]
    ]
    disabled_platforms = [
        {
            "id": item["id"],
            "variant": item["variant"],
            "arch": item["arch"],
            "status": item["status"],
        }
        for item in platform_config["platforms"]
        if not item["controller_enabled"]
    ]
    prefix = platform_config["package_name_prefix"]
    source_template = release_config["upstream"]["source_url_template"]
    stop_after_eol = release_config["policy"].get("stop_after_eol") is True

    plans: list[dict[str, Any]] = []
    build_rows: list[dict[str, Any]] = []
    version_rows: list[dict[str, Any]] = []

    for name in sorted(selected, key=parse_series):
        entry = configured[name]
        candidates = [
            version for version in hashes if series_text(version) == name
        ]
        if not candidates:
            raise PlanError(f"No stable official SHA-256 releases found for Redis {name}")
        version_tuple = exact if exact is not None else max(candidates)
        assert version_tuple is not None
        record = hashes[version_tuple]
        version = record["version"]
        source_url = source_template.format(version=version)
        if not source_url.startswith("https://"):
            raise PlanError("The configured Redis source URL must use HTTPS")

        eol_text = entry.get("eol")
        eol_date = dt.date.fromisoformat(eol_text) if eol_text else None
        eol = bool(stop_after_eol and eol_date is not None and as_of > eol_date)
        current_assets = github_releases.get(version, set())
        expected: list[str] = []
        missing: list[str] = []
        for platform in enabled_platforms:
            assets = expected_assets(version, prefix, platform)
            expected.extend(assets)
            if not eol:
                missing.extend(asset for asset in assets if asset not in current_assets)

        if eol:
            action = "skip_eol"
        elif not enabled_platforms:
            action = "skip_no_enabled_platforms"
        elif not missing:
            action = "skip_complete"
        elif current_assets:
            action = "plan_complete_release"
        else:
            action = "plan_new_release"

        plan = {
            "series": name,
            "version": version,
            "release_type": entry["release_type"],
            "eol": eol_text,
            "source_url": source_url,
            "source_sha256": record["sha256"],
            "release_exists": bool(current_assets),
            "expected_assets": sorted(expected),
            "missing_assets": sorted(missing),
            "action": action,
        }
        plans.append(plan)

        if not eol and missing:
            version_rows.append(
                {
                    "series": name,
                    "version": version,
                    "source_url": source_url,
                    "source_sha256": record["sha256"],
                    "action": action,
                }
            )
            for platform in enabled_platforms:
                platform_assets = expected_assets(version, prefix, platform)
                platform_missing = [
                    asset for asset in platform_assets if asset not in current_assets
                ]
                if not platform_missing:
                    continue
                build_rows.append(
                    {
                        "series": name,
                        "version": version,
                        "source_url": source_url,
                        "source_sha256": record["sha256"],
                        "platform_id": platform["id"],
                        "variant": platform["variant"],
                        "os": platform["os"],
                        "arch": platform["arch"],
                        "build_workflow": platform["build_workflow"],
                        "missing_assets": platform_missing,
                    }
                )

    floor = parse_series(release_config["policy"]["new_series_floor"])
    configured_series = {parse_series(name) for name in configured}
    candidates_by_series: dict[tuple[int, int], tuple[int, int, int]] = {}
    for version in hashes:
        key = version[:2]
        if key <= floor or key in configured_series:
            continue
        current = candidates_by_series.get(key)
        if current is None or version > current:
            candidates_by_series[key] = version

    new_series = []
    for key in sorted(candidates_by_series):
        latest = candidates_by_series[key]
        record = hashes[latest]
        new_series.append(
            {
                "series": f"{key[0]}.{key[1]}",
                "latest_version": record["version"],
                "source_sha256": record["sha256"],
                "action": release_config["policy"]["new_series"],
            }
        )

    return {
        "schema": 1,
        "controller_mode": "plan_only",
        "as_of": as_of.isoformat(),
        "release_plans": plans,
        "version_matrix": {"include": version_rows},
        "build_matrix": {"include": build_rows},
        "new_series_candidates": new_series,
        "disabled_platforms": disabled_platforms,
        "has_planned_builds": bool(build_rows),
    }


def render_summary(plan: dict[str, Any]) -> str:
    lines = [
        "# Redis release controller plan",
        "",
        "> Plan only: this controller does not dispatch builds or publish releases.",
        "",
        f"As of: `{plan['as_of']}`",
        "",
        "| Series | Version | Release | Missing assets | Action |",
        "| --- | --- | --- | ---: | --- |",
    ]
    for item in plan["release_plans"]:
        exists = "exists" if item["release_exists"] else "missing"
        lines.append(
            f"| {item['series']} | {item['version']} | {exists} | "
            f"{len(item['missing_assets'])} | `{item['action']}` |"
        )
    lines.extend(
        [
            "",
            f"Planned platform jobs: **{len(plan['build_matrix']['include'])}**",
            f"New series candidates: **{len(plan['new_series_candidates'])}**",
            f"Disabled platform rows: **{len(plan['disabled_platforms'])}**",
            "",
        ]
    )
    if plan["new_series_candidates"]:
        lines.extend(["## New series candidates", ""])
        for item in plan["new_series_candidates"]:
            lines.append(
                f"- Redis {item['series']}: {item['latest_version']} "
                f"(`{item['action']}`)"
            )
        lines.append("")
    return "\n".join(lines)


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def write_github_output(path: Path, plan: dict[str, Any]) -> None:
    values = {
        "controller_mode": plan["controller_mode"],
        "has_planned_builds": str(plan["has_planned_builds"]).lower(),
        "version_matrix": json.dumps(plan["version_matrix"], separators=(",", ":")),
        "build_matrix": json.dumps(plan["build_matrix"], separators=(",", ":")),
        "new_series_count": str(len(plan["new_series_candidates"])),
    }
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-config", type=Path, required=True)
    parser.add_argument("--platform-config", type=Path, required=True)
    parser.add_argument("--hashes", type=Path, required=True)
    parser.add_argument("--github-releases", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--as-of", default=dt.date.today().isoformat())
    parser.add_argument("--series", action="append")
    parser.add_argument("--version")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        as_of = dt.date.fromisoformat(args.as_of)
        release_config = load_json(args.release_config)
        platform_config = load_json(args.platform_config)
        validate_release_config(release_config)
        validate_platform_config(platform_config)
        hashes = parse_hashes(args.hashes)
        github_data = load_json(args.github_releases) if args.github_releases else []
        github_releases = index_releases(github_data)
        requested_series = set(args.series) if args.series else None
        plan = resolve(
            release_config,
            platform_config,
            hashes,
            github_releases,
            as_of,
            requested_series=requested_series,
            requested_version=args.version,
        )
        args.output_dir.mkdir(parents=True, exist_ok=True)
        write_json(args.output_dir / "release-plan.json", plan)
        write_json(args.output_dir / "version-matrix.json", plan["version_matrix"])
        write_json(args.output_dir / "build-matrix.json", plan["build_matrix"])
        write_json(
            args.output_dir / "new-series.json", plan["new_series_candidates"]
        )
        summary = render_summary(plan)
        (args.output_dir / "summary.md").write_text(summary, encoding="utf-8")
        if args.github_output:
            write_github_output(args.github_output, plan)
        print(summary)
        return 0
    except (PlanError, ValueError) as exc:
        print(f"release controller error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
