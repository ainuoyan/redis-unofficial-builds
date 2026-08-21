#!/usr/bin/env bash
set -euo pipefail
umask 022

readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly OUTPUT_DIR="${OUTPUT_DIR:-${PROJECT_ROOT}/dist}"
readonly PACKAGE_ASSETS_ROOT="${PROJECT_ROOT}/packaging/linux"
readonly INSTALL_PREFIX="/usr/local/redis"
readonly REDIS_VERSION="${REDIS_VERSION:?REDIS_VERSION is required}"
readonly REDIS_SOURCE_SHA256="${REDIS_SOURCE_SHA256:?REDIS_SOURCE_SHA256 is required}"
readonly REDIS_HASHES_COMMIT="${REDIS_HASHES_COMMIT:?REDIS_HASHES_COMMIT is required}"
readonly EXPECTED_MACHINE_ARCH="${EXPECTED_MACHINE_ARCH:?EXPECTED_MACHINE_ARCH is required}"
readonly PACKAGE_ARCH="${PACKAGE_ARCH:?PACKAGE_ARCH is required}"
readonly PACKAGE_VARIANT="${PACKAGE_VARIANT:-linux-glibc2.28}"
readonly GLIBC_BASELINE="${GLIBC_BASELINE:-2.28}"
readonly BUILD_IMAGE="${BUILD_IMAGE:-unknown}"
readonly BUILD_WORKFLOW_PATH="${BUILD_WORKFLOW_PATH:-.github/workflows/build-linux.yml}"
readonly REQUESTED_PACKAGING_REVISION="${PACKAGING_REVISION:-}"
readonly REQUESTED_SOURCE_ARCHIVE="${REDIS_SOURCE_ARCHIVE:-}"
readonly SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-0}"
readonly SOURCE_ARCHIVE="redis-${REDIS_VERSION}.tar.gz"
readonly SOURCE_URL="https://download.redis.io/releases/${SOURCE_ARCHIVE}"
readonly PACKAGE_NAME="Redis-${REDIS_VERSION}-${PACKAGE_VARIANT}-${PACKAGE_ARCH}"
readonly MAX_DEPENDENCY_NOTICE_FILES=256
readonly MAX_DEPENDENCY_NOTICE_FILE_BYTES=1048576
readonly MAX_DEPENDENCY_NOTICE_SOURCE_BYTES=8388608
readonly MAX_DEPENDENCY_NOTICES_BYTES=10485760
readonly MAX_CONTRIBUTOR_LICENSE_BYTES=1048576
export SOURCE_DATE_EPOCH

if [[ ! "$REDIS_VERSION" =~ ^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$ ]]; then
  echo "Invalid Redis version: $REDIS_VERSION" >&2
  exit 1
fi

if [[ ! "$REDIS_SOURCE_SHA256" =~ ^[0-9a-f]{64}$ ]]; then
  echo "Invalid Redis source SHA256: $REDIS_SOURCE_SHA256" >&2
  exit 1
fi

