# Redis Unofficial Builds

[简体中文](README.zh-CN.md)

Automated, tested, unofficial Redis binary packages. This repository is
designed to host builds for multiple operating systems, Linux ABI baselines,
and CPU architectures in the same versioned GitHub Release.

> This project is not affiliated with or endorsed by Redis Ltd. Redis binaries
> remain subject to the upstream license included in each archive.

## Current support

| Package variant | Architecture | Runtime requirement | Status |
| --- | --- | --- | --- |
| `linux-glibc2.28` | `x64` | Linux, glibc 2.28+, systemd by default | Available |
| `linux-glibc2.28` | `arm64` | Linux, glibc 2.28+, systemd by default | Available |
| `linux-glibc2.17-legacy` | `x64` / `arm64` | Legacy Linux, systemd | Designed |
| `linux-musl1.2` | `x64` / `arm64` | Alpine/musl, OpenRC | Designed |
| `macos12` | `x64` / `arm64` | macOS 12+, launchd | Designed |
| `windows-msys2` | `x64` | Windows Service | Designed, primary Windows backend |
| `windows-cygwin` | `x64` | Windows Service | Designed, compatibility backend |

The current Linux packages use the fixed prefix `/usr/local/redis`. A separate
package is not required for every newer glibc release: a glibc 2.28 build is
intended to run on glibc 2.28 and newer systems. Alpine uses musl and therefore
requires a separate build.

The complete target architecture, release gates, and implementation order are
in the [multi-platform release design](docs/PLATFORM-DESIGN.md). The initial
multi-version policy tracks the latest patch of every current Redis Open Source
GA series: 6.2, 7.2, 7.4, 8.0, 8.2, 8.4, 8.6, 8.8, and 8.10. Existing package
availability remains distinct from this designed target.

