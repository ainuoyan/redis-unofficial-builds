#!/usr/bin/env python3
"""Create a deterministic cross-platform Redis package."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from portable_contract import (
    ContractError,
    EXPERIMENTAL_WORKFLOW,
    archive_name,
    backend_assets,
    packaged_asset_bytes,
    packaging_patchset_sha256,
    require_regular_file,
    validate_identity,
    validate_publication_contract,
    validate_single_line,
)


MAX_BINARY_BYTES = 128 * 1024 * 1024
MAX_NOTICE_FILES = 256
MAX_NOTICE_FILE_BYTES = 1024 * 1024
MAX_NOTICE_SOURCE_BYTES = 8 * 1024 * 1024
MAX_NOTICE_OUTPUT_BYTES = 10 * 1024 * 1024
MAX_CONTRIBUTOR_LICENSE_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 8 * 1024 * 1024
NOTICE_NAME_RE = re.compile(
    r"^(?:license|licence|copying|notice|copyright|readme)(?:[._-].*)?$",
    re.IGNORECASE,
)
WINDOWS_DLL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,126}\.dll$", re.I)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--binary-dir", type=Path, required=True)
    parser.add_argument("--service-wrapper", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--packaging-root", type=Path, required=True)
    parser.add_argument("--redis-version", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--hashes-commit", required=True)
    parser.add_argument("--packaging-revision", required=True)
    parser.add_argument("--variant", required=True)
    parser.add_argument("--arch", required=True)
    parser.add_argument("--build-environment", required=True)
    parser.add_argument("--compiler", required=True)
    parser.add_argument(
        "--package-status",
        choices=("experimental", "release"),
        default="experimental",
    )
    parser.add_argument(
        "--build-workflow",
        type=Path,
        default=EXPERIMENTAL_WORKFLOW,
    )
    return parser.parse_args()


def require_real_directory(path: Path, description: str) -> Path:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise ContractError(f"unable to inspect {description}: {path}") from exc
    if not stat.S_ISDIR(mode) or path.is_symlink():
        raise ContractError(f"{description} must be a real directory: {path}")
    return path


def copy_regular(source: Path, destination: Path, mode: int) -> None:
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise ContractError(f"unable to inspect package input: {source}") from exc
    if not stat.S_ISREG(metadata.st_mode) or source.is_symlink():
        raise ContractError(f"package input is not a regular file: {source}")
    if metadata.st_size <= 0 or metadata.st_size > MAX_BINARY_BYTES:
        raise ContractError(f"package input violates the size limit: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)


def collect_dependency_notices(source_root: Path, version: str) -> bytes:
    deps_root = require_real_directory(source_root / "deps", "Redis deps directory")
    selected: list[tuple[str, Path, int]] = []
    total = 0
    for current_root, directory_names, file_names in os.walk(
        deps_root, followlinks=False
    ):
        current = Path(current_root)
        for directory_name in directory_names:
            if (current / directory_name).is_symlink():
                raise ContractError("Redis deps contains a symlinked directory")
        for file_name in file_names:
            if NOTICE_NAME_RE.fullmatch(file_name) is None:
                continue
            candidate = current / file_name
            relative = candidate.relative_to(source_root).as_posix()
            if len(relative) > 512 or re.fullmatch(r"deps/[A-Za-z0-9._/@:+-]+", relative) is None:
                raise ContractError(f"unsafe dependency notice path: {relative}")
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode) or candidate.is_symlink():
                raise ContractError(f"dependency notice is not a regular file: {relative}")
            if metadata.st_size <= 0 or metadata.st_size > MAX_NOTICE_FILE_BYTES:
                raise ContractError(f"dependency notice violates the size limit: {relative}")
            total += metadata.st_size
            if total > MAX_NOTICE_SOURCE_BYTES:
                raise ContractError("dependency notice sources exceed the size limit")
            selected.append((relative, candidate, metadata.st_size))
            if len(selected) > MAX_NOTICE_FILES:
                raise ContractError("too many dependency notice sources")
    selected.sort(key=lambda item: item[0].encode("utf-8"))
    if not selected:
        raise ContractError("no dependency notices were found in the Redis source")
    output = bytearray(
        (
            "UPSTREAM_DEPENDENCY_NOTICES_FORMAT=1\n"
            f"REDIS_VERSION={version}\n"
            "SOURCE_SUBTREE=deps\n\n"
        ).encode("ascii")
    )
    for relative, candidate, size in selected:
        body = candidate.read_bytes()
        if len(body) != size or b"\x00" in body:
            raise ContractError(f"dependency notice is not stable plain text: {relative}")
        output.extend(f"===== BEGIN {relative} ({size} bytes) =====\n".encode("ascii"))
        output.extend(body)
        output.extend(f"\n===== END {relative} =====\n".encode("ascii"))
    if len(output) > MAX_NOTICE_OUTPUT_BYTES:
        raise ContractError("generated dependency notices exceed the size limit")
    return bytes(output)


def source_license(source_root: Path) -> Path:
    for name in ("LICENSE.txt", "COPYING"):
        candidate = source_root / name
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    raise ContractError("Redis source does not contain LICENSE.txt or COPYING")


def sanitize_config(source: Path) -> bytes:
    metadata = source.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or source.is_symlink()
        or metadata.st_size <= 0
        or metadata.st_size > MAX_CONFIG_BYTES
    ):
        raise ContractError(f"Redis configuration violates the size/type contract: {source}")
    text = source.read_text(encoding="utf-8")
    lines = []
    for line in text.splitlines():
        if re.match(r"^[ \t]*loadmodule[ \t]", line, re.IGNORECASE):
            lines.append(f"# Disabled by the core package profile: {line}")
        else:
            lines.append(line)
    return ("\n".join(lines) + "\n").encode("utf-8")


def copy_binaries(
    binary_dir: Path,
    package_root: Path,
    os_name: str,
    service_wrapper: Path | None,
) -> None:
    suffix = ".exe" if os_name == "windows" else ""
    for name in ("redis-server", "redis-cli", "redis-benchmark"):
        copy_regular(binary_dir / f"{name}{suffix}", package_root / "bin" / f"{name}{suffix}", 0o755)
    for alias in ("redis-check-aof", "redis-check-rdb", "redis-sentinel"):
        copy_regular(
            binary_dir / f"redis-server{suffix}",
            package_root / "bin" / f"{alias}{suffix}",
            0o755,
        )
    if os_name != "windows":
        if service_wrapper is not None:
            raise ContractError("service wrapper is valid only for Windows packages")
        return
    dlls = sorted(binary_dir.glob("*.dll"), key=lambda value: value.name.lower())
    if not dlls or not any(value.name.lower() == "msys-2.0.dll" for value in dlls):
        raise ContractError("MSYS2 package is missing msys-2.0.dll")
    for dll in dlls:
        if WINDOWS_DLL_RE.fullmatch(dll.name) is None:
            raise ContractError(f"unsafe Windows runtime DLL name: {dll.name}")
        copy_regular(dll, package_root / "bin" / dll.name, 0o755)
    if service_wrapper is None:
        raise ContractError("Windows package requires the reviewed service wrapper")
    copy_regular(service_wrapper, package_root / "bin/RedisService.exe", 0o755)
    copy_regular(
        binary_dir / "MSYS2-RUNTIME-NOTICES.txt",
        package_root / "MSYS2-RUNTIME-NOTICES.txt",
        0o644,
    )


def write_metadata(
    package_root: Path,
    *,
    args: argparse.Namespace,
    backend: dict[str, object],
    notices: bytes,
    contributor_digest: str,
    patchset_digest: str,
) -> None:
    version_parts = args.redis_version.split(".")
    package_info = {
        "PACKAGE_FORMAT": "3",
        "PACKAGE_STATUS": args.package_status,
        "PACKAGE_ID": "redis-unofficial-builds",
        "REDIS_VERSION": args.redis_version,
        "REDIS_SERIES": ".".join(version_parts[:2]),
        "BUILD_PROFILE": "core",
        "PACKAGE_VARIANT": args.variant,
        "PACKAGE_ARCH": args.arch,
        "OS": str(backend["os"]),
        "RUNTIME": str(backend["runtime"]),
        "RUNTIME_BASELINE": str(backend["runtime_baseline"]),
        "SERVICE_BACKEND": str(backend["service_backend"]),
        "INSTALL_PREFIX": str(backend["install_prefix"]),
        "UPSTREAM_SOURCE_SHA256": args.source_sha256,
        "UPSTREAM_CONTRIBUTOR_LICENSE_SHA256": contributor_digest,
        "UPSTREAM_DEPENDENCY_NOTICES_SHA256": hashlib.sha256(notices).hexdigest(),
        "PATCHSET_SHA256": patchset_digest,
    }
    package_root.joinpath("PACKAGE-INFO").write_text(
        "".join(f"{key}={value}\n" for key, value in package_info.items()),
        encoding="utf-8",
    )
    package_root.joinpath("PACKAGE-INFO").chmod(0o644)
    build_info = (
        f"Redis version: {args.redis_version}\n"
        f"Package variant: {args.variant}\n"
        f"Package architecture: {args.arch}\n"
        f"Package status: {args.package_status}\n"
        f"Build environment: {validate_single_line('build environment', args.build_environment)}\n"
        f"Compiler: {validate_single_line('compiler', args.compiler)}\n"
        f"Redis source SHA256: {args.source_sha256}\n"
        f"Redis hashes snapshot: {args.hashes_commit}\n"
        f"Packaging patch-set SHA256: {patchset_digest}\n"
        f"Packaging revision: {args.packaging_revision}\n"
    )
    package_root.joinpath("BUILD-INFO").write_text(build_info, encoding="utf-8")
    package_root.joinpath("BUILD-INFO").chmod(0o644)


def package_readme(args: argparse.Namespace, backend: dict[str, object]) -> str:
    package_archive = archive_name(args.redis_version, args.variant, args.arch)
    lifecycle = {
        "linux-musl1.2": f"""Host prerequisites: bash, OpenRC, getent, util-linux (flock, findmnt,
setpriv), procps (pgrep), tar, and standard POSIX account/file utilities. The default service
listens only on /usr/local/redis/data/redis.sock. Lifecycle scripts accept only a root-owned,
non-group/world-writable staging package tree.