if [[ ! "$REDIS_HASHES_COMMIT" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid Redis hashes snapshot Git OID: $REDIS_HASHES_COMMIT" >&2
  exit 1
fi

if [[ ! "$PACKAGE_VARIANT" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
  echo "Invalid package variant: $PACKAGE_VARIANT" >&2
  exit 1
fi

if [[ ! "$GLIBC_BASELINE" =~ ^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$ ]]; then
  echo "Invalid glibc baseline: $GLIBC_BASELINE" >&2
  exit 1
fi

case "$BUILD_WORKFLOW_PATH" in
  .github/workflows/build-linux.yml)
    [[ "$PACKAGE_VARIANT" == linux-glibc2.28 && "$GLIBC_BASELINE" == 2.28 ]] || {
      echo "The release workflow only accepts linux-glibc2.28 with glibc 2.28." >&2
      exit 1
    }
    package_status=""
    ;;
  .github/workflows/build-experimental.yml)
    [[ "$PACKAGE_VARIANT" == linux-glibc2.17-legacy && "$GLIBC_BASELINE" == 2.17 ]] || {
      echo "The experimental workflow only accepts the reviewed glibc 2.17 legacy identity." >&2
      exit 1
    }
    package_status="experimental"
    ;;
  *)
    echo "Unsupported build workflow path: $BUILD_WORKFLOW_PATH" >&2
    exit 1
    ;;
esac
if [[ ! -f "$PROJECT_ROOT/$BUILD_WORKFLOW_PATH" \
  || -L "$PROJECT_ROOT/$BUILD_WORKFLOW_PATH" ]]; then
  echo "Build workflow is missing or unsafe: $BUILD_WORKFLOW_PATH" >&2
  exit 1
fi

if [[ "$SOURCE_DATE_EPOCH" != 0 ]]; then
  echo "Invalid SOURCE_DATE_EPOCH: $SOURCE_DATE_EPOCH" >&2
  exit 1
fi

validate_build_info_value() {
  local name="$1"
  local value="$2"
  local LC_ALL=C
  if [[ -z "$value" || ${#value} -gt 512 \
    || "$value" == *$'\n'* || "$value" == *$'\r'* || "$value" == *$'\t'* \
    || "$value" =~ [^[:print:]] ]]; then
    echo "Invalid single-line BUILD-INFO value for $name." >&2
    exit 1
  fi
}

validate_build_info_value BUILD_IMAGE "$BUILD_IMAGE"
if [[ -n "${GITHUB_SHA:-}" && ! "${GITHUB_SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "GITHUB_SHA must be a full lowercase commit SHA when set." >&2
  exit 1
fi
if [[ -n "$REQUESTED_PACKAGING_REVISION" \
  && ! "$REQUESTED_PACKAGING_REVISION" =~ ^[0-9a-f]{40}$ ]]; then
  echo "PACKAGING_REVISION must be a full lowercase commit SHA when set." >&2
  exit 1
fi
if [[ -n "${GITHUB_SHA:-}" && -n "$REQUESTED_PACKAGING_REVISION" \
  && "$GITHUB_SHA" != "$REQUESTED_PACKAGING_REVISION" ]]; then
  echo "PACKAGING_REVISION does not match GITHUB_SHA." >&2
  exit 1
fi
if [[ "${GITHUB_ACTIONS:-}" == true ]]; then
  [[ -n "${GITHUB_SHA:-}" ]] || {
    echo "GITHUB_SHA is required in GitHub Actions." >&2
    exit 1
  }
  resolved_packaging_revision="$GITHUB_SHA"
else
  resolved_packaging_revision="${REQUESTED_PACKAGING_REVISION:-${GITHUB_SHA:-}}"
fi
if [[ ! "$resolved_packaging_revision" =~ ^[0-9a-f]{40}$ ]]; then
  echo "PACKAGING_REVISION is required to bind a local package to reviewed packaging source." >&2
  exit 1
fi
readonly RESOLVED_PACKAGING_REVISION="$resolved_packaging_revision"
for build_info_name in GITHUB_SERVER_URL GITHUB_REPOSITORY GITHUB_RUN_ID; do
  if [[ -n "${!build_info_name:-}" ]]; then
    validate_build_info_value "$build_info_name" "${!build_info_name}"
  fi
done

collect_upstream_dependency_notices() {
  local deps_root="$1"
  local output_path="$2"
  local candidate_list="$work_dir/dependency-notice-candidates"
  local candidate base_name lower_name relative_path metadata file_size links
  local notice_count=0 source_bytes=0 generated_bytes
  local LC_ALL=C
  local -a notice_paths=() notice_sizes=()

  if [[ ! -d "$deps_root" || -L "$deps_root" ]]; then
    echo "The Redis source archive does not contain a real deps directory." >&2
    return 1
  fi
  if ! find -P "$deps_root" -mindepth 1 -print0 \
    | LC_ALL=C sort -z >"$candidate_list"; then
    echo "Unable to enumerate upstream dependency notice candidates." >&2
    return 1
  fi

  while IFS= read -r -d '' candidate; do
    base_name="${candidate##*/}"
    lower_name="${base_name,,}"
    case "$lower_name" in
      license|license.*|license-*|license_*|\
      licence|licence.*|licence-*|licence_*|\
      copying|copying.*|copying-*|copying_*|\
      notice|notice.*|notice-*|notice_*|\
      copyright|copyright.*|copyright-*|copyright_*|\
      readme|readme.*|readme-*|readme_*) ;;
      *) continue ;;
    esac

    relative_path="$candidate"
    if [[ ${#relative_path} -gt 512 \
      || ! "$relative_path" =~ ^deps/[A-Za-z0-9._/@:+-]+$ ]]; then
      echo "Unsafe upstream dependency notice path: $relative_path" >&2
      return 1
    fi
    if [[ ! -f "$candidate" || -L "$candidate" ]]; then
      echo "Upstream dependency notice is not a regular non-symlink file: $relative_path" >&2
      return 1
    fi
    metadata="$(stat -c '%s %h' -- "$candidate")"
    read -r file_size links <<<"$metadata"
    if [[ ! "$file_size" =~ ^(0|[1-9][0-9]*)$ || "$links" != 1 \
      || "$file_size" -eq 0 \
      || "$file_size" -gt "$MAX_DEPENDENCY_NOTICE_FILE_BYTES" ]]; then
      echo "Upstream dependency notice violates the file size or link limit: $relative_path" >&2
      return 1
    fi
    if ! grep -Iq . -- "$candidate"; then
      echo "Upstream dependency notice is empty or not plain text: $relative_path" >&2
      return 1
    fi

    notice_count=$((notice_count + 1))
    source_bytes=$((source_bytes + file_size))
    if (( notice_count > MAX_DEPENDENCY_NOTICE_FILES \
      || source_bytes > MAX_DEPENDENCY_NOTICE_SOURCE_BYTES )); then
      echo "Upstream dependency notices exceed the collection limit." >&2
      return 1
    fi
    notice_paths+=("$relative_path")
    notice_sizes+=("$file_size")
  done <"$candidate_list"
  rm -f "$candidate_list"

  if (( notice_count == 0 )); then
    echo "No upstream dependency notices were found under deps/." >&2
    return 1
  fi

  {
    printf 'UPSTREAM_DEPENDENCY_NOTICES_FORMAT=1\n'
    printf 'REDIS_VERSION=%s\n' "$REDIS_VERSION"
    printf 'SOURCE_SUBTREE=deps\n\n'
    for ((candidate = 0; candidate < notice_count; candidate++)); do
      relative_path="${notice_paths[candidate]}"
      file_size="${notice_sizes[candidate]}"
      printf '===== BEGIN %s (%s bytes) =====\n' "$relative_path" "$file_size"
      cat -- "$relative_path"
      printf '\n===== END %s =====\n' "$relative_path"
    done
  } >"$output_path"
  chmod 0644 "$output_path"

  generated_bytes="$(stat -c '%s' -- "$output_path")"
  if [[ ! "$generated_bytes" =~ ^[1-9][0-9]*$ \
    || "$generated_bytes" -gt "$MAX_DEPENDENCY_NOTICES_BYTES" ]]; then
    echo "Generated upstream dependency notices exceed the package limit." >&2
    return 1
  fi
}

case "$PACKAGE_ARCH" in
  x64)
    package_machine_arch=x86_64
    expected_file_arch='x86-64'
    ;;
  arm64)
    package_machine_arch=aarch64
    expected_file_arch='ARM aarch64'
    ;;
  *)
    echo "Unsupported package architecture: $PACKAGE_ARCH" >&2
    exit 1
    ;;
