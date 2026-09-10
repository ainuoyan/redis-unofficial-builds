#!/bin/bash -p
set -Eeuo pipefail

PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
unset CDPATH ENV BASH_ENV

bootstrap_fail() {
  printf '[redis-package] ERROR: lifecycle scripts must be run from a root-controlled, non-writable package tree.\n' >&2
  exit 1
}

bootstrap_validate_no_extended_acl() {
  local path="$1" permissions
  permissions="$(LC_ALL=C /bin/ls -lde "$path")" || bootstrap_fail
  permissions="${permissions%% *}"
  [[ "${#permissions}" == 10 \
    || ( "${#permissions}" == 11 && "${permissions: -1}" == @ ) ]] \
    || bootstrap_fail
}

bootstrap_validate_path_chain() {
  local current="$1" first=true owner mode mode_value metadata
  while :; do
    [[ -d "$current" && ! -L "$current" ]] || bootstrap_fail
    metadata="$(stat -f '%u %p' "$current")" || bootstrap_fail
    read -r owner mode <<<"$metadata"
    [[ "$owner" == 0 && "$mode" =~ ^[0-7]{5,6}$ ]] || bootstrap_fail
    mode_value=$((8#$mode))
    if (( (mode_value & 0022) != 0 )); then
      if [[ "$first" == true ]] || (( (mode_value & 01000) == 0 )); then
        bootstrap_fail
      fi
    fi
    bootstrap_validate_no_extended_acl "$current"
    [[ "$current" == / ]] && break
    current="$(dirname "$current")"
    first=false
  done
}

bootstrap_validate_file() {
  local path="$1" owner mode links mode_value metadata
  [[ -f "$path" && ! -L "$path" ]] || bootstrap_fail
  metadata="$(stat -f '%u %p %l' "$path")" || bootstrap_fail
  read -r owner mode links <<<"$metadata"
  [[ "$owner" == 0 && "$links" == 1 && "$mode" =~ ^[0-7]{5,6}$ ]] \
    || bootstrap_fail
  mode_value=$((8#$mode))
  (( (mode_value & 0022) == 0 && (mode_value & 07000) == 0 )) || bootstrap_fail
  bootstrap_validate_no_extended_acl "$path"
}

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
bootstrap_validate_path_chain "$SCRIPT_DIR"
bootstrap_validate_file "${BASH_SOURCE[0]}"
bootstrap_validate_file "$SCRIPT_DIR/common.sh"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

[[ "$#" -eq 0 ]] || die "Usage: install.sh"
require_root
require_commands pgrep awk cat chmod chown dscl find install jot launchctl mktemp mv plutil rm rmdir sed sleep stat sudo uname
acquire_lock
package_root="$(package_root_from_script)"
validate_package "$package_root"
version="$(metadata_value "$package_root/PACKAGE-INFO" REDIS_VERSION)"
package_status="$(metadata_value "$package_root/PACKAGE-INFO" PACKAGE_STATUS)"
if [[ -e "$REDIS_STATE_FILE" || -L "$REDIS_STATE_FILE" ]]; then
  validate_state
  if [[ "$(metadata_value "$REDIS_STATE_FILE" REDIS_VERSION)" == "$version" \
    && -x "$REDIS_PREFIX/bin/redis-server" ]]; then
    info "Redis $version is already installed; no changes were made."
    exit 0
  fi
  die "A managed installation already exists; use update.sh."
fi
if [[ -e "$REDIS_PREFIX" || -L "$REDIS_PREFIX" || -e "$REDIS_PLIST" || -L "$REDIS_PLIST" ]]; then
  die "Refusing to overwrite an existing installation or LaunchDaemon."
fi

rollback_install() {
  local status="$1"
  (( status != 0 )) || status=1
  stop_service || die "Install rollback could not stop Redis; installation files were preserved."
  rm -f -- "$REDIS_PLIST"
  if [[ -d "$REDIS_PREFIX" && ! -L "$REDIS_PREFIX" ]]; then
    refuse_nested_mounts "$REDIS_PREFIX"
    rm -rf -- "$REDIS_PREFIX"
  fi
  exit "$status"
}
trap 'finish_lifecycle "$?" rollback_install' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
ensure_service_account
install -d -o root -g wheel -m 0755 "$REDIS_PREFIX"
install -d -o root -g "$REDIS_GROUP" -m 0750 "$REDIS_PREFIX/conf"
install -d -o "$REDIS_USER" -g "$REDIS_GROUP" -m 0750 "$REDIS_PREFIX/data" "$REDIS_PREFIX/log"
write_default_config "$package_root/conf/redis.conf" "$REDIS_PREFIX/conf/redis.conf"
install -m 0644 "$package_root/conf/sentinel.conf" "$REDIS_PREFIX/conf/sentinel.conf"
chown root:"$REDIS_GROUP" "$REDIS_PREFIX/conf"/*.conf
chmod 0640 "$REDIS_PREFIX/conf"/*.conf
install_program_files "$package_root"
install -o root -g wheel -m 0644 "$package_root/launchd/io.github.ainuoyan.redis-unofficial.plist" "$REDIS_PLIST"
write_state "$version" "$package_status"
start_service
wait_ready "$REDIS_PREFIX" || false
trap release_lock EXIT
trap - INT TERM HUP
info "Installed Redis $version as the LaunchDaemon $REDIS_LABEL."
