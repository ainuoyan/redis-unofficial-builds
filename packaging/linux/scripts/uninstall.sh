#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

show_help() {
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    cat <<'EOF'
用法：sudo ./uninstall.sh [--purge]

默认注销服务并删除程序文件，但保留 conf/、data/、安装状态和服务账号。

选项：
  --purge     同时删除 /usr/local/redis 中的配置和数据。只有在安装状态明确
              记录账号由本项目创建且 UID/GID 仍一致时，才删除 redis 用户和组。
              /usr/local/redis-backups 中的备份始终保留。
  -h, --help  显示帮助。
EOF
  else
    cat <<'EOF'
Usage: sudo ./uninstall.sh [--purge]

By default, unregister the service and remove program files while preserving
conf/, data/, installation state, and the service account.

Options:
  --purge     Also delete configuration and data under /usr/local/redis. The
              redis user/group are removed only when state proves this project
              created them and their UID/GID still match. Backups under
              /usr/local/redis-backups are always retained.
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
require_commands awk getent grep id rm
[[ "$REDIS_INSTALL_PREFIX" == "/usr/local/redis" ]] \
  || die_message unexpected_path "$REDIS_INSTALL_PREFIX"
validate_state_file
managed_install_exists || die_message unmanaged_install

service_manager="$(state_value SERVICE_MANAGER)"
case "$service_manager" in
  systemd|none) ;;
  *) die_message state_invalid "$REDIS_STATE_FILE" ;;
esac

created_user="$(state_value CREATED_USER)"
created_group="$(state_value CREATED_GROUP)"
recorded_uid="$(state_value REDIS_UID)"
recorded_gid="$(state_value REDIS_GID)"

if [[ "$service_manager" == "systemd" ]]; then
  require_systemd_runtime
  fragment_path="$(service_fragment_path)"
  if [[ -n "$fragment_path" && -f "$fragment_path" ]] \
    && ! unit_file_is_managed "$fragment_path"; then
    die_message unit_conflict "$fragment_path"
  fi

  if [[ -n "$fragment_path" && -f "$fragment_path" ]]; then
    systemctl stop "$REDIS_SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl disable "$REDIS_SERVICE_NAME" >/dev/null 2>&1 || true
  fi
  if unit_file_is_managed "$REDIS_SERVICE_UNIT"; then
    rm -f "$REDIS_SERVICE_UNIT"
  fi
  systemctl daemon-reload
  systemctl reset-failed "$REDIS_SERVICE_NAME" >/dev/null 2>&1 || true
fi

if [[ "$purge" == true ]]; then
  require_commands groupdel userdel
  rm -rf "$REDIS_INSTALL_PREFIX"

  if [[ "$created_user" == "1" ]] && id "$REDIS_USER" >/dev/null 2>&1; then
    current_uid="$(id -u "$REDIS_USER")"
    if [[ -n "$recorded_uid" && "$current_uid" == "$recorded_uid" ]]; then
      userdel "$REDIS_USER" || warn "Unable to remove user $REDIS_USER."
    else
      warn "The redis user UID changed; the account was preserved. / redis 用户的 UID 已变化，账号已保留。"
    fi
  fi
  if [[ "$created_group" == "1" ]] && getent group "$REDIS_GROUP" >/dev/null 2>&1; then
    current_gid="$(getent group "$REDIS_GROUP" | awk -F: '{print $3}')"
    if [[ -n "$recorded_gid" && "$current_gid" == "$recorded_gid" ]]; then
      groupdel "$REDIS_GROUP" || warn "Unable to remove group $REDIS_GROUP."
    else
      warn "The redis group GID changed; the group was preserved. / redis 用户组的 GID 已变化，用户组已保留。"
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
  "$REDIS_INSTALL_PREFIX/README.txt"

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