esac

if [[ "$EXPECTED_MACHINE_ARCH" != "$package_machine_arch" ]]; then
  echo "Package architecture $PACKAGE_ARCH requires machine architecture $package_machine_arch." >&2
  exit 1
fi

if (( EUID == 0 )); then
  echo "Refusing to execute upstream Redis build or test code as root." >&2
  echo "Run this script as an unprivileged user in a disposable build environment." >&2
  exit 1
fi

resolved_output_dir="$(realpath -m -- "$OUTPUT_DIR")"
case "$resolved_output_dir/" in
  "$INSTALL_PREFIX"|"$INSTALL_PREFIX/"*)
    echo "OUTPUT_DIR must not be inside the live install prefix: $resolved_output_dir" >&2
    exit 1
    ;;
esac
if [[ -e "$INSTALL_PREFIX" || -L "$INSTALL_PREFIX" ]]; then
  echo "Refusing to run an upstream source build on a host with $INSTALL_PREFIX present." >&2
  echo "Use a disposable container or virtual machine for packaging." >&2
  exit 1
fi

work_dir="$(mktemp -d)"
test_dir="$work_dir/smoke"
smoke_pid=""
staging_root="$work_dir/staging"
package_root="$staging_root/redis"

cleanup() {
  if [[ "$smoke_pid" =~ ^[1-9][0-9]*$ ]]; then
    kill -- "$smoke_pid" 2>/dev/null || true
    wait "$smoke_pid" 2>/dev/null || true
  fi
  rm -rf "$work_dir"
}
trap cleanup EXIT

