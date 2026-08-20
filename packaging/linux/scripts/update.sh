#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly PACKAGE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

show_help() {
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    cat <<'EOF'
用法：sudo ./update.sh [--adopt] [--no-service] [--no-start] [--force-service]

使用临时目录中解压的新安装包更新 /usr/local/redis。配置和数据不会被
覆盖；更新前会创建备份，服务启动失败时自动回滚。

选项：
  --adopt          接管尚无本项目状态文件的已有安装。
  --no-service     与 --adopt 一起使用，接管时不注册 systemd 服务。
  --no-start       注册服务，但接管后暂不启动；普通更新会保留原运行状态。
  --force-service  替换其他软件管理的 redis.service。
  -h, --help       显示帮助。
EOF
  else
    cat <<'EOF'
Usage: sudo ./update.sh [--adopt] [--no-service] [--no-start] [--force-service]

Update /usr/local/redis from a package extracted to a temporary directory.
Configuration and data are preserved. A backup is created first, and a failed
service start triggers an automatic rollback.

Options:
  --adopt          Adopt an existing installation that has no project state file.
  --no-service     With --adopt, do not register a systemd service.
  --no-start       Register but do not start an adopted service; normal updates
                   preserve the previous running state.
  --force-service  Replace a redis.service managed by another installation.
  -h, --help       Show this help message.
EOF
  fi
}

adopt=false
no_service=false
no_start=false
force_service=false

while (($# > 0)); do
  case "$1" in
    --adopt) adopt=true ;;
    --no-service) no_service=true ;;
    --no-start) no_start=true ;;
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

if [[ "$PACKAGE_ROOT" == "$REDIS_INSTALL_PREFIX" ]]; then
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    die "不能从正在使用的安装目录运行 update.sh，请先将新包解压到临时目录。"
  else
    die "Do not run update.sh from the live installation. Extract the new package to a temporary directory first."
  fi
fi

[[ -x "$REDIS_INSTALL_PREFIX/bin/redis-server" ]] || {
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    die "未在 $REDIS_INSTALL_PREFIX 找到现有 Redis，请改用 install.sh。"
  else
    die "No existing Redis installation found in $REDIS_INSTALL_PREFIX. Run install.sh instead."
  fi
}

was_managed=false
if managed_install_exists; then
  was_managed=true
elif [[ "$adopt" != true ]]; then
  die_message unmanaged_install
fi

if [[ "$no_service" == true && "$adopt" != true ]]; then
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    die "--no-service 只用于 --adopt 接管；普通更新会保留原服务模式。"
  else
    die "--no-service is only valid with --adopt; normal updates preserve the service mode."
  fi
fi

if [[ "$was_managed" == true ]]; then
  service_manager="$(state_value SERVICE_MANAGER)"
else
  service_manager=systemd
  [[ "$no_service" == true ]] && service_manager=none
fi
case "$service_manager" in
  systemd|none) ;;
  *) die_message state_invalid "$REDIS_STATE_FILE" ;;
esac

if [[ "$service_manager" == "systemd" ]]; then
  require_systemd_runtime
  assert_service_slot_available "$force_service"
fi

old_version="$(redis_version_from_binary "$REDIS_INSTALL_PREFIX/bin/redis-server" || true)"
new_version="$(redis_version_from_binary "$PACKAGE_ROOT/bin/redis-server")"
[[ -n "$new_version" ]] || die_message version_unreadable

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
safe_old_version="${old_version:-unknown}"
safe_old_version="${safe_old_version//[^0-9A-Za-z._-]/_}"
backup_dir="$REDIS_BACKUP_ROOT/redis-${safe_old_version}-${timestamp}"
previous_bin_dir="$REDIS_INSTALL_PREFIX/.bin.previous-${timestamp}"
staged_bin_dir="$REDIS_INSTALL_PREFIX/.bin.new-${timestamp}"

install -d -o root -g root -m 0700 "$backup_dir"
for backup_item in bin conf scripts systemd PACKAGE-INFO BUILD-INFO LICENSE.txt README.txt; do
  if [[ -e "$REDIS_INSTALL_PREFIX/$backup_item" ]]; then
    cp -a "$REDIS_INSTALL_PREFIX/$backup_item" "$backup_dir/"
  fi
done
if [[ -f "$REDIS_STATE_FILE" ]]; then
  cp -a "$REDIS_STATE_FILE" "$backup_dir/.redis-package-state"
fi

unit_override_existed=false
if [[ "$service_manager" == "systemd" && -f "$REDIS_SERVICE_UNIT" ]]; then
  unit_override_existed=true
  cp -a "$REDIS_SERVICE_UNIT" "$backup_dir/redis.service"
fi

service_was_active=false
service_was_enabled=false
service_existed=false
if [[ "$service_manager" == "systemd" ]]; then
  if [[ -n "$(service_fragment_path)" ]]; then
    service_existed=true
  fi
  if systemctl is-active --quiet "$REDIS_SERVICE_NAME"; then
    service_was_active=true
  fi
  if systemctl is-enabled --quiet "$REDIS_SERVICE_NAME"; then
    service_was_enabled=true
  fi
fi

load_account_ownership_from_state

