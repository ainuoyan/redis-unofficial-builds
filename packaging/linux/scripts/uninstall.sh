#!/bin/bash -p
set -Eeuo pipefail

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset CDPATH ENV BASH_ENV

bootstrap_fail() {
  printf '[redis-package] ERROR: lifecycle scripts must be run from a root-controlled, non-writable installation. / 生命周期脚本必须从 root 控制且不可写的安装目录运行。\n' >&2
  exit 1
}

bootstrap_validate_no_extended_acl() {
  local path="$1" listing permissions
  listing="$(LC_ALL=C ls -ld --color=never -- "$path")" || bootstrap_fail
  permissions="${listing%% *}"
  [[ "${#permissions}" == 10 \
    || ( "${#permissions}" == 11 && "${permissions: -1}" == . ) ]] \
    || bootstrap_fail
}

bootstrap_validate_path_chain() {
  local current="$1" first=true owner mode mode_value metadata
  while :; do
    [[ -d "$current" && ! -L "$current" ]] || bootstrap_fail
    metadata="$(stat -c '%u %a' -- "$current")"
    read -r owner mode <<<"$metadata"
    [[ "$owner" == "0" && "$mode" =~ ^[0-7]{3,4}$ ]] || bootstrap_fail
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
  metadata="$(stat -c '%u %a %h' -- "$path")"
  read -r owner mode links <<<"$metadata"
  [[ "$owner" == "0" && "$links" == "1" && "$mode" =~ ^[0-7]{3,4}$ ]] \
    || bootstrap_fail
  mode_value=$((8#$mode))
  (( (mode_value & 0022) == 0 && (mode_value & 07000) == 0 )) || bootstrap_fail
  bootstrap_validate_no_extended_acl "$path"
}

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly PACKAGE_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
bootstrap_validate_path_chain "$SCRIPT_DIR"
bootstrap_validate_file "$SCRIPT_DIR/uninstall.sh"
bootstrap_validate_file "$SCRIPT_DIR/common.sh"

# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

show_help() {
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    cat <<'EOF'
用法：sudo ./uninstall.sh [--purge]

默认注销服务并删除程序文件，但保留 conf/、data/、安装状态和服务账号。

选项：
  --purge     删除整个 /usr/local/redis 目录，包括其中的配置、数据和
              任何其他文件。只有在安装状态明确记录账号由本项目创建且
              UID/GID 仍一致时，才删除 redis 用户和组。
              /usr/local/redis-backups 中的备份始终保留。
  -h, --help  显示帮助。
EOF
  else
    cat <<'EOF'
Usage: sudo ./uninstall.sh [--purge]

By default, unregister the service and remove program files while preserving
conf/, data/, installation state, and the service account.

Options:
  --purge     Also delete the entire /usr/local/redis directory, including its
              configuration, data, and any other files. The redis user/group are
              removed only when state proves this project created them and their
              UID/GID still match. Backups under /usr/local/redis-backups are
              always retained.
  -h, --help  Show this help message.
EOF
  fi
}

purge=false
while (($# > 0)); do
  case "$1" in
    --purge) purge=true ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *) die_message unknown_option "$1" ;;
  esac
  shift
done

require_root
require_commands awk cat chmod dirname find flock getent grep id ls readlink realpath rm sed stat
acquire_lifecycle_lock
[[ "$REDIS_INSTALL_PREFIX" == "/usr/local/redis" ]] \
  || die_message unexpected_path "$REDIS_INSTALL_PREFIX"
validate_state_file
managed_install_exists || die_message unmanaged_install
validate_existing_prefix_paths
validate_destructive_targets \
  "$REDIS_INSTALL_PREFIX" \
  "$REDIS_INSTALL_PREFIX/bin" \
  "$REDIS_INSTALL_PREFIX/scripts" \
  "$REDIS_INSTALL_PREFIX/systemd" \
  "$REDIS_INSTALL_PREFIX/PACKAGE-INFO" \
  "$REDIS_INSTALL_PREFIX/BUILD-INFO" \
  "$REDIS_INSTALL_PREFIX/LICENSE.txt" \
  "$REDIS_INSTALL_PREFIX/README.txt" \
  "$REDIS_INSTALL_PREFIX/THIRD_PARTY_NOTICES.md" \
  "$REDIS_INSTALL_PREFIX/UPSTREAM-CONTRIBUTOR-LICENSE.txt" \
  "$REDIS_INSTALL_PREFIX/UPSTREAM-DEPENDENCY-NOTICES.txt" \
  "$REDIS_STATE_FILE"
if [[ "$purge" == true ]]; then
  require_commands groupdel userdel
  validate_destructive_targets \
    "$REDIS_INSTALL_PREFIX/conf" \
    "$REDIS_INSTALL_PREFIX/conf/redis.conf" \
    "$REDIS_INSTALL_PREFIX/conf/sentinel.conf" \
    "$REDIS_INSTALL_PREFIX/data"
fi

service_manager="$(state_value SERVICE_MANAGER)"
case "$service_manager" in
  systemd|none) ;;
  *) die_message state_invalid "$REDIS_STATE_FILE" ;;
