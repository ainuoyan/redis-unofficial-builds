#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
readonly OUTPUT_DIR="${OUTPUT_DIR:-${PROJECT_ROOT}/dist}"
readonly INSTALL_PREFIX="/usr/local/redis"
readonly REDIS_VERSION="${REDIS_VERSION:?REDIS_VERSION is required}"
readonly REDIS_SOURCE_SHA256="${REDIS_SOURCE_SHA256:?REDIS_SOURCE_SHA256 is required}"
readonly EXPECTED_MACHINE_ARCH="${EXPECTED_MACHINE_ARCH:?EXPECTED_MACHINE_ARCH is required}"
readonly PACKAGE_ARCH="${PACKAGE_ARCH:?PACKAGE_ARCH is required}"
readonly PACKAGE_VARIANT="${PACKAGE_VARIANT:-linux-glibc2.28}"
readonly BUILD_IMAGE="${BUILD_IMAGE:-unknown}"
readonly SOURCE_ARCHIVE="redis-${REDIS_VERSION}.tar.gz"
readonly SOURCE_URL="https://download.redis.io/releases/${SOURCE_ARCHIVE}"
readonly PACKAGE_NAME="Redis-Rzon-${REDIS_VERSION}-${PACKAGE_VARIANT}-${PACKAGE_ARCH}"

if [[ ! "$PACKAGE_VARIANT" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
  echo "Invalid package variant: $PACKAGE_VARIANT" >&2
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

src/redis-cli -h 127.0.0.1 -p "$test_port" set rzon-build-test ok >/dev/null
if [[ "$(src/redis-cli -h 127.0.0.1 -p "$test_port" get rzon-build-test)" != "ok" ]]; then
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
if [[ "$(printf '%s\n' "$max_glibc_number" '2.28' | sort -V | tail -n 1)" != "2.28" ]]; then
  echo "The package requires $max_glibc, which is newer than GLIBC_2.28." >&2
  exit 1
fi

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
  echo "Maximum required glibc symbol: $max_glibc"
  echo "TLS support: disabled"
  echo "Redis source: $SOURCE_URL"
  echo "Redis source SHA256: $REDIS_SOURCE_SHA256"
  if [[ -n "${GITHUB_SERVER_URL:-}" && -n "${GITHUB_REPOSITORY:-}" && -n "${GITHUB_RUN_ID:-}" ]]; then
    echo "GitHub Actions run: ${GITHUB_SERVER_URL}/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"
  fi
} >"$INSTALL_PREFIX/BUILD-INFO"

cat >"$INSTALL_PREFIX/README.txt" <<EOF
Redis ${REDIS_VERSION} unofficial Linux binary package

Package variant: ${PACKAGE_VARIANT}
Package architecture: ${PACKAGE_ARCH}
Install location: /usr/local/redis
Start command:
  /usr/local/redis/bin/redis-server /usr/local/redis/conf/redis.conf

This build does not enable Redis TLS support.
Redis remains subject to the upstream license in LICENSE.txt.
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
