#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly OUTPUT_DIR="${OUTPUT_DIR:-${PROJECT_ROOT}/dist}"
readonly PACKAGE_ASSETS_ROOT="${PROJECT_ROOT}/packaging/linux"
readonly INSTALL_PREFIX="/usr/local/redis"
readonly REDIS_VERSION="${REDIS_VERSION:?REDIS_VERSION is required}"
readonly REDIS_SOURCE_SHA256="${REDIS_SOURCE_SHA256:?REDIS_SOURCE_SHA256 is required}"
readonly EXPECTED_MACHINE_ARCH="${EXPECTED_MACHINE_ARCH:?EXPECTED_MACHINE_ARCH is required}"
readonly PACKAGE_ARCH="${PACKAGE_ARCH:?PACKAGE_ARCH is required}"
readonly PACKAGE_VARIANT="${PACKAGE_VARIANT:-linux-glibc2.28}"
readonly GLIBC_BASELINE="${GLIBC_BASELINE:-2.28}"
readonly BUILD_IMAGE="${BUILD_IMAGE:-unknown}"
readonly SOURCE_ARCHIVE="redis-${REDIS_VERSION}.tar.gz"
readonly SOURCE_URL="https://download.redis.io/releases/${SOURCE_ARCHIVE}"
readonly PACKAGE_NAME="Redis-${REDIS_VERSION}-${PACKAGE_VARIANT}-${PACKAGE_ARCH}"

