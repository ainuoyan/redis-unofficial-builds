#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

[[ "$#" -eq 0 ]] || die "Usage: install.sh"
require_root
require_commands awk cat chmod chown dscl find install jot launchctl mktemp mv plutil rm rmdir sed sleep stat sudo uname
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
if [[ -e "$REDIS_PREFIX" || -L "$REDIS_PREFIX" || -e "$REDIS_PLIST" || -L "$REDIS_PLIST" ]]; then
  die "Refusing to overwrite an existing installation or LaunchDaemon."
fi

rollback_install() {
  local status="$?"
  trap - ERR INT TERM HUP
  stop_service >/dev/null 2>&1 || true
  rm -f -- "$REDIS_PLIST"
  [[ -d "$REDIS_PREFIX" && ! -L "$REDIS_PREFIX" ]] && rm -rf -- "$REDIS_PREFIX"
  exit "$status"
}
trap rollback_install ERR INT TERM HUP
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
write_state "$version"
start_service
wait_ready "$REDIS_PREFIX" || false
trap - ERR INT TERM HUP
info "Installed Redis $version as the experimental LaunchDaemon $REDIS_LABEL."