esac

created_user="$(state_value CREATED_USER)"
created_group="$(state_value CREATED_GROUP)"
recorded_uid="$(state_value REDIS_UID)"
recorded_gid="$(state_value REDIS_GID)"
state_format="$(state_value STATE_FORMAT)"
recorded_home=""
recorded_shell=""
if [[ "$state_format" == 3 ]]; then
  recorded_home="$(state_value REDIS_HOME)"
  recorded_shell="$(state_value REDIS_SHELL)"
fi

if [[ "$service_manager" == "systemd" ]]; then
  require_systemd_runtime
  validate_service_override_path
  fragment_path="$(service_fragment_path)"
  if [[ -n "$fragment_path" && -f "$fragment_path" ]] \
    && ! unit_file_is_managed "$fragment_path"; then
    die_message unit_conflict "$fragment_path"
  fi

  if [[ -n "$fragment_path" && -f "$fragment_path" ]]; then
    validate_effective_service_contract
    service_was_active=false
    service_active_state="$(LC_ALL=C systemctl show \
      --property=ActiveState --value "$REDIS_SERVICE_NAME")"
    case "$service_active_state" in
      active|reloading) service_was_active=true ;;
      inactive|failed) ;;
      *) die "Refusing to remove files while $REDIS_SERVICE_NAME is ${service_active_state:-unknown}." ;;
    esac
    systemctl stop "$REDIS_SERVICE_NAME"
    service_active_state="$(LC_ALL=C systemctl show \
      --property=ActiveState --value "$REDIS_SERVICE_NAME")"
    case "$service_active_state" in
      inactive|failed) ;;
      *) die "Refusing to remove files while $REDIS_SERVICE_NAME is ${service_active_state:-unknown}." ;;
    esac
    if ! assert_no_live_install_redis_server; then
      if [[ "$service_was_active" == true ]]; then
        systemctl start "$REDIS_SERVICE_NAME" \
          || warn "Unable to restart $REDIS_SERVICE_NAME after the process-safety check failed."
      fi
      exit 1
    fi
    systemctl disable "$REDIS_SERVICE_NAME"
  else
    assert_no_live_install_redis_server
    # A trusted package state can outlive a manually deleted fragment. Remove
    # only dangling enablement links that still point at this package's exact
    # /etc unit path; differently targeted third-party links are preserved.
    remove_stale_managed_service_enablement_links
  fi
  if unit_file_is_managed "$REDIS_SERVICE_UNIT"; then
    rm -f "$REDIS_SERVICE_UNIT"
  fi
  systemctl daemon-reload
  systemctl reset-failed "$REDIS_SERVICE_NAME" >/dev/null 2>&1 || true