Prepare:   stage="$(sudo mktemp -d /var/tmp/redis-unofficial.XXXXXX)"
           sudo tar -xzf {package_archive} -C "$stage"
Install:   sudo "$stage/redis/scripts/install.sh"
Update:    sudo "$stage/redis/scripts/update.sh"   (use a newly extracted package)
Uninstall: sudo /usr/local/redis/scripts/uninstall.sh
Purge:     sudo /usr/local/redis/scripts/uninstall.sh --purge

English is the default. Add --lang zh for Chinese, --lang en for English.
默认英文；添加 --lang zh 切换中文。中文需要 UTF-8 终端，显示异常时切回 --lang en。

主机前提：bash、OpenRC、getent、util-linux（flock、findmnt、setpriv）、procps（pgrep）、tar
及标准 POSIX 账号/文件工具。默认服务只监听
/usr/local/redis/data/redis.sock。生命周期脚本只接受 root 控制且不可写的暂存包目录。""",
        "macos15": f"""Host prerequisites: macOS 15 or newer and an Administrator account. The
default service listens only on /usr/local/redis/data/redis.sock. Lifecycle scripts accept only
a root-owned, non-group/world-writable staging package tree.

Prepare:   stage="$(sudo mktemp -d /private/var/tmp/redis-unofficial.XXXXXX)"
           sudo tar -xzf {package_archive} -C "$stage"
