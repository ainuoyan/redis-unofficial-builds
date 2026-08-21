#!/usr/bin/env bash
set -euo pipefail
umask 022

readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly REDIS_VERSION="${REDIS_VERSION:?REDIS_VERSION is required}"
readonly REDIS_SOURCE_SHA256="${REDIS_SOURCE_SHA256:?REDIS_SOURCE_SHA256 is required}"
readonly REDIS_HASHES_COMMIT="${REDIS_HASHES_COMMIT:?REDIS_HASHES_COMMIT is required}"
readonly REDIS_SOURCE_ARCHIVE="${REDIS_SOURCE_ARCHIVE:?REDIS_SOURCE_ARCHIVE is required}"
readonly PACKAGE_VARIANT="${PACKAGE_VARIANT:?PACKAGE_VARIANT is required}"
readonly PACKAGE_ARCH="${PACKAGE_ARCH:?PACKAGE_ARCH is required}"
readonly EXPECTED_MACHINE_ARCH="${EXPECTED_MACHINE_ARCH:?EXPECTED_MACHINE_ARCH is required}"
readonly BUILD_ENVIRONMENT="${BUILD_ENVIRONMENT:?BUILD_ENVIRONMENT is required}"
readonly PACKAGING_REVISION="${PACKAGING_REVISION:?PACKAGING_REVISION is required}"
readonly OUTPUT_DIR="${OUTPUT_DIR:-$PROJECT_ROOT/dist}"
readonly SERVICE_WRAPPER="${SERVICE_WRAPPER:-}"
readonly RUN_FULL_TESTS="${RUN_FULL_TESTS:-true}"
readonly SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"
export SOURCE_DATE_EPOCH

case "$PACKAGE_VARIANT:$PACKAGE_ARCH:$EXPECTED_MACHINE_ARCH" in
  linux-musl1.2:x64:x86_64|linux-musl1.2:arm64:aarch64|\
  macos12:x64:x86_64|macos12:arm64:arm64|\
  windows-msys2:x64:x86_64) ;;
  *)
    echo "Unsupported experimental platform identity." >&2
    exit 1
    ;;
esac

[[ "$REDIS_VERSION" =~ ^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$ ]] \
  || { echo "Invalid Redis version: $REDIS_VERSION" >&2; exit 1; }
[[ "$REDIS_SOURCE_SHA256" =~ ^[0-9a-f]{64}$ ]] \
  || { echo "Invalid Redis source SHA-256." >&2; exit 1; }
[[ "$REDIS_HASHES_COMMIT" =~ ^[0-9a-f]{40}$ ]] \
  || { echo "Invalid redis-hashes commit." >&2; exit 1; }
[[ "$PACKAGING_REVISION" =~ ^[0-9a-f]{40}$ ]] \
  || { echo "Invalid packaging revision." >&2; exit 1; }
[[ "$SOURCE_DATE_EPOCH" == 0 ]] \
  || { echo "SOURCE_DATE_EPOCH must be zero." >&2; exit 1; }
case "$RUN_FULL_TESTS" in true|false) ;; *) echo "Invalid RUN_FULL_TESTS value." >&2; exit 1 ;; esac

if (( EUID == 0 )); then
  echo "Refusing to execute upstream Redis code as root." >&2
  exit 1
fi

actual_arch="$(uname -m)"
[[ "$actual_arch" == "$EXPECTED_MACHINE_ARCH" ]] || {
  echo "Architecture mismatch: expected $EXPECTED_MACHINE_ARCH, got $actual_arch" >&2
  exit 1
}

source_archive="$({
  python3 - "$REDIS_SOURCE_ARCHIVE" <<'PY'
import os
import stat
import sys

path = os.path.realpath(sys.argv[1])
mode = os.lstat(path).st_mode
if not stat.S_ISREG(mode) or os.path.islink(path):
    raise SystemExit("source archive must be a regular non-symlink file")
print(path)
PY
})"

python3 - "$source_archive" "$REDIS_SOURCE_SHA256" "$REDIS_VERSION" <<'PY'
import hashlib
import pathlib
import sys
import tarfile