The Windows design references the Apache-2.0
[`redis-windows/redis-windows`](https://github.com/redis-windows/redis-windows)
project and turns its public issue reports into explicit regression tests and
scope decisions. See [Windows issue coverage](docs/WINDOWS-ISSUE-COVERAGE.md)
and [third-party notices](THIRD_PARTY_NOTICES.md). Native Windows ARM64 is not
claimed until a native toolchain and real ARM64 service/data tests exist.

## Release assets

Redis 7.4.11 Linux assets are named as follows:

```text
Redis-Rzon-7.4.11-linux-glibc2.28-x64.tar.gz
Redis-Rzon-7.4.11-linux-glibc2.28-x64.tar.gz.sha256
Redis-Rzon-7.4.11-linux-glibc2.28-arm64.tar.gz
Redis-Rzon-7.4.11-linux-glibc2.28-arm64.tar.gz.sha256
```

Each archive has a `redis/` root:

```text
redis/
├── bin/
├── conf/
│   ├── redis.conf
│   └── sentinel.conf
├── scripts/
│   ├── common.sh
│   ├── install.sh
│   ├── update.sh
│   └── uninstall.sh
├── systemd/
│   ├── redis.service
│   └── redis-hardening.conf.example
├── PACKAGE-INFO
├── BUILD-INFO
├── LICENSE.txt
└── README.txt
```

`PACKAGE-INFO` contains machine-readable version, architecture, libc, minimum
glibc, service backend, and install-prefix metadata. Install and update scripts
validate it and execute the packaged `redis-server --version` before making any
system changes.

## Linux installation

Download the package matching the server architecture. Extract it to a
temporary directory instead of overwriting `/usr/local/redis`:

```bash
sha256sum -c Redis-Rzon-7.4.11-linux-glibc2.28-x64.tar.gz.sha256
mkdir redis-install
tar -xzf Redis-Rzon-7.4.11-linux-glibc2.28-x64.tar.gz -C redis-install
sudo ./redis-install/redis/scripts/install.sh
```

The default installation:

- checks Linux, architecture, glibc, binary execution, and systemd first;
- installs into `/usr/local/redis`;
- reuses an existing `redis` account or creates a non-login system account;
- creates a new default configuration using `/usr/local/redis/data`;
- registers `/etc/systemd/system/redis.service` and starts it;
- records which account and files were created by this project.

The base systemd unit respects `dir`, `logfile`, socket, TLS, module, ACL, and
include paths from the Redis configuration. Strict hardening is supplied only
as an optional example because its writable-path policy must match each user's
configuration.

Useful options:

```bash
# Register and enable the service without starting it
sudo ./redis-install/redis/scripts/install.sh --no-start

# Install program files without requiring or registering systemd
sudo ./redis-install/redis/scripts/install.sh --no-service

# Preserve and adopt an existing configuration/data directory
sudo ./redis-install/redis/scripts/install.sh --adopt
```

An existing `redis.service` owned by another package is never replaced unless
`--force-service` is explicitly supplied.

## Linux update and adoption

Run the update script from a newly extracted package:

```bash
mkdir redis-update
tar -xzf Redis-Rzon-7.4.11-linux-glibc2.28-x64.tar.gz -C redis-update
sudo ./redis-update/redis/scripts/update.sh
```

The updater validates the new binary before stopping Redis, backs up the old
program, configuration, scripts, unit, metadata, and installation state under
`/usr/local/redis-backups/`, replaces program files, and preserves the existing
configuration and data. If the updated service does not start, program files,
service state, and the unit are rolled back.

The automatic backup does not copy the potentially large Redis data directory.
Take an application-consistent filesystem or storage snapshot before a
production version upgrade; binary rollback is not a substitute for a data
backup.

An existing manual installation has no project state file and is rejected by
default. After reviewing its service user, permissions, configuration paths,
and backup, migrate it explicitly with:

```bash
sudo ./redis-update/redis/scripts/update.sh --adopt
```

Use `--adopt --no-service` when adopting a binary-only installation.

## Linux uninstall

Remove the service and program while preserving configuration, data, state,
and the service account:

```bash
sudo /usr/local/redis/scripts/uninstall.sh
```

Delete the complete prefix as well:

```bash
sudo /usr/local/redis/scripts/uninstall.sh --purge
```

`--purge` removes the `redis` user or group only when the installation state
proves this project created it and its current UID/GID still match. Pre-existing
accounts and `/usr/local/redis-backups/` are preserved.

## Language

Maintenance scripts select English or Chinese from `LC_ALL`, `LC_MESSAGES`, or
`LANG`. Override automatic selection when needed:

```bash
REDIS_INSTALL_LANG=en sudo -E ./redis/scripts/install.sh
REDIS_INSTALL_LANG=zh_CN sudo -E ./redis/scripts/install.sh
```

The scripts use command exit codes and machine-readable properties rather than
parsing localized `systemctl` status text.

## Linux compatibility and testing

- GitHub runners: `ubuntu-24.04` for x64 and `ubuntu-24.04-arm` for ARM64.
- Build user space: `rockylinux/rockylinux:8`, using glibc 2.28.
- All packaged ELF binaries are checked so their highest required glibc symbol
  does not exceed `GLIBC_2.28`.
- `-march=native` is not used.
- TLS is disabled, matching a default Redis `make` build.
- systemd is the default service backend; `--no-service` supports binary-only
  installations. OpenRC requires a future musl/OpenRC package.

Ubuntu 24.04 supplies the runner kernel only. Redis is compiled inside the
Rocky Linux 8 container and does not link against Ubuntu's glibc. ARM64 runtime
kernel warnings such as `ARM64-COW-BUG` are not suppressed automatically.

Each architecture runs upstream checksum verification, `make test`, Redis
PING/SET/GET smoke tests, ELF architecture checks, dependency checks, and glibc
ABI checks. The packaged scripts are then tested for English/Chinese help,
wrong-architecture rejection before mutation, systemd installation, custom
configured data paths, updates, persistence, uninstall/reinstall, account-safe
purge, and binary-only installation.

## Automation

The current [Linux workflow](.github/workflows/build-linux.yml) accepts an
exact official Redis version and x64, ARM64, or both architectures. It can only
run by explicit manual dispatch or a future reusable-workflow caller; it has no
push or schedule trigger. Manual Release publication defaults to disabled.

The [release-plan workflow](.github/workflows/resolve-versions.yml) checks all
tracked series daily, when controller files reach `main`, or on manual
dispatch. It only uploads JSON/Markdown plans and never invokes the Linux
workflow.

The multi-platform controller is implemented in safe **plan-only** mode. It is
specified in the [release design](docs/PLATFORM-DESIGN.md), documented in the
[release controller guide](docs/RELEASE-CONTROLLER.md), and declared by
[`config/release-lines.json`](config/release-lines.json). For each enrolled
series it identifies a new official SHA-256 entry and produces the missing
version/platform matrices. It does not yet dispatch those matrices or publish
a Release. A brand-new `X.Y` series is detected and reported as a candidate for
a small policy PR so license, toolchain, and patch-set changes are reviewed
once before its later patch updates become eligible for automation.

The build matrices remain outputs for review until an independently permissioned
orchestration job is deliberately implemented.

## Local Linux build

Run the build script on Rocky Linux 8 or another controlled glibc 2.28 build
environment:

```bash
export REDIS_VERSION=7.4.11
export REDIS_SOURCE_SHA256=3c266ece0abd54ed3b1c912c6eb86b7508cf382cb690ee6649d3843f018f6357
export PACKAGE_VARIANT=linux-glibc2.28
export GLIBC_BASELINE=2.28
export EXPECTED_MACHINE_ARCH="$(uname -m)"

case "$(uname -m)" in
  x86_64) export PACKAGE_ARCH=x64 ;;
  aarch64) export PACKAGE_ARCH=arm64 ;;
  *) echo "Unsupported architecture"; exit 1 ;;
esac

./scripts/linux/build-redis.sh
```

## License

Repository build scripts and workflows use the [MIT License](LICENSE). Redis
binaries, configuration, and source remain subject to the upstream license
included in each package. Referenced or incorporated third-party work is
documented in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
