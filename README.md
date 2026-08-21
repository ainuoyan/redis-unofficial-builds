# Redis Unofficial Builds

[简体中文](README.zh-CN.md)

Unofficial, versioned binary distributions of Redis. The stable publication
path remains the glibc 2.28 Linux build. Additional ABI and operating-system
backends have a separate experimental, manually triggered artifact path.

> This project is not affiliated with or endorsed by Redis Ltd. Redis and its
> bundled dependencies remain subject to the license and notice files included
> in each package.

## Available packages

Only rows marked **implemented** are eligible for GitHub Release publication.
An **experimental** row has build/package code but is not a supported or
Release-published package.

| Variant | Architecture | Runtime requirement | Status |
| --- | --- | --- | --- |
| `linux-glibc2.28` | `x64` | Linux, glibc 2.28+; systemd unless `--no-service` is used | Implemented |
| `linux-glibc2.28` | `arm64` | Linux, glibc 2.28+; systemd unless `--no-service` is used | Implemented |
| `linux-glibc2.17-legacy` | `x64` / `arm64` | Linux, glibc 2.17+, systemd | Experimental artifact only |
| `linux-musl1.2` | `x64` / `arm64` | musl 1.2 Linux, OpenRC | Experimental artifact only |
| `macos12` | `x64` / `arm64` | macOS 12+, launchd | Experimental artifact only |
| `windows-msys2` | `x64` | Windows Server 2022 test runner, Windows SCM | Experimental artifact only |
| `windows-cygwin` | `x64` | Windows Service Control Manager | Design only |