archive = pathlib.Path(sys.argv[1])
expected_digest = sys.argv[2]
version = sys.argv[3]
digest = hashlib.sha256()
with archive.open("rb") as handle:
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
if digest.hexdigest() != expected_digest:
    raise SystemExit("Redis source archive SHA-256 mismatch")

prefix = f"redis-{version}/"
with tarfile.open(archive, "r:gz") as source:
    members = source.getmembers()
    if not members or len(members) > 10000:
        raise SystemExit("Redis source archive has an invalid member count")
    for member in members:
        name = member.name
        canonical_name = name[:-1] if member.isdir() and name.endswith("/") else name
        if canonical_name == prefix[:-1] and member.isdir():
            continue
        if not canonical_name.startswith(prefix) or "\\" in canonical_name:
            raise SystemExit(f"unsafe Redis source member: {name!r}")
        relative = canonical_name[len(prefix):]
        if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
            raise SystemExit(f"noncanonical Redis source member: {name!r}")
        if not (member.isfile() or member.isdir()):
            raise SystemExit(f"unsupported Redis source member type: {name!r}")
PY

temp_parent="${TMPDIR:-/tmp}"
if [[ "$PACKAGE_VARIANT" == macos12 ]]; then
  temp_parent=/tmp
fi
if ! temp_parent="$(cd "$temp_parent" 2>/dev/null && pwd -P)"; then
  echo "Temporary directory is unavailable." >&2
  exit 1
fi
[[ -d "$temp_parent" && -w "$temp_parent" ]] || {
  echo "Temporary directory is not writable: $temp_parent" >&2
  exit 1
}
work_dir="$(mktemp -d "$temp_parent/redis-experimental.XXXXXX")"
server_pid=""
cleanup() {
  if [[ "$server_pid" =~ ^[1-9][0-9]*$ ]]; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  case "$work_dir" in
    "$temp_parent"/redis-experimental.*) rm -rf -- "$work_dir" ;;
    *) echo "Refusing to remove unexpected temporary path: $work_dir" >&2 ;;
  esac
}
trap cleanup EXIT INT TERM HUP

cp "$source_archive" "$work_dir/redis-${REDIS_VERSION}.tar.gz"
tar -xzf "$work_dir/redis-${REDIS_VERSION}.tar.gz" -C "$work_dir"
source_root="$work_dir/redis-${REDIS_VERSION}"
[[ -d "$source_root" && ! -L "$source_root" ]] || {
  echo "Redis source root is missing after extraction." >&2
  exit 1
}
cd "$source_root"

make_args=(BUILD_TLS=no)
if [[ "$PACKAGE_VARIANT" == windows-msys2 ]]; then
  python3 "$PROJECT_ROOT/scripts/experimental/prepare_windows_source.py" \
    --source-root "$source_root"
  make_args+=(MALLOC=libc "CFLAGS=-D__GNU_VISIBLE=1 -Wno-char-subscripts -O2 -fstack-protector-strong")
elif [[ "$PACKAGE_VARIANT" == macos12 ]]; then
  export MACOSX_DEPLOYMENT_TARGET=12.0
fi

jobs="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '2')"
[[ "$jobs" =~ ^[1-9][0-9]*$ ]] || jobs=2
if [[ -x scripts/build.sh ]]; then
  make -j"$jobs" build redis "${make_args[@]}"
  if [[ "$RUN_FULL_TESTS" == true ]]; then
    make test redis "${make_args[@]}"
  fi
else
  make -j"$jobs" "${make_args[@]}"
  if [[ "$RUN_FULL_TESTS" == true ]]; then
    [[ -f ./runtest && -x ./runtest && ! -L ./runtest ]] || {
      echo "Redis test runner must be a regular executable file." >&2
      exit 1
    }
    ./runtest --clients 1 --timeout 1200
  fi
fi

suffix=""
[[ "$PACKAGE_VARIANT" == windows-msys2 ]] && suffix=.exe
for binary_name in redis-server redis-cli redis-benchmark; do
  [[ -f "src/${binary_name}${suffix}" && ! -L "src/${binary_name}${suffix}" ]] || {
    echo "Build did not produce src/${binary_name}${suffix}." >&2
    exit 1
  }
