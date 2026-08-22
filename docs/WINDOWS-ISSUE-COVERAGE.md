# Windows issue coverage

This document records how the experimental MSYS2 backend uses reports from
[`redis-windows/redis-windows`](https://github.com/redis-windows/redis-windows).
No stable Windows package is currently published. A manual Actions workflow
can produce a seven-day experimental MSYS2 x64 artifact; that fact alone does
not establish Windows production support. The table is an acceptance
specification and prevents a packaging change from being presented as proof
that an upstream or compatibility-runtime defect is fixed.

Review baseline: upstream commit
[`17fd667560f7903820dcabeebb9d20ade1159fe9`](https://github.com/redis-windows/redis-windows/commit/17fd667560f7903820dcabeebb9d20ade1159fe9)
and the open issue list observed on 2026-08-20.

## Findings from the referenced implementation

At the audited commit, the upstream workflow builds both MSYS2 and Cygwin,
bundles their runtime DLLs, and supplies a .NET Windows service wrapper. Those
are useful building blocks, but several implementation details map directly to
reported issues:

- The [build workflow](https://github.com/redis-windows/redis-windows/blob/17fd667560f7903820dcabeebb9d20ade1159fe9/.github/workflows/build-redis.yml)
  uses `-O0`, which is unsuitable for a performance release and is a likely
  contributor to performance reports.
- [RedisConfiguration.cs](https://github.com/redis-windows/redis-windows/blob/17fd667560f7903820dcabeebb9d20ade1159fe9/Service/RedisConfiguration.cs)
  converts Windows paths to `/cygdrive/...` regardless of backend selection,
  which does not match the MSYS2 `/c/...` convention. It also constructs
  shutdown CLI arguments from server configuration arguments instead of Redis
  connection arguments.
- [RedisProcessManager.cs](https://github.com/redis-windows/redis-windows/blob/17fd667560f7903820dcabeebb9d20ade1159fe9/Service/RedisProcessManager.cs)
  waits a fixed 100 ms rather than proving Redis is ready and falls back to
  process-tree termination when graceful shutdown does not work.
- [Program.cs](https://github.com/redis-windows/redis-windows/blob/17fd667560f7903820dcabeebb9d20ade1159fe9/Program.cs)
  logs a Redis child exit but does not fail/stop the Windows service host.
- [RedisService.csproj](https://github.com/redis-windows/redis-windows/blob/17fd667560f7903820dcabeebb9d20ade1159fe9/RedisService.csproj)
  defines a wrapper version, but native Redis executables lack PE VERSIONINFO.

These are code-reading conclusions, not assertions made by the referenced
project.

## Current experimental coverage

This repository contains an independently implemented, self-contained .NET
SCM wrapper; no source file from the reference project is copied. The current
wrapper uses the MSYS2 `/c/...` path convention, runs Redis in the foreground,
performs protocol readiness, reports child/start failures to SCM, uses bounded
graceful shutdown with a process-tree fallback, and writes diagnostics under
the fixed installation prefix. PowerShell scripts validate package identity,
refuse reparse points, apply SID-based ACLs, preserve `conf` and `data` on a
normal uninstall, back up managed program state for update rollback, and
require explicit purge for data removal.

The Windows build job currently tests only the fixed path
`C:\Program Files\Redis-Unofficial` and the default unauthenticated loopback endpoint
on `windows-2022`. It covers fresh install, same-version update idempotency,
PING, explicit `SAVE`, SCM restart followed by key reload, ordinary uninstall
retention, update-based service recovery, and purge. It does not yet cover
non-ASCII paths, authenticated or TLS shutdown, Sentinel, BGSAVE/AOF-specific
behavior, port conflicts, unexpected child exit, rollback fault injection,
load ceilings, Windows client releases, or native PE VERSIONINFO. Consequently
no issue row below is marked **Verified**.

## Design requirements and regression tests

The issues below can be addressed substantially by packaging, a service
wrapper, or documentation. A row cannot be marked **Verified** until its named
regression passes on every applicable backend.

| Issues | Required treatment | Required regression |
| --- | --- | --- |
| [#22](https://github.com/redis-windows/redis-windows/issues/22), [#38](https://github.com/redis-windows/redis-windows/issues/38), [#60](https://github.com/redis-windows/redis-windows/issues/60), [#77](https://github.com/redis-windows/redis-windows/issues/77) | MSYS2 path adapter; service normally passes only an absolute config path; no blind `/cygdrive` rewrite | Install/restart from paths containing spaces, Chinese text, drive roots, and relative config references |
| [#53](https://github.com/redis-windows/redis-windows/issues/53), [#77](https://github.com/redis-windows/redis-windows/issues/77) | Normalize config paths, require/create approved log and data directories, set a deterministic working directory | Relative and absolute `logfile`/`dir`; missing parent; read-only parent; restart persistence |
| [#41](https://github.com/redis-windows/redis-windows/issues/41), [#42](https://github.com/redis-windows/redis-windows/issues/42), [#51](https://github.com/redis-windows/redis-windows/issues/51), [#52](https://github.com/redis-windows/redis-windows/issues/52), [#74](https://github.com/redis-windows/redis-windows/issues/74) | Foreground Redis child, readiness PING, startup failure propagation, child-exit propagation, bounded SCM recovery, diagnostic logs | Good config starts; invalid config/port/path fails SCM start; unexpected child exit changes service state; selected config is observable |
| [#36](https://github.com/redis-windows/redis-windows/issues/36), [#41](https://github.com/redis-windows/redis-windows/issues/41) | Graceful `SHUTDOWN` using host/port/ACL/TLS connection settings, wait for exit, then bounded process-tree fallback; never expose credentials in service arguments/logs | Stop after writes; no stale PID; RDB/AOF integrity; authenticated and TLS configurations; forced fallback is logged |
| [#34](https://github.com/redis-windows/redis-windows/issues/34) | Separate Sentinel mode, config, service name, data directory, and health check | Install/start/restart/uninstall Redis Sentinel; invalid Sentinel config fails service start |
| [#44](https://github.com/redis-windows/redis-windows/issues/44) | Document and enforce that `RedisService.exe`/PowerShell owns service registration; never advertise unsupported `redis-cli --service-install` | Help examples execute successfully; invalid legacy syntax has an actionable error |
| [#45](https://github.com/redis-windows/redis-windows/issues/45) | Preflight configured ports and surface the owning PID when permitted; never kill an unrelated process | Occupied port blocks installation/start without mutating or terminating the existing listener |
| [#63](https://github.com/redis-windows/redis-windows/issues/63) | Add PE VERSIONINFO to every native EXE and version the service wrapper from Redis version plus packaging revision | PowerShell version-resource assertions for every EXE and `--version` consistency |
| [#76](https://github.com/redis-windows/redis-windows/issues/76) | Add a WinGet manifest only after stable versioned ZIP/checksum pairs, non-overwriting publication, upgrade/uninstall behavior, and a signing policy exist | WinGet validation plus clean install, upgrade, repair, and uninstall in a fresh VM |

## Investigation required before claiming a fix

These reports can originate in Cygwin/MSYS2 POSIX emulation, Redis filesystem
assumptions, or load limits. Packaging can add tests and patches, but cannot
honestly close them in advance.

| Issues | Plan | Stable acceptance rule |
| --- | --- | --- |
| [#47](https://github.com/redis-windows/redis-windows/issues/47) | Reproduce BGSAVE temp-file rename and fsync behavior on NTFS for the MSYS2 backend; keep a minimal versioned patch only if required | Repeated BGSAVE produces a valid `dump.rdb`, no orphan temp files, restart reloads all data |
| [#27](https://github.com/redis-windows/redis-windows/issues/27), [#30](https://github.com/redis-windows/redis-windows/issues/30), [#54](https://github.com/redis-windows/redis-windows/issues/54) | Measure descriptor/socket limits and crashes at bounded connection counts in the MSYS2 runtime; publish a tested `maxclients` ceiling | No crash or corruption at the documented ceiling; exceeding it fails predictably; do not advertise Linux limits |
| [#48](https://github.com/redis-windows/redis-windows/issues/48), [#57](https://github.com/redis-windows/redis-windows/issues/57) | Replace `-O0` with a pinned optimized release build, benchmark the MSYS2 runtime, and store results per Redis series | No material regression from the previous package; publish numbers and environment, not a Linux-performance promise |

If a runtime defect remains, that artifact stays experimental.

## Explicitly separate scope

| Issues | Decision |
| --- | --- |
| [#26](https://github.com/redis-windows/redis-windows/issues/26), [#28](https://github.com/redis-windows/redis-windows/issues/28), [#58](https://github.com/redis-windows/redis-windows/issues/58), [#79](https://github.com/redis-windows/redis-windows/issues/79) | Redis 8 upstream bundles RedisJSON, Bloom, Search/Query, and related modules, while this repository's package contract is `BUILD_PROFILE=core`. A module-enabled profile requires separate toolchains, assets, compatibility tests, and license review; a core ZIP must not claim those commands are present. |
| [#39](https://github.com/redis-windows/redis-windows/issues/39), [#62](https://github.com/redis-windows/redis-windows/issues/62) | Valkey is a different upstream project and is outside this repository's Redis package scope. Any Valkey distribution requires separate source policy, names, releases, and notices. |
| [#75](https://github.com/redis-windows/redis-windows/issues/75) | Windows 7 is unsupported. Release notes must state the tested minimum Windows client/server versions derived from the pinned MSYS2 and service-wrapper runtimes. No package is labelled Win7-compatible without a dedicated supported toolchain and VM gate. |

## Status vocabulary

- **Planned**: design exists; no published fix is claimed.
- **Verified**: a reproducer failed on the reference build and passes on this
  project in CI.
- **Runtime limitation**: documented ceiling or incompatibility remains.
- **Separate scope**: belongs to a different edition/upstream and is not a core
  package defect.

Any stable Windows Release notes must link to this table and use these terms.
Experimental artifact summaries must also state the unverified gates. Neither
may claim that all `redis-windows` issues are fixed.