actual_arch="$(uname -m)"
if [[ "$actual_arch" != "$EXPECTED_MACHINE_ARCH" ]]; then
  echo "Architecture mismatch: expected $EXPECTED_MACHINE_ARCH, got $actual_arch" >&2
  exit 1
fi

mkdir -p "$resolved_output_dir"
cd "$work_dir"

if [[ -n "$REQUESTED_SOURCE_ARCHIVE" ]]; then
  requested_source_archive="$(realpath -e -- "$REQUESTED_SOURCE_ARCHIVE")"
  [[ -f "$requested_source_archive" && ! -L "$requested_source_archive" ]] || {
    echo "REDIS_SOURCE_ARCHIVE must name a regular non-symlink file." >&2
    exit 1
  }
  cp -- "$requested_source_archive" "$SOURCE_ARCHIVE"
else
  curl --fail --silent --show-error --location \
    --proto '=https' --proto-redir '=https' --tlsv1.2 --max-redirs 5 \
    --retry 3 --retry-connrefused --connect-timeout 20 --max-time 600 \
    "$SOURCE_URL" --output "$SOURCE_ARCHIVE"
fi

printf '%s  %s\n' "$REDIS_SOURCE_SHA256" "$SOURCE_ARCHIVE" | sha256sum --check --strict

tar --no-same-owner --no-same-permissions -xzf "$SOURCE_ARCHIVE"
cd "redis-${REDIS_VERSION}"

if [[ -x scripts/build.sh ]]; then
  # Redis 8.10+ builds bundled modules by default. This backend intentionally
  # publishes the stable core profile; a full profile needs a separate variant
  # and pinned Rust/LLVM/CMake dependency chain.
  make -j"$(nproc)" build redis BUILD_TLS=no
  make test redis BUILD_TLS=no
else
  make -j"$(nproc)" BUILD_TLS=no
  make BUILD_TLS=no test
fi

src/redis-server --version
src/redis-cli --version

install -d -m 0700 "$test_dir"
test_socket="$test_dir/redis.sock"

src/redis-server \
  --port 0 \
  --unixsocket "$test_socket" \
  --unixsocketperm 700 \
  --daemonize no \
  --logfile "$test_dir/redis.log" \
  --dir "$test_dir" \
  --save "" \
  --appendonly no &
smoke_pid=$!

for _ in {1..30}; do
  if ! kill -0 "$smoke_pid" 2>/dev/null; then
    wait "$smoke_pid" 2>/dev/null || true
    cat "$test_dir/redis.log" >&2
    echo "Redis smoke test failed: the server exited before becoming ready." >&2
    exit 1
  fi
  if [[ "$(src/redis-cli -s "$test_socket" ping 2>/dev/null || true)" == "PONG" ]]; then
    break
  fi
  sleep 1
done

if [[ "$(src/redis-cli -s "$test_socket" ping)" != "PONG" ]]; then
  cat "$test_dir/redis.log" >&2
  echo "Redis smoke test failed: PING did not return PONG." >&2
  exit 1
fi