done
"src/redis-server${suffix}" --version | grep -F "v=${REDIS_VERSION}" >/dev/null
"src/redis-cli${suffix}" --version | grep -F "$REDIS_VERSION" >/dev/null

smoke_port="$(python3 - <<'PY'
import socket
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
PY
)"
smoke_dir="$work_dir/smoke"
mkdir -p "$smoke_dir"
"src/redis-server${suffix}" \
  --bind 127.0.0.1 \
  --protected-mode yes \
  --port "$smoke_port" \
  --daemonize no \
  --logfile "$smoke_dir/redis.log" \
  --dir "$smoke_dir" \
  --save "" \
  --appendonly no &
server_pid=$!
ready=false
for _ in {1..30}; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    break
  fi
  if [[ "$("src/redis-cli${suffix}" -h 127.0.0.1 -p "$smoke_port" ping 2>/dev/null || true)" == PONG ]]; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  cat "$smoke_dir/redis.log" >&2 || true
  echo "Redis smoke test did not become ready." >&2
  exit 1
fi
"src/redis-cli${suffix}" -h 127.0.0.1 -p "$smoke_port" set redis-unofficial-build-test ok >/dev/null
[[ "$("src/redis-cli${suffix}" -h 127.0.0.1 -p "$smoke_port" get redis-unofficial-build-test)" == ok ]]
"src/redis-cli${suffix}" -h 127.0.0.1 -p "$smoke_port" shutdown nosave
wait "$server_pid"
server_pid=""

