#!/bin/bash -p
set -euo pipefail

PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
unset CDPATH ENV BASH_ENV


# Parse only UI options before loading package code; preserve operation arguments.
REDIS_UI_LANGUAGE="${REDIS_INSTALL_LANG:-en}"
redis_operation_args=()
while (( $# > 0 )); do
  case "$1" in
    --lang)
      if (( $# < 2 )); then
        printf 'Usage: --lang en|zh\n' >&2
        exit 2
      fi
      REDIS_UI_LANGUAGE="$2"
      shift
      ;;
    *) redis_operation_args+=("$1") ;;
  esac
  shift
done
case "$REDIS_UI_LANGUAGE" in
  en) ;;
  zh|zh_CN) REDIS_UI_LANGUAGE=zh ;;
  *) printf 'Invalid language. Use --lang en or --lang zh.\n' >&2; exit 2 ;;
esac
if (( ${#redis_operation_args[@]} > 0 )); then
  set -- "${redis_operation_args[@]}"
else
  set --
fi
unset redis_operation_args

bootstrap_fail() {
  if [[ "$REDIS_UI_LANGUAGE" == zh ]]; then
    printf '[redis-package] 错误：生命周期脚本必须从 root 控制且不可写的安装目录运行。\n' >&2
  else
    printf '[redis-package] ERROR: lifecycle scripts must be run from a root-controlled, non-writable installation.\n' >&2
  fi
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

if [[ "$#" -eq 1 && ( "$1" == --help || "$1" == -h ) ]]; then
  info "Usage: uninstall.sh [--purge] [--lang en|zh]" "用法：uninstall.sh [--purge] [--lang en|zh]"
  info "English is the default. Chinese output requires a UTF-8 terminal." "默认英文。中文输出需要支持 UTF-8 的终端；显示异常时使用 --lang en。"
  exit 0
fi

purge=false
case "$#:$*" in 0:) ;; 1:--purge) purge=true ;; *) die "Usage: uninstall.sh [--purge]" "用法：uninstall.sh [--purge] [--lang en|zh]" ;; esac
require_root
require_commands pgrep sleep awk find launchctl rm stat
acquire_lock
if [[ ! -e "$REDIS_STATE_FILE" && ! -L "$REDIS_STATE_FILE" ]]; then
  if [[ ! -e "$REDIS_PREFIX" && ! -L "$REDIS_PREFIX" \
    && ! -e "$REDIS_PLIST" && ! -L "$REDIS_PLIST" ]]; then
    info "Redis is already uninstalled." "Redis 已卸载。"
    exit 0
  fi
  die "Refusing to remove an installation without valid managed state." "拒绝删除没有有效受管理状态的安装。"
fi
validate_state
stop_service || die "Unable to stop Redis; no files were removed." "无法停止 Redis；未删除任何文件。"
launchctl disable "$REDIS_DOMAIN_LABEL" >/dev/null 2>&1 || true
rm -f -- "$REDIS_PLIST"
if [[ "$purge" == true ]]; then
  [[ -d "$REDIS_PREFIX" && ! -L "$REDIS_PREFIX" ]] || die "Install prefix is unsafe." "安装目录不安全。"
  refuse_nested_mounts "$REDIS_PREFIX"
  rm -rf -- "$REDIS_PREFIX"
  info "Removed Redis program, configuration, data, and logs. The service account was preserved." "已删除 Redis 程序、配置、数据和日志。服务账号已保留。"
else
  refuse_nested_mounts "$REDIS_PREFIX/bin" "$REDIS_PREFIX/scripts" "$REDIS_PREFIX/launchd"
  rm -rf -- "$REDIS_PREFIX/bin" "$REDIS_PREFIX/scripts" "$REDIS_PREFIX/launchd"
  rm -f -- "$REDIS_PREFIX/BUILD-INFO" "$REDIS_PREFIX/LICENSE.txt" \
    "$REDIS_PREFIX/README.txt" "$REDIS_PREFIX/THIRD_PARTY_NOTICES.md" \
    "$REDIS_PREFIX/UPSTREAM-CONTRIBUTOR-LICENSE.txt" \
    "$REDIS_PREFIX/UPSTREAM-DEPENDENCY-NOTICES.txt"
  info "Removed Redis program and LaunchDaemon; conf, data, logs, and state were preserved." "已删除 Redis 程序和 LaunchDaemon；配置、数据、日志和状态已保留。"
fi
