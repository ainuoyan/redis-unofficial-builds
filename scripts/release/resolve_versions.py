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


VERSION_RE = re.compile(r"^([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})$")
SERIES_RE = re.compile(r"^([0-9]{1,6})\.([0-9]{1,6})$")
STABLE_HASH_LINE_RE = re.compile(
    r"^hash redis-([0-9]{1,6})\.([0-9]{1,6})\.([0-9]{1,6})\.tar\.gz "
    r"(sha1|sha256) ([0-9a-f]+) (\S+)$"
)
STABLE_HASH_PREFIX_RE = re.compile(
    r"^hash\s+redis-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz(?:\s|$)"
)
WORKFLOW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml$")
GIT_OID_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
SUPPORTED_BACKEND_CONTRACTS = {
    "build-linux.yml": {
        ("linux-glibc2.28", "linux", "x64", "tar.gz"),
        ("linux-glibc2.28", "linux", "arm64", "tar.gz"),
    }
}
EXPECTED_PACKAGE_NAME_PREFIX = "Redis-Rzon"
REQUIRED_ENABLED_PLATFORM_IDS = {
    "linux-glibc2.28-x64",
    "linux-glibc2.28-arm64",
}
REQUIRED_DESIGN_PLATFORM_CONTRACTS = {
    "linux-glibc2.17-legacy-x64": (
        "linux-glibc2.17-legacy", "linux", "x64", "tar.gz", "designed"
    ),
    "linux-glibc2.17-legacy-arm64": (
        "linux-glibc2.17-legacy", "linux", "arm64", "tar.gz", "designed"
    ),
    "linux-musl1.2-x64": (
        "linux-musl1.2", "linux", "x64", "tar.gz", "designed"
    ),
    "linux-musl1.2-arm64": (
        "linux-musl1.2", "linux", "arm64", "tar.gz", "designed"
    ),
    "macos12-x64": ("macos12", "macos", "x64", "tar.gz", "designed"),
    "macos12-arm64": ("macos12", "macos", "arm64", "tar.gz", "designed"),
    "windows-msys2-x64": (
        "windows-msys2", "windows", "x64", "zip", "designed"
    ),
    "windows-cygwin-x64": (
        "windows-cygwin", "windows", "x64", "zip", "designed"
    ),
}
MAX_INPUT_BYTES = 16 * 1024 * 1024

EXPECTED_UPSTREAM = {
    "project": "redis/redis",
    "hashes_repository": "redis/redis-hashes",
    "hashes_ref": "master",
    "hashes_path": "README",
    "hashes_url": "https://raw.githubusercontent.com/redis/redis-hashes/master/README",
    "source_url_template": "https://download.redis.io/releases/redis-{version}.tar.gz",
    "allow_prerelease": False,
}

EXPECTED_POLICY = {
    "patch_updates": "auto_release",
    "new_series": "candidate_then_pull_request",
    "stop_after_eol": True,
    "retain_existing_releases": True,
    "controller_mode": "plan_only",
}


class PlanError(RuntimeError):
    """Raised when release metadata cannot be trusted."""


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PlanError(f"Duplicate JSON key: {key}")
        value[key] = item
    return value


def _reject_nonfinite_json(value: str) -> None:
    raise PlanError(f"Non-finite JSON number is not allowed: {value}")


def load_json(path: Path) -> Any:
    try:
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise PlanError(f"JSON input is larger than {MAX_INPUT_BYTES} bytes: {path}")
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise PlanError(f"Unable to read JSON from {path}: {exc}") from exc


def parse_series(value: str) -> tuple[int, int]:
    if not isinstance(value, str):
        raise PlanError("Redis series must be a string")
    match = SERIES_RE.fullmatch(value)
    if not match:
        raise PlanError(f"Invalid Redis series: {value}")
    parsed = tuple(int(part) for part in match.groups())
    if value != f"{parsed[0]}.{parsed[1]}":
        raise PlanError(f"Noncanonical Redis series: {value}")
    return parsed


