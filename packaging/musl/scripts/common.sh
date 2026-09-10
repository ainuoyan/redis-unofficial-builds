#!/usr/bin/env bash
set -Eeuo pipefail

PATH=/sbin:/usr/sbin:/bin:/usr/bin
export PATH
LC_ALL=C
LANG=C
export LC_ALL LANG
umask 077

readonly REDIS_PREFIX="/usr/local/redis"
readonly REDIS_BACKUP_ROOT="/usr/local/redis-backups"
readonly REDIS_USER="redis"
readonly REDIS_GROUP="redis"
readonly REDIS_SERVICE="redis-unofficial"
readonly REDIS_INIT_SCRIPT="/etc/init.d/redis-unofficial"
readonly REDIS_STATE_FILE="$REDIS_PREFIX/.redis-package-state"
readonly REDIS_SOCKET="$REDIS_PREFIX/data/redis.sock"
readonly REDIS_LOCK_FILE="/run/redis-unofficial.lifecycle.lock"

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
  if [[ -e "$REDIS_LOCK_FILE" && ( ! -f "$REDIS_LOCK_FILE" || -L "$REDIS_LOCK_FILE" ) ]]; then
    die "Lifecycle lock path is unsafe: $REDIS_LOCK_FILE"
  fi
  exec 9>"$REDIS_LOCK_FILE"
  chmod 0600 "$REDIS_LOCK_FILE"
  flock -n 9 || die "Another Redis lifecycle operation is running."
}

# flock is released by the shell when its descriptor closes on exit.
release_lock() { :; }

finish_lifecycle() {
  local status="$1" callback="$2"
  trap - EXIT INT TERM HUP
  # Isolate rollback exits/errors so lock cleanup always runs in the parent.
  set +e
  (set -Ee; "$callback" "$status")
  status=$?
  release_lock || status=1
  exit "$status"
}

assert_service_stopped() {
  local status attempt
  for ((attempt = 0; attempt < 30; attempt++)); do
    status=0
    pgrep -u "$REDIS_USER" >/dev/null 2>&1 || status=$?
    case "$status" in
      1) return 0 ;;
      0) sleep 1 ;;
      *) die "Unable to inspect Redis account processes; no files were removed." ;;
    esac
  done
  die "Redis account still has live processes; no files were removed."
}

