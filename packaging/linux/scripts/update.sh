#!/bin/bash -p
set -Eeuo pipefail

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
unset CDPATH ENV BASH_ENV

bootstrap_fail() {
  printf '[redis-package] ERROR: lifecycle scripts must be run from a root-controlled, non-writable package tree. / 生命周期脚本必须从 root 控制且不可写的安装包目录运行。\n' >&2
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
bootstrap_validate_file "$SCRIPT_DIR/update.sh"
bootstrap_validate_file "$SCRIPT_DIR/common.sh"

# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

show_help() {
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    cat <<'EOF'
用法：sudo ./update.sh [--adopt] [--no-service] [--no-start] [--force-service]
                         [--allow-downgrade]

使用 root 控制且不可写的暂存目录中解压的新安装包更新 /usr/local/redis。配置和数据不会被
覆盖；更新前会创建备份，服务启动失败时自动回滚。

选项：
  --adopt          接管尚无本项目状态文件的已有安装。
  --no-service     与 --adopt 一起使用，接管时不注册 systemd 服务。
  --no-start       注册服务，但接管后暂不启动；普通更新会保留原运行状态。
  --force-service  仅替换其他软件管理但未运行的 redis.service；正在运行的外部服务始终拒绝替换。
  --allow-downgrade
                   明确允许安装比当前受管理版本更旧的 Redis；执行前必须另做数据快照。
  -h, --help       显示帮助。
EOF
  else
    cat <<'EOF'
Usage: sudo ./update.sh [--adopt] [--no-service] [--no-start] [--force-service]
                        [--allow-downgrade]

Update /usr/local/redis from a package extracted to a root-controlled,
non-writable staging directory.
Configuration and data are preserved. A backup is created first, and a failed
service start triggers an automatic rollback.

Options:
  --adopt          Adopt an existing installation that has no project state file.
  --no-service     With --adopt, do not register a systemd service.
  --no-start       Register but do not start an adopted service; normal updates
                   preserve the previous running state.
  --force-service  Replace an inactive redis.service managed by another
                   installation; an active external service is always rejected.
  --allow-downgrade
                   Explicitly install an older Redis version; take a separate
                   data snapshot first.
  -h, --help       Show this help message.
EOF
  fi
}

adopt=false
no_service=false
no_start=false
force_service=false
allow_downgrade=false

while (($# > 0)); do
  case "$1" in
    --adopt) adopt=true ;;
    --no-service) no_service=true ;;
    --no-start) no_start=true ;;
    --force-service|--force) force_service=true ;;
    --allow-downgrade) allow_downgrade=true ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *) die_message unknown_option "$1" ;;
  esac
  shift
done

require_root
require_commands awk cat chmod chown cp date dirname env find flock getconf getent grep groupadd groupdel head id install ls mktemp mv readlink realpath rm rmdir sed setpriv sha256sum sleep stat timeout uname useradd userdel wc
acquire_lifecycle_lock
validate_package_root_security "$PACKAGE_ROOT"
validate_package_root "$PACKAGE_ROOT"
preflight_package_compatibility "$PACKAGE_ROOT"
validate_state_file

if [[ "$PACKAGE_ROOT" == "$REDIS_INSTALL_PREFIX" ]]; then
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    die "不能从正在使用的安装目录运行 update.sh，请先将新包解压到 root 控制的暂存目录。"
  else
    die "Do not run update.sh from the live installation. Extract the new package to a root-controlled staging directory first."
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
validate_existing_install_layout
validate_redis_config_trust "$REDIS_INSTALL_PREFIX/conf/redis.conf"
validate_destructive_targets \
  "$REDIS_INSTALL_PREFIX" \
  "$REDIS_INSTALL_PREFIX/bin" \
  "$REDIS_INSTALL_PREFIX/conf" \
  "$REDIS_INSTALL_PREFIX/conf/redis.conf" \
  "$REDIS_INSTALL_PREFIX/conf/sentinel.conf" \
  "$REDIS_INSTALL_PREFIX/data" \
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

if [[ "$was_managed" == true ]]; then
  old_version="$(state_value REDIS_VERSION)"
  [[ "$(state_value PACKAGE_VARIANT)" == "$(package_info_value "$PACKAGE_ROOT" PACKAGE_VARIANT)" ]] \
    || die "Refusing to update across package variants; use a separately reviewed migration."
else
  old_version="unmanaged"