if [[ ! "$PACKAGE_VARIANT" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
  echo "Invalid package variant: $PACKAGE_VARIANT" >&2
  exit 1
fi

if [[ ! "$GLIBC_BASELINE" =~ ^[0-9]+\.[0-9]+$ ]]; then
  echo "Invalid glibc baseline: $GLIBC_BASELINE" >&2
  exit 1
fi

if [[ "$PACKAGE_ARCH" != "x64" && "$PACKAGE_ARCH" != "arm64" ]]; then
  echo "Unsupported package architecture: $PACKAGE_ARCH" >&2
  exit 1
fi

work_dir="$(mktemp -d)"
test_dir=""

cleanup() {
  if [[ -n "$test_dir" && -f "$test_dir/redis.pid" ]]; then
    kill "$(<"$test_dir/redis.pid")" 2>/dev/null || true
  fi
  if [[ -n "$test_dir" ]]; then
    rm -rf "$test_dir"
  fi
  rm -rf "$work_dir"
}
trap cleanup EXIT

actual_arch="$(uname -m)"
if [[ "$actual_arch" != "$EXPECTED_MACHINE_ARCH" ]]; then
  echo "Architecture mismatch: expected $EXPECTED_MACHINE_ARCH, got $actual_arch" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"
cd "$work_dir"

curl --fail --silent --show-error --location \
  --proto '=https' --tlsv1.2 \
  "$SOURCE_URL" --output "$SOURCE_ARCHIVE"

printf '%s  %s\n' "$REDIS_SOURCE_SHA256" "$SOURCE_ARCHIVE" | sha256sum --check --strict

tar -xzf "$SOURCE_ARCHIVE"
cd "redis-${REDIS_VERSION}"

make -j"$(nproc)"
make test

src/redis-server --version
src/redis-cli --version

test_dir="$(mktemp -d)"
test_port=16379

src/redis-server \
  --bind 127.0.0.1 \
  --protected-mode yes \
  --port "$test_port" \
  --daemonize yes \
  --pidfile "$test_dir/redis.pid" \
  --logfile "$test_dir/redis.log" \
  --dir "$test_dir" \
  --save "" \
  --appendonly no

for _ in {1..30}; do
  if [[ "$(src/redis-cli -h 127.0.0.1 -p "$test_port" ping 2>/dev/null || true)" == "PONG" ]]; then
    break
  fi
  sleep 1
done

if [[ "$(src/redis-cli -h 127.0.0.1 -p "$test_port" ping)" != "PONG" ]]; then
  cat "$test_dir/redis.log" >&2
  echo "Redis smoke test failed: PING did not return PONG." >&2
  exit 1
fi

src/redis-cli -h 127.0.0.1 -p "$test_port" set redis-unofficial-build-test ok >/dev/null
if [[ "$(src/redis-cli -h 127.0.0.1 -p "$test_port" get redis-unofficial-build-test)" != "ok" ]]; then
  echo "Redis smoke test failed: SET/GET mismatch." >&2
  exit 1
fi

src/redis-cli -h 127.0.0.1 -p "$test_port" shutdown nosave
rm -rf "$test_dir"
test_dir=""

# This runs inside a disposable build container, so the fixed-prefix cleanup
# cannot remove files from the GitHub runner host.
rm -rf "$INSTALL_PREFIX"
make PREFIX="$INSTALL_PREFIX" install

install -d -m 0755 "$INSTALL_PREFIX/conf"
install -m 0644 redis.conf "$INSTALL_PREFIX/conf/redis.conf"
install -m 0644 sentinel.conf "$INSTALL_PREFIX/conf/sentinel.conf"
install -m 0644 LICENSE.txt "$INSTALL_PREFIX/LICENSE.txt"

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

install -d -m 0755 "$INSTALL_PREFIX/scripts"
install -m 0755 \
  "$PACKAGE_ASSETS_ROOT/scripts/common.sh" \
  "$PACKAGE_ASSETS_ROOT/scripts/install.sh" \
  "$PACKAGE_ASSETS_ROOT/scripts/update.sh" \
  "$PACKAGE_ASSETS_ROOT/scripts/uninstall.sh" \
  "$INSTALL_PREFIX/scripts/"
install -d -m 0755 "$INSTALL_PREFIX/systemd"
for systemd_asset in "$PACKAGE_ASSETS_ROOT"/systemd/*; do
  install -m 0644 "$systemd_asset" "$INSTALL_PREFIX/systemd/"
done

for binary in "$INSTALL_PREFIX"/bin/redis-*; do
  if ! file -L "$binary" | grep -qE 'ELF 64-bit.*(x86-64|ARM aarch64)'; then
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
  for binary in "$INSTALL_PREFIX"/bin/redis-*; do
    readelf --version-info "$binary" 2>/dev/null \
      | grep -oE 'GLIBC_[0-9]+(\.[0-9]+)*' || true
  done
} | sort -Vu | tail -n 1)"

if [[ -z "$max_glibc" ]]; then
  echo "Unable to determine the required glibc symbol version." >&2
  exit 1
fi

max_glibc_number="${max_glibc#GLIBC_}"
if [[ "$(printf '%s\n' "$max_glibc_number" "$GLIBC_BASELINE" | sort -V | tail -n 1)" != "$GLIBC_BASELINE" ]]; then
  echo "The package requires $max_glibc, which is newer than the GLIBC_${GLIBC_BASELINE} baseline." >&2
  exit 1
fi

cat >"$INSTALL_PREFIX/PACKAGE-INFO" <<EOF
PACKAGE_FORMAT=1
PACKAGE_ID=redis-unofficial-builds
REDIS_VERSION=${REDIS_VERSION}
PACKAGE_VARIANT=${PACKAGE_VARIANT}
PACKAGE_ARCH=${PACKAGE_ARCH}
OS=linux
LIBC=glibc
MIN_GLIBC=${GLIBC_BASELINE}
MAX_GLIBC_SYMBOL=${max_glibc_number}
SERVICE_BACKEND=systemd
INSTALL_PREFIX=/usr/local/redis
EOF

{
  echo "Redis version: $REDIS_VERSION"
  echo "Package variant: $PACKAGE_VARIANT"
  echo "Package architecture: $PACKAGE_ARCH"
  echo "Machine architecture: $actual_arch"
  echo "Install prefix: $INSTALL_PREFIX"
  echo "Build image: $BUILD_IMAGE"
  echo "Build OS: $(. /etc/os-release && printf '%s %s' "$NAME" "$VERSION_ID")"
  echo "Compiler: $(gcc --version | head -n 1)"
  echo "glibc: $(ldd --version | head -n 1)"
  echo "Supported glibc baseline: $GLIBC_BASELINE"
  echo "Maximum required glibc symbol: $max_glibc"
  echo "TLS support: disabled"
  echo "Service installer: included"
  echo "Redis source: $SOURCE_URL"
  echo "Redis source SHA256: $REDIS_SOURCE_SHA256"
  if [[ -n "${GITHUB_SERVER_URL:-}" && -n "${GITHUB_REPOSITORY:-}" && -n "${GITHUB_RUN_ID:-}" ]]; then
    echo "GitHub Actions run: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
  fi
} >"$INSTALL_PREFIX/BUILD-INFO"

cat >"$INSTALL_PREFIX/README.txt" <<EOF
Redis ${REDIS_VERSION} unofficial Linux binary package
Redis ${REDIS_VERSION} 非官方 Linux 二进制安装包

Package variant: ${PACKAGE_VARIANT}
Package architecture: ${PACKAGE_ARCH}
Install location: /usr/local/redis
Compatibility: Linux ${PACKAGE_ARCH}, glibc ${GLIBC_BASELINE} or newer

安装包类型：${PACKAGE_VARIANT}
CPU 架构：${PACKAGE_ARCH}
安装目录：/usr/local/redis
兼容要求：Linux ${PACKAGE_ARCH}，glibc ${GLIBC_BASELINE} 或更高版本

New installation (run from an archive extracted to a temporary directory):
新安装（从解压到临时目录的安装包运行）：
  sudo ./redis/scripts/install.sh

Update an existing installation while preserving conf/ and data/:
更新现有安装并保留 conf/ 和 data/：
  sudo ./redis/scripts/update.sh

The automatic update backup does not copy data/. Take a storage snapshot before
a production upgrade.
自动更新备份不会复制 data/；生产升级前请另外创建存储快照。

Adopt an existing unmanaged installation only after reviewing its configuration:
确认现有配置允许被接管后，迁移未由本项目管理的安装：
  sudo ./redis/scripts/update.sh --adopt

Service commands:
服务命令：
  systemctl status redis.service
  journalctl -u redis.service

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

package_path="${OUTPUT_DIR}/${PACKAGE_NAME}.tar.gz"
checksum_path="${package_path}.sha256"

tar \
  --sort=name \
  --numeric-owner \
  --owner=0 \
  --group=0 \
  -czf "$package_path" \
  -C /usr/local redis

(
  cd "$OUTPUT_DIR"
  sha256sum "${PACKAGE_NAME}.tar.gz" >"${PACKAGE_NAME}.tar.gz.sha256"
)

tar -tzf "$package_path" | sed -n '1,80p'
(
  cd "$OUTPUT_DIR"
  sha256sum --check "${PACKAGE_NAME}.tar.gz.sha256"
)

echo "Created $package_path"
echo "Created $checksum_path"
