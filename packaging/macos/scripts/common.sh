#!/usr/bin/env bash
set -euo pipefail

PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
LC_ALL=C
LANG=C
export LC_ALL LANG
umask 077

readonly REDIS_PREFIX="/usr/local/redis"
readonly REDIS_BACKUP_ROOT="/usr/local/redis-backups"
readonly REDIS_USER="_redis_unofficial"
readonly REDIS_GROUP="_redis_unofficial"
readonly REDIS_LABEL="io.github.ainuoyan.redis-unofficial"
readonly REDIS_DOMAIN_LABEL="system/io.github.ainuoyan.redis-unofficial"
readonly REDIS_PLIST="/Library/LaunchDaemons/io.github.ainuoyan.redis-unofficial.plist"
readonly REDIS_STATE_FILE="$REDIS_PREFIX/.redis-package-state"
readonly REDIS_SOCKET="$REDIS_PREFIX/data/redis.sock"
readonly REDIS_LOCK_DIR="/var/run/redis-unofficial.lifecycle.lock"

info() { printf '[redis-package] %s\n' "$*"; }
die() { printf '[redis-package] ERROR: %s\n' "$*" >&2; exit 1; }

require_root() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "This operation requires root."
}

require_commands() {
  local name
  for name in "$@"; do
    command -v "$name" >/dev/null 2>&1 || die "Required command not found: $name"
  done
}

acquire_lock() {
  if ! mkdir -m 0700 "$REDIS_LOCK_DIR" 2>/dev/null; then
    die "Another Redis lifecycle operation is running, or the lock path is unsafe."
  fi
  trap 'rmdir "$REDIS_LOCK_DIR" 2>/dev/null || true' EXIT
}

metadata_value() {
  local file="$1" key="$2"
  awk -F= -v wanted="$key" '$1 == wanted { sub(/^[^=]*=/, ""); print; found++ } END { exit found == 1 ? 0 : 1 }' "$file"
}

version_less_than() {
  local left_major left_minor left_patch right_major right_minor right_patch
  IFS=. read -r left_major left_minor left_patch <<<"$1"
  IFS=. read -r right_major right_minor right_patch <<<"$2"
  (( left_major < right_major \
    || (left_major == right_major && left_minor < right_minor) \
    || (left_major == right_major && left_minor == right_minor && left_patch < right_patch) ))
}

package_root_from_script() {
  local script_dir root
  script_dir="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd -P)"
  root="$(cd "$script_dir/.." && pwd -P)"
  [[ "$root" != "$REDIS_PREFIX" && -d "$root" && ! -L "$root" ]] \
    || die "Package root is missing or points at the live installation."
  printf '%s\n' "$root"
}

validate_package() {
  local root="$1" machine package_arch version binary_version validation_dir validation_binary
  [[ -f "$root/PACKAGE-INFO" && ! -L "$root/PACKAGE-INFO" ]] \
    || die "PACKAGE-INFO is missing or unsafe."
  [[ "$(metadata_value "$root/PACKAGE-INFO" PACKAGE_FORMAT)" == 3 \
    && "$(metadata_value "$root/PACKAGE-INFO" PACKAGE_STATUS)" == experimental \
    && "$(metadata_value "$root/PACKAGE-INFO" PACKAGE_ID)" == redis-unofficial-builds \
    && "$(metadata_value "$root/PACKAGE-INFO" PACKAGE_VARIANT)" == macos12 \
    && "$(metadata_value "$root/PACKAGE-INFO" SERVICE_BACKEND)" == launchd \
    && "$(metadata_value "$root/PACKAGE-INFO" INSTALL_PREFIX)" == "$REDIS_PREFIX" ]] \
    || die "The package metadata does not match the macOS/launchd contract."
  package_arch="$(metadata_value "$root/PACKAGE-INFO" PACKAGE_ARCH)"
  machine="$(uname -m)"
  case "$machine:$package_arch" in x86_64:x64|arm64:arm64) ;; *) die "Package architecture does not match $machine." ;; esac
  [[ -x "$root/bin/redis-server" && ! -L "$root/bin/redis-server" \
    && -x "$root/bin/redis-cli" && ! -L "$root/bin/redis-cli" \
    && -f "$root/conf/redis.conf" && ! -L "$root/conf/redis.conf" \
    && -f "$root/launchd/io.github.ainuoyan.redis-unofficial.plist" && ! -L "$root/launchd/io.github.ainuoyan.redis-unofficial.plist" ]] \
    || die "The package is incomplete or contains unsafe lifecycle inputs."
  version="$(metadata_value "$root/PACKAGE-INFO" REDIS_VERSION)"
  [[ "$version" =~ ^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$ ]] \
    || die "The package declares an invalid Redis version."
  [[ -d /var/tmp && ! -L /var/tmp ]] || die "/var/tmp is missing or unsafe."
  validation_dir="$(mktemp -d /var/tmp/redis-unofficial-validate.XXXXXX)"
  [[ -d "$validation_dir" && ! -L "$validation_dir" ]] \
    || die "Unable to create a safe binary-validation directory."
  chmod 0755 "$validation_dir"
  validation_binary="$validation_dir/redis-server"
  install -o root -g wheel -m 0755 "$root/bin/redis-server" "$validation_binary"
  if ! binary_version="$(sudo -u nobody -H -- "$validation_binary" --version 2>/dev/null \
    | sed -n 's/.* v=\([^ ]*\).*/\1/p')"; then
    rm -f -- "$validation_binary"
    rmdir -- "$validation_dir"
    die "The Redis binary could not be executed as an unprivileged account."
  fi
  rm -f -- "$validation_binary"
  rmdir -- "$validation_dir"
  [[ "$binary_version" == "$version" ]] || die "The package binary version does not match PACKAGE-INFO."
  /usr/bin/plutil -lint "$root/launchd/io.github.ainuoyan.redis-unofficial.plist" >/dev/null \
    || die "The package launchd property list is invalid."
}

