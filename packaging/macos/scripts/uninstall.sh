#!/bin/bash -p
set -euo pipefail

PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
unset CDPATH ENV BASH_ENV

bootstrap_fail() {
  printf '[redis-package] ERROR: lifecycle scripts must be run from a root-controlled, non-writable installation.\n' >&2
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

purge=false
case "$#:$*" in 0:) ;; 1:--purge) purge=true ;; *) die "Usage: uninstall.sh [--purge]" ;; esac
require_root
require_commands pgrep sleep awk find launchctl rm stat
acquire_lock
if [[ ! -e "$REDIS_STATE_FILE" && ! -L "$REDIS_STATE_FILE" ]]; then
  if [[ ! -e "$REDIS_PREFIX" && ! -L "$REDIS_PREFIX" \
    && ! -e "$REDIS_PLIST" && ! -L "$REDIS_PLIST" ]]; then
    info "Redis is already uninstalled."
    exit 0
  fi
  die "Refusing to remove an installation without valid managed state."
fi
validate_state
stop_service || die "Unable to stop Redis; no files were removed."
launchctl disable "$REDIS_DOMAIN_LABEL" >/dev/null 2>&1 || true
rm -f -- "$REDIS_PLIST"
if [[ "$purge" == true ]]; then
  [[ -d "$REDIS_PREFIX" && ! -L "$REDIS_PREFIX" ]] || die "Install prefix is unsafe."
  refuse_nested_mounts "$REDIS_PREFIX"
  rm -rf -- "$REDIS_PREFIX"
  info "Removed Redis program, configuration, data, and logs. The service account was preserved."
else
  refuse_nested_mounts "$REDIS_PREFIX/bin" "$REDIS_PREFIX/scripts" "$REDIS_PREFIX/launchd"
  rm -rf -- "$REDIS_PREFIX/bin" "$REDIS_PREFIX/scripts" "$REDIS_PREFIX/launchd"
  rm -f -- "$REDIS_PREFIX/BUILD-INFO" "$REDIS_PREFIX/LICENSE.txt" \
    "$REDIS_PREFIX/README.txt" "$REDIS_PREFIX/THIRD_PARTY_NOTICES.md" \
    "$REDIS_PREFIX/UPSTREAM-CONTRIBUTOR-LICENSE.txt" \
    "$REDIS_PREFIX/UPSTREAM-DEPENDENCY-NOTICES.txt"
  info "Removed Redis program and LaunchDaemon; conf, data, logs, and state were preserved."
fi