def parse_version(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise PlanError("Redis version must be a string")
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise PlanError(f"Invalid stable Redis version: {value}")
    parsed = tuple(int(part) for part in match.groups())
    if value != version_text(parsed):
        raise PlanError(f"Noncanonical stable Redis version: {value}")
    return parsed


def version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def series_text(version: tuple[int, int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def parse_git_oid(value: str) -> str:
    if not isinstance(value, str) or not GIT_OID_RE.fullmatch(value):
        raise PlanError("Redis hashes snapshot must be a lowercase 40-character Git OID")
    return value


def validate_release_config(config: Any) -> None:
    if not isinstance(config, dict):
        raise PlanError("release-lines.json must contain a JSON object")
    if type(config.get("schema")) is not int or config.get("schema") != 1:
        raise PlanError("release-lines.json must use schema 1")
    if set(config) != {"schema", "upstream", "policy", "series"}:
        raise PlanError("release-lines.json contains unknown or missing top-level keys")
    upstream = config.get("upstream")
    policy = config.get("policy")
    entries = config.get("series")
    if not isinstance(upstream, dict) or not isinstance(policy, dict):
        raise PlanError("Release configuration requires upstream and policy objects")
    if not isinstance(entries, list) or not entries:
        raise PlanError("Release configuration requires at least one series")
    if set(upstream) != set(EXPECTED_UPSTREAM):
        raise PlanError("Release configuration contains unknown or missing upstream keys")
    if set(policy) != set(EXPECTED_POLICY) | {"new_series_floor"}:
        raise PlanError("Release configuration contains unknown or missing policy keys")
    for key, expected in EXPECTED_UPSTREAM.items():
        actual = upstream.get(key)
        if type(actual) is not type(expected) or actual != expected:
            raise PlanError(
                f"Invalid upstream.{key}; expected the reviewed official value"
            )
    for key, expected in EXPECTED_POLICY.items():
        actual = policy.get(key)
        if type(actual) is not type(expected) or actual != expected:
            raise PlanError(f"Invalid policy.{key}; expected {expected!r}")

    seen: set[str] = set()
    previous: tuple[int, int] | None = None
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "series", "release_type", "eol"
        }:
            raise PlanError(
                "Every series entry must contain exactly series, release_type, and eol"
            )
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
        release_type = entry.get("release_type")
        if not isinstance(release_type, str) or release_type not in {
            "standard",
            "extended",
        }:
            raise PlanError(f"Invalid release_type for Redis {name}")
        eol = entry.get("eol")
        if eol is not None:
            try:
                parsed_eol = dt.date.fromisoformat(eol)
            except (TypeError, ValueError) as exc:
                raise PlanError(f"Invalid EOL date for Redis {name}: {eol}") from exc
            if eol != parsed_eol.isoformat():
                raise PlanError(f"Noncanonical EOL date for Redis {name}: {eol}")

    floor = policy.get("new_series_floor")
    if not isinstance(floor, str) or parse_series(floor) != previous:
        raise PlanError("new_series_floor must name the highest configured series")


def validate_platform_config(config: Any, workflows_dir: Path | None = None) -> None:
    if not isinstance(config, dict):
        raise PlanError("platforms.json must contain a JSON object")
    if type(config.get("schema")) is not int or config.get("schema") != 1:
        raise PlanError("platforms.json must use schema 1")
    if set(config) != {"schema", "package_name_prefix", "platforms"}:
        raise PlanError("platforms.json contains unknown or missing top-level keys")
    prefix = config.get("package_name_prefix")
    platforms = config.get("platforms")
    if prefix != EXPECTED_PACKAGE_NAME_PREFIX:
        raise PlanError(
            "package_name_prefix does not match the implemented backend contract"
        )
    if not isinstance(platforms, list) or not platforms:
        raise PlanError("Platform configuration requires at least one platform")

    if workflows_dir is not None and (
        not workflows_dir.is_dir() or workflows_dir.is_symlink()
    ):
        raise PlanError(f"Invalid workflows directory: {workflows_dir}")

    seen: set[str] = set()
    enabled_ids: set[str] = set()
    asset_keys: set[tuple[str, str, str]] = set()
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
        if (
            not isinstance(platform_id, str)
            or not SAFE_NAME_RE.fullmatch(platform_id)
            or platform_id in seen
        ):
            raise PlanError(f"Invalid or duplicate platform id: {platform_id}")
        seen.add(platform_id)
        for key in ("variant", "os", "arch"):
            if not isinstance(platform[key], str) or not SAFE_NAME_RE.fullmatch(
                platform[key]
            ):
                raise PlanError(f"Invalid {key} for platform {platform_id}")
        if platform_id != f"{platform['variant']}-{platform['arch']}":
            raise PlanError(
                f"Platform id must equal variant-arch for platform {platform_id}"
            )
        if platform["os"] not in {"linux", "macos", "windows"}:
            raise PlanError(f"Unsupported os for platform {platform_id}")
        if platform["arch"] not in {"x64", "arm64"}:
            raise PlanError(f"Unsupported arch for platform {platform_id}")
        expected_extension = "zip" if platform["os"] == "windows" else "tar.gz"
        if platform["archive_extension"] != expected_extension:
            raise PlanError(f"Invalid archive extension for platform {platform_id}")
        if not isinstance(platform["status"], str) or platform["status"] not in {
            "implemented",
            "designed",
            "experimental",
        }:
            raise PlanError(f"Invalid status for platform {platform_id}")
        if not isinstance(platform["controller_enabled"], bool):
            raise PlanError(f"controller_enabled must be boolean for {platform_id}")
        if platform["controller_enabled"]:
            enabled_ids.add(platform_id)
            workflow = platform["build_workflow"]
            if (
                platform["status"] != "implemented"
                or not isinstance(workflow, str)
                or not WORKFLOW_RE.fullmatch(workflow)
            ):
                raise PlanError(
                    f"Enabled platform {platform_id} must be implemented and name a workflow"
                )
            if workflow not in SUPPORTED_BACKEND_CONTRACTS:
                raise PlanError(
                    f"Enabled platform {platform_id} names an unsupported workflow"
                )
            if (
                platform["variant"],
                platform["os"],
                platform["arch"],
                platform["archive_extension"],
            ) not in SUPPORTED_BACKEND_CONTRACTS[workflow]:
                raise PlanError(
                    f"Enabled platform {platform_id} does not match {workflow}"
                )
            if workflows_dir is not None:
                workflow_path = workflows_dir / workflow
                if not workflow_path.is_file() or workflow_path.is_symlink():
                    raise PlanError(
                        f"Enabled platform {platform_id} names a missing workflow: {workflow}"
                    )
            asset_key = (
                platform["variant"],
                platform["arch"],
                platform["archive_extension"],
            )
            if asset_key in asset_keys:
                raise PlanError(
                    f"Enabled platform {platform_id} collides with another asset name"
                )
            asset_keys.add(asset_key)
        elif platform["build_workflow"] is not None:
            raise PlanError(f"Disabled platform {platform_id} must not name a workflow")

    if not REQUIRED_ENABLED_PLATFORM_IDS.issubset(enabled_ids):
        missing = sorted(REQUIRED_ENABLED_PLATFORM_IDS - enabled_ids)
        raise PlanError(
            "Required implemented platforms are not controller-enabled: "
            + ", ".join(missing)
        )


def validate_repository_platform_matrix(config: dict[str, Any]) -> None:
    """Keep the checked-in multi-platform design from silently shrinking."""

    validate_platform_config(config)
    indexed = {platform["id"]: platform for platform in config["platforms"]}
    for platform_id, expected in REQUIRED_DESIGN_PLATFORM_CONTRACTS.items():
        platform = indexed.get(platform_id)
        if platform is None:
            raise PlanError(f"Required designed platform is missing: {platform_id}")
        actual = (
            platform["variant"],
            platform["os"],
            platform["arch"],
            platform["archive_extension"],
            platform["status"],
        )
        if actual != expected or platform["controller_enabled"]:
            raise PlanError(
                f"Designed platform contract changed without backend review: {platform_id}"
            )


def parse_hashes(path: Path) -> dict[tuple[int, int, int], dict[str, str]]:
    try:
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise PlanError(
                f"Redis hashes input is larger than {MAX_INPUT_BYTES} bytes: {path}"
            )
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PlanError(f"Unable to read Redis hashes from {path}: {exc}") from exc

    releases: dict[tuple[int, int, int], dict[str, str]] = {}
    for line in lines:
        stripped = line.strip()
        stable_match = STABLE_HASH_LINE_RE.fullmatch(stripped)
        if stable_match is None:
            if STABLE_HASH_PREFIX_RE.match(stripped):
                raise PlanError(f"Malformed stable Redis hash entry: {stripped}")
            continue
        version = tuple(int(part) for part in stable_match.group(1, 2, 3))
        version_string = version_text(version)
        declared_version = ".".join(stable_match.group(1, 2, 3))
        if declared_version != version_string:
            raise PlanError(
                f"Official hash entry uses a noncanonical version: {declared_version}"
            )
        declared_source_url = stable_match.group(6)
        allowed_source_urls = {
            f"http://download.redis.io/releases/redis-{version_string}.tar.gz",
            f"https://download.redis.io/releases/redis-{version_string}.tar.gz",
        }
        if declared_source_url not in allowed_source_urls:
            raise PlanError(
                "Official hash entry declares an unexpected source URL for Redis "
                f"{version_string}: {declared_source_url}"
            )
        algorithm = stable_match.group(4)
        digest = stable_match.group(5)
        expected_digest_length = 40 if algorithm == "sha1" else 64
        if len(digest) != expected_digest_length:
            raise PlanError(
                f"Invalid {algorithm} digest for Redis {version_string}"
            )
        if algorithm != "sha256":
            continue
        record = {
            "version": version_string,
            "series": series_text(version),
            "algorithm": algorithm,
            "sha256": digest,
            "hashes_source_url": declared_source_url,
        }
        existing = releases.get(version)
        if existing is not None and existing != record:
            raise PlanError(f"Conflicting official hash entries for Redis {record['version']}")
        releases[version] = record
    if not releases:
        raise PlanError("No stable Redis SHA-256 entries were found")
    return releases


def index_releases(data: Any) -> dict[str, dict[str, Any]]:
    if data is None:
        return {}
    if not isinstance(data, list):
        raise PlanError("GitHub releases inventory must be a JSON array")
    indexed: dict[str, dict[str, Any]] = {}
    for release in data:
        if not isinstance(release, dict):
            raise PlanError("Invalid release object in GitHub inventory")
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            raise PlanError("GitHub release is missing tag_name")
        if not isinstance(release.get("draft", False), bool) or not isinstance(
            release.get("prerelease", False), bool
        ):
            raise PlanError(f"GitHub release {tag} has invalid publication state")
        normalized = tag.removeprefix("redis-").removeprefix("v")
        if VERSION_RE.fullmatch(normalized) and tag != normalized:
            raise PlanError(
                f"Noncanonical stable Redis release tag {tag}; expected {normalized}"
            )
        if not VERSION_RE.fullmatch(tag):
            continue
        parsed_tag = tuple(int(part) for part in VERSION_RE.fullmatch(tag).groups())
        canonical_tag = version_text(parsed_tag)
        if tag != canonical_tag:
            raise PlanError(
                f"Noncanonical stable Redis release tag {tag}; expected {canonical_tag}"
            )
        normalized = tag
        if normalized in indexed:
            raise PlanError(f"GitHub inventory contains duplicate release tag {tag}")
        assets = release.get("assets", [])
        if not isinstance(assets, list):
            raise PlanError(f"GitHub release {tag} has an invalid assets value")
        names: set[str] = set()
        for asset in assets:
            if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
                raise PlanError(f"GitHub release {tag} has an invalid asset entry")
            name = asset["name"]
            if name in names:
                raise PlanError(f"GitHub release {tag} has duplicate asset name {name}")
            names.add(name)
        indexed[normalized] = {
            "tag_name": tag,
            "draft": release.get("draft", False),
            "prerelease": release.get("prerelease", False),
            "assets": names,
        }
    return indexed


def expected_assets(
    version: str, prefix: str, platform: dict[str, Any]
) -> tuple[str, str]:
    archive = (
        f"{prefix}-{version}-{platform['variant']}-{platform['arch']}."
        f"{platform['archive_extension']}"
    )
    return archive, f"{archive}.sha256"


def expected_release_assets(version: str) -> tuple[str, str, str]:
    return (
        "SHA256SUMS",
        "manifest.json",
        f"redis-unofficial-builds-{version}.spdx.json",
    )


def resolve(
    release_config: dict[str, Any],
    platform_config: dict[str, Any],
    hashes: dict[tuple[int, int, int], dict[str, str]],
    github_releases: dict[str, dict[str, Any]],
    as_of: dt.date,
    requested_series: set[str] | None = None,
    requested_version: str | None = None,
    hashes_commit: str | None = None,
) -> dict[str, Any]:
    validate_release_config(release_config)
    validate_platform_config(platform_config)
    if type(as_of) is not dt.date:
        raise PlanError("as_of must be a date")
    if hashes_commit is not None:
        parse_git_oid(hashes_commit)
    if requested_version is not None and requested_series is not None:
        raise PlanError("An exact version and a series filter are mutually exclusive")
    if requested_series is not None:
        if not isinstance(requested_series, (set, frozenset)):
            raise PlanError("Requested Redis series must be a set")
        for requested in requested_series:
            parse_series(requested)

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
            if requested_series is not None or exact is not None:
                raise PlanError(
                    f"No stable official SHA-256 releases found for Redis {name}"
                )
            plans.append(
                {
                    "series": name,
                    "version": "none",
                    "release_type": entry["release_type"],
                    "eol": entry.get("eol"),
                    "source_url": "",
                    "source_sha256": "",
                    "release_exists": False,
                    "release_draft": False,
                    "release_prerelease": False,
                    "expected_assets": [],
                    "missing_assets": [],
                    "unexpected_assets": [],
                    "blocked": True,
                    "action": "blocked_no_official_stable_release",
                }
            )
            continue
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
        current_release = github_releases.get(version)
        release_exists = current_release is not None
        current_assets = current_release["assets"] if current_release else set()
        expected: list[str] = list(expected_release_assets(version))
        for platform in enabled_platforms:
            assets = expected_assets(version, prefix, platform)
            expected.extend(assets)
        expected_set = set(expected)
        missing = sorted(expected_set - current_assets) if not eol else []
        unexpected = sorted(current_assets - expected_set) if release_exists else []
        blocked = False

        if eol:
            action = "skip_eol"
        elif not enabled_platforms:
            action = "skip_no_enabled_platforms"
        elif release_exists and (
            current_release["draft"] or current_release["prerelease"]
        ):
            action = "blocked_nonfinal_release_state"
            blocked = True
        elif release_exists and missing:
            action = "blocked_incomplete_immutable_release"
            blocked = True
        elif release_exists and unexpected:
            action = "blocked_unexpected_immutable_release_assets"
            blocked = True
        elif release_exists:
            action = "skip_complete"
        else:
            action = "plan_new_release"

        plan = {
            "series": name,
            "version": version,
            "release_type": entry["release_type"],
            "eol": eol_text,
            "source_url": source_url,
            "source_sha256": record["sha256"],
            "release_exists": release_exists,
            "release_draft": current_release["draft"] if current_release else False,
            "release_prerelease": (
                current_release["prerelease"] if current_release else False
            ),
            "expected_assets": sorted(expected_set),
            "missing_assets": missing,
            "unexpected_assets": unexpected,
            "blocked": blocked,
            "action": action,
        }
        plans.append(plan)

        if not eol and enabled_platforms and missing and not release_exists:
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
        "hashes_commit": hashes_commit,
        "release_plans": plans,
        "version_matrix": {"include": version_rows},
        "build_matrix": {"include": build_rows},
        "new_series_candidates": new_series,
        "disabled_platforms": disabled_platforms,
        "has_planned_builds": bool(build_rows),
        "blocked_release_count": sum(item["blocked"] for item in plans),
    }


def render_summary(plan: dict[str, Any]) -> str:
    lines = [
        "# Redis release controller plan",
        "",
        "> Plan only: this controller does not dispatch builds or publish releases.",
        "",
        f"As of: `{plan['as_of']}`",
        f"Redis hashes snapshot: `{plan['hashes_commit'] or 'not recorded'}`",
        "",
        "| Series | Version | Release | Missing | Unexpected | Action |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for item in plan["release_plans"]:
        if item["release_draft"]:
            state = "draft"
        elif item["release_prerelease"]:
            state = "prerelease"
        else:
            state = "published" if item["release_exists"] else "missing"
        lines.append(
            f"| {item['series']} | {item['version']} | {state} | "
            f"{len(item['missing_assets'])} | {len(item['unexpected_assets'])} | "
            f"`{item['action']}` |"
        )
    lines.extend(
        [
            "",
            f"Planned platform jobs: **{len(plan['build_matrix']['include'])}**",
            f"New series candidates: **{len(plan['new_series_candidates'])}**",
            f"Disabled platform rows: **{len(plan['disabled_platforms'])}**",
            f"Blocked rows: **{plan['blocked_release_count']}**",
            "",
        ]
    )
    if plan["blocked_release_count"]:
        lines.extend(
            [
                "> Blocked rows require maintainer review and are excluded from all "
                "build matrices; existing Releases are never modified.",
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
        "blocked_release_count": str(plan["blocked_release_count"]),
        "hashes_commit": plan["hashes_commit"] or "",
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
    parser.add_argument("--hashes-commit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        as_of = dt.date.fromisoformat(args.as_of)
        if args.as_of != as_of.isoformat():
            raise PlanError(f"Noncanonical as-of date: {args.as_of}")
        release_config = load_json(args.release_config)
        platform_config = load_json(args.platform_config)
        validate_release_config(release_config)
        workflows_dir = args.platform_config.resolve().parent.parent / ".github/workflows"
        validate_platform_config(platform_config, workflows_dir)
        validate_repository_platform_matrix(platform_config)
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
            hashes_commit=args.hashes_commit,
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
    except (PlanError, ValueError, OSError) as exc:
        print(f"release controller error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