fi
new_version="$(package_info_value "$PACKAGE_ROOT" REDIS_VERSION)"
if [[ "$was_managed" == true ]] \
  && ! redis_version_is_at_least "$new_version" "$old_version"; then
  if [[ "$allow_downgrade" != true ]]; then
    if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
      die "默认拒绝从 Redis $old_version 降级到 $new_version；确认已创建独立数据快照后，使用 --allow-downgrade 明确继续。"
    else
      die "Refusing to downgrade Redis $old_version to $new_version by default; after taking a separate data snapshot, re-run with --allow-downgrade."
    fi
  fi
  warn "Redis downgrade explicitly authorized: $old_version -> $new_version. / 已明确允许 Redis 降级：$old_version -> $new_version。"
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
safe_old_version="${old_version:-unknown}"
safe_old_version="${safe_old_version//[^0-9A-Za-z._-]/_}"
if [[ -e "$REDIS_BACKUP_ROOT" || -L "$REDIS_BACKUP_ROOT" ]]; then
  assert_root_owned_directory "$REDIS_BACKUP_ROOT"
else
  install -d -o root -g root -m 0700 "$REDIS_BACKUP_ROOT"
fi
backup_dir="$(mktemp -d "$REDIS_BACKUP_ROOT/redis-${safe_old_version}-${timestamp}.XXXXXX")"
chown root:root "$backup_dir"
chmod 0700 "$backup_dir"
previous_bin_dir=""
staged_bin_dir=""

for backup_item in \
  bin conf scripts systemd PACKAGE-INFO BUILD-INFO LICENSE.txt README.txt \
  THIRD_PARTY_NOTICES.md UPSTREAM-CONTRIBUTOR-LICENSE.txt \
  UPSTREAM-DEPENDENCY-NOTICES.txt; do
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
service_existed=false
service_was_foreign=false
service_was_disabled=false
if [[ "$service_manager" == "systemd" ]]; then
  existing_unit="$(service_fragment_path)"
  if [[ -n "$existing_unit" ]]; then
    service_existed=true
    if [[ -f "$existing_unit" ]] && ! unit_file_is_managed "$existing_unit"; then
      service_was_foreign=true
      service_enablement_state="$(LC_ALL=C systemctl is-enabled \
        "$REDIS_SERVICE_NAME" 2>/dev/null || true)"
      case "$service_enablement_state" in
        disabled|static|indirect|generated|transient) service_was_disabled=true ;;
        enabled|enabled-runtime|linked|linked-runtime|alias) ;;
        *) die "Refusing to replace $REDIS_SERVICE_NAME with an unsupported enablement state: ${service_enablement_state:-unknown}." ;;
      esac
    fi
  fi
  service_active_state=inactive
  if [[ "$service_existed" == true ]]; then
    service_active_state="$(LC_ALL=C systemctl show \
      --property=ActiveState --value "$REDIS_SERVICE_NAME")"
  fi
  case "$service_active_state" in
    active|reloading) service_was_active=true ;;
    inactive|failed) ;;
    *) die "Refusing to update $REDIS_SERVICE_NAME while its state is ${service_active_state:-unknown}." ;;
  esac
  if [[ "$service_was_active" == true ]]; then
    if [[ "$service_was_foreign" == true ]]; then
      die_message active_foreign_service "$REDIS_SERVICE_NAME"
    fi
    validate_effective_service_contract
  fi
fi

load_account_ownership_from_state
redis_user_existed=false
redis_group_existed=false
id "$REDIS_USER" >/dev/null 2>&1 && redis_user_existed=true
getent group "$REDIS_GROUP" >/dev/null 2>&1 && redis_group_existed=true
data_existed=false
data_metadata=""
if [[ -d "$REDIS_INSTALL_PREFIX/data" ]]; then
  data_existed=true
  data_metadata="$(stat -c '%u:%g:%a' -- "$REDIS_INSTALL_PREFIX/data")"
fi