Install:   sudo "$stage/redis/scripts/install.sh"
Update:    sudo "$stage/redis/scripts/update.sh"   (use a newly extracted package)
Uninstall: sudo /usr/local/redis/scripts/uninstall.sh
Purge:     sudo /usr/local/redis/scripts/uninstall.sh --purge

English is the default. Add --lang zh for Chinese, --lang en for English.
默认英文；添加 --lang zh 切换中文。中文需要 UTF-8 终端，显示异常时切回 --lang en。

主机前提：macOS 15 或更高版本及管理员账号。默认服务只监听
/usr/local/redis/data/redis.sock。生命周期脚本只接受 root 控制且不可写的暂存包目录。""",
        "windows-msys2": fr"""Host prerequisites: x64 Windows and an elevated Windows PowerShell 5.1
or newer session. The default service endpoint is 127.0.0.1:6379. Lifecycle scripts accept only
an Administrator/SYSTEM-controlled staging tree without untrusted write access or reparse points.

Prepare:   $stage = Join-Path $env:ProgramFiles ('Redis-Unofficial-Staging-' + [Guid]::NewGuid().ToString('N'))
           New-Item -ItemType Directory -Path $stage | Out-Null
           icacls $stage /setowner '*S-1-5-32-544'
           icacls $stage /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'
           Expand-Archive -LiteralPath .\{package_archive} -DestinationPath $stage
           icacls $stage /setowner '*S-1-5-32-544' /T /C
           icacls $stage /inheritance:r /grant:r '*S-1-5-18:F' '*S-1-5-32-544:F' /T /C