rollback_needed=false
rollback_update() {
  local status=$?
  trap - ERR

  if [[ "$rollback_needed" != true ]]; then
    exit "$status"
  fi

  set +e
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    warn "更新失败，正在从 $backup_dir 恢复 Redis ${old_version:-未知版本}。"
  else
    warn "Update failed. Restoring Redis ${old_version:-unknown} from $backup_dir."
  fi

  if [[ "$service_manager" == "systemd" ]]; then
    systemctl stop "$REDIS_SERVICE_NAME" >/dev/null 2>&1
  fi

  rm -rf "$REDIS_INSTALL_PREFIX/bin"
  [[ -d "$backup_dir/bin" ]] && cp -a "$backup_dir/bin" "$REDIS_INSTALL_PREFIX/bin"

  for restore_item in scripts systemd; do
    rm -rf "$REDIS_INSTALL_PREFIX/$restore_item"
    [[ -d "$backup_dir/$restore_item" ]] \
      && cp -a "$backup_dir/$restore_item" "$REDIS_INSTALL_PREFIX/$restore_item"
  done

  for restore_file in PACKAGE-INFO BUILD-INFO LICENSE.txt README.txt; do
    if [[ -f "$backup_dir/$restore_file" ]]; then
      cp -a "$backup_dir/$restore_file" "$REDIS_INSTALL_PREFIX/$restore_file"
    else
      rm -f "$REDIS_INSTALL_PREFIX/$restore_file"
    fi
  done

  if [[ -f "$backup_dir/.redis-package-state" ]]; then
    cp -a "$backup_dir/.redis-package-state" "$REDIS_STATE_FILE"
  else
    rm -f "$REDIS_STATE_FILE"
  fi

  if [[ "$service_manager" == "systemd" ]]; then
    if [[ "$unit_override_existed" == true ]]; then
      cp -a "$backup_dir/redis.service" "$REDIS_SERVICE_UNIT"
    else
      rm -f "$REDIS_SERVICE_UNIT"
    fi
    systemctl daemon-reload
    if [[ "$service_was_enabled" == true ]]; then
      systemctl enable "$REDIS_SERVICE_NAME" >/dev/null 2>&1
    else
      systemctl disable "$REDIS_SERVICE_NAME" >/dev/null 2>&1
    fi
    if [[ "$service_was_active" == true ]]; then
      systemctl start "$REDIS_SERVICE_NAME"
    fi
  fi

  rm -rf "$staged_bin_dir" "$previous_bin_dir"
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    die "更新已回滚，请检查上方错误后重试。"
  else
    die "Update rolled back. Inspect the error above before retrying."
  fi
}
trap rollback_update ERR

rollback_needed=true
if [[ "$service_manager" == "systemd" && "$service_was_active" == true ]]; then
  systemctl stop "$REDIS_SERVICE_NAME"
fi

install -d -o root -g root -m 0755 "$staged_bin_dir"
cp -a "$PACKAGE_ROOT/bin/." "$staged_bin_dir/"
chown -R root:root "$staged_bin_dir"

mv "$REDIS_INSTALL_PREFIX/bin" "$previous_bin_dir"
mv "$staged_bin_dir" "$REDIS_INSTALL_PREFIX/bin"

rm -rf "$REDIS_INSTALL_PREFIX/scripts" "$REDIS_INSTALL_PREFIX/systemd"
cp -a "$PACKAGE_ROOT/scripts" "$REDIS_INSTALL_PREFIX/"
cp -a "$PACKAGE_ROOT/systemd" "$REDIS_INSTALL_PREFIX/"
chown -R root:root \
  "$REDIS_INSTALL_PREFIX/bin" \
  "$REDIS_INSTALL_PREFIX/scripts" \
  "$REDIS_INSTALL_PREFIX/systemd"

for metadata_file in PACKAGE-INFO BUILD-INFO LICENSE.txt README.txt; do
  if [[ -f "$PACKAGE_ROOT/$metadata_file" ]]; then
    install -o root -g root -m 0644 \
      "$PACKAGE_ROOT/$metadata_file" \
      "$REDIS_INSTALL_PREFIX/$metadata_file"
  fi
done

ensure_redis_account
install_default_configs "$PACKAGE_ROOT"
prepare_runtime_layout

if [[ "$service_manager" == "systemd" ]]; then
  install_service_unit "$REDIS_INSTALL_PREFIX"
  if [[ "$service_was_enabled" == true || "$service_existed" == false ]]; then
    systemctl enable "$REDIS_SERVICE_NAME"
  fi

  should_start=false
  if [[ "$service_was_active" == true ]]; then
    should_start=true
  elif [[ "$was_managed" == false && "$no_start" == false ]]; then
    should_start=true
  fi
  if [[ "$should_start" == true ]]; then
    systemctl start "$REDIS_SERVICE_NAME"
    wait_for_service
  fi
fi

running_version="$(redis_version_from_binary "$REDIS_INSTALL_PREFIX/bin/redis-server")"
[[ "$running_version" == "$new_version" ]] || {
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    printf '[redis-package] ERROR: 已安装二进制报告 %s，预期为 %s。\n' \
      "$running_version" "$new_version" >&2
  else
    printf '[redis-package] ERROR: Installed binary reports %s instead of %s.\n' \
      "$running_version" "$new_version" >&2
  fi
  false
}

write_install_state "$REDIS_INSTALL_PREFIX" "$service_manager"
rollback_needed=false
trap - ERR
rm -rf "$previous_bin_dir"

if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
  info "Redis 已从 ${old_version:-未知版本} 更新到 $new_version。"
  info "现有配置和数据已保留。"
  info "备份目录：$backup_dir"
  if [[ "$service_manager" == "systemd" ]]; then
    info "服务状态：systemctl status $REDIS_SERVICE_NAME"
  else
    info "未管理系统服务；如 Redis 正在运行，请手动重启进程以使用新版本。"
  fi
else
  info "Redis updated from ${old_version:-unknown} to $new_version."
  info "Existing configuration and data were preserved."
  info "Backup: $backup_dir"
  if [[ "$service_manager" == "systemd" ]]; then
    info "Service: systemctl status $REDIS_SERVICE_NAME"
  else
    info "No system service was managed; restart any running Redis process manually."
  fi
fi
