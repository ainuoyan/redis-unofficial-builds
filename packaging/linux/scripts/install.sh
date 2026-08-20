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
bootstrap_validate_file "$SCRIPT_DIR/install.sh"
bootstrap_validate_file "$SCRIPT_DIR/common.sh"

# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

show_help() {
  if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
    cat <<'EOF'
用法：sudo ./install.sh [--no-start] [--no-service] [--adopt] [--force-service]
                         [--allow-downgrade]

将 Redis 安装到 /usr/local/redis。默认注册、启用并启动 redis.service。

选项：
  --no-start       注册并启用服务，但暂不启动。
  --no-service     安装完整包布局，但不注册或要求 systemd。
  --adopt          接管已有配置或数据目录；不会覆盖 conf/ 和 data/。
  --force-service  仅替换其他软件管理但未运行的 redis.service；正在运行的外部服务始终拒绝替换。
  --allow-downgrade
                   仅在保留了旧安装状态的重装中，明确允许安装更旧的 Redis；执行前必须另做数据快照。
  -h, --help       显示帮助。

语言由 LC_ALL、LC_MESSAGES 或 LANG 自动选择，也可设置
REDIS_INSTALL_LANG=en 或 REDIS_INSTALL_LANG=zh_CN。
EOF
  else
    cat <<'EOF'
Usage: sudo ./install.sh [--no-start] [--no-service] [--adopt] [--force-service]
                         [--allow-downgrade]

Install Redis into /usr/local/redis. By default, redis.service is registered,
enabled, and started.

Options:
  --no-start       Register and enable the service without starting it.
  --no-service     Install the complete package layout without registering or
                   requiring systemd.
  --adopt          Adopt existing configuration or data; conf/ and data/ are preserved.
  --force-service  Replace an inactive redis.service managed by another
                   installation; an active external service is always rejected.
  --allow-downgrade
                   Explicitly reinstall an older Redis version when retained
                   project state exists; take a separate data snapshot first.
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
allow_downgrade=false

while (($# > 0)); do
  case "$1" in
    --no-start) no_start=true ;;
    --no-service) no_service=true ;;
    --adopt) adopt=true ;;
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
require_commands awk chmod chown cp date dirname env find flock getent grep groupadd head id install ls mktemp mv readlink realpath rm rmdir sed setpriv sha256sum sleep stat timeout uname useradd userdel groupdel
acquire_lifecycle_lock
validate_package_root_security "$PACKAGE_ROOT"
validate_package_root "$PACKAGE_ROOT"
preflight_package_compatibility "$PACKAGE_ROOT"
validate_state_file
validate_install_prefix_parent

if managed_install_exists; then
  retained_version="$(state_value REDIS_VERSION)"
  package_version="$(package_info_value "$PACKAGE_ROOT" REDIS_VERSION)"
  if ! redis_version_is_at_least "$package_version" "$retained_version" \
    && [[ "$allow_downgrade" != true ]]; then
    if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
      die "默认拒绝从保留状态中的 Redis $retained_version 降级重装到 $package_version；确认已创建独立数据快照后，使用 --allow-downgrade 明确继续。"
    else
      die "Refusing to reinstall Redis $package_version over retained state for $retained_version; after taking a separate data snapshot, re-run with --allow-downgrade."
    fi
  fi
fi

if [[ -e "$REDIS_INSTALL_PREFIX" || -L "$REDIS_INSTALL_PREFIX" ]]; then
  validate_existing_prefix_paths
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
  if [[ -f "$REDIS_INSTALL_PREFIX/conf/redis.conf" ]]; then
    validate_redis_config_trust "$REDIS_INSTALL_PREFIX/conf/redis.conf"
  fi
fi

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

if ! managed_install_exists; then
  for unmanaged_path in \
    bin scripts systemd PACKAGE-INFO BUILD-INFO LICENSE.txt README.txt \
    THIRD_PARTY_NOTICES.md UPSTREAM-CONTRIBUTOR-LICENSE.txt \
    UPSTREAM-DEPENDENCY-NOTICES.txt; do
    if [[ -e "$REDIS_INSTALL_PREFIX/$unmanaged_path" \
      || -L "$REDIS_INSTALL_PREFIX/$unmanaged_path" ]]; then
      die_message unmanaged_install
    fi
  done
fi

service_manager=systemd
if [[ "$no_service" == true ]]; then
  service_manager=none
else
  require_systemd_runtime
  assert_service_slot_available "$force_service"
fi

# Apart from acquiring the shared lifecycle lock, no installation, account, or
# service mutation occurs before all compatibility and service checks succeed.
load_account_ownership_from_state
redis_user_existed=false
redis_group_existed=false
id "$REDIS_USER" >/dev/null 2>&1 && redis_user_existed=true
getent group "$REDIS_GROUP" >/dev/null 2>&1 && redis_group_existed=true

prefix_existed=false
data_existed=false
prefix_metadata=""
data_metadata=""
if [[ -d "$REDIS_INSTALL_PREFIX" ]]; then
  prefix_existed=true
  prefix_metadata="$(stat -c '%u:%g:%a' -- "$REDIS_INSTALL_PREFIX")"