directory_service_value() {
  local node="$1" key="$2"
  dscl . -read "$node" "$key" 2>/dev/null | awk -v key="$key" '$1 == key ":" { print $2; found++ } END { exit found == 1 ? 0 : 1 }'
}

unused_directory_id() {
  local kind="$1" candidate node key
  for candidate in $(jot 50 450 499); do
    node="/Users"
    key="UniqueID"
    if [[ "$kind" == group ]]; then
      node="/Groups"
      key="PrimaryGroupID"
    fi
    if ! dscl . -search "$node" "$key" "$candidate" 2>/dev/null | grep -q .; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

ensure_service_account() {
  local gid uid
  if dscl . -read "/Groups/$REDIS_GROUP" >/dev/null 2>&1; then
    gid="$(directory_service_value "/Groups/$REDIS_GROUP" PrimaryGroupID)"
    [[ "$gid" =~ ^[1-9][0-9]*$ ]] || die "Existing Redis group is invalid."
  else
    gid="$(unused_directory_id group)" || die "No unused system GID is available."
    dscl . -create "/Groups/$REDIS_GROUP"
    dscl . -create "/Groups/$REDIS_GROUP" PrimaryGroupID "$gid"
    dscl . -create "/Groups/$REDIS_GROUP" RealName "Redis Unofficial service"
    dscl . -create "/Groups/$REDIS_GROUP" Password '*'
  fi
  if dscl . -read "/Users/$REDIS_USER" >/dev/null 2>&1; then
    uid="$(directory_service_value "/Users/$REDIS_USER" UniqueID)"
    [[ "$uid" =~ ^[1-9][0-9]*$ \
      && "$(directory_service_value "/Users/$REDIS_USER" PrimaryGroupID)" == "$gid" \
      && "$(directory_service_value "/Users/$REDIS_USER" UserShell)" == /usr/bin/false ]] \
      || die "Existing Redis account is not compatible."
  else
    uid="$(unused_directory_id user)" || die "No unused system UID is available."
    dscl . -create "/Users/$REDIS_USER"
    dscl . -create "/Users/$REDIS_USER" UniqueID "$uid"
    dscl . -create "/Users/$REDIS_USER" PrimaryGroupID "$gid"
    dscl . -create "/Users/$REDIS_USER" UserShell /usr/bin/false
    dscl . -create "/Users/$REDIS_USER" NFSHomeDirectory /var/empty
    dscl . -create "/Users/$REDIS_USER" RealName "Redis Unofficial service"
    dscl . -create "/Users/$REDIS_USER" Password '*'
  fi
}

write_default_config() {
  local source="$1" destination="$2"
  install -m 0644 "$source" "$destination"
  cat >>"$destination" <<'EOF'

# Managed experimental package defaults. Later records override upstream defaults.
bind 127.0.0.1 -::1
protected-mode yes
port 0
daemonize no
supervised no
dir /usr/local/redis/data
logfile /usr/local/redis/log/redis.log
unixsocket /usr/local/redis/data/redis.sock
unixsocketperm 770
pidfile /var/run/redis-unofficial.pid
EOF
}

write_state() {
  local version="$1" temporary
  temporary="$REDIS_STATE_FILE.tmp.$$"
  {
    printf 'STATE_FORMAT=1\n'
    printf 'PACKAGE_ID=redis-unofficial-builds\n'
    printf 'PACKAGE_STATUS=experimental\n'
    printf 'INSTALL_PREFIX=%s\n' "$REDIS_PREFIX"
    printf 'REDIS_VERSION=%s\n' "$version"
    printf 'PACKAGE_VARIANT=macos12\n'
    printf 'SERVICE_MANAGER=launchd\n'
  } >"$temporary"
  chmod 0600 "$temporary"
  chown root:wheel "$temporary"
  mv -f "$temporary" "$REDIS_STATE_FILE"
}

validate_state() {
  [[ -f "$REDIS_STATE_FILE" && ! -L "$REDIS_STATE_FILE" \
    && "$(stat -f '%u:%g:%Lp:%l' "$REDIS_STATE_FILE")" == 0:0:600:1 \
    && "$(metadata_value "$REDIS_STATE_FILE" STATE_FORMAT)" == 1 \
    && "$(metadata_value "$REDIS_STATE_FILE" PACKAGE_ID)" == redis-unofficial-builds \
    && "$(metadata_value "$REDIS_STATE_FILE" INSTALL_PREFIX)" == "$REDIS_PREFIX" \
    && "$(metadata_value "$REDIS_STATE_FILE" PACKAGE_VARIANT)" == macos12 \
    && "$(metadata_value "$REDIS_STATE_FILE" SERVICE_MANAGER)" == launchd ]] \
    || die "The existing installation state is missing or invalid."
}

install_program_files() {
  local root="$1" name
  install -d -o root -g wheel -m 0755 "$REDIS_PREFIX/bin" "$REDIS_PREFIX/scripts" "$REDIS_PREFIX/launchd"
  for name in redis-server redis-cli redis-benchmark redis-check-aof redis-check-rdb redis-sentinel; do
    install -m 0755 "$root/bin/$name" "$REDIS_PREFIX/bin/$name"
  done
  for name in common.sh install.sh update.sh uninstall.sh; do
    install -m 0755 "$root/scripts/$name" "$REDIS_PREFIX/scripts/$name"
  done
  install -m 0644 "$root/launchd/io.github.ainuoyan.redis-unofficial.plist" "$REDIS_PREFIX/launchd/"
  install -m 0644 "$root/PACKAGE-INFO" "$root/BUILD-INFO" "$root/LICENSE.txt" \
    "$root/README.txt" "$root/THIRD_PARTY_NOTICES.md" \
    "$root/UPSTREAM-DEPENDENCY-NOTICES.txt" "$REDIS_PREFIX/"
  if [[ -f "$root/UPSTREAM-CONTRIBUTOR-LICENSE.txt" ]]; then
    install -m 0644 "$root/UPSTREAM-CONTRIBUTOR-LICENSE.txt" "$REDIS_PREFIX/"
  else
    rm -f "$REDIS_PREFIX/UPSTREAM-CONTRIBUTOR-LICENSE.txt"
  fi
}

launchd_loaded() { launchctl print "$REDIS_DOMAIN_LABEL" >/dev/null 2>&1; }
stop_service() { launchd_loaded && launchctl bootout system "$REDIS_PLIST"; }
start_service() {
  launchctl enable "$REDIS_DOMAIN_LABEL"
  launchctl bootstrap system "$REDIS_PLIST"
  launchctl kickstart "$REDIS_DOMAIN_LABEL"
}

wait_ready() {
  local root="$1"
  for _ in $(jot 30); do
    [[ "$("$root/bin/redis-cli" -s "$REDIS_SOCKET" ping 2>/dev/null || true)" == PONG ]] && return 0
    sleep 1
  done
  return 1
}

refuse_nested_mounts() {
  local device unexpected
  device="$(stat -f %d "$REDIS_PREFIX")"
  unexpected="$(find "$REDIS_PREFIX" -type d -exec stat -f '%d %N' {} \; | awk -v expected="$device" '$1 != expected { print; exit }')"
  [[ -z "$unexpected" ]] || die "Refusing to remove an installation containing another filesystem: $unexpected"
}
