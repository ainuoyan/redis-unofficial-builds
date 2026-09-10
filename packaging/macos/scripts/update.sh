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

[[ "$#" -eq 0 ]] || die "Usage: update.sh"
require_root
require_commands cat pgrep awk date find install jot launchctl mktemp mv plutil rm rmdir sed sleep stat sudo tar uname
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
  && ! -e "$REDIS_PREFIX/launchd" && ! -L "$REDIS_PREFIX/launchd" \
  && ! -e "$REDIS_PLIST" && ! -L "$REDIS_PLIST" ]]; then
  recovering_uninstalled=true
fi
# Refresh managed files even when only packaging scripts changed.

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
  local status="$1"
  (( status != 0 )) || status=1
  stop_service || die "Rollback could not stop Redis; files and backup were preserved: $backup"
  if [[ "$recovering_uninstalled" == true ]]; then
    launchctl disable "$REDIS_DOMAIN_LABEL" >/dev/null 2>&1 || true
    refuse_nested_mounts "$REDIS_PREFIX/bin" "$REDIS_PREFIX/scripts" "$REDIS_PREFIX/launchd"
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
  if [[ "$was_running" == true ]]; then
    start_service || die "Files restored, but the old service could not start; backup: $backup"
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
install -m 0644 "$package_root/launchd/io.github.ainuoyan.redis-unofficial.plist" "$REDIS_PLIST"
write_state "$new_version" "$new_status"
if [[ "$recovering_uninstalled" == true ]]; then
  start_service
  wait_ready "$REDIS_PREFIX" || false
elif [[ "$was_running" == true ]]; then
  start_service
  wait_ready "$REDIS_PREFIX" || false
fi
trap release_lock EXIT
trap - INT TERM HUP
info "Updated Redis from $old_version to $new_version; configuration and data were preserved."