The implemented publication target remains glibc 2.28 Linux only. A glibc
2.28 package normally runs on a newer glibc system of the same architecture;
Alpine and other musl systems require the separately named musl artifact.
Linux packages are plain `.tar.gz` archives and do not require RPM, DEB, Snap,
or APK. Published files are available from the repository's
[GitHub Releases](https://github.com/ainuoyan/redis-unofficial-builds/releases).

Before using the lifecycle instructions below, confirm that the selected
Release has the exact seven-asset current-format inventory described in the
next section. Older four-asset Releases are legacy binary bundles: they do not
contain the current lifecycle scripts or metadata, and automation deliberately
refuses to modify or complete them.

Packages produced by the current Linux workflow use the fixed prefix
`/usr/local/redis` and the `core` build profile. They include Redis server and
command-line binaries but exclude the modules bundled with Redis 8 source
releases. TLS is disabled, matching a default Redis `make` build.

The complete status and acceptance criteria are documented in the
[multi-platform release design](docs/PLATFORM-DESIGN.md). The Windows design
references
[`redis-windows/redis-windows`](https://github.com/redis-windows/redis-windows)
at fixed commit
[`17fd667560f7903820dcabeebb9d20ade1159fe9`](https://github.com/redis-windows/redis-windows/commit/17fd667560f7903820dcabeebb9d20ade1159fe9).
No stable Windows Release, Windows production support, Cygwin package, or
native Windows ARM64 support is currently claimed. See
[Windows issue coverage](docs/WINDOWS-ISSUE-COVERAGE.md) and
[third-party notices](THIRD_PARTY_NOTICES.md).

### Experimental manual artifacts

`.github/workflows/build-experimental.yml` can be triggered manually for an
exact official stable Redis version. It verifies that version against an
immutable `redis/redis-hashes` commit, builds each selected architecture
natively, validates archive contents, and uploads seven-day Actions artifacts:

- glibc 2.17 legacy: x64 and ARM64 `.tar.gz` packages with the reviewed
  systemd lifecycle scripts;
- musl 1.2: x64 and ARM64 `.tar.gz` packages with OpenRC lifecycle scripts;
- macOS 12+: native x64 and ARM64 `.tar.gz` packages with launchd lifecycle
  scripts; and
- Windows: one x64 MSYS2 `.zip` package with a dedicated SCM wrapper and
  PowerShell lifecycle scripts.

Each package has an adjacent `.sha256` file and declares `experimental` in its
package metadata. The workflow has `contents: read`, contains no Release/tag
operation, is not callable by the release controller, and does not generate
the stable Release manifest, SBOM, or attestations. Passing a build is not the
same as completing the oldest-host, rollback, persistence, load, and security
acceptance gates in the platform design. Use the package-local `README.txt`
for its experimental layout; the stable lifecycle instructions below apply to
`linux-glibc2.28` Release packages only. A checked-in experimental row does not
prove that a successful native workflow run exists; verify the selected run
and its logs before downloading an artifact.

## Release and package contents

A Release produced by the current Linux publisher contains exactly seven
assets. For an exact Redis version `X.Y.Z` they are:

```text
Redis-Rzon-X.Y.Z-linux-glibc2.28-x64.tar.gz
Redis-Rzon-X.Y.Z-linux-glibc2.28-x64.tar.gz.sha256
Redis-Rzon-X.Y.Z-linux-glibc2.28-arm64.tar.gz
Redis-Rzon-X.Y.Z-linux-glibc2.28-arm64.tar.gz.sha256
SHA256SUMS
manifest.json
redis-unofficial-builds-X.Y.Z.spdx.json
```

`SHA256SUMS` covers the other six assets. `manifest.json` binds the Redis
source checksum, immutable `redis-hashes` snapshot commit, packaging revision,
patch-set checksum, architecture, ABI baseline, archive size, and archive
checksum. The SPDX 2.3 document is a **release-package-level inventory** of the
Redis source and the two published archives; it is not a file-level or complete
transitive-dependency SBOM.

Every current `PACKAGE_FORMAT=2` archive has this `redis/` layout; the
contributor-license file is conditional for older Redis versions:

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
├── THIRD_PARTY_NOTICES.md
├── UPSTREAM-CONTRIBUTOR-LICENSE.txt
├── UPSTREAM-DEPENDENCY-NOTICES.txt
└── README.txt
```

`UPSTREAM-CONTRIBUTOR-LICENSE.txt` is present for Redis 7.4 and newer and is a
byte-for-byte copy of the verified source archive's root
`REDISCONTRIBUTIONS.txt`. For older versions it is included only when that
upstream file exists; no placeholder license text is generated.

`UPSTREAM-DEPENDENCY-NOTICES.txt` is a deterministic collection of recognized
license, notice, copyright, and README files found under the verified Redis
source tree's `deps/` directory. Framed entries record their upstream relative
paths and byte lengths. It supplements, rather than replaces, `LICENSE.txt`,
any `UPSTREAM-CONTRIBUTOR-LICENSE.txt`, and `THIRD_PARTY_NOTICES.md` and is not
a claim that an automated scan has identified every legal obligation.

`PACKAGE-INFO` and `BUILD-INFO` record the package contract and provenance.
Lifecycle scripts validate these files and run the packaged
`redis-server --version` under an unprivileged, no-new-privileges identity
before making system changes.

### Verify a download

Download the archive, its adjacent checksum, and optionally the aggregate
metadata. Replace `X.Y.Z` with the exact current-format Release version and
verify the archive before extraction:

```bash
version=X.Y.Z
archive="Redis-Rzon-${version}-linux-glibc2.28-x64.tar.gz"
sha256sum -c "${archive}.sha256"
```

When all seven assets are present in one directory, verify the complete
checksummed set:

```bash
sha256sum -c SHA256SUMS
```

The adjacent checksum and `SHA256SUMS` share the GitHub Release trust boundary
with the archives; neither is an independent signature. Releases created by
the current workflow also have GitHub Artifact Attestations: SLSA provenance
for all seven assets and an SPDX predicate for each archive. With
[GitHub CLI](https://cli.github.com/) installed, verify the two predicates
separately:

```bash
gh attestation verify \
  "$archive" \
  --repo ainuoyan/redis-unofficial-builds \
  --predicate-type https://slsa.dev/provenance/v1 \
  --deny-self-hosted-runners

gh attestation verify \
  "$archive" \
  --repo ainuoyan/redis-unofficial-builds \
  --predicate-type https://spdx.dev/Document/v2.3 \
  --deny-self-hosted-runners
```

The publisher itself verifies workflow identity, the signer and source
repository digest (both bound to the packaging revision), protected
default-branch ref, predicate type, and the prohibition on self-hosted runners
before publishing.

## Linux prerequisites

The lifecycle scripts require root privileges and:

- `sha256sum` and GNU `tar` for the documented verification and extraction
  commands;
- Bash;
- common GNU userland tools from coreutils/findutils plus `awk`, `grep`, and
  `sed`;
- util-linux commands `flock` and `setpriv`;
- account-management commands `getent`, `groupadd`, `groupdel`, `useradd`,
  and `userdel`;
- a running systemd instance and `systemctl` when service mode is selected;
  `--no-service` instead performs a complete managed installation without
  requiring or registering systemd; and
- glibc 2.28 or newer on the implemented package variant. `getconf` is required
  to verify the host glibc version, and the packaged binary is then executed as
  the final compatibility check.

Distribution package names differ. Typical providers are `bash`, `coreutils`,
`tar`, `findutils`, `gawk`, `grep`, `sed`, `util-linux`, `shadow-utils`,
`glibc`, and `systemd`.

## Linux installation

Extract into a root-owned staging directory. The package root and all of its
files must be root controlled, not group/world-writable, free of extended
ACLs, and free of unexpected links or special files. Do not execute lifecycle
scripts from a user-owned download directory.

```bash
version=X.Y.Z
archive="Redis-Rzon-${version}-linux-glibc2.28-x64.tar.gz"
sha256sum -c "${archive}.sha256"
stage="$(sudo mktemp -d /var/tmp/redis-unofficial-builds.XXXXXX)"
sudo chmod 0755 "$stage"
sudo sh -c 'umask 022; exec tar --no-same-owner --no-same-permissions -xzf "$1" -C "$2"' sh \
  "$archive" "$stage"
sudo "$stage/redis/scripts/install.sh"
```

A fresh default installation:

- installs into `/usr/local/redis`;
- creates or safely reuses the `redis` service account;
- creates `/usr/local/redis/data` and a local Unix socket at
  `/usr/local/redis/data/redis.sock` with mode `0770`;
- sets `port 0`, so TCP is disabled by default;
- installs, enables, and starts `/etc/systemd/system/redis.service`; and
- records the package, account, and service state in a root-owned mode `0600`
  state file.

Test the default socket:

```bash
sudo -u redis /usr/local/redis/bin/redis-cli \
  -s /usr/local/redis/data/redis.sock PING
```

Enable TCP only after reviewing `bind`, `port`, protected mode, ACLs,
firewalling, and transport security. TLS is not compiled into this package.

Useful installation modes:

```bash
# Register and enable the service without starting it
sudo "$stage/redis/scripts/install.sh" --no-start

# Perform a complete managed installation without requiring or registering systemd
sudo "$stage/redis/scripts/install.sh" --no-service

# Preserve and adopt existing conf/ or data/
sudo "$stage/redis/scripts/install.sh" --adopt
```

`--no-service` still creates or safely reuses the `redis` account, installs
configuration and data directories, and records package metadata and lifecycle
state. It does not register or start a service. Before updating or uninstalling
such an installation, manually stop every process whose executable is exactly
`/usr/local/redis/bin/redis-server`; the scripts refuse to modify or remove the
installation while any such process remains.

### Account, service, and configuration safety

An existing `redis` user is reused only when it has a nonzero UID, uses the
`redis` group as its primary and only group, and has a non-login shell
(`nologin` or `false`). Its home must be a canonical absolute path without
`.` or `..` components. An existing `redis` group must have a nonzero GID. A
managed installation with current-format state additionally requires the
recorded UID, primary GID, home, shell, and supplementary-group set to remain
unchanged. When older state is upgraded, account-creation ownership is not
carried into the new state for either the user or group because the older
format cannot prove the complete identity.

A `redis.service` managed by another installation is rejected by default.
`--force-service` permits replacement only when the foreign unit is
`inactive` or `failed`. An `active` or `reloading` foreign service is always
refused, even with `--force-service`; an administrator must stop and inspect
it first. Replacing a disabled foreign unit enables the new managed unit; if
the transaction fails, rollback disables it before restoring the foreign unit.
An already-enabled foreign unit keeps its enablement. For a managed active
service, the effective unit and all drop-ins must retain the expected user,
group, working directory, command, environment,
credentials, execution hooks, and no-new-privileges contract.

Before install, adoption, update, or service start, configuration trust checks
follow `include` directives recursively (up to 64 unique files) and validate
`loadmodule` and `aclfile` references. Referenced paths:

- may be absolute or relative to `/usr/local/redis`;
- must not contain whitespace, globs, or backslashes;
- must resolve without symlink path components; and
- must identify root-owned, single-link regular files beneath root-owned,
  non-group/world-writable directory chains without extended ACLs.

Quoted paths containing whitespace are therefore not accepted by lifecycle
validation, even if Redis itself could parse them. `loadmodule` may retain
additional module arguments, but its module file is still checked. These
rules protect root-run maintenance from configuration-controlled file
substitution; correct unsafe ownership or references before maintenance.

The same trust contract makes a configured `aclfile` root-owned and not
group/world-writable, so the `redis` service account cannot update it with
`ACL SAVE`. Direct runtime writes to a managed `aclfile` are unsupported.
Deploy ACL changes offline as root and restart Redis, or use an equivalent
site-controlled workflow that restores the trusted ownership and modes before
running any root lifecycle script. Do not make the ACL file writable merely to
enable `ACL SAVE` and then run package maintenance against it.

The base unit leaves Redis data, log, TLS, ACL, module, and include paths to
`redis.conf`. The supplied hardening drop-in is an example only because its
writable-path policy must be adapted to the deployed configuration.

## Linux update and adoption

Verify and extract the new package into a new root-controlled staging
directory, then run:

```bash
sudo "$stage/redis/scripts/update.sh"
```

The updater validates the new package and binary before stopping Redis. It
backs up the old program, configuration, scripts, service unit, notices,
metadata, and state under `/usr/local/redis-backups/`, then replaces program
files while preserving configuration and data. It waits for a Redis protocol
response, not merely a systemd active state. A failed start or `INT`, `TERM`,
or `HUP` triggers rollback of managed program and service state.

Readiness after an install or update (re)start is budgeted at 30 seconds by
default. While Redis restores a large dataset it answers PING with
`-LOADING`; when the budget expires in that state, the scripts report the
loading condition explicitly instead of a generic start failure. Raise the
budget per run with `sudo REDIS_READY_TIMEOUT=<seconds> ...` (1–99999).

Downgrades are rejected by default:

```bash
sudo "$stage/redis/scripts/update.sh" --allow-downgrade
```

Use `--allow-downgrade` only after checking Redis downgrade compatibility and
taking a separate, application-consistent data snapshot. The automatic backup
does **not** copy the data directory. `SIGKILL`, host failure, and storage
failure cannot execute shell rollback code.

An ordinary uninstall retains lifecycle state. Reinstalling an older package
over that retained state is also rejected by default. Only after the same
compatibility review and independent snapshot should an administrator use
`install.sh --allow-downgrade`.

An unmanaged installation is rejected unless explicitly adopted:

```bash
sudo "$stage/redis/scripts/update.sh" --adopt
sudo "$stage/redis/scripts/update.sh" --adopt --no-service
```

Adoption requires the fixed `/usr/local/redis` layout, root-controlled program
and configuration paths, a compatible service account, and trusted
configuration references. Existing configuration and data contents are
preserved. Normal updates preserve the recorded service mode, enablement, and
running state.

Install, update, and uninstall share an exclusive lock. Recursive replacement
or removal is refused when a target or any descendant is a mount point. Keep
mounted data outside the managed program tree and reference it from
`redis.conf`.

The install and update scripts deliberately do not copy SELinux labels or
extended attributes from the staging archive. On an enforcing host, apply the
site policy or run the appropriate relabel operation (for example,
`restorecon` where configured) after installation and before service start if
the default filesystem labeling is insufficient. Use `--no-start` for a new
installation, or stop a managed service before updating, when relabeling must
occur before Redis executes; start the service after relabeling.

## Linux uninstall

Remove the service and program while preserving `conf/`, `data/`, installation
state, backups, and the service account:

```bash
sudo /usr/local/redis/scripts/uninstall.sh
```

Remove the entire prefix, including configuration and data:

```bash
sudo /usr/local/redis/scripts/uninstall.sh --purge
```

For current-format state, `--purge` removes a project-created `redis` account
only when its UID, primary GID, home, shell, and lack of supplementary groups
exactly match the recorded identity; the group must also retain its recorded
GID and have no unexpected explicit members. Older state that lacks the full
identity record is handled conservatively and preserves both account and
group. Pre-existing accounts and `/usr/local/redis-backups/` are retained.
Mount-point checks also apply to purge.

## Language

Maintenance scripts provide English and Simplified Chinese help and routine
messages. Selection uses `LC_ALL`, `LC_MESSAGES`, or `LANG` and can be
overridden:

```bash
sudo env REDIS_INSTALL_LANG=en "$stage/redis/scripts/install.sh"
sudo env REDIS_INSTALL_LANG=zh_CN "$stage/redis/scripts/install.sh"
```

Machine-readable commands run with a fixed C locale. Some bootstrap and
rollback safety diagnostics intentionally use a fixed bilingual or
locale-neutral form.

## Compatibility and testing

- x64 jobs use `ubuntu-24.04`; ARM64 jobs use `ubuntu-24.04-arm`.
- Compilation occurs inside a digest-pinned, multi-architecture Rocky Linux 8
  image with glibc 2.28. The runner contributes its kernel, not its glibc.
- All packaged ELF files are checked for architecture, unresolved
  dependencies, and a highest required symbol no newer than `GLIBC_2.28`.
- `-march=native` is not used.
- Each architecture runs official source checksum verification, upstream
  tests, PING/SET/GET smoke tests, and lifecycle integration tests.
- Lifecycle integration tests execute the scripts on a GNU/Linux userland;
  hosts without GNU `stat -c` and `realpath -e` (for example macOS) skip
  those cases instead of failing.

Rocky repository dependencies are resolved at build time, so the project
records compiler/runtime details but does not claim bit-for-bit reproducible
archives. ARM64 kernel warnings such as `ARM64-COW-BUG` are not suppressed.

## Release automation and immutability

The [Linux workflow](.github/workflows/build-linux.yml) has only manual and
`workflow_call` entry points; it has no push or schedule trigger. Publication
is disabled by default.

The current publisher creates only a brand-new numeric `X.Y.Z` Release and
tag. It:

1. requires both architectures and the exact seven-asset contract;
2. runs only from the protected default branch through the GitHub Environment
   named `release`;
3. creates one draft Release containing all assets;
4. binds each remote asset's numeric ID, byte size, and GitHub SHA-256 digest
   to the verified local file, then downloads and revalidates the draft;
5. rechecks the same draft and asset identities immediately before publishing
   it by numeric Release ID; and
6. reads back the published identities, downloads the assets again, and
   repeats semantic and attestation verification.

It never adds to, overwrites, deletes from, or otherwise repairs an existing
Release. An existing incomplete, legacy, draft, prerelease, or extra-asset
Release is a blocking condition requiring maintainer review. An exact existing
Release is downloaded and revalidated, including attestations, then skipped.
This pipeline policy cannot prevent another authorized repository writer from
changing a draft in the final API race window or changing a mutable published
Release later. Repository-level **Immutable Releases must be enabled before
production publication**, and Release write access must be restricted to the
reviewed publication path and trusted maintainers.
A manually requested `force_rebuild` can create nonpublishing workflow
artifacts after this validation, but it cannot republish the Release.

The workflow file cannot create repository protections. Administrators must
configure a branch protection rule or ruleset for the default branch, set
required reviewers plus deployment-branch restrictions on the `release`
Environment, and enable repository-level Immutable Releases.

The [plan workflow](.github/workflows/resolve-versions.yml) runs daily, on
relevant changes to `main`, or manually. It validates configuration, resolves
`redis/redis-hashes` `master` to an immutable 40-character commit, downloads
the hash index at that commit, and records the commit in its output. It
produces plans only; it cannot dispatch builds or publish Releases. New Redis
`X.Y` series require reviewed configuration enrollment. See the
[release controller guide](docs/RELEASE-CONTROLLER.md).

## Local Linux build

Run the builder as a non-root user inside a disposable Rocky Linux 8 or
equivalent controlled glibc 2.28 environment. Redis build and test targets
execute upstream source-controlled programs, so the environment must not
contain unrelated secrets or a live installation. The script refuses UID 0
and refuses to run when `/usr/local/redis` exists.

```bash
export REDIS_VERSION=7.4.11
export REDIS_SOURCE_SHA256=3c266ece0abd54ed3b1c912c6eb86b7508cf382cb690ee6649d3843f018f6357
export REDIS_HASHES_COMMIT=0123456789abcdef0123456789abcdef01234567
export PACKAGING_REVISION="$(git rev-parse HEAD)"
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

Replace the example `REDIS_HASHES_COMMIT` with the exact commit from which the
official SHA-256 was obtained. `PACKAGING_REVISION` must resolve to the
40-character commit containing the reviewed packaging code. The build records
both immutable revisions in `BUILD-INFO`, and Release metadata binds them to
the archives.

## License and trademarks

Redis license terms differ by version: Redis 7.2.x and earlier are under
BSD-3-Clause; Redis 7.4.x through 7.8.x use the RSALv2/SSPLv1 dual license;
Redis 8 and later offer RSALv2, SSPLv1, or AGPLv3. Consult the package's
`LICENSE.txt` and the official [Redis license summary](https://redis.io/legal/licenses/)
for the applicable terms. Redis names and marks remain subject to the official
[Redis Trademark Policy](https://redis.io/legal/trademark-policy/).

Repository-authored build and packaging code uses the [MIT License](LICENSE).
The upstream contributor text is preserved when applicable as
`UPSTREAM-CONTRIBUTOR-LICENSE.txt`, and dependency notices extracted from the
corresponding verified Redis source are in
`UPSTREAM-DEPENDENCY-NOTICES.txt`. Project references and attributions are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). These files are informational,
not legal advice; anyone redistributing a package must determine and satisfy
the obligations that apply to that Redis version and use.
