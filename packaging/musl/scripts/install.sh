#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

[[ "$#" -eq 0 ]] || die "Usage: install.sh"
require_root
require_commands addgroup adduser awk cat chmod chown env findmnt flock getent grep install ldd mktemp mv rc-service rc-update rm rmdir sed seq setpriv sleep stat uname
acquire_lock
package_root="$(package_root_from_script)"
validate_package "$package_root"
version="$(metadata_value "$package_root/PACKAGE-INFO" REDIS_VERSION)"

if [[ -e "$REDIS_STATE_FILE" || -L "$REDIS_STATE_FILE" ]]; then
  validate_state
  if [[ "$(metadata_value "$REDIS_STATE_FILE" REDIS_VERSION)" == "$version" \
    && -x "$REDIS_PREFIX/bin/redis-server" ]]; then
    info "Redis $version is already installed; no changes were made."
    exit 0
  fi
  die "A managed installation already exists; use update.sh."
fi
if [[ -e "$REDIS_PREFIX" || -L "$REDIS_PREFIX" || -e "$REDIS_INIT_SCRIPT" || -L "$REDIS_INIT_SCRIPT" ]]; then
  die "Refusing to overwrite an existing installation or OpenRC service."
fi

ensure_service_account
rollback_install() {
  local status="$?"
  trap - ERR INT TERM HUP
  rc-service "$REDIS_SERVICE" stop >/dev/null 2>&1 || true
  rc-update del "$REDIS_SERVICE" default >/dev/null 2>&1 || true
  rm -f -- "$REDIS_INIT_SCRIPT"
  [[ -d "$REDIS_PREFIX" && ! -L "$REDIS_PREFIX" ]] && rm -rf -- "$REDIS_PREFIX"
  exit "$status"
}
trap rollback_install ERR INT TERM HUP
install -d -o root -g root -m 0755 "$REDIS_PREFIX"
install -d -o root -g "$REDIS_GROUP" -m 0750 "$REDIS_PREFIX/conf"
install -d -o "$REDIS_USER" -g "$REDIS_GROUP" -m 0750 "$REDIS_PREFIX/data" "$REDIS_PREFIX/log"
write_default_config "$package_root/conf/redis.conf" "$REDIS_PREFIX/conf/redis.conf"
install -m 0644 "$package_root/conf/sentinel.conf" "$REDIS_PREFIX/conf/sentinel.conf"
chown root:"$REDIS_GROUP" "$REDIS_PREFIX/conf"/*.conf
chmod 0640 "$REDIS_PREFIX/conf"/*.conf
install_program_files "$package_root"
install -o root -g root -m 0755 "$package_root/openrc/redis" "$REDIS_INIT_SCRIPT"
write_state "$version"
rc-update add "$REDIS_SERVICE" default
if ! rc-service "$REDIS_SERVICE" start || ! wait_ready "$REDIS_PREFIX"; then
  rc-service "$REDIS_SERVICE" stop >/dev/null 2>&1 || true
  rc-update del "$REDIS_SERVICE" default >/dev/null 2>&1 || true
  die "Redis did not pass its OpenRC readiness check; inspect $REDIS_PREFIX/log/redis.log."
fi
trap - ERR INT TERM HUP
info "Installed Redis $version as the experimental OpenRC service $REDIS_SERVICE."
