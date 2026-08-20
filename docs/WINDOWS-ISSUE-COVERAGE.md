# Windows issue coverage

This document records how the Windows design uses reports from
[`redis-windows/redis-windows`](https://github.com/redis-windows/redis-windows).
It is both an engineering backlog and a guard against claiming that packaging
alone fixed an upstream/runtime defect.

Review baseline: upstream commit
[`17fd667`](https://github.com/redis-windows/redis-windows/commit/17fd667560f7903820dcabeebb9d20ade1159fe9)
and the open issue list observed on 2026-08-20.

## Findings from the referenced implementation

The current upstream workflow builds both MSYS2 and Cygwin, bundles their
runtime DLLs, and supplies a .NET Windows service wrapper. Those are useful
building blocks, but several implementation details map directly to reported
issues:

- The [build workflow](https://github.com/redis-windows/redis-windows/blob/main/.github/workflows/build-redis.yml)
  uses `-O0`, which is unsuitable for a performance release and is a likely
  contributor to performance reports.
- [RedisConfiguration.cs](https://github.com/redis-windows/redis-windows/blob/main/Service/RedisConfiguration.cs)
  converts Windows paths to `/cygdrive/...` without selecting the actual MSYS2
  or Cygwin backend. It also constructs shutdown CLI arguments from server
  configuration arguments instead of Redis connection arguments.
- [RedisProcessManager.cs](https://github.com/redis-windows/redis-windows/blob/main/Service/RedisProcessManager.cs)
  waits a fixed 100 ms rather than proving Redis is ready and falls back to
  process-tree termination when graceful shutdown does not work.
- [Program.cs](https://github.com/redis-windows/redis-windows/blob/main/Program.cs)
  logs a Redis child exit but does not fail/stop the Windows service host.
- [RedisService.csproj](https://github.com/redis-windows/redis-windows/blob/main/RedisService.csproj)
  now gives the wrapper a version, but native Redis executables still need PE
  VERSIONINFO.

These are code-reading conclusions, not assertions made by the referenced
project.

## Planned fixes and regression tests

The issues below can be addressed substantially by the packaging, service
wrapper, or documentation in this repository. They are only marked resolved
after the named regression test passes for both supported Windows artifacts
where applicable.

| Issues | Planned treatment | Required regression |
| --- | --- | --- |
| [#22](https://github.com/redis-windows/redis-windows/issues/22), [#38](https://github.com/redis-windows/redis-windows/issues/38), [#60](https://github.com/redis-windows/redis-windows/issues/60), [#77](https://github.com/redis-windows/redis-windows/issues/77) | Backend-specific MSYS2/Cygwin path adapter; service normally passes only an absolute config path; no blind `/cygdrive` rewrite | Install/restart from paths containing spaces, Chinese text, drive roots, relative config references, and both runtime variants |
| [#53](https://github.com/redis-windows/redis-windows/issues/53), [#77](https://github.com/redis-windows/redis-windows/issues/77) | Normalize config paths, require/create approved log and data directories, set a deterministic working directory | Relative and absolute `logfile`/`dir`; missing parent; read-only parent; restart persistence |
| [#41](https://github.com/redis-windows/redis-windows/issues/41), [#42](https://github.com/redis-windows/redis-windows/issues/42), [#51](https://github.com/redis-windows/redis-windows/issues/51), [#52](https://github.com/redis-windows/redis-windows/issues/52), [#74](https://github.com/redis-windows/redis-windows/issues/74) | Foreground Redis child, readiness PING, startup failure propagation, child-exit propagation, bounded SCM recovery, diagnostic logs | Good config starts; invalid config/port/path fails SCM start; unexpected child exit changes service state; selected config is observable |
| [#36](https://github.com/redis-windows/redis-windows/issues/36), [#41](https://github.com/redis-windows/redis-windows/issues/41) | Graceful `SHUTDOWN` using host/port/ACL/TLS connection settings, wait for exit, then bounded process-tree fallback; never expose credentials in service arguments/logs | Stop after writes; no stale PID; RDB/AOF integrity; authenticated and TLS configurations; forced fallback is logged |
| [#34](https://github.com/redis-windows/redis-windows/issues/34) | Separate Sentinel mode, config, service name, data directory, and health check | Install/start/restart/uninstall Redis Sentinel; invalid Sentinel config fails service start |
| [#44](https://github.com/redis-windows/redis-windows/issues/44) | Document and enforce that `RedisService.exe`/PowerShell owns service registration; never advertise unsupported `redis-cli --service-install` | Help examples execute successfully; invalid legacy syntax has an actionable error |
| [#45](https://github.com/redis-windows/redis-windows/issues/45) | Preflight configured ports and surface the owning PID when permitted; never kill an unrelated process | Occupied port blocks installation/start without mutating or terminating the existing listener |
| [#63](https://github.com/redis-windows/redis-windows/issues/63) | Add PE VERSIONINFO to every native EXE and version the service wrapper from Redis version plus packaging revision | PowerShell version-resource assertions for every EXE and `--version` consistency |
| [#76](https://github.com/redis-windows/redis-windows/issues/76) | Add a WinGet manifest only after stable immutable ZIPs, checksums, upgrade/uninstall behavior, and signing policy exist | WinGet validation plus clean install, upgrade, repair, and uninstall in a fresh VM |

## Investigation required before claiming a fix

These reports can originate in Cygwin/MSYS2 POSIX emulation, Redis filesystem
assumptions, or load limits. Packaging can add tests and patches, but cannot
honestly close them in advance.

| Issues | Plan | Stable acceptance rule |
| --- | --- | --- |
| [#47](https://github.com/redis-windows/redis-windows/issues/47) | Reproduce BGSAVE temp-file rename and fsync behavior on NTFS for both backends; keep a minimal versioned patch only if required | Repeated BGSAVE produces a valid `dump.rdb`, no orphan temp files, restart reloads all data |
| [#27](https://github.com/redis-windows/redis-windows/issues/27), [#30](https://github.com/redis-windows/redis-windows/issues/30), [#54](https://github.com/redis-windows/redis-windows/issues/54) | Measure descriptor/socket limits and crashes at bounded connection counts; compare MSYS2/Cygwin; publish a tested `maxclients` ceiling | No crash or corruption at the documented ceiling; exceeding it fails predictably; do not advertise Linux limits |
| [#48](https://github.com/redis-windows/redis-windows/issues/48), [#57](https://github.com/redis-windows/redis-windows/issues/57) | Replace `-O0` with a pinned optimized release build, benchmark both runtimes, and store results per Redis series | No material regression from the previous package; publish numbers and environment, not a Linux-performance promise |

If a runtime defect remains, that artifact stays experimental even if another
Windows backend passes.

## Explicitly separate scope

| Issues | Decision |
| --- | --- |
| [#26](https://github.com/redis-windows/redis-windows/issues/26), [#28](https://github.com/redis-windows/redis-windows/issues/28), [#58](https://github.com/redis-windows/redis-windows/issues/58), [#79](https://github.com/redis-windows/redis-windows/issues/79) | RedisJSON, Bloom, Search/Query, and related commands are a separate modules/Stack edition with separate builds, compatibility tests, and license review. A core Redis ZIP must not pretend those modules are present. |
| [#39](https://github.com/redis-windows/redis-windows/issues/39), [#62](https://github.com/redis-windows/redis-windows/issues/62) | Valkey is a different upstream project. It may use the same packaging framework later but must have separate source policy, names, releases, and notices. |
| [#75](https://github.com/redis-windows/redis-windows/issues/75) | Windows 7 is unsupported. The release notes will state the tested minimum Windows client/server versions derived from the pinned MSYS2/Cygwin and service-wrapper runtimes. No package is labelled Win7-compatible without a dedicated supported toolchain and VM gate. |

## Status vocabulary

- **Planned**: design exists; no published fix is claimed.
- **Verified**: a reproducer failed on the reference build and passes on this
  project in CI.
- **Runtime limitation**: documented ceiling or incompatibility remains.
- **Separate scope**: belongs to a different edition/upstream and is not a core
  package defect.

Release notes must link to this table and use these terms. They must not say
"all redis-windows issues fixed".