fi
if [[ -d "$REDIS_INSTALL_PREFIX/data" ]]; then
  data_existed=true
  data_metadata="$(stat -c '%u:%g:%a' -- "$REDIS_INSTALL_PREFIX/data")"
fi

if [[ -e "$REDIS_BACKUP_ROOT" || -L "$REDIS_BACKUP_ROOT" ]]; then
  assert_root_owned_directory "$REDIS_BACKUP_ROOT"
else
  install -d -o root -g root -m 0700 "$REDIS_BACKUP_ROOT"
fi
install_backup_dir="$(mktemp -d \
  "$REDIS_BACKUP_ROOT/install-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")"
chown root:root "$install_backup_dir"
chmod 0700 "$install_backup_dir"
backup_has_prior_content=false
for backup_item in \
  bin conf scripts systemd PACKAGE-INFO BUILD-INFO LICENSE.txt README.txt \
  THIRD_PARTY_NOTICES.md UPSTREAM-CONTRIBUTOR-LICENSE.txt \
  UPSTREAM-DEPENDENCY-NOTICES.txt; do
  if [[ -e "$REDIS_INSTALL_PREFIX/$backup_item" ]]; then
    cp -a "$REDIS_INSTALL_PREFIX/$backup_item" "$install_backup_dir/"
    backup_has_prior_content=true
  fi
done
if [[ -f "$REDIS_STATE_FILE" ]]; then
  cp -a "$REDIS_STATE_FILE" "$install_backup_dir/.redis-package-state"
  backup_has_prior_content=true
fi

unit_override_existed=false
service_was_active=false
service_existed=false
service_was_foreign=false
service_was_disabled=false
if [[ "$service_manager" == "systemd" ]]; then
  existing_unit="$(service_fragment_path)"
  if [[ -n "$existing_unit" ]]; then
    service_existed=true
  fi
  if [[ -f "$REDIS_SERVICE_UNIT" ]]; then
    unit_override_existed=true
    cp -a "$REDIS_SERVICE_UNIT" "$install_backup_dir/redis.service.override"
    backup_has_prior_content=true
  fi
  if [[ -n "$existing_unit" && -f "$existing_unit" ]] \
    && ! unit_file_is_managed "$existing_unit"; then
    service_was_foreign=true
    service_enablement_state="$(LC_ALL=C systemctl is-enabled \
      "$REDIS_SERVICE_NAME" 2>/dev/null || true)"
    case "$service_enablement_state" in
      disabled|static|indirect|generated|transient) service_was_disabled=true ;;
      enabled|enabled-runtime|linked|linked-runtime|alias) ;;
      *) die "Refusing to replace $REDIS_SERVICE_NAME with an unsupported enablement state: ${service_enablement_state:-unknown}." ;;
    esac
    cp -a "$existing_unit" "$install_backup_dir/redis.service.fragment"
    backup_has_prior_content=true
    if [[ "$REDIS_UI_LANGUAGE" == "zh" ]]; then
      info "现有服务单元已备份到 $install_backup_dir/redis.service.fragment"
    else
      info "Existing service unit backed up to $install_backup_dir/redis.service.fragment"
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
    *) die "Refusing to replace $REDIS_SERVICE_NAME while its state is ${service_active_state:-unknown}." ;;
  esac
  if [[ "$service_was_active" == true ]]; then
    if [[ "$service_was_foreign" == true ]]; then
      die_message active_foreign_service "$REDIS_SERVICE_NAME"
    fi
    validate_effective_service_contract
  fi
fi