if [[ "$PACKAGE_VARIANT" == windows-msys2 ]]; then
  runtime_dir="$work_dir/windows-runtime"
  mkdir -p "$runtime_dir"
  for executable in src/redis-server.exe src/redis-cli.exe src/redis-benchmark.exe; do
    while IFS= read -r dependency; do
      [[ "$dependency" == /usr/bin/*.dll ]] || {
        echo "Unexpected Windows runtime dependency: $dependency" >&2
        exit 1
      }
      cp "$dependency" "$runtime_dir/"
    done < <(ldd "$executable" | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^\/usr\/bin\/.*\.dll$/) print $i }' | sort -u)
  done
  [[ -f "$runtime_dir/msys-2.0.dll" ]] || {
    echo "MSYS2 runtime dependency discovery did not find msys-2.0.dll." >&2
    exit 1
  }
  cp "$runtime_dir"/*.dll src/
  runtime_packages="$work_dir/windows-runtime-packages"
  runtime_mapping="$work_dir/windows-runtime-mapping"
  : >"$runtime_packages"
  : >"$runtime_mapping"
  for runtime_dll in "$runtime_dir"/*.dll; do
    runtime_package="$(pacman -Qqo "/usr/bin/${runtime_dll##*/}")"
    [[ "$runtime_package" =~ ^[A-Za-z0-9@._+-]+$ ]] || {
      echo "Unsafe MSYS2 runtime package name: $runtime_package" >&2
      exit 1
    }
    printf 'DLL=%s PACKAGE=%s\n' "${runtime_dll##*/}" "$runtime_package" >>"$runtime_mapping"
    printf '%s\n' "$runtime_package" >>"$runtime_packages"
  done
  LC_ALL=C sort "$runtime_mapping" -o "$runtime_mapping"
  LC_ALL=C sort -u "$runtime_packages" -o "$runtime_packages"
  runtime_notices="src/MSYS2-RUNTIME-NOTICES.txt"
  printf 'MSYS2_RUNTIME_NOTICES_FORMAT=1\n' >"$runtime_notices"
  cat "$runtime_mapping" >>"$runtime_notices"
  while IFS= read -r runtime_package; do
    [[ "$runtime_package" =~ ^[A-Za-z0-9@._+-]+$ ]] || {
      echo "Unsafe MSYS2 runtime package name: $runtime_package" >&2
      exit 1
    }
    package_record="$(pacman -Q "$runtime_package")"
    printf 'PACKAGE=%s\n' "$package_record" >>"$runtime_notices"
    license_root="/usr/share/licenses/${runtime_package}"
    license_files=()
    if [[ -d "$license_root" && ! -L "$license_root" ]]; then
      while IFS= read -r license_file; do
        license_files+=("$license_file")
      done < <(find -P "$license_root" -type f | LC_ALL=C sort)
    elif [[ "$runtime_package" == msys2-runtime ]]; then
      license_root=/usr/share/doc/Cygwin
      [[ -d "$license_root" && ! -L "$license_root" ]] || {
        echo "MSYS2 runtime package lacks its Cygwin license directory." >&2
        exit 1
      }
      license_files=("$license_root/COPYING" "$license_root/CYGWIN_LICENSE")
    else
      echo "MSYS2 runtime package lacks a license directory: $runtime_package" >&2
      exit 1
    fi
    ((${#license_files[@]} > 0)) || {
      echo "MSYS2 runtime package has no license files: $runtime_package" >&2
      exit 1
    }
    for license_file in "${license_files[@]}"; do
      [[ -f "$license_file" && ! -L "$license_file" ]] || {
        echo "Unsafe MSYS2 runtime license file: $license_file" >&2
        exit 1
      }
      license_size="$(wc -c <"$license_file")"
      (( license_size > 0 && license_size <= 1048576 )) || {
        echo "MSYS2 runtime license file violates the size limit: $license_file" >&2
        exit 1
      }
      printf '===== BEGIN %s (%s bytes) =====\n' "$license_file" "$license_size" >>"$runtime_notices"
      cat "$license_file" >>"$runtime_notices"
      printf '\n===== END %s =====\n' "$license_file" >>"$runtime_notices"
    done
  done <"$runtime_packages"
  runtime_notice_size="$(wc -c <"$runtime_notices")"
  (( runtime_notice_size > 0 && runtime_notice_size <= 10485760 )) || {
    echo "Generated MSYS2 runtime notices violate the size limit." >&2
    exit 1
  }
fi

compiler="$(cc --version | sed -n '1p')"
mkdir -p "$OUTPUT_DIR"
package_args=(
  --source-root "$source_root"
  --binary-dir "$source_root/src"
  --output-dir "$OUTPUT_DIR"
  --packaging-root "$PROJECT_ROOT"
  --redis-version "$REDIS_VERSION"
  --source-sha256 "$REDIS_SOURCE_SHA256"
  --hashes-commit "$REDIS_HASHES_COMMIT"
  --packaging-revision "$PACKAGING_REVISION"
  --variant "$PACKAGE_VARIANT"
  --arch "$PACKAGE_ARCH"
  --build-environment "$BUILD_ENVIRONMENT"
  --compiler "$compiler"
)
if [[ "$PACKAGE_VARIANT" == windows-msys2 ]]; then
  [[ -n "$SERVICE_WRAPPER" ]] || {
    echo "SERVICE_WRAPPER is required for Windows packages." >&2
    exit 1
  }
  package_args+=(--service-wrapper "$SERVICE_WRAPPER")
fi
python3 "$PROJECT_ROOT/scripts/experimental/create_portable_package.py" "${package_args[@]}"

archive="$OUTPUT_DIR/Redis-${REDIS_VERSION}-${PACKAGE_VARIANT}-${PACKAGE_ARCH}"
case "$PACKAGE_VARIANT" in
  windows-msys2) archive+=.zip ;;
  *) archive+=.tar.gz ;;
esac
python3 "$PROJECT_ROOT/scripts/experimental/validate_portable_asset.py" \
  --archive "$archive" \
  --checksum "${archive}.sha256" \
  --packaging-root "$PROJECT_ROOT" \
  --redis-version "$REDIS_VERSION" \
  --source-sha256 "$REDIS_SOURCE_SHA256" \
  --hashes-commit "$REDIS_HASHES_COMMIT" \
  --packaging-revision "$PACKAGING_REVISION" \
  --variant "$PACKAGE_VARIANT" \
  --arch "$PACKAGE_ARCH"