stop_service() {
  if [[ -e "$REDIS_INIT_SCRIPT" ]]; then
    rc-service "$REDIS_SERVICE" stop || return 1
  fi
  assert_service_stopped
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

validate_package_tree_security() {
  local root="$1" path owner mode links mode_value metadata permissions
  while IFS= read -r -d '' path; do
    [[ ! -L "$path" ]] || die "Package tree contains a symbolic link: $path"
    if [[ -d "$path" ]]; then
      metadata="$(stat -c '%u %a' -- "$path")" \
        || die "Unable to inspect package directory: $path"
      read -r owner mode <<<"$metadata"
      links=1
    elif [[ -f "$path" ]]; then
      metadata="$(stat -c '%u %a %h' -- "$path")" \
        || die "Unable to inspect package file: $path"
      read -r owner mode links <<<"$metadata"
    else
      die "Package tree contains an unsupported file type: $path"
    fi
    [[ "$owner" == 0 && "$links" == 1 && "$mode" =~ ^[0-7]{3,4}$ ]] \
      || die "Package tree is not exclusively root controlled: $path"
    mode_value=$((8#$mode))
    (( (mode_value & 07022) == 0 )) \
      || die "Package tree contains a writable or special-mode path: $path"
    permissions="$(LC_ALL=C /bin/ls -ld -- "$path")" \
      || die "Unable to inspect package ACLs: $path"
    permissions="${permissions%% *}"
    [[ "${#permissions}" == 10 \
      || ( "${#permissions}" == 11 && "${permissions: -1}" == . ) ]] \
      || die "Package tree contains an extended ACL: $path"
  done < <(find "$root" -xdev -print0)
}

validate_package() {
  local root="$1" expected_arch machine package_arch package_status version binary_version account uid gid
  local validation_dir validation_binary ldd_output
  validate_package_tree_security "$root"
  [[ -f "$root/PACKAGE-INFO" && ! -L "$root/PACKAGE-INFO" ]] \
    || die "PACKAGE-INFO is missing or unsafe."
  package_status="$(metadata_value "$root/PACKAGE-INFO" PACKAGE_STATUS)"
  [[ "$package_status" == experimental || "$package_status" == release ]] \
    || die "The package has an unsupported publication status."
  [[ "$(metadata_value "$root/PACKAGE-INFO" PACKAGE_FORMAT)" == 3 \
    && "$(metadata_value "$root/PACKAGE-INFO" PACKAGE_ID)" == redis-unofficial-builds \
    && "$(metadata_value "$root/PACKAGE-INFO" PACKAGE_VARIANT)" == linux-musl1.2 \
    && "$(metadata_value "$root/PACKAGE-INFO" SERVICE_BACKEND)" == openrc \
    && "$(metadata_value "$root/PACKAGE-INFO" INSTALL_PREFIX)" == "$REDIS_PREFIX" ]] \
    || die "The package metadata does not match the musl/OpenRC contract."
  package_arch="$(metadata_value "$root/PACKAGE-INFO" PACKAGE_ARCH)"
  machine="$(uname -m)"
  case "$machine:$package_arch" in x86_64:x64|aarch64:arm64) ;; *) die "Package architecture does not match $machine." ;; esac
  [[ -x "$root/bin/redis-server" && ! -L "$root/bin/redis-server" \
    && -x "$root/bin/redis-cli" && ! -L "$root/bin/redis-cli" \
    && -f "$root/conf/redis.conf" && ! -L "$root/conf/redis.conf" \
    && -f "$root/openrc/redis" && ! -L "$root/openrc/redis" ]] \
    || die "The package is incomplete or contains unsafe lifecycle inputs."
  version="$(metadata_value "$root/PACKAGE-INFO" REDIS_VERSION)"
  [[ "$version" =~ ^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$ ]] \
    || die "The package declares an invalid Redis version."
  account="$(getent passwd "$REDIS_USER" || getent passwd nobody)"
  uid="$(awk -F: '{print $3}' <<<"$account")"
  gid="$(awk -F: '{print $4}' <<<"$account")"
  [[ "$uid" =~ ^[1-9][0-9]*$ && "$gid" =~ ^[1-9][0-9]*$ ]] \
    || die "No safe unprivileged account is available for binary validation."
  [[ -d /var/tmp && ! -L /var/tmp ]] || die "/var/tmp is missing or unsafe."
  validation_dir="$(mktemp -d /var/tmp/redis-unofficial-validate.XXXXXX)"
  [[ -d "$validation_dir" && ! -L "$validation_dir" ]] \
    || die "Unable to create a safe binary-validation directory."
  chmod 0755 "$validation_dir"
  validation_binary="$validation_dir/redis-server"
  install -o root -g root -m 0755 "$root/bin/redis-server" "$validation_binary"
  if ! binary_version="$(setpriv --reuid "$uid" --regid "$gid" --clear-groups --no-new-privs -- \
    env -i PATH="$PATH" "$validation_binary" --version 2>/dev/null \
    | sed -n 's/.* v=\([^ ]*\).*/\1/p')"; then
    rm -f -- "$validation_binary"
    rmdir -- "$validation_dir"
    die "The Redis binary could not be executed as an unprivileged account."
  fi
  ldd_output="$(setpriv --reuid "$uid" --regid "$gid" --clear-groups --no-new-privs -- \
    env -i PATH="$PATH" ldd "$validation_binary" 2>&1 || true)"
  rm -f -- "$validation_binary"
  rmdir -- "$validation_dir"
  [[ "$binary_version" == "$version" ]] || die "The package binary version does not match PACKAGE-INFO."
  if grep -Fq 'not found' <<<"$ldd_output"; then
    die "The Redis binary has unresolved runtime dependencies."
  fi
}

ensure_service_account() {
  if getent group "$REDIS_GROUP" >/dev/null; then
    [[ "$(getent group "$REDIS_GROUP" | awk -F: '{print $3}')" != 0 ]] \
      || die "Refusing to use a GID 0 Redis group."
  else
    addgroup -S "$REDIS_GROUP"
  fi
  if getent passwd "$REDIS_USER" >/dev/null; then
    local record
    record="$(getent passwd "$REDIS_USER")"
    [[ "$(awk -F: '{print $3}' <<<"$record")" != 0 \
      && "$(awk -F: '{print $4}' <<<"$record")" == "$(getent group "$REDIS_GROUP" | awk -F: '{print $3}')" \
      && "$(awk -F: '{print $7}' <<<"$record")" =~ /(nologin|false)$ ]] \
      || die "Existing redis account is not a compatible system account."
  else
    adduser -S -D -H -s /sbin/nologin -G "$REDIS_GROUP" "$REDIS_USER"
  fi
}

write_default_config() {
  local source="$1" destination="$2" temporary
  [[ ! -e "$destination" && ! -L "$destination" ]] \
    || die "Refusing to overwrite an existing Redis configuration: $destination"
  temporary="$(mktemp "${destination}.tmp.XXXXXX")"
  # Replace each managed directive at its first position and remove duplicates.
  # Missing defaults are added once; comments and unrelated settings stay intact.
  if ! LC_ALL=C awk '
    BEGIN {
      count = split("bind protected-mode port daemonize supervised dir logfile unixsocket unixsocketperm pidfile", keys, " ")
      defaults["bind"] = "127.0.0.1 -::1"
      defaults["protected-mode"] = "yes"
      defaults["port"] = "0"
      defaults["daemonize"] = "no"
      defaults["supervised"] = "no"
      defaults["dir"] = "/usr/local/redis/data"
      defaults["logfile"] = "/usr/local/redis/log/redis.log"
      defaults["unixsocket"] = "/usr/local/redis/data/redis.sock"
      defaults["unixsocketperm"] = "770"
      defaults["pidfile"] = "/run/redis-unofficial.pid"
    }
    /^[[:space:]]*#/ { print; next }
    {
      key = tolower($1)
      if (key ~ /^"[^"]*"$/ || key ~ /^\047[^\047]*\047$/)
        key = substr(key, 2, length(key) - 2)
      if (key in defaults) {
        if (!seen[key]++) print key " " defaults[key]
        next
      }
      print
    }
    END {
      for (i = 1; i <= count; i++)
        if (!seen[keys[i]]) print keys[i] " " defaults[keys[i]]
    }
  ' "$source" >"$temporary"; then
    rm -f -- "$temporary"
    return 1
  fi
  if ! install -m 0644 "$temporary" "$destination"; then
    rm -f -- "$temporary"
    return 1
  fi
  rm -f -- "$temporary"
}

write_state() {
  local version="$1" package_status="$2" temporary
  temporary="$REDIS_STATE_FILE.tmp.$$"
  {
    printf 'STATE_FORMAT=2\n'
    printf 'PACKAGE_ID=redis-unofficial-builds\n'
    printf 'PACKAGE_STATUS=%s\n' "$package_status"
    printf 'INSTALL_PREFIX=%s\n' "$REDIS_PREFIX"
    printf 'REDIS_VERSION=%s\n' "$version"
    printf 'PACKAGE_VARIANT=linux-musl1.2\n'
    printf 'SERVICE_MANAGER=openrc\n'
    printf 'SERVICE_ID=%s\n' "$REDIS_SERVICE"
  } >"$temporary"
  chmod 0600 "$temporary"
  chown root:root "$temporary"
  mv -f "$temporary" "$REDIS_STATE_FILE"
}

validate_state() {
  [[ -f "$REDIS_STATE_FILE" && ! -L "$REDIS_STATE_FILE" \
    && "$(stat -c '%u:%g:%a:%h' "$REDIS_STATE_FILE")" == 0:0:600:1 \
    && "$(metadata_value "$REDIS_STATE_FILE" STATE_FORMAT)" == 2 \
    && "$(metadata_value "$REDIS_STATE_FILE" PACKAGE_ID)" == redis-unofficial-builds \
    && "$(metadata_value "$REDIS_STATE_FILE" PACKAGE_STATUS)" =~ ^(experimental|release)$ \
    && "$(metadata_value "$REDIS_STATE_FILE" INSTALL_PREFIX)" == "$REDIS_PREFIX" \
    && "$(metadata_value "$REDIS_STATE_FILE" PACKAGE_VARIANT)" == linux-musl1.2 \
    && "$(metadata_value "$REDIS_STATE_FILE" SERVICE_MANAGER)" == openrc \
    && "$(metadata_value "$REDIS_STATE_FILE" SERVICE_ID)" == "$REDIS_SERVICE" ]] \
    || die "The existing installation state is missing or invalid."
}

install_program_files() {
  local root="$1" name
  install -d -o root -g root -m 0755 "$REDIS_PREFIX/bin" "$REDIS_PREFIX/scripts" "$REDIS_PREFIX/openrc"
  for name in redis-server redis-cli redis-benchmark redis-check-aof redis-check-rdb redis-sentinel; do
    install -m 0755 "$root/bin/$name" "$REDIS_PREFIX/bin/$name"
  done
  for name in common.sh install.sh update.sh uninstall.sh; do
    install -m 0755 "$root/scripts/$name" "$REDIS_PREFIX/scripts/$name"
  done
  install -m 0755 "$root/openrc/redis" "$REDIS_PREFIX/openrc/redis"
  install -m 0644 "$root/PACKAGE-INFO" "$root/BUILD-INFO" "$root/LICENSE.txt" \
    "$root/README.txt" "$root/THIRD_PARTY_NOTICES.md" \
    "$root/UPSTREAM-DEPENDENCY-NOTICES.txt" "$REDIS_PREFIX/"
  if [[ -f "$root/UPSTREAM-CONTRIBUTOR-LICENSE.txt" ]]; then
    install -m 0644 "$root/UPSTREAM-CONTRIBUTOR-LICENSE.txt" "$REDIS_PREFIX/"
  else
    rm -f "$REDIS_PREFIX/UPSTREAM-CONTRIBUTOR-LICENSE.txt"
  fi
}

probe_ready() (
  local root="$1" socket="${REDIS_READY_SOCKET:-$REDIS_SOCKET}" directory pid="" attempt
  [[ "$socket" == /* && "$socket" != *$'\n'* && "$socket" != *$'\r'* ]] || return 1
  directory="$(mktemp -d)" || return 1
  trap 'if [[ -n "$pid" ]]; then kill -KILL "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi; rm -f -- "$directory/response"; rmdir -- "$directory"' EXIT
  # Bound a stalled CLI independently of its connection/authentication behavior.
  env -u REDISCLI_AUTH "$root/bin/redis-cli" -s "$socket" ping >"$directory/response" 2>/dev/null &
  pid=$!
  for ((attempt = 0; attempt < 30; attempt++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" || true
      pid=""
      cat "$directory/response"
      return 0
    fi
    sleep 0.1
  done
  return 1
)

wait_ready() {
  local root="$1" response consecutive_ready=0 attempt
  for ((attempt = 0; attempt < 30; attempt++)); do
    response="$(probe_ready "$root" || true)"
    if [[ "$response" == PONG || "$response" == 'NOAUTH '* || "$response" == 'NOPERM '* ]]; then
      consecutive_ready=$((consecutive_ready + 1))
      (( consecutive_ready >= 2 )) && return 0
    else
      consecutive_ready=0
    fi
    sleep 1
  done
  return 1
}

refuse_nested_mounts() {
  local path listing unexpected
  listing="$(findmnt -rn -o TARGET 2>/dev/null)" \
    || die "Unable to inspect the system mount table."
  for path in "$@"; do
    [[ -e "$path" || -L "$path" ]] || continue
    [[ -d "$path" && ! -L "$path" ]] || die "Recursive removal target is unsafe: $path"
    unexpected="$(printf '%s\n' "$listing" \
      | awk -v target="$path" '$0 == target || index($0, target "/") == 1 { print; exit }')"
    [[ -z "$unexpected" ]] \
      || die "Refusing to remove a mount point or a directory containing one: $unexpected"
  done
}