install_rollback_needed=false
service_unit_mutation_started=false
service_start_attempted=false
service_enablement_mutated=false
rollback_install() {
  local status="${1:-1}"
  local restore_item restore_file prefix_uid prefix_gid prefix_mode
  local data_uid data_gid data_mode rollback_failed=false retained_new_data=false
  trap - ERR EXIT INT TERM HUP

  if [[ "$install_rollback_needed" != true ]]; then
    exit "$status"
  fi

  set +e
  warn "Installation failed; restoring the previous host state from $install_backup_dir. / 安装失败，正在恢复先前的主机状态。"

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
    if [[ -d "$install_backup_dir/$restore_item" ]]; then
      cp -a "$install_backup_dir/$restore_item" "$REDIS_INSTALL_PREFIX/$restore_item" \
        || rollback_failed=true
    fi
  done
  for restore_file in \
    PACKAGE-INFO BUILD-INFO LICENSE.txt README.txt THIRD_PARTY_NOTICES.md \
    UPSTREAM-CONTRIBUTOR-LICENSE.txt UPSTREAM-DEPENDENCY-NOTICES.txt; do
    if [[ -f "$install_backup_dir/$restore_file" ]]; then
      cp -a "$install_backup_dir/$restore_file" "$REDIS_INSTALL_PREFIX/$restore_file" \
        || rollback_failed=true
    else
      rm -f "$REDIS_INSTALL_PREFIX/$restore_file" || rollback_failed=true
    fi
  done
  if [[ -f "$install_backup_dir/.redis-package-state" ]]; then
    cp -a "$install_backup_dir/.redis-package-state" "$REDIS_STATE_FILE" \
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

  if [[ "$service_manager" == "systemd" \
    && "$service_unit_mutation_started" == true ]]; then
    if [[ "$unit_override_existed" == true ]]; then
      cp -a "$install_backup_dir/redis.service.override" "$REDIS_SERVICE_UNIT" \
        || rollback_failed=true
    else
      rm -f "$REDIS_SERVICE_UNIT" || rollback_failed=true
    fi
    systemctl daemon-reload || rollback_failed=true
    if [[ "$service_was_active" == true ]]; then
      systemctl start "$REDIS_SERVICE_NAME" || rollback_failed=true
      systemctl is-active --quiet "$REDIS_SERVICE_NAME" \
        || rollback_failed=true
    fi
  fi

  if [[ "$retained_new_data" == false ]]; then
    if [[ "$redis_user_existed" == false && "$ACCOUNT_CREATED_USER" == true ]]; then
      userdel "$REDIS_USER" >/dev/null 2>&1 || rollback_failed=true
    fi
    if [[ "$redis_group_existed" == false && "$ACCOUNT_CREATED_GROUP" == true ]]; then
      groupdel "$REDIS_GROUP" >/dev/null 2>&1 || rollback_failed=true
    fi
  fi

  if [[ "$prefix_existed" == true && -d "$REDIS_INSTALL_PREFIX" ]]; then
    IFS=: read -r prefix_uid prefix_gid prefix_mode <<<"$prefix_metadata"
    chown "$prefix_uid:$prefix_gid" "$REDIS_INSTALL_PREFIX" \
      || rollback_failed=true
    chmod "$prefix_mode" "$REDIS_INSTALL_PREFIX" \
      || rollback_failed=true
  elif [[ "$prefix_existed" == false && -d "$REDIS_INSTALL_PREFIX" ]]; then
    rmdir "$REDIS_INSTALL_PREFIX" 2>/dev/null || rollback_failed=true
  fi

  if [[ "$rollback_failed" == true ]]; then
    warn "Installation rollback was incomplete; retain $install_backup_dir and inspect the host manually. / 安装回滚不完整，请保留备份并手工检查。"
    exit 2
  fi
  warn "Installation was rolled back. / 安装已回滚。"
  [[ "$status" -ne 0 ]] || status=1
  exit "$status"
}
trap 'rollback_install $?' ERR
trap 'rollback_install $?' EXIT
trap 'rollback_install 130' INT
trap 'rollback_install 143' TERM
trap 'rollback_install 129' HUP

assert_no_live_install_redis_server
install_rollback_needed=true
ensure_redis_account

install -d -o root -g root -m 0755 "$REDIS_INSTALL_PREFIX"
rm -rf \
  "$REDIS_INSTALL_PREFIX/bin" \
  "$REDIS_INSTALL_PREFIX/scripts" \
  "$REDIS_INSTALL_PREFIX/systemd"
cp -a --no-preserve=context,xattr \
  "$PACKAGE_ROOT/bin" "$REDIS_INSTALL_PREFIX/"
cp -a --no-preserve=context,xattr \
  "$PACKAGE_ROOT/scripts" "$REDIS_INSTALL_PREFIX/"
cp -a --no-preserve=context,xattr \
  "$PACKAGE_ROOT/systemd" "$REDIS_INSTALL_PREFIX/"

install_default_configs "$PACKAGE_ROOT"
validate_redis_config_trust "$REDIS_INSTALL_PREFIX/conf/redis.conf"
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

chown -R root:root \
  "$REDIS_INSTALL_PREFIX/bin" \
  "$REDIS_INSTALL_PREFIX/scripts" \
  "$REDIS_INSTALL_PREFIX/systemd"
prepare_runtime_layout

if [[ "$service_manager" == "systemd" ]]; then
  service_unit_mutation_started=true
  install_service_unit "$REDIS_INSTALL_PREFIX"
  validate_effective_service_contract
  if [[ "$service_existed" == false \
    || ( "$service_was_foreign" == true && "$service_was_disabled" == true ) ]]; then
    service_enablement_mutated=true
    systemctl enable "$REDIS_SERVICE_NAME"
  fi
fi

write_install_state "$REDIS_INSTALL_PREFIX" "$service_manager"

if [[ "$service_manager" == "systemd" && "$no_start" == false ]]; then
  service_start_attempted=true
  if systemctl is-active --quiet "$REDIS_SERVICE_NAME"; then
    systemctl restart "$REDIS_SERVICE_NAME"
  else
    systemctl start "$REDIS_SERVICE_NAME"
  fi
  wait_for_service
fi

install_rollback_needed=false
trap - ERR EXIT INT TERM HUP
if [[ "$backup_has_prior_content" == false ]]; then
  rmdir "$install_backup_dir"
fi

installed_version="$(package_info_value "$REDIS_INSTALL_PREFIX" REDIS_VERSION)"
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