Install:   & "$stage\redis\scripts\Install-Redis.ps1"
Update:    & "$stage\redis\scripts\Update-Redis.ps1"   (use a newly extracted package)
Uninstall: & 'C:\Program Files\Redis-Unofficial\scripts\Uninstall-Redis.ps1'
Purge:     & 'C:\Program Files\Redis-Unofficial\scripts\Uninstall-Redis.ps1' -Purge

For Explorer or cmd.exe, use Install-Redis.bat, Update-Redis.bat,
Uninstall-Redis.bat or Purge-Redis.bat in scripts (Run as administrator).
The window stays open to show the result. Purge asks for confirmation before
removing configuration, data and logs. The same protected staging rules apply.
资源管理器或 cmd.exe 可使用 scripts 中同名 .bat 入口（以管理员身份运行）。
Purge-Redis.bat 会先确认，再彻底删除配置、数据和日志。

English is the default. BAT: --lang zh / --lang en; PowerShell: -Lang zh / -Lang en.
默认英文。BAT 使用 --lang zh 切换中文，PowerShell 使用 -Lang zh；en 切回英文。
BAT stays ASCII; PowerShell scripts use UTF-8 with BOM. Do not save them as ANSI.
中文需要支持 UTF-8 和中文字形的终端。请保留 PowerShell 脚本的 UTF-8 BOM；不要另存为 ANSI。
Script output temporarily uses UTF-8 (also for paths in English messages), then restores the previous encoding.
Redirected Chinese output must be read as UTF-8; if display is unreadable, select English.

Direct start (no installation): scripts\Start-Redis.bat [--lang en|zh]
Run from a writable extracted package as a normal user. Uses the existing
conf\redis.conf with the package root as working directory. No configuration
is generated or overridden; data paths follow that file (dir ./ means package root).
Do not share data with another running Redis instance. Press Ctrl+C to stop.
免安装启动：运行 scripts\Start-Redis.bat，中文可加 --lang zh，无需管理员权限。
使用当前用户可写的解压目录，直接加载现有 conf\redis.conf；不生成或覆盖配置。
工作目录为包根目录，数据路径按配置执行；不创建 portable 目录。不要与其他运行中的
Redis 共用数据目录。前台运行时按 Ctrl+C 停止。

Edit only C:\Program Files\Redis-Unofficial\conf\redis.conf, then run:
Restart-Service -Name RedisUnofficial

The wrapper reads bind, port and requirepass directly from redis.conf. No
RedisService.json or separate password file is needed. A local non-loopback
bind address and a nondefault port are supported. Passwords containing spaces
must be quoted using Redis configuration syntax. Save as UTF-8 without BOM.
Readiness and graceful shutdown use REDISCLI_AUTH, not a password argument.
Stopping uses the settings captured at startup; restarting reads the edited
configuration. Runtime CONFIG SET/ACL changes are not tracked by the wrapper.

Use daemonize no, supervised no and a nonzero plain TCP port. Inline user ACLs,
aclfile, TLS and renamed PING/AUTH/SHUTDOWN commands are not supported by this
service wrapper; existing ACL deployments must not upgrade without a separate
access-control migration. Explicit include paths inside the installation
directory are supported (no globs or symbolic links); relative includes follow
Redis's working directory, including preceding dir directives. Graceful stop
waits up to 60 seconds after the shutdown command before process-tree fallback.
Update preserves conf/data, backs up legacy RedisService.json for rollback,
and removes it from the active installation after the new wrapper self-test.
Run Update-Redis.ps1 from the new package even if the Redis version is unchanged.

