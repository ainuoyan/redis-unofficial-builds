# Third-party and upstream notices

This file identifies upstream material and design references used by
`redis-unofficial-builds`. It is informational and is not legal advice.
Redistributors remain responsible for determining and satisfying the license,
notice, source-offer, attribution, and trademark obligations applicable to
their chosen Redis version and use.

## Redis

Packages are built from versioned official Redis source archives after
verification against a SHA-256 entry from an immutable commit of the official
[`redis/redis-hashes`](https://github.com/redis/redis-hashes) repository.

- Source project: [`redis/redis`](https://github.com/redis/redis)
- Official source downloads: <https://download.redis.io/releases/>
- Official license summary: <https://redis.io/legal/licenses/>
- Official trademark policy: <https://redis.io/legal/trademark-policy/>

Redis license terms vary by release line:

| Redis version | Official license choices |
| --- | --- |
| 7.2.x and earlier | BSD-3-Clause |
| 7.4.x through 7.8.x | RSALv2 or SSPLv1 |
| 8 and later | RSALv2, SSPLv1, or AGPLv3 |

Every package includes the license file from its verified Redis source archive
as `LICENSE.txt`. Redis 7.4 and newer packages also include a byte-for-byte
copy of upstream `REDISCONTRIBUTIONS.txt` as
`UPSTREAM-CONTRIBUTOR-LICENSE.txt`. An older package includes that file only
when the corresponding upstream archive provides it; this project does not
generate replacement contributor-license text.

`UPSTREAM-DEPENDENCY-NOTICES.txt` deterministically collects recognized
license, licence, copying, notice, copyright, and README files from the
verified source tree's `deps/` directory. Entries are framed with upstream
relative paths and byte lengths. This bounded collection preserves relevant
upstream text but is not a legal classification and does not assert that every
transitive component or obligation has been identified.

Redis names and marks belong to their respective owner. The project name and
package metadata identify compatibility and upstream origin; they do not imply
affiliation, endorsement, or trademark ownership.

## redis-windows/redis-windows

The Windows backend design references
[`redis-windows/redis-windows`](https://github.com/redis-windows/redis-windows),
including its MSYS2/Cygwin build approach, runtime packaging, service wrapper,
and public issue reports. The review baseline is immutable commit
[`17fd667560f7903820dcabeebb9d20ade1159fe9`](https://github.com/redis-windows/redis-windows/commit/17fd667560f7903820dcabeebb9d20ade1159fe9).

That project is distributed under the Apache License 2.0; its license at the
reviewed commit is:
<https://github.com/redis-windows/redis-windows/blob/17fd667560f7903820dcabeebb9d20ade1159fe9/LICENSE>.

The current repository uses that codebase as a design and test reference. Its
MSYS2 approach and public issue reports informed the experimental backend, but
the Windows service wrapper and lifecycle scripts in this repository are an
independent implementation; no source file from the referenced project is
incorporated. Any later incorporation must record the exact source commit, retain
required copyright and attribution notices, include the Apache License 2.0
and applicable upstream `NOTICE` material, and identify modifications.

## Build environment

GitHub Actions, Rocky Linux, manylinux/musllinux images, MSYS2, .NET,
compilers, build tools, and system libraries used during compilation remain
subject to their own terms. Packages do not redistribute complete build
images. The Windows experiment does redistribute the runtime DLLs discovered
for its MSYS2 Redis executables and a self-contained .NET service wrapper; the
archive validator checks their bounded inventory and PE architecture, but the
current rolling MSYS2 package resolution is not claimed to be bit-for-bit
reproducible. The package records owning MSYS2 package versions and embeds the
license files installed for every copied runtime-DLL package as
`MSYS2-RUNTIME-NOTICES.txt`. This is not a substitute for a complete legal or
corresponding-source review; redistributors remain responsible for the
corresponding runtime notices and obligations. Other runtime shared-library
requirements are inspected during build/package validation, while the declared
platform baseline is recorded in package metadata; those libraries are normally
provided by the target operating system.