else
  assert_no_live_install_redis_server
fi

if [[ "$purge" == true ]]; then
  rm -rf "$REDIS_INSTALL_PREFIX"

  user_deleted=false
  if [[ "$created_user" == "1" ]] && id "$REDIS_USER" >/dev/null 2>&1; then
    if [[ "$state_format" != 3 ]]; then
      warn "The retained state predates exact account home/shell tracking; the redis user and group were preserved. / 保留的状态早于账号 home/shell 精确记录；redis 用户和组均已保留。"
    elif redis_account_matches_recorded_identity \
      "$recorded_uid" "$recorded_gid" "$recorded_home" "$recorded_shell"; then
      if userdel "$REDIS_USER"; then
        user_deleted=true
      else
        warn "Unable to remove user $REDIS_USER; the user and group were preserved. / 无法删除 redis 用户；用户和组均已保留。"
      fi
    else
      warn "The redis user identity changed (UID, primary GID, home, shell, or supplementary groups); the user and group were preserved. / redis 用户身份（UID、主 GID、home、shell 或附加组）已变化；用户和组均已保留。"
    fi
  elif [[ "$created_user" == "1" ]]; then
    warn "The recorded redis user no longer exists; the recorded group was preserved. / 状态中记录的 redis 用户已不存在；用户组已保留。"
  fi
  if [[ "$created_group" == "1" ]] && getent group "$REDIS_GROUP" >/dev/null 2>&1; then
    if [[ "$user_deleted" == true ]] \
      && redis_group_matches_recorded_identity "$recorded_gid"; then
      groupdel "$REDIS_GROUP" \
        || warn "Unable to remove group $REDIS_GROUP; it was preserved. / 无法删除 redis 用户组；该组已保留。"
    elif [[ "$user_deleted" == true ]]; then
      warn "The redis group identity or membership changed; the group was preserved. / redis 用户组身份或成员已变化；该组已保留。"
    else
      warn "The redis group was preserved because the recorded service-user identity could not be safely removed. / 因无法安全删除状态中记录的服务用户身份，redis 用户组已保留。"
    fi
  fi

  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    info "Redis 服务、程序、配置和数据已删除。"
    info "备份仍保留在 $REDIS_BACKUP_ROOT。"
  else
    info "Redis service, program, configuration, and data were removed."
    info "Backups were retained in $REDIS_BACKUP_ROOT."
  fi
  exit 0
fi

rm -rf \
  "$REDIS_INSTALL_PREFIX/bin" \
  "$REDIS_INSTALL_PREFIX/scripts" \
  "$REDIS_INSTALL_PREFIX/systemd"
rm -f \
  "$REDIS_INSTALL_PREFIX/PACKAGE-INFO" \
  "$REDIS_INSTALL_PREFIX/BUILD-INFO" \
  "$REDIS_INSTALL_PREFIX/LICENSE.txt" \
  "$REDIS_INSTALL_PREFIX/README.txt" \
  "$REDIS_INSTALL_PREFIX/THIRD_PARTY_NOTICES.md" \
  "$REDIS_INSTALL_PREFIX/UPSTREAM-CONTRIBUTOR-LICENSE.txt" \
  "$REDIS_INSTALL_PREFIX/UPSTREAM-DEPENDENCY-NOTICES.txt"

if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
  info "Redis 服务和程序文件已删除。"
  info "配置已保留：$REDIS_INSTALL_PREFIX/conf"
  info "数据已保留：$REDIS_INSTALL_PREFIX/data"
  info "安装状态和账号已保留，之后可重新运行 install.sh。"
else
  info "Redis service and program files were removed."
  info "Configuration preserved: $REDIS_INSTALL_PREFIX/conf"
  info "Data preserved: $REDIS_INSTALL_PREFIX/data"
  info "Installation state and account were preserved for a future install.sh."
fi
