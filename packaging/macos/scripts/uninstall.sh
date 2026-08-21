#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

purge=false
case "$#:$*" in 0:) ;; 1:--purge) purge=true ;; *) die "Usage: uninstall.sh [--purge]" ;; esac
require_root
require_commands awk find launchctl rm stat
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
stop_service >/dev/null 2>&1 || true
launchctl disable "$REDIS_DOMAIN_LABEL" >/dev/null 2>&1 || true
rm -f -- "$REDIS_PLIST"
if [[ "$purge" == true ]]; then
  [[ -d "$REDIS_PREFIX" && ! -L "$REDIS_PREFIX" ]] || die "Install prefix is unsafe."
  refuse_nested_mounts
  rm -rf -- "$REDIS_PREFIX"
  info "Removed Redis program, configuration, data, and logs. The service account was preserved."
else
  rm -rf -- "$REDIS_PREFIX/bin" "$REDIS_PREFIX/scripts" "$REDIS_PREFIX/launchd"
  rm -f -- "$REDIS_PREFIX/BUILD-INFO" "$REDIS_PREFIX/LICENSE.txt" \
    "$REDIS_PREFIX/README.txt" "$REDIS_PREFIX/THIRD_PARTY_NOTICES.md" \
    "$REDIS_PREFIX/UPSTREAM-CONTRIBUTOR-LICENSE.txt" \
    "$REDIS_PREFIX/UPSTREAM-DEPENDENCY-NOTICES.txt"
  info "Removed Redis program and LaunchDaemon; conf, data, logs, and state were preserved."
fi