src/redis-cli -s "$test_socket" set redis-unofficial-build-test ok >/dev/null
if [[ "$(src/redis-cli -s "$test_socket" get redis-unofficial-build-test)" != "ok" ]]; then
  echo "Redis smoke test failed: SET/GET mismatch." >&2
  exit 1
fi

src/redis-cli -s "$test_socket" shutdown nosave
if ! wait "$smoke_pid"; then
  cat "$test_dir/redis.log" >&2
  echo "Redis smoke test failed: the server did not shut down cleanly." >&2
  exit 1
fi
smoke_pid=""

# Install only into a private staging tree. Upstream build and test targets still
# execute source-controlled programs, so packaging belongs in a disposable build
# environment without a live /usr/local/redis installation or unrelated secrets.
install -d -m 0755 "$package_root"
make PREFIX="$package_root" BUILD_TLS=no install

install -d -m 0755 "$package_root/conf"
awk '
  /^[[:space:]]*loadmodule[[:space:]]/ {
    print "# Disabled by the redis-unofficial-builds core profile: " $0
    next
  }
  { print }
' redis.conf >"$package_root/conf/redis.conf"
chmod 0644 "$package_root/conf/redis.conf"
install -m 0644 sentinel.conf "$package_root/conf/sentinel.conf"
if [[ -f LICENSE.txt ]]; then
  upstream_license_file=LICENSE.txt
elif [[ -f COPYING ]]; then
  upstream_license_file=COPYING
else
  echo "The Redis source archive does not contain LICENSE.txt or COPYING." >&2
  exit 1
fi
install -m 0644 "$upstream_license_file" "$package_root/LICENSE.txt"
install -m 0644 "$PROJECT_ROOT/THIRD_PARTY_NOTICES.md" \
  "$package_root/THIRD_PARTY_NOTICES.md"

contributor_license_source=REDISCONTRIBUTIONS.txt
IFS=. read -r contributor_version_major contributor_version_minor _ \
  <<<"$REDIS_VERSION"
contributor_license_required=false
if (( contributor_version_major > 7 \
  || (contributor_version_major == 7 && contributor_version_minor >= 4) )); then
  contributor_license_required=true
fi
contributor_license_sha256=absent
if [[ -e "$contributor_license_source" || -L "$contributor_license_source" ]]; then
  if [[ ! -f "$contributor_license_source" || -L "$contributor_license_source" ]]; then
    echo "REDISCONTRIBUTIONS.txt is not a regular non-symlink file." >&2
    exit 1
  fi
  read -r contributor_license_bytes contributor_license_links <<<"$(
    stat -c '%s %h' -- "$contributor_license_source"
  )"
  if [[ ! "$contributor_license_bytes" =~ ^[1-9][0-9]*$ \
    || "$contributor_license_links" != 1 \
    || "$contributor_license_bytes" -gt "$MAX_CONTRIBUTOR_LICENSE_BYTES" ]] \
    || ! grep -Iq . -- "$contributor_license_source"; then
    echo "REDISCONTRIBUTIONS.txt violates the upstream contributor license contract." >&2
    exit 1
  fi
  install -m 0644 "$contributor_license_source" \
    "$package_root/UPSTREAM-CONTRIBUTOR-LICENSE.txt"
  contributor_license_sha256="$(
    sha256sum "$package_root/UPSTREAM-CONTRIBUTOR-LICENSE.txt" | awk '{print $1}'
  )"
elif [[ "$contributor_license_required" == true ]]; then
  echo "Redis $REDIS_VERSION requires REDISCONTRIBUTIONS.txt, but the official source archive does not contain it." >&2
  exit 1
fi

collect_upstream_dependency_notices \
  deps "$package_root/UPSTREAM-DEPENDENCY-NOTICES.txt"
dependency_notices_sha256="$(
  sha256sum "$package_root/UPSTREAM-DEPENDENCY-NOTICES.txt" | awk '{print $1}'
)"