rollback_needed=false
service_unit_mutation_started=false
service_start_attempted=false
service_enablement_mutated=false
rollback_update() {
  local status="${1:-1}"
  local restore_item restore_file data_uid data_gid data_mode
  local rollback_failed=false retained_new_data=false
  trap - ERR EXIT INT TERM HUP

  if [[ "$rollback_needed" != true ]]; then
    exit "$status"
  fi

  set +e
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    warn "更新失败，正在从 $backup_dir 恢复 Redis ${old_version:-未知版本}。"
  else
    warn "Update failed. Restoring Redis ${old_version:-unknown} from $backup_dir."
  fi

  if [[ "$service_manager" == "systemd" \
    && "$service_start_attempted" == true ]]; then
    systemctl stop "$REDIS_SERVICE_NAME" >/dev/null 2>&1 \
      || rollback_failed=true
  fi
  if [[ "$service_manager" == "systemd" \
    && "$service_enablement_mutated" == true ]]; then
    systemctl disable "$REDIS_SERVICE_NAME" >/dev/null 2>&1 \
      || rollback_failed=true
  fi

  for restore_item in bin conf scripts systemd; do
    rm -rf "$REDIS_INSTALL_PREFIX/$restore_item" || rollback_failed=true
    if [[ -d "$backup_dir/$restore_item" ]]; then
      cp -a "$backup_dir/$restore_item" "$REDIS_INSTALL_PREFIX/$restore_item" \
        || rollback_failed=true
    fi
  done

  for restore_file in \
    PACKAGE-INFO BUILD-INFO LICENSE.txt README.txt THIRD_PARTY_NOTICES.md \
    UPSTREAM-CONTRIBUTOR-LICENSE.txt UPSTREAM-DEPENDENCY-NOTICES.txt; do
    if [[ -f "$backup_dir/$restore_file" ]]; then
      cp -a "$backup_dir/$restore_file" "$REDIS_INSTALL_PREFIX/$restore_file" \
        || rollback_failed=true
    else
      rm -f "$REDIS_INSTALL_PREFIX/$restore_file" || rollback_failed=true
    fi
  done

  if [[ -f "$backup_dir/.redis-package-state" ]]; then
    cp -a "$backup_dir/.redis-package-state" "$REDIS_STATE_FILE" \
      || rollback_failed=true
  else
    rm -f "$REDIS_STATE_FILE" || rollback_failed=true
  fi

  if [[ "$data_existed" == true && -d "$REDIS_INSTALL_PREFIX/data" ]]; then
    IFS=: read -r data_uid data_gid data_mode <<<"$data_metadata"
    chown "$data_uid:$data_gid" "$REDIS_INSTALL_PREFIX/data" \
      || rollback_failed=true
    chmod "$data_mode" "$REDIS_INSTALL_PREFIX/data" \
      || rollback_failed=true
  elif [[ "$data_existed" == false && -d "$REDIS_INSTALL_PREFIX/data" ]]; then
    if ! rmdir "$REDIS_INSTALL_PREFIX/data" 2>/dev/null; then
      retained_new_data=true
      rollback_failed=true
    fi
  fi

  if [[ "$service_manager" == "systemd" ]]; then
    if [[ "$unit_override_existed" == true ]]; then
      cp -a "$backup_dir/redis.service" "$REDIS_SERVICE_UNIT" \
        || rollback_failed=true
    else
      rm -f "$REDIS_SERVICE_UNIT" || rollback_failed=true
    fi
    systemctl daemon-reload || rollback_failed=true
  fi

  if [[ "$retained_new_data" == false \
    && "$redis_user_existed" == false && "$ACCOUNT_CREATED_USER" == true ]]; then
    userdel "$REDIS_USER" >/dev/null 2>&1 || rollback_failed=true
  fi
  if [[ "$retained_new_data" == false \
    && "$redis_group_existed" == false && "$ACCOUNT_CREATED_GROUP" == true ]]; then
    groupdel "$REDIS_GROUP" >/dev/null 2>&1 || rollback_failed=true
  fi

  if [[ "$service_manager" == "systemd" ]]; then
    if [[ "$service_was_active" == true ]]; then
      systemctl start "$REDIS_SERVICE_NAME" || rollback_failed=true
      if [[ "$rollback_failed" == false && "$was_managed" == true ]] \
        && ! wait_for_service; then
        rollback_failed=true
      elif [[ "$rollback_failed" == false ]] \
        && ! systemctl is-active --quiet "$REDIS_SERVICE_NAME"; then
        rollback_failed=true
      fi
    fi
  fi

  if [[ -n "$staged_bin_dir" ]]; then
    rm -rf -- "$staged_bin_dir" || rollback_failed=true
  fi
  if [[ -n "$previous_bin_dir" ]]; then
    rm -rf -- "$previous_bin_dir" || rollback_failed=true
  fi
  if [[ "$rollback_failed" == true ]]; then
    if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
      warn "回滚未能完整恢复；请保留备份 $backup_dir 并手动检查服务。"
    else
      warn "Rollback was incomplete; retain $backup_dir and inspect the service manually."
    fi
    exit 2
  elif [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    warn "更新已回滚，请检查上方错误后重试。"
  else
    warn "Update rolled back. Inspect the error above before retrying."
  fi
  [[ "$status" -ne 0 ]] || status=1
  exit "$status"
}
trap 'rollback_update $?' ERR
trap 'rollback_update $?' EXIT
trap 'rollback_update 130' INT
trap 'rollback_update 143' TERM
trap 'rollback_update 129' HUP

if [[ "$service_manager" == "systemd" && "$service_was_active" == true ]]; then
  rollback_needed=true
  systemctl stop "$REDIS_SERVICE_NAME"
fi
if ! assert_no_live_install_redis_server; then
  exit 1
fi
rollback_needed=true
previous_bin_dir="$(mktemp -d "$REDIS_INSTALL_PREFIX/.bin.previous-${timestamp}.XXXXXX")"
rmdir "$previous_bin_dir"
staged_bin_dir="$(mktemp -d "$REDIS_INSTALL_PREFIX/.bin.new-${timestamp}.XXXXXX")"
chown root:root "$staged_bin_dir"
chmod 0755 "$staged_bin_dir"

cp -a --no-preserve=context,xattr \
  "$PACKAGE_ROOT/bin/." "$staged_bin_dir/"
chown -R root:root "$staged_bin_dir"

mv "$REDIS_INSTALL_PREFIX/bin" "$previous_bin_dir"
mv "$staged_bin_dir" "$REDIS_INSTALL_PREFIX/bin"

rm -rf "$REDIS_INSTALL_PREFIX/scripts" "$REDIS_INSTALL_PREFIX/systemd"
cp -a --no-preserve=context,xattr \
  "$PACKAGE_ROOT/scripts" "$REDIS_INSTALL_PREFIX/"
cp -a --no-preserve=context,xattr \
  "$PACKAGE_ROOT/systemd" "$REDIS_INSTALL_PREFIX/"
chown -R root:root \
  "$REDIS_INSTALL_PREFIX/bin" \
  "$REDIS_INSTALL_PREFIX/scripts" \
  "$REDIS_INSTALL_PREFIX/systemd"

for metadata_file in \
  PACKAGE-INFO BUILD-INFO LICENSE.txt README.txt THIRD_PARTY_NOTICES.md \
  UPSTREAM-CONTRIBUTOR-LICENSE.txt UPSTREAM-DEPENDENCY-NOTICES.txt; do
  if [[ -f "$PACKAGE_ROOT/$metadata_file" ]]; then
    install -o root -g root -m 0644 \
      "$PACKAGE_ROOT/$metadata_file" \
      "$REDIS_INSTALL_PREFIX/$metadata_file"
  fi
done
if [[ ! -f "$PACKAGE_ROOT/UPSTREAM-CONTRIBUTOR-LICENSE.txt" ]]; then
  rm -f "$REDIS_INSTALL_PREFIX/UPSTREAM-CONTRIBUTOR-LICENSE.txt"
fi

ensure_redis_account
install_default_configs "$PACKAGE_ROOT"
prepare_runtime_layout
validate_redis_config_trust "$REDIS_INSTALL_PREFIX/conf/redis.conf"

if [[ "$service_manager" == "systemd" ]]; then
  service_unit_mutation_started=true
  install_service_unit "$REDIS_INSTALL_PREFIX"
  validate_effective_service_contract
  if [[ "$service_existed" == false \
    || ( "$service_was_foreign" == true && "$service_was_disabled" == true ) ]]; then
    service_enablement_mutated=true
    systemctl enable "$REDIS_SERVICE_NAME"
  fi

  should_start=false
  if [[ "$service_was_active" == true ]]; then
    should_start=true
  elif [[ "$was_managed" == false && "$no_start" == false ]]; then
    should_start=true
  fi
  if [[ "$should_start" == true ]]; then
    service_start_attempted=true
    systemctl start "$REDIS_SERVICE_NAME"
    wait_for_service
  fi
fi

write_install_state "$REDIS_INSTALL_PREFIX" "$service_manager"
rm -rf -- "$previous_bin_dir" "$staged_bin_dir"
rollback_needed=false
trap - ERR EXIT INT TERM HUP

if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
  info "Redis 已从 ${old_version:-未知版本} 更新到 $new_version。"
  info "现有配置和数据已保留。"
  info "备份目录：$backup_dir"
  if [[ "$service_manager" == "systemd" ]]; then
    info "服务状态：systemctl status $REDIS_SERVICE_NAME"
  else
    info "未管理系统服务；更新器不会启动 Redis。请按需手动启动新二进制。"
  fi
else
  info "Redis updated from ${old_version:-unknown} to $new_version."
  info "Existing configuration and data were preserved."
  info "Backup: $backup_dir"
  if [[ "$service_manager" == "systemd" ]]; then
    info "Service: systemctl status $REDIS_SERVICE_NAME"
  else
    info "No system service was managed; the updater did not start Redis. Start the new binary manually when needed."
  fi
fi
