#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

[[ "$#" -eq 0 ]] || die "Usage: update.sh"
require_root
require_commands awk date find install jot launchctl mktemp mv plutil rm rmdir sed sleep stat sudo tar uname
acquire_lock
validate_state
package_root="$(package_root_from_script)"
validate_package "$package_root"
new_version="$(metadata_value "$package_root/PACKAGE-INFO" REDIS_VERSION)"
old_version="$(metadata_value "$REDIS_STATE_FILE" REDIS_VERSION)"
version_less_than "$new_version" "$old_version" \
  && die "Downgrades require a separate data-compatibility migration and are not supported by this experimental updater."
recovering_uninstalled=false
if [[ ! -e "$REDIS_PREFIX/bin" && ! -L "$REDIS_PREFIX/bin" \
  && ! -e "$REDIS_PREFIX/scripts" && ! -L "$REDIS_PREFIX/scripts" \
  && ! -e "$REDIS_PREFIX/launchd" && ! -L "$REDIS_PREFIX/launchd" \
  && ! -e "$REDIS_PLIST" && ! -L "$REDIS_PLIST" ]]; then
  recovering_uninstalled=true
fi
if [[ "$new_version" == "$old_version" && "$recovering_uninstalled" == false \
  && -x "$REDIS_PREFIX/bin/redis-server" && -d "$REDIS_PREFIX/scripts" \
  && ! -L "$REDIS_PREFIX/scripts" && -d "$REDIS_PREFIX/launchd" \
  && ! -L "$REDIS_PREFIX/launchd" && -f "$REDIS_PLIST" \
  && ! -L "$REDIS_PLIST" ]]; then
  info "Redis $new_version is already installed; no changes were made."
  exit 0
fi

backup="$REDIS_BACKUP_ROOT/${old_version}-$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -o root -g wheel -m 0700 "$REDIS_BACKUP_ROOT" "$backup"
managed_backup_paths=(PACKAGE-INFO .redis-package-state)
for managed_path in bin scripts launchd BUILD-INFO LICENSE.txt README.txt \
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
  [[ -f "$REDIS_PLIST" && ! -L "$REDIS_PLIST" ]] \
    || die "Managed LaunchDaemon file is missing or unsafe."
  install -m 0644 "$REDIS_PLIST" "$backup/io.github.ainuoyan.redis-unofficial.plist"
fi
was_running=false
launchd_loaded && was_running=true

rollback() {
  local status="$?"
  trap - ERR INT TERM HUP
  stop_service >/dev/null 2>&1 || true
  if [[ "$recovering_uninstalled" == true ]]; then
    launchctl disable "$REDIS_DOMAIN_LABEL" >/dev/null 2>&1 || true
    rm -rf -- "$REDIS_PREFIX/bin" "$REDIS_PREFIX/scripts" "$REDIS_PREFIX/launchd"
    rm -f -- "$REDIS_PLIST" "$REDIS_PREFIX/BUILD-INFO" \
      "$REDIS_PREFIX/LICENSE.txt" "$REDIS_PREFIX/README.txt" \
      "$REDIS_PREFIX/THIRD_PARTY_NOTICES.md" \
      "$REDIS_PREFIX/UPSTREAM-CONTRIBUTOR-LICENSE.txt" \
      "$REDIS_PREFIX/UPSTREAM-DEPENDENCY-NOTICES.txt"
  fi
  tar -C "$REDIS_PREFIX" -xf "$backup/managed-files.tar"
  if [[ -f "$backup/io.github.ainuoyan.redis-unofficial.plist" ]]; then
    install -m 0644 "$backup/io.github.ainuoyan.redis-unofficial.plist" "$REDIS_PLIST"
  fi
  if [[ "$was_running" == true ]]; then start_service >/dev/null 2>&1 || true; fi
  printf '[redis-package] ERROR: update failed; managed files were rolled back from %s\n' "$backup" >&2
  exit "$status"
}
trap rollback ERR INT TERM HUP
if [[ "$was_running" == true ]]; then stop_service; fi
install_program_files "$package_root"
install -m 0644 "$package_root/launchd/io.github.ainuoyan.redis-unofficial.plist" "$REDIS_PLIST"
write_state "$new_version"
if [[ "$recovering_uninstalled" == true ]]; then
  start_service
  wait_ready "$REDIS_PREFIX" || false
elif [[ "$was_running" == true ]]; then
  start_service
  wait_ready "$REDIS_PREFIX" || false
fi
trap - ERR INT TERM HUP
info "Updated Redis from $old_version to $new_version; configuration and data were preserved."