for package_script in "$PACKAGE_ASSETS_ROOT"/scripts/*.sh; do
  bash -n "$package_script"
done
grep -Fqx '# Managed by redis-unofficial-builds' \
  "$PACKAGE_ASSETS_ROOT/systemd/redis.service"
grep -Eq '^ExecStart=/usr/local/redis/bin/redis-server .+ --daemonize no$' \
  "$PACKAGE_ASSETS_ROOT/systemd/redis.service"
if grep -Eq -- '--dir|--logfile' "$PACKAGE_ASSETS_ROOT/systemd/redis.service"; then
  echo "The base systemd unit must not override user data or log paths." >&2
  exit 1
fi

install -d -m 0755 "$package_root/scripts"
install -m 0755 \
  "$PACKAGE_ASSETS_ROOT/scripts/common.sh" \
  "$PACKAGE_ASSETS_ROOT/scripts/install.sh" \
  "$PACKAGE_ASSETS_ROOT/scripts/update.sh" \
  "$PACKAGE_ASSETS_ROOT/scripts/uninstall.sh" \
  "$package_root/scripts/"
install -d -m 0755 "$package_root/systemd"
for systemd_asset in "$PACKAGE_ASSETS_ROOT"/systemd/*; do
  install -m 0644 "$systemd_asset" "$package_root/systemd/"
done

for binary in "$package_root"/bin/redis-*; do
  if ! file -L "$binary" | grep -qE "ELF 64-bit.*${expected_file_arch}"; then
    echo "Unexpected binary format: $(file -L "$binary")" >&2
    exit 1
  fi

  if ldd "$binary" 2>&1 | grep -q 'not found'; then
    echo "Unresolved shared library dependency in $binary:" >&2
    ldd "$binary" >&2
    exit 1
  fi
done

max_glibc="$({
  for binary in "$package_root"/bin/redis-*; do
    readelf --version-info "$binary" 2>/dev/null \
      | grep -oE 'GLIBC_[0-9]+(\.[0-9]+)*' || true
  done
} | sort -Vu | tail -n 1)"

if [[ -z "$max_glibc" ]]; then
  echo "Unable to determine the required glibc symbol version." >&2
  exit 1
fi

max_glibc_number="${max_glibc#GLIBC_}"
if [[ ! "$max_glibc_number" =~ ^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$ ]]; then
  echo "Invalid required glibc symbol version: $max_glibc" >&2
  exit 1
fi
if [[ "$(printf '%s\n' "$max_glibc_number" "$GLIBC_BASELINE" | sort -V | tail -n 1)" != "$GLIBC_BASELINE" ]]; then
  echo "The package requires $max_glibc, which is newer than the GLIBC_${GLIBC_BASELINE} baseline." >&2
  exit 1
fi

redis_series="${REDIS_VERSION%.*}"
redis_major="${REDIS_VERSION%%.*}"
build_profile_description="core"
build_profile_description_zh="core"
if (( redis_major >= 8 )); then
  build_profile_description="core (bundled Redis 8 modules are not included)"
  build_profile_description_zh="core（不包含 Redis 8 捆绑模块）"
fi
patchset_sha256="$(
  cd "$PROJECT_ROOT"
  {
    printf '%s\0' \
      "$BUILD_WORKFLOW_PATH" \
      scripts/linux/build-redis.sh \
      THIRD_PARTY_NOTICES.md
    find packaging/linux -type f -print0
  } \
    | LC_ALL=C sort -z \
    | xargs -0 sha256sum \
    | sha256sum \
    | awk '{print $1}'
)"

{
  printf 'PACKAGE_FORMAT=2\n'
  if [[ -n "$package_status" ]]; then
    printf 'PACKAGE_STATUS=%s\n' "$package_status"
  fi
  cat <<EOF
PACKAGE_ID=redis-unofficial-builds
REDIS_VERSION=${REDIS_VERSION}
REDIS_SERIES=${redis_series}
BUILD_PROFILE=core
PACKAGE_VARIANT=${PACKAGE_VARIANT}
PACKAGE_ARCH=${PACKAGE_ARCH}
OS=linux
LIBC=glibc
MIN_GLIBC=${GLIBC_BASELINE}
MAX_GLIBC_SYMBOL=${max_glibc_number}
SERVICE_BACKEND=systemd
INSTALL_PREFIX=/usr/local/redis
UPSTREAM_SOURCE_SHA256=${REDIS_SOURCE_SHA256}
UPSTREAM_CONTRIBUTOR_LICENSE_SHA256=${contributor_license_sha256}
UPSTREAM_DEPENDENCY_NOTICES_SHA256=${dependency_notices_sha256}
PATCHSET_SHA256=${patchset_sha256}
EOF
} >"$package_root/PACKAGE-INFO"

{
  echo "Redis version: $REDIS_VERSION"
  if [[ -n "$package_status" ]]; then
    echo "Package status: experimental; GitHub Release publication is disabled"
  fi
  echo "Package variant: $PACKAGE_VARIANT"
  echo "Package architecture: $PACKAGE_ARCH"
  echo "Build profile: $build_profile_description"
  echo "Machine architecture: $actual_arch"
  echo "Install prefix: $INSTALL_PREFIX"
  echo "Build image: $BUILD_IMAGE"
  echo "Build OS: $(. /etc/os-release && printf '%s %s' "$NAME" "$VERSION_ID")"
  echo "Compiler: $(gcc --version | head -n 1)"
  echo "glibc: $(ldd --version | head -n 1)"
  if command -v rpm >/dev/null 2>&1; then
    echo "Selected build dependency packages:"
    for dependency_package in \
      binutils curl file findutils gcc gzip make procps-ng shadow-utils tar tcl \
      util-linux which devtoolset-10-binutils devtoolset-10-gcc; do
      if rpm -q "$dependency_package" >/dev/null 2>&1; then
        rpm -q "$dependency_package"
      fi
    done \
      | LC_ALL=C sort \
      | sed 's/^/  /'
  fi
  echo "Supported glibc baseline: $GLIBC_BASELINE"
  echo "Maximum required glibc symbol: $max_glibc"
  echo "TLS support: disabled"
  echo "Service installer: included"
  echo "Redis source: $SOURCE_URL"
  echo "Redis source SHA256: $REDIS_SOURCE_SHA256"
  echo "Redis hashes snapshot: $REDIS_HASHES_COMMIT"
  echo "Upstream license source file: $upstream_license_file"
  echo "Upstream contributor license SHA256: $contributor_license_sha256"
  echo "Upstream dependency notices SHA256: $dependency_notices_sha256"
  echo "Packaging patch-set SHA256: $patchset_sha256"
  echo "Packaging revision: $RESOLVED_PACKAGING_REVISION"
  if [[ -n "${GITHUB_SERVER_URL:-}" && -n "${GITHUB_REPOSITORY:-}" && -n "${GITHUB_RUN_ID:-}" ]]; then
    echo "GitHub Actions run: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
  fi
} >"$package_root/BUILD-INFO"

cat >"$package_root/README.txt" <<EOF
Redis ${REDIS_VERSION} unofficial Linux binary package
Redis ${REDIS_VERSION} 非官方 Linux 二进制安装包

$(if [[ -n "$package_status" ]]; then
  printf '%s\n' \
    'Status: experimental Actions artifact; GitHub Release publication is disabled' \
    '状态：实验性 Actions artifact；禁止上传到 GitHub Release'
fi)
Package variant: ${PACKAGE_VARIANT}
Package architecture: ${PACKAGE_ARCH}
Build profile: ${build_profile_description}
Install location: /usr/local/redis
Runtime requirement: Linux ${PACKAGE_ARCH}, glibc ${GLIBC_BASELINE} or newer
Service mode: systemd by default; install supports --no-service, update supports it only with --adopt, and uninstall supports either mode
No-service mode installs the complete package layout without registering or requiring systemd; stop every Redis process from /usr/local/redis manually before updating or uninstalling it
Compatibility scope: validate the target distribution and kernel before production use
Fresh-install default endpoint: Unix socket /usr/local/redis/data/redis.sock (TCP disabled)

安装包类型：${PACKAGE_VARIANT}
CPU 架构：${PACKAGE_ARCH}
构建配置：${build_profile_description_zh}
安装目录：/usr/local/redis
运行要求：Linux ${PACKAGE_ARCH}，glibc ${GLIBC_BASELINE} 或更高版本
服务模式：默认使用 systemd；install 支持 --no-service，update 仅在 --adopt 时支持，uninstall 支持两种模式
无服务模式会安装完整包布局，但不注册或要求 systemd；更新或卸载前必须手工停止所有来自 /usr/local/redis 的 Redis 进程
兼容范围：生产使用前仍须在目标发行版和内核上验证
全新安装默认端点：Unix 套接字 /usr/local/redis/data/redis.sock（TCP 已禁用）

New installation (extract into a root-owned, non-writable staging directory):
新安装（解压到 root 所有且不可写的暂存目录）：
  stage="\$(sudo mktemp -d /var/tmp/redis-unofficial-builds.XXXXXX)"
  sudo chmod 0755 "\$stage"
  sudo sh -c 'umask 022; exec tar --no-same-owner --no-same-permissions -xzf "\$1" -C "\$2"' sh \
    ${PACKAGE_NAME}.tar.gz "\$stage"
  sudo "\$stage/redis/scripts/install.sh"

Update an existing installation while preserving conf/ and data/:
更新现有安装并保留 conf/ 和 data/：
  sudo "\$stage/redis/scripts/update.sh"

The automatic update backup does not copy data/. Take a storage snapshot before
a production upgrade.
自动更新备份不会复制 data/；生产升级前请另外创建存储快照。

Adopt an existing unmanaged installation only after reviewing its configuration:
确认现有配置允许被接管后，迁移未由本项目管理的安装：
  sudo "\$stage/redis/scripts/update.sh" --adopt

Service commands:
服务命令：
  systemctl status redis.service
  journalctl -u redis.service
  sudo -u redis /usr/local/redis/bin/redis-cli -s /usr/local/redis/data/redis.sock PING

Uninstall but preserve configuration and data:
卸载程序但保留配置和数据：
  sudo /usr/local/redis/scripts/uninstall.sh

Use REDIS_INSTALL_LANG=en or REDIS_INSTALL_LANG=zh_CN to override the
installer language. Strict systemd hardening is provided as an optional
example under systemd/ and is not enabled automatically.

可使用 REDIS_INSTALL_LANG=en 或 REDIS_INSTALL_LANG=zh_CN 指定安装脚本语言。
严格的 systemd 加固配置位于 systemd/ 目录，仅作为可选示例，不会自动启用。

This build does not enable Redis TLS support.
Redis remains subject to the upstream license in LICENSE.txt.
此构建未启用 Redis TLS；Redis 仍遵循 LICENSE.txt 中的上游许可证。
EOF

package_path="${resolved_output_dir}/${PACKAGE_NAME}.tar.gz"
checksum_path="${package_path}.sha256"
temporary_package_path="$work_dir/${PACKAGE_NAME}.tar.gz"
temporary_checksum_path="${temporary_package_path}.sha256"

for output_path in "$package_path" "$checksum_path"; do
  if [[ -e "$output_path" || -L "$output_path" ]]; then
    echo "Refusing to overwrite an existing build output: $output_path" >&2
    exit 1
  fi
done

tar \
  --sort=name \
  --numeric-owner \
  --owner=0 \
  --group=0 \
  --mtime="@${SOURCE_DATE_EPOCH}" \
  --format=posix \
  --pax-option=delete=atime,delete=ctime \
  -cf - \
  -C "$staging_root" redis \
  | gzip -n >"$temporary_package_path"

(
  cd "$work_dir"
  sha256sum "${PACKAGE_NAME}.tar.gz" >"${PACKAGE_NAME}.tar.gz.sha256"
)

# Publish the checksum first and the archive last. A consumer never observes a
# newly published archive without its adjacent checksum.
install -m 0644 "$temporary_checksum_path" "$checksum_path"
install -m 0644 "$temporary_package_path" "$package_path"

tar -tzf "$package_path" | sed -n '1,80p'
(
  cd "$resolved_output_dir"
  sha256sum --check "${PACKAGE_NAME}.tar.gz.sha256"
)

echo "Created $package_path"
echo "Created $checksum_path"