主机前提：x64 Windows，以及以管理员身份运行的 Windows PowerShell 5.1 或
更高版本。生命周期脚本只接受由 Administrators/SYSTEM 控制、普通用户不可写且不含
重解析点的暂存包目录。默认服务端点为 127.0.0.1:6379。只需修改
C:\Program Files\Redis-Unofficial\conf\redis.conf，再执行
Restart-Service -Name RedisUnofficial。包装器直接读取 bind、port、requirepass，
支持本机非回环 IP 和自定义端口，不再需要 RedisService.json 或独立密码文件。
带空格的密码按 Redis 语法加引号，配置保存为无 BOM 的 UTF-8。认证使用
REDISCLI_AUTH，不把密码放入命令行。停止使用启动时的配置，重启读取新配置；
不会跟踪运行时 CONFIG SET/ACL 修改。

服务要求 daemonize no、supervised no、非零普通 TCP 端口；不支持内联 user ACL、
aclfile、TLS 或重命名 PING/AUTH/SHUTDOWN。已有 ACL 部署需单独评估权限迁移，
不能直接升级或简单删除 ACL 规则。支持安装目录内的明确 include 路径，不支持
通配符或符号链接；相对路径按 Redis 当前工作目录解析，受前面的 dir 影响。
发送关闭命令后最多等待 60 秒，再终止进程树。更新保留 conf/data，备份旧 JSON
供失败回滚，并在新包装器自检成功后移除活动目录中的 JSON。即使 Redis 版本相同，
也应从新包运行 Update-Redis.ps1 更新包装器。""",
    }[args.variant]
    if args.variant in {"linux-musl1.2", "macos15"}:
        lifecycle += """

Same-version updates refresh programs/scripts and preserve conf/data. Stop errors
or remaining service-account processes block replacement/deletion. Readiness
accepts PONG/NOAUTH/NOPERM over the private Unix socket with bounded CLI probes.
Keep that socket enabled when adding TCP/authentication. For a changed socket path:
sudo env REDIS_READY_SOCKET=/absolute/path/to/redis.sock ./scripts/update.sh

同版本更新也刷新程序与脚本，保留配置和数据。停服失败或服务账户下仍有进程时，
不会继续替换或删除。就绪检查通过私有 socket 接受 PONG/NOAUTH/NOPERM，CLI 探测
有超时限制。开启 TCP/认证时保留控制 socket；更改其路径后按上述命令指定探测路径。
"""
    else:
        lifecycle += """

New services use LocalService. Update migrates legacy LocalSystem installations
without deleting the service; rollback restores the old account. Custom accounts
require a separate migration review. Recovery actions are applied on creation,
update and rollback. All same-version managed files are refreshed; conf/data stay.
Windows currently uses -O0 and does not run the complete upstream Redis test suite.
Protocol smoke and native service tests cover the declared package contract.

新服务使用 LocalService；更新旧 LocalSystem 安装时保留服务注册并迁移账户，
回滚恢复原账户。自定义账户需单独评估迁移。创建、更新及回滚都设置故障恢复。
同版本也刷新全部受管文件，保留配置和数据。Windows 当前使用 -O0，不运行完整
上游 Redis 测试套件，已声明包契约由协议冒烟与原生服务测试覆盖。
"""
    if args.package_status == "release":
        title = "unofficial release package"
        title_zh = "非官方正式发布安装包"
        publication = """This package is part of the numeric stable GitHub Release for this exact
Redis version. Its archive, checksum, release manifest, package-level SPDX
document, and GitHub attestations must be verified together before use.

此安装包属于该 Redis 精确版本的纯数字稳定 GitHub Release。使用前必须同时验证
压缩包、校验文件、Release 清单、发布包级 SPDX 文档和 GitHub 证明。"""
    else:
        title = "experimental unofficial package"
        title_zh = "实验性非官方安装包"
        publication = """This artifact is produced only by the manual experimental workflow. It may be
published only in a separately tagged GitHub prerelease after all seven
platform jobs for this exact Redis version and packaging revision pass and the
downloaded assets are revalidated. It is not eligible for the numeric stable
Release and does not claim production support.

此产物仅由手工实验构建工作流生成。只有同一 Redis 版本、同一打包提交的七个平台
Job 全部通过，且下载后的产物完成复验后，才能进入使用独立 Tag 的 GitHub 预发布。
它不具备纯数字稳定 Release 发布资格，也不代表生产支持。"""
    return f"""Redis {args.redis_version} {title}
Redis {args.redis_version} {title_zh}

