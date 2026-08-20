# Third-party notices

## Redis

Redis source archives, binaries, sample configuration files, and their
licenses are provided by the Redis project. Every release archive produced by
this repository must include the exact upstream license files from the source
archive used for that build. This repository is not affiliated with or
endorsed by Redis Ltd.

- Project: <https://github.com/redis/redis>
- Official source hashes: <https://github.com/redis/redis-hashes>

## redis-windows/redis-windows

The Windows build and service-management design in this repository references
the open-source [`redis-windows/redis-windows`](https://github.com/redis-windows/redis-windows)
project, including its MSYS2/Cygwin build approach, runtime-DLL packaging, and
Windows service-wrapper experience. Its public issue tracker is also used as
input for Windows regression tests and compatibility decisions.

The referenced project is licensed under the Apache License 2.0:
<https://github.com/redis-windows/redis-windows/blob/main/LICENSE>.

No upstream source file is considered incorporated merely because the design
was studied. If a later implementation copies or modifies code from that
project, it must:

1. record the exact upstream commit in `packaging/windows/UPSTREAM.md`;
2. retain applicable copyright and attribution notices;
3. include a copy of the Apache License 2.0 and any upstream `NOTICE` file in
   both the source tree and affected binary archives; and
4. mark modified files as changed from the upstream version.
