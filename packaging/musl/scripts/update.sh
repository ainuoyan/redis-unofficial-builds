#!/bin/bash -p
set -Eeuo pipefail

PATH=/sbin:/usr/sbin:/bin:/usr/bin
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
    printf '[redis-package] 错误：生命周期脚本必须从 root 控制且不可写的安装包目录运行。\n' >&2
  else
    printf '[redis-package] ERROR: lifecycle scripts must be run from a root-controlled, non-writable package tree.\n' >&2
  fi
  exit 1
}

bootstrap_validate_no_extended_acl() {
  local path="$1" permissions
  permissions="$(LC_ALL=C /bin/ls -ld -- "$path")" || bootstrap_fail
  permissions="${permissions%% *}"
  [[ "${#permissions}" == 10 \
    || ( "${#permissions}" == 11 && "${permissions: -1}" == . ) ]] \
    || bootstrap_fail
}

bootstrap_validate_path_chain() {
  local current="$1" first=true owner mode mode_value metadata
  while :; do
    [[ -d "$current" && ! -L "$current" ]] || bootstrap_fail
    metadata="$(stat -c '%u %a' -- "$current")" || bootstrap_fail
    read -r owner mode <<<"$metadata"
    [[ "$owner" == 0 && "$mode" =~ ^[0-7]{3,4}$ ]] || bootstrap_fail
    mode_value=$((8#$mode))
    if (( (mode_value & 0022) != 0 )); then
      if [[ "$first" == true ]] || (( (mode_value & 01000) == 0 )); then
        bootstrap_fail
      fi
    fi
    bootstrap_validate_no_extended_acl "$current"
    [[ "$current" == / ]] && break
    current="$(dirname -- "$current")"
    first=false
  done
}

bootstrap_validate_file() {
  local path="$1" owner mode links mode_value metadata
  [[ -f "$path" && ! -L "$path" ]] || bootstrap_fail
  metadata="$(stat -c '%u %a %h' -- "$path")" || bootstrap_fail
  read -r owner mode links <<<"$metadata"
  [[ "$owner" == 0 && "$links" == 1 && "$mode" =~ ^[0-7]{3,4}$ ]] \
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
  info "Usage: update.sh [--lang en|zh]" "用法：update.sh [--lang en|zh]"
  info "English is the default. Chinese output requires a UTF-8 terminal." "默认英文。中文输出需要支持 UTF-8 的终端；显示异常时使用 --lang en。"
  exit 0
fi

[[ "$#" -eq 0 ]] || die "Usage: update.sh" "用法：update.sh [--lang en|zh]"
require_root
require_commands cat pgrep awk date env find findmnt flock getent grep install ldd mktemp mv rc-service rc-update rm rmdir sed seq setpriv sleep stat tar uname
acquire_lock
validate_state
package_root="$(package_root_from_script)"
validate_package "$package_root"
new_version="$(metadata_value "$package_root/PACKAGE-INFO" REDIS_VERSION)"
new_status="$(metadata_value "$package_root/PACKAGE-INFO" PACKAGE_STATUS)"
old_version="$(metadata_value "$REDIS_STATE_FILE" REDIS_VERSION)"
version_less_than "$new_version" "$old_version" \
  && die "Downgrades require a separate data-compatibility migration and are not supported by this updater." "降级需要单独的数据兼容性迁移；此更新脚本不支持降级。"
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
    [[ ! -L "$REDIS_PREFIX/$managed_path" ]] || die "Managed path is an unsafe symlink: $managed_path" "受管理路径是不安全的符号链接：$managed_path"
    managed_backup_paths+=("$managed_path")
  elif [[ "$recovering_uninstalled" == false && "$managed_path" != UPSTREAM-CONTRIBUTOR-LICENSE.txt ]]; then
    die "Managed installation is incomplete: $managed_path" "受管理的安装不完整：$managed_path"
  fi
done
tar -C "$REDIS_PREFIX" -cf "$backup/managed-files.tar" "${managed_backup_paths[@]}"
if [[ "$recovering_uninstalled" == false ]]; then
  [[ -f "$REDIS_INIT_SCRIPT" && ! -L "$REDIS_INIT_SCRIPT" ]] \
    || die "Managed OpenRC service file is missing or unsafe." "受管理的 OpenRC 服务文件不存在或不安全。"
  install -m 0755 "$REDIS_INIT_SCRIPT" "$backup/redis-unofficial.init"
fi
was_running=false
rc-service "$REDIS_SERVICE" status >/dev/null 2>&1 && was_running=true

rollback() {
  local status="$1"
  (( status != 0 )) || status=1
  stop_service || die "Rollback could not stop Redis; files and backup were preserved: $backup" "回滚无法停止 Redis；文件和备份已保留：$backup"
  if [[ "$recovering_uninstalled" == true ]]; then
    rc-update del "$REDIS_SERVICE" default >/dev/null 2>&1 || true
    refuse_nested_mounts "$REDIS_PREFIX/bin" "$REDIS_PREFIX/scripts" "$REDIS_PREFIX/openrc"
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
    rc-service "$REDIS_SERVICE" start || die "Files restored, but the old service could not start; backup: $backup" "文件已恢复，但旧服务无法启动；备份：$backup"
    wait_ready "$REDIS_PREFIX" || die "Files restored, but readiness failed; backup: $backup" "文件已恢复，但就绪检查失败；备份：$backup"
  fi
  printf '[redis-package] %s\n' "$(ui_text "ERROR: update failed; managed files were rolled back from $backup" "错误：更新失败；已从 $backup 回滚受管理文件。")" >&2
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
info "Updated Redis from $old_version to $new_version; configuration and data were preserved." "已将 Redis 从 $old_version 更新到 $new_version；配置和数据已保留。"
