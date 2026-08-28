#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

[[ "$#" -eq 0 ]] || die "Usage: update.sh"
require_root
require_commands cat pgrep awk date env findmnt flock getent grep install ldd mktemp mv rc-service rc-update rm rmdir sed seq setpriv sleep stat tar uname
acquire_lock
validate_state
package_root="$(package_root_from_script)"
validate_package "$package_root"
new_version="$(metadata_value "$package_root/PACKAGE-INFO" REDIS_VERSION)"
new_status="$(metadata_value "$package_root/PACKAGE-INFO" PACKAGE_STATUS)"
old_version="$(metadata_value "$REDIS_STATE_FILE" REDIS_VERSION)"
version_less_than "$new_version" "$old_version" \
  && die "Downgrades require a separate data-compatibility migration and are not supported by this updater."
recovering_uninstalled=false
if [[ ! -e "$REDIS_PREFIX/bin" && ! -L "$REDIS_PREFIX/bin" \
  && ! -e "$REDIS_PREFIX/scripts" && ! -L "$REDIS_PREFIX/scripts" \
  && ! -e "$REDIS_PREFIX/openrc" && ! -L "$REDIS_PREFIX/openrc" \
  && ! -e "$REDIS_INIT_SCRIPT" && ! -L "$REDIS_INIT_SCRIPT" ]]; then
  recovering_uninstalled=true
fi
# Refresh managed files even when only packaging scripts changed.

backup="$REDIS_BACKUP_ROOT/${old_version}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -o root -g root -m 0700 "$REDIS_BACKUP_ROOT" "$backup"
managed_backup_paths=(PACKAGE-INFO .redis-package-state)
for managed_path in bin scripts openrc BUILD-INFO LICENSE.txt README.txt \
  THIRD_PARTY_NOTICES.md UPSTREAM-DEPENDENCY-NOTICES.txt \
  UPSTREAM-CONTRIBUTOR-LICENSE.txt; do
  if [[ -e "$REDIS_PREFIX/$managed_path" || -L "$REDIS_PREFIX/$managed_path" ]]; then
    [[ ! -L "$REDIS_PREFIX/$managed_path" ]] || die "Managed path is an unsafe symlink: $managed_path"
    managed_backup_paths+=("$managed_path")
  elif [[ "$recovering_uninstalled" == false && "$managed_path" != UPSTREAM-CONTRIBUTOR-LICENSE.txt ]]; then
    die "Managed installation is incomplete: $managed_path"
  fi
done
tar -C "$REDIS_PREFIX" -cf "$backup/managed-files.tar" "${managed_backup_paths[@]}"
if [[ "$recovering_uninstalled" == false ]]; then
  [[ -f "$REDIS_INIT_SCRIPT" && ! -L "$REDIS_INIT_SCRIPT" ]] \
    || die "Managed OpenRC service file is missing or unsafe."
  install -m 0755 "$REDIS_INIT_SCRIPT" "$backup/redis-unofficial.init"
fi
was_running=false
rc-service "$REDIS_SERVICE" status >/dev/null 2>&1 && was_running=true

rollback() {
  local status="$1"
  (( status != 0 )) || status=1
  stop_service || die "Rollback could not stop Redis; files and backup were preserved: $backup"
  if [[ "$recovering_uninstalled" == true ]]; then
    rc-update del "$REDIS_SERVICE" default >/dev/null 2>&1 || true
    rm -rf -- "$REDIS_PREFIX/bin" "$REDIS_PREFIX/scripts" "$REDIS_PREFIX/openrc"
    rm -f -- "$REDIS_INIT_SCRIPT" "$REDIS_PREFIX/BUILD-INFO" \
      "$REDIS_PREFIX/LICENSE.txt" "$REDIS_PREFIX/README.txt" \
      "$REDIS_PREFIX/THIRD_PARTY_NOTICES.md" \
      "$REDIS_PREFIX/UPSTREAM-CONTRIBUTOR-LICENSE.txt" \
      "$REDIS_PREFIX/UPSTREAM-DEPENDENCY-NOTICES.txt"
  fi
  tar -C "$REDIS_PREFIX" -xf "$backup/managed-files.tar"
  if [[ -f "$backup/redis-unofficial.init" ]]; then
    install -m 0755 "$backup/redis-unofficial.init" "$REDIS_INIT_SCRIPT"
  fi
  if [[ "$was_running" == true ]]; then
    rc-service "$REDIS_SERVICE" start || die "Files restored, but the old service could not start; backup: $backup"
    wait_ready "$REDIS_PREFIX" || die "Files restored, but readiness failed; backup: $backup"
  fi
  printf '[redis-package] ERROR: update failed; managed files were rolled back from %s\n' "$backup" >&2
  exit "$status"
}
trap 'finish_lifecycle "$?" rollback' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

stop_service
install_program_files "$package_root"
install -m 0755 "$package_root/openrc/redis" "$REDIS_INIT_SCRIPT"
write_state "$new_version" "$new_status"
if [[ "$recovering_uninstalled" == true ]]; then
  rc-update add "$REDIS_SERVICE" default
  rc-service "$REDIS_SERVICE" start
  wait_ready "$REDIS_PREFIX" || false
elif [[ "$was_running" == true ]]; then
  rc-service "$REDIS_SERVICE" start
  wait_ready "$REDIS_PREFIX" || false
fi
trap release_lock EXIT
trap - INT TERM HUP
info "Updated Redis from $old_version to $new_version; configuration and data were preserved."
