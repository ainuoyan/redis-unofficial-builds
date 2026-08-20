#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PACKAGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

show_help() {
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    cat <<'EOF'
用法：sudo ./install.sh [--no-start] [--no-service] [--adopt] [--force-service]

将 Redis 安装到 /usr/local/redis。默认注册、启用并启动 redis.service。

选项：
  --no-start       注册并启用服务，但暂不启动。
  --no-service     仅安装程序，不注册 systemd 服务。
  --adopt          接管已有配置或数据目录；不会覆盖 conf/ 和 data/。
  --force-service  替换其他软件管理的 redis.service。
  -h, --help       显示帮助。

语言由 LC_ALL、LC_MESSAGES 或 LANG 自动选择，也可设置
REDIS_INSTALL_LANG=en 或 REDIS_INSTALL_LANG=zh_CN。
EOF
  else
    cat <<'EOF'
Usage: sudo ./install.sh [--no-start] [--no-service] [--adopt] [--force-service]

Install Redis into /usr/local/redis. By default, redis.service is registered,
enabled, and started.

Options:
  --no-start       Register and enable the service without starting it.
  --no-service     Install the program without registering a systemd service.
  --adopt          Adopt existing configuration or data; conf/ and data/ are preserved.
  --force-service  Replace a redis.service managed by another installation.
  -h, --help       Show this help message.

The language is selected from LC_ALL, LC_MESSAGES, or LANG. Override it with
REDIS_INSTALL_LANG=en or REDIS_INSTALL_LANG=zh_CN.
EOF
  fi
}

no_start=false
no_service=false
adopt=false
force_service=false

while (($# > 0)); do
  case "$1" in
    --no-start) no_start=true ;;
    --no-service) no_service=true ;;
    --adopt) adopt=true ;;
    --force-service|--force) force_service=true ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *) die_message unknown_option "$1" ;;
  esac
  shift
done

require_root
require_commands awk chmod chown cp date getent grep groupadd id install mv rm sed uname useradd
validate_package_root "$PACKAGE_ROOT"
preflight_package_compatibility "$PACKAGE_ROOT"
validate_state_file

if [[ -x "$REDIS_INSTALL_PREFIX/bin/redis-server" ]]; then
  if managed_install_exists; then
    if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
      die "Redis 已由本项目安装，请从新安装包运行 update.sh。"
    else
      die "Redis is already managed by this project. Run update.sh from the new package."
    fi
  fi
  if [[ "$adopt" == true ]]; then
    if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
      die "发现现有 redis-server，请从新安装包运行 update.sh --adopt。"
    else
      die "An existing redis-server was found. Run update.sh --adopt from the new package."
    fi
  fi
  die_message unmanaged_install
fi

if ! managed_install_exists \
  && [[ -e "$REDIS_INSTALL_PREFIX/conf/redis.conf" || -d "$REDIS_INSTALL_PREFIX/data" ]] \
  && [[ "$adopt" != true ]]; then
  die_message unmanaged_install
fi

service_manager=systemd
if [[ "$no_service" == true ]]; then
  service_manager=none
else
  require_systemd_runtime
  assert_service_slot_available "$force_service"
fi

# No filesystem, account, or service mutation occurs before all compatibility
# and service-manager checks above have succeeded.
load_account_ownership_from_state
ensure_redis_account

if [[ "$service_manager" == "systemd" && "$force_service" == true ]]; then
  existing_unit="$(service_fragment_path)"
  if [[ -z "$existing_unit" && -f "$REDIS_SERVICE_UNIT" ]]; then
    existing_unit="$REDIS_SERVICE_UNIT"
  fi
  if [[ -n "$existing_unit" && -f "$existing_unit" ]] \
    && ! unit_file_is_managed "$existing_unit"; then
    service_backup_dir="$REDIS_BACKUP_ROOT/service-unit-$(date -u +%Y%m%dT%H%M%SZ)"
    install -d -o root -g root -m 0700 "$service_backup_dir"
    cp -a "$existing_unit" "$service_backup_dir/redis.service"
    if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
      info "现有服务单元已备份到 $service_backup_dir/redis.service"
    else
      info "Existing service unit backed up to $service_backup_dir/redis.service"
    fi
  fi
fi

install -d -o root -g root -m 0755 "$REDIS_INSTALL_PREFIX"
rm -rf \
  "$REDIS_INSTALL_PREFIX/bin" \
  "$REDIS_INSTALL_PREFIX/scripts" \
  "$REDIS_INSTALL_PREFIX/systemd"
cp -a "$PACKAGE_ROOT/bin" "$REDIS_INSTALL_PREFIX/"
cp -a "$PACKAGE_ROOT/scripts" "$REDIS_INSTALL_PREFIX/"
cp -a "$PACKAGE_ROOT/systemd" "$REDIS_INSTALL_PREFIX/"

install_default_configs "$PACKAGE_ROOT"
for metadata_file in PACKAGE-INFO BUILD-INFO LICENSE.txt README.txt; do
  if [[ -f "$PACKAGE_ROOT/$metadata_file" ]]; then
    install -o root -g root -m 0644 \
      "$PACKAGE_ROOT/$metadata_file" \
      "$REDIS_INSTALL_PREFIX/$metadata_file"
  fi
done

chown -R root:root \
  "$REDIS_INSTALL_PREFIX/bin" \
  "$REDIS_INSTALL_PREFIX/scripts" \
  "$REDIS_INSTALL_PREFIX/systemd"
prepare_runtime_layout

if [[ "$service_manager" == "systemd" ]]; then
  install_service_unit "$REDIS_INSTALL_PREFIX"
  systemctl enable "$REDIS_SERVICE_NAME"
fi

write_install_state "$REDIS_INSTALL_PREFIX" "$service_manager"

if [[ "$service_manager" == "systemd" && "$no_start" == false ]]; then
  if systemctl is-active --quiet "$REDIS_SERVICE_NAME"; then
    systemctl restart "$REDIS_SERVICE_NAME"
  else
    systemctl start "$REDIS_SERVICE_NAME"
  fi
  wait_for_service
fi

installed_version="$(redis_version_from_binary "$REDIS_INSTALL_PREFIX/bin/redis-server")"
if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
  info "Redis ${installed_version:-未知版本} 已安装到 $REDIS_INSTALL_PREFIX。"
  if [[ "$service_manager" == "systemd" ]]; then
    info "服务状态：systemctl status $REDIS_SERVICE_NAME"
    info "服务日志：journalctl -u $REDIS_SERVICE_NAME"
  else
    info "已按 --no-service 模式安装，未注册系统服务。"
  fi
else
  info "Redis ${installed_version:-unknown} installed in $REDIS_INSTALL_PREFIX."
  if [[ "$service_manager" == "systemd" ]]; then
    info "Service: systemctl status $REDIS_SERVICE_NAME"
    info "Logs: journalctl -u $REDIS_SERVICE_NAME"
  else
    info "Installed with --no-service; no system service was registered."
  fi
fi