Variant: {args.variant}
Architecture: {args.arch}
Runtime: {backend['runtime']} {backend['runtime_baseline']}
Service backend: {backend['service_backend']}
Install prefix: {backend['install_prefix']}

Before extraction, verify the adjacent .sha256 record against this archive.
解压前请先使用相邻的 .sha256 记录校验压缩包。

Packages created before the current runtime-identity cleanup are not in-place
upgrade compatible. Back up conf/ and data/, uninstall with the scripts from
the installed package, and then perform a fresh install.

当前运行身份清理之前创建的包不支持原地升级。请先备份 conf/ 与 data/，使用已安装
包内的脚本完成卸载，再执行全新安装。

{lifecycle}

{publication}

Review scripts/ and the platform service template before installation. Preserve
conf/ and data/ independently before every update or removal operation.
安装前请审查 scripts/ 与平台服务模板；每次更新或卸载前应另行备份 conf/ 和 data/。
"""


def normalized_entries(package_root: Path) -> list[tuple[str, Path, bool]]:
    entries: list[tuple[str, Path, bool]] = [("redis", package_root, True)]
    for current_root, directory_names, file_names in os.walk(
        package_root, followlinks=False
    ):
        current = Path(current_root)
        directory_names.sort()
        file_names.sort()
        for directory_name in directory_names:
            path = current / directory_name
            if path.is_symlink():
                raise ContractError("package staging tree contains a symlink")
            relative = path.relative_to(package_root).as_posix()
            entries.append((f"redis/{relative}", path, True))
        for file_name in file_names:
            path = current / file_name
            if path.is_symlink() or not path.is_file():
                raise ContractError("package staging tree contains a special file")
            relative = path.relative_to(package_root).as_posix()
            entries.append((f"redis/{relative}", path, False))
    return sorted(entries, key=lambda item: item[0].encode("utf-8"))


def write_tar_gz(package_root: Path, output: Path) -> None:
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                for name, source, is_directory in normalized_entries(package_root):
                    info = tarfile.TarInfo(name)
                    info.uid = 0
                    info.gid = 0
                    info.uname = ""
                    info.gname = ""
                    info.mtime = 0
                    if is_directory:
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        archive.addfile(info)
                    else:
                        info.mode = source.stat().st_mode & 0o777
                        info.size = source.stat().st_size
                        with source.open("rb") as handle:
                            archive.addfile(info, handle)


def write_zip(package_root: Path, output: Path) -> None:
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, source, is_directory in normalized_entries(package_root):
            member_name = f"{name}/" if is_directory else name
            info = zipfile.ZipInfo(member_name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            mode = 0o755 if is_directory else source.stat().st_mode & 0o777
            info.external_attr = ((stat.S_IFDIR if is_directory else stat.S_IFREG) | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, b"" if is_directory else source.read_bytes())


def copy_exclusive(source: Path, destination: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(destination, flags, 0o644)
    try:
        with source.open("rb") as input_handle, os.fdopen(descriptor, "wb") as output_handle:
            descriptor = -1
            shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        destination.unlink(missing_ok=True)
        raise


def main() -> int:
    args = parse_args()
    try:
        backend = validate_identity(args.redis_version, args.variant, args.arch)
        build_workflow = validate_publication_contract(
            args.package_status, args.build_workflow
        )
        if re.fullmatch(r"[0-9a-f]{64}", args.source_sha256) is None:
            raise ContractError("invalid Redis source SHA-256")
        if re.fullmatch(r"[0-9a-f]{40}", args.hashes_commit) is None:
            raise ContractError("invalid redis-hashes commit")
        if re.fullmatch(r"[0-9a-f]{40}", args.packaging_revision) is None:
            raise ContractError("invalid packaging revision")
        source_root = require_real_directory(args.source_root.resolve(), "Redis source root")
        binary_dir = require_real_directory(args.binary_dir.resolve(), "binary directory")
        packaging_root = require_real_directory(args.packaging_root.resolve(), "packaging root")
        output_dir = args.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        name = archive_name(args.redis_version, args.variant, args.arch)
        output = output_dir / name
        checksum = output_dir / f"{name}.sha256"
        if output.exists() or output.is_symlink() or checksum.exists() or checksum.is_symlink():
            raise ContractError("refusing to overwrite an existing package output")

        with tempfile.TemporaryDirectory(prefix="redis-portable-package-") as directory:
            package_root = Path(directory) / "redis"
            package_root.mkdir(mode=0o755)
            copy_binaries(binary_dir, package_root, str(backend["os"]), args.service_wrapper)
            package_root.joinpath("conf").mkdir(mode=0o755)
            package_root.joinpath("conf/redis.conf").write_bytes(
                sanitize_config(source_root / "redis.conf")
            )
            package_root.joinpath("conf/sentinel.conf").write_bytes(
                sanitize_config(source_root / "sentinel.conf")
            )
            package_root.joinpath("conf/redis.conf").chmod(0o644)
            package_root.joinpath("conf/sentinel.conf").chmod(0o644)
            copy_regular(source_license(source_root), package_root / "LICENSE.txt", 0o644)
            copy_regular(
                packaging_root / "THIRD_PARTY_NOTICES.md",
                package_root / "THIRD_PARTY_NOTICES.md",
                0o644,
            )

            notices = collect_dependency_notices(source_root, args.redis_version)
            package_root.joinpath("UPSTREAM-DEPENDENCY-NOTICES.txt").write_bytes(notices)
            package_root.joinpath("UPSTREAM-DEPENDENCY-NOTICES.txt").chmod(0o644)
            contributor = source_root / "REDISCONTRIBUTIONS.txt"
            contributor_required = tuple(map(int, args.redis_version.split("."))) >= (7, 4, 0)
            contributor_digest = "absent"
            if contributor.exists() or contributor.is_symlink():
                if contributor.is_symlink() or not contributor.is_file():
                    raise ContractError("REDISCONTRIBUTIONS.txt is not a regular file")
                body = contributor.read_bytes()
                if not body or len(body) > MAX_CONTRIBUTOR_LICENSE_BYTES or b"\x00" in body:
                    raise ContractError("REDISCONTRIBUTIONS.txt violates the package contract")
                package_root.joinpath("UPSTREAM-CONTRIBUTOR-LICENSE.txt").write_bytes(body)
                package_root.joinpath("UPSTREAM-CONTRIBUTOR-LICENSE.txt").chmod(0o644)
                contributor_digest = hashlib.sha256(body).hexdigest()
            elif contributor_required:
                raise ContractError("Redis 7.4 or newer requires REDISCONTRIBUTIONS.txt")

            asset_root = Path(str(backend["asset_root"]))
            for relative, mode in backend_assets(args.variant).items():
                source = require_regular_file(packaging_root, asset_root / relative)
                copy_regular(source, package_root / relative, mode)
                if args.variant == "windows-msys2" and relative.endswith((".bat", ".ps1")):
                    # cmd.exe stays ASCII; Windows PowerShell 5.1 needs a BOM
                    # to distinguish UTF-8 Chinese source from the system ANSI page.
                    (package_root / relative).write_bytes(
                        packaged_asset_bytes(source, args.variant, relative)
                    )
            package_root.joinpath("README.txt").write_text(
                package_readme(args, backend), encoding="utf-8"
            )
            package_root.joinpath("README.txt").chmod(0o644)
            patchset_digest = packaging_patchset_sha256(
                packaging_root, args.variant, build_workflow
            )
            write_metadata(
                package_root,
                args=args,
                backend=backend,
                notices=notices,
                contributor_digest=contributor_digest,
                patchset_digest=patchset_digest,
            )

            temporary_output = Path(directory) / name
            if backend["extension"] == "zip":
                write_zip(package_root, temporary_output)
            else:
                write_tar_gz(package_root, temporary_output)
            digest = hashlib.sha256(temporary_output.read_bytes()).hexdigest()
            temporary_checksum = Path(directory) / f"{name}.sha256"
            temporary_checksum.write_text(f"{digest}  {name}\n", encoding="ascii")
            created_outputs: list[Path] = []
            try:
                copy_exclusive(temporary_output, output)
                created_outputs.append(output)
                copy_exclusive(temporary_checksum, checksum)
                created_outputs.append(checksum)
            except BaseException:
                for created_output in created_outputs:
                    created_output.unlink(missing_ok=True)
                raise
        print(f"Created {args.package_status} package: {output}")
        return 0
    except (ContractError, OSError, UnicodeError, ValueError, tarfile.TarError) as exc:
        print(f"portable package error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
