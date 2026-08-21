# Redis 非官方构建

[English](README.md)

本仓库提供按版本发布的 Redis 非官方二进制包。当前已实现 x64 与 ARM64 Linux
压缩包构建，并维护其他 ABI 和操作系统后端的审查方案。

> 本项目与 Redis Ltd. 无关联，也未获得其背书。Redis 及其捆绑依赖仍受每个包内
> 许可证和 notices 文件的约束。

## 可用包

只有标记为“已实现”的平台才具备发布资格。

| 包变体 | 架构 | 运行要求 | 状态 |
| --- | --- | --- | --- |
| `linux-glibc2.28` | `x64` | Linux、glibc 2.28+；默认 systemd，可使用 `--no-service` | 已实现 |
| `linux-glibc2.28` | `arm64` | Linux、glibc 2.28+；默认 systemd，可使用 `--no-service` | 已实现 |
| `linux-glibc2.17-legacy` | `x64` / `arm64` | 旧版 glibc Linux、systemd | 仅设计 |
| `linux-musl1.2` | `x64` / `arm64` | musl Linux、OpenRC | 仅设计 |
| `macos12` | `x64` / `arm64` | macOS、launchd | 仅设计 |
| `windows-msys2` | `x64` | Windows 服务控制管理器 | 仅设计 |
| `windows-cygwin` | `x64` | Windows 服务控制管理器 | 仅设计 |

当前已实现的发布目标仅支持 Linux。同架构的 glibc 2.28 包通常可在更高版本 glibc 上运行；
Alpine 等 musl 系统需要单独的 musl 后端，该后端尚未实现。Linux 包是普通
`.tar.gz` 压缩包，不依赖 RPM、DEB、Snap 或 APK。正式产物从仓库的
[GitHub Releases](https://github.com/ainuoyan/redis-unofficial-builds/releases)
下载。

使用下文生命周期操作前，必须确认所选 Release 精确包含下一节定义的 7 个当前格式
产物。旧的 4 产物 Release 属于历史二进制包，不包含当前的生命周期脚本和元数据；
自动化会拒绝修改或补全这些 Release。

当前 Linux 工作流生成的包固定安装到 `/usr/local/redis`，采用 `core` 构建配置：
包含 Redis 服务端及命令行程序，不包含 Redis 8 源码包捆绑的模块。TLS 未启用，
与默认 Redis `make` 构建一致。

平台状态和验收标准见[多平台发布方案](docs/PLATFORM-DESIGN.zh-CN.md)。Windows
方案固定参考
[`redis-windows/redis-windows`](https://github.com/redis-windows/redis-windows)
提交
[`17fd667560f7903820dcabeebb9d20ade1159fe9`](https://github.com/redis-windows/redis-windows/commit/17fd667560f7903820dcabeebb9d20ade1159fe9)。
当前不宣称存在 Windows 包或原生 Windows ARM64 支持。详见
[Windows issue 覆盖表](docs/WINDOWS-ISSUE-COVERAGE.md)和
[第三方说明](THIRD_PARTY_NOTICES.md)。

## Release 与包内容

当前 Linux 发布器生成的每个 Release 必须恰好包含 7 个产物。对于确切 Redis
版本 `X.Y.Z`，文件名为：

```text
Redis-Rzon-X.Y.Z-linux-glibc2.28-x64.tar.gz
Redis-Rzon-X.Y.Z-linux-glibc2.28-x64.tar.gz.sha256
Redis-Rzon-X.Y.Z-linux-glibc2.28-arm64.tar.gz
Redis-Rzon-X.Y.Z-linux-glibc2.28-arm64.tar.gz.sha256
SHA256SUMS
manifest.json
redis-unofficial-builds-X.Y.Z.spdx.json
```

`SHA256SUMS` 覆盖其余 6 个产物。`manifest.json` 绑定 Redis 源码校验和、
不可变 `redis-hashes` 快照提交、打包提交、补丁集校验和、架构、ABI 基线、包
大小及包校验和。SPDX 2.3 文件是 Redis 源码及两个发布包的
**Release 包级清单**，不是文件级或完整传递依赖 SBOM。

所有当前 `PACKAGE_FORMAT=2` 压缩包都采用以下 `redis/` 布局；较早 Redis 版本的
贡献者许可文件按上游是否提供而定：

```text
redis/
├── bin/
├── conf/
│   ├── redis.conf
│   └── sentinel.conf
├── scripts/
│   ├── common.sh
│   ├── install.sh
│   ├── update.sh
│   └── uninstall.sh
├── systemd/
│   ├── redis.service
│   └── redis-hardening.conf.example
├── PACKAGE-INFO
├── BUILD-INFO
├── LICENSE.txt
├── THIRD_PARTY_NOTICES.md
├── UPSTREAM-CONTRIBUTOR-LICENSE.txt
├── UPSTREAM-DEPENDENCY-NOTICES.txt
└── README.txt
```

Redis 7.4 及以上版本的 `UPSTREAM-CONTRIBUTOR-LICENSE.txt` 是已校验官方源码包
根目录 `REDISCONTRIBUTIONS.txt` 的逐字节副本。更早版本仅在官方源码存在该文件
时包含它，不生成占位许可文本。

`UPSTREAM-DEPENDENCY-NOTICES.txt` 按确定顺序汇总已校验 Redis 源码树 `deps/`
下识别出的许可证、notice、版权和 README 文件；每段记录上游相对路径与字节长度。
它只作为 `LICENSE.txt`、可能存在的 `UPSTREAM-CONTRIBUTOR-LICENSE.txt` 和
`THIRD_PARTY_NOTICES.md` 的补充，不表示自动扫描已经识别所有法律义务。

`PACKAGE-INFO` 和 `BUILD-INFO` 记录包约定和来源。生命周期脚本会验证这些文件，
并先以无特权、no-new-privileges 身份执行包内 `redis-server --version`，通过后
才修改系统。

### 校验下载

将 `X.Y.Z` 替换为当前格式 Release 的确切版本，并在解压前校验压缩包：

```bash
version=X.Y.Z
archive="Redis-Rzon-${version}-linux-glibc2.28-x64.tar.gz"
sha256sum -c "${archive}.sha256"
```

如果同一目录中已有全部 7 个产物，可校验完整集合：

```bash
sha256sum -c SHA256SUMS
```

相邻 `.sha256` 和 `SHA256SUMS` 与压缩包处于同一 GitHub Release 信任边界，
均不是独立签名。当前工作流还为 7 个产物生成 SLSA 来源证明，并为两个压缩包生成
SPDX 证明。安装 [GitHub CLI](https://cli.github.com/) 后，应分别验证两种 predicate：

```bash
gh attestation verify \
  "$archive" \
  --repo ainuoyan/redis-unofficial-builds \
  --predicate-type https://slsa.dev/provenance/v1 \
  --deny-self-hosted-runners

gh attestation verify \
  "$archive" \
  --repo ainuoyan/redis-unofficial-builds \
  --predicate-type https://spdx.dev/Document/v2.3 \
  --deny-self-hosted-runners
```

发布器在发布前进一步校验工作流身份、签名者和源码仓库摘要（两者均绑定打包提交）、
受保护默认分支、predicate 类型，并拒绝自托管 Runner 证明。

## Linux 运行依赖

生命周期脚本必须以 root 运行，并需要：

- 文档中的校验和解压命令需要 `sha256sum` 与 GNU `tar`；
- Bash；
- coreutils/findutils 常用 GNU 命令，以及 `awk`、`grep`、`sed`；
- util-linux 的 `flock`、`setpriv`；
- `getent`、`groupadd`、`groupdel`、`useradd`、`userdel` 等账号管理命令；
- 使用服务模式时需要正在运行的 systemd 和 `systemctl`；`--no-service` 则执行
  完整的受管安装，但不要求或注册 systemd；
- 当前包变体要求 glibc 2.28 或以上。脚本要求使用 `getconf` 验证主机 glibc 版本，
  随后还会实际执行新二进制完成最终兼容性检查。

不同发行版的软件包名不同，常见提供者包括 `bash`、`coreutils`、`tar`、
`findutils`、`gawk`、`grep`、`sed`、`util-linux`、`shadow-utils`、`glibc` 和
`systemd`。

## Linux 安装

必须解压到 root 所有的暂存目录。包目录及其文件需要由 root 控制、组和其他用户
不可写、没有扩展 ACL、异常链接或特殊文件。不要从普通用户所有的下载目录直接执行
生命周期脚本。

```bash
version=X.Y.Z
archive="Redis-Rzon-${version}-linux-glibc2.28-x64.tar.gz"
sha256sum -c "${archive}.sha256"
stage="$(sudo mktemp -d /var/tmp/redis-unofficial-builds.XXXXXX)"
sudo chmod 0755 "$stage"
sudo sh -c 'umask 022; exec tar --no-same-owner --no-same-permissions -xzf "$1" -C "$2"' sh \
  "$archive" "$stage"
sudo "$stage/redis/scripts/install.sh"
```

全新默认安装会：

- 安装到 `/usr/local/redis`；
- 创建或安全复用 `redis` 服务账号；
- 创建 `/usr/local/redis/data`，并启用权限为 `0770` 的本地 Unix socket
  `/usr/local/redis/data/redis.sock`；
- 设置 `port 0`，默认关闭 TCP；
- 安装、启用并启动 `/etc/systemd/system/redis.service`；
- 在 root 所有、权限 `0600` 的状态文件中记录包、账号和服务状态。

测试默认 Socket：

```bash
sudo -u redis /usr/local/redis/bin/redis-cli \
  -s /usr/local/redis/data/redis.sock PING
```

只有在审查 `bind`、`port`、保护模式、ACL、防火墙和传输安全后才应启用 TCP。
本包未编译 TLS。

常用安装模式：

```bash
# 注册并启用服务，但暂不启动
sudo "$stage/redis/scripts/install.sh" --no-start

# 执行完整受管安装，但不要求或注册 systemd
sudo "$stage/redis/scripts/install.sh" --no-service

# 保留并接管已有 conf/ 或 data/
sudo "$stage/redis/scripts/install.sh" --adopt
```

`--no-service` 仍会创建或安全复用 `redis` 账号，安装配置和数据目录，并记录包元数据
及生命周期状态，但不会注册或启动服务。更新或卸载此模式的安装前，必须手工停止所有
可执行文件精确为 `/usr/local/redis/bin/redis-server` 的进程；只要仍有此类进程，
脚本就会拒绝修改或删除安装。

### 账号、服务与配置安全边界

已有 `redis` 用户只有同时满足以下条件才会复用：UID 非 0；`redis` 是主组和唯一
所属组；登录 Shell 为 `nologin` 或 `false`；home 是不含 `.` 或 `..` 路径段的规范
绝对路径。已有 `redis` 组的 GID 必须非 0。对于当前格式状态，受管安装还要求已记录的
UID、主 GID、home、shell 及附加组集合均未变化。旧格式状态升级时不会继承“由项目创建
用户/组”的归属标志，因为旧格式无法证明完整身份。

其他安装管理的 `redis.service` 默认拒绝替换。`--force-service` 只允许替换状态为
`inactive` 或 `failed` 的外部单元；`active` 或 `reloading` 的外部服务即使指定
`--force-service` 也始终拒绝，必须由管理员先停止并检查。替换 disabled 外部单元时
会启用新的受管单元；若事务失败，回滚会先禁用新单元再恢复外部单元。原本 enabled
的外部单元保持启用状态。对于正在运行的受管服务，有效单元及所有 drop-in 必须
保持预期用户、组、工作目录、命令、环境、凭据、执行
钩子和 no-new-privileges 约定。

安装、接管、更新或启动服务前，配置可信检查会递归跟踪 `include`（最多 64 个唯一
文件），并验证 `loadmodule` 与 `aclfile` 引用。引用路径：

- 可以是绝对路径，或相对于 `/usr/local/redis`；
- 不得包含空白、glob 通配符或反斜杠；
- 路径组件不得为符号链接；
- 必须指向 root 所有、仅一个硬链接的普通文件；父目录链也必须由 root 所有、
  组和其他用户不可写且没有扩展 ACL。

因此，即使 Redis 本身能解析，生命周期校验也不接受带空白的引号路径。
`loadmodule` 可以保留模块参数，但模块文件本身仍会校验。这些限制用于防止以 root
执行维护时由配置替换文件；维护前应先修正不安全的所有权或引用。

同一信任约定要求配置的 `aclfile` 由 root 所有且组和其他用户不可写，因此 `redis`
服务账号不能通过 `ACL SAVE` 更新它；本生命周期约定不支持直接写入受管
`aclfile`。应由 root 离线部署 ACL 变更并重启 Redis，或使用等效的站点流程，在运行
任何 root 生命周期脚本前恢复可信所有权和权限。不要仅为启用 `ACL SAVE` 放宽 ACL
文件权限后再对其执行包维护。

基础 systemd 单元让 `redis.conf` 决定数据、日志、TLS、ACL、模块和 include 路径。
附带的严格加固 drop-in 仅为样例，其中可写路径必须按实际配置调整。

## Linux 更新与接管

把已校验的新包解压到新的 root 控制暂存目录，然后运行：

```bash
sudo "$stage/redis/scripts/update.sh"
```

更新器会在停止 Redis 前校验新包和二进制，把旧程序、配置、脚本、服务单元、
notices、元数据及状态备份到 `/usr/local/redis-backups/`，替换程序文件并保留配置
和数据。它以 Redis 协议响应而不是仅凭 systemd active 状态判断就绪。启动失败或
收到 `INT`、`TERM`、`HUP` 时会回滚受管程序和服务状态。

安装或更新后的就绪检查默认预算为 30 秒。Redis 恢复大数据集时会以 `-LOADING`
应答 PING；若预算在此状态耗尽，脚本会明确报告正在加载数据集，而不是笼统的启动
失败。可用 `sudo REDIS_READY_TIMEOUT=<秒> ...`（1–99999）按次增大预算。

默认拒绝降级：

```bash
sudo "$stage/redis/scripts/update.sh" --allow-downgrade
```

只有在确认 Redis 降级兼容性并创建独立、应用一致的数据快照后，才应使用
`--allow-downgrade`。自动备份**不会**复制数据目录。`SIGKILL`、主机故障或存储
故障无法执行 Shell 回滚代码。

普通卸载会保留生命周期状态；之后用较旧包重新安装也会默认拒绝降级。只有完成同样
的兼容性检查并创建独立快照后，管理员才应使用 `install.sh --allow-downgrade`。

未受本项目管理的安装需要显式接管：

```bash
sudo "$stage/redis/scripts/update.sh" --adopt
sudo "$stage/redis/scripts/update.sh" --adopt --no-service
```

接管要求固定 `/usr/local/redis` 布局、由 root 控制的程序和配置路径、兼容服务
账号及可信配置引用；已有配置和数据内容会保留。普通更新保留状态文件记录的服务
模式、启用状态及运行状态。

安装、更新和卸载共用排他锁。递归替换或删除的目标本身或任一后代是挂载点时，操作
会被拒绝。挂载数据应放在受管程序树之外，再由 `redis.conf` 引用。

安装和更新脚本不会从暂存包复制 SELinux 标签或扩展属性。在 SELinux enforcing
主机上，如果文件系统默认标签不足，应在启动服务前应用站点策略或执行相应重标记
操作，例如已配置策略时使用 `restorecon`。如果必须先重标记再执行 Redis，全新安装
应使用 `--no-start`；更新前应先停止受管服务，完成更新和重标记后再启动。

## Linux 卸载

删除服务和程序，但保留 `conf/`、`data/`、安装状态、备份和服务账号：

```bash
sudo /usr/local/redis/scripts/uninstall.sh
```

同时删除整个安装前缀，包括配置和数据：

```bash
sudo /usr/local/redis/scripts/uninstall.sh --purge
```

对于当前格式的状态，只有项目创建的 `redis` 账号仍精确匹配状态中记录的 UID、主
GID、home、shell，且没有附加组时，`--purge` 才删除账号；用户组还必须保持记录的
GID，且不能出现非预期的显式成员。缺少完整身份记录的旧状态会按保守策略保留用户和
组。系统原有账号和 `/usr/local/redis-backups/` 始终保留。挂载点检查同样适用于
彻底卸载。

## 中英文界面

维护脚本提供英文和简体中文帮助与常规信息。语言从 `LC_ALL`、`LC_MESSAGES` 或
`LANG` 选择，也可覆盖：

```bash
sudo env REDIS_INSTALL_LANG=en "$stage/redis/scripts/install.sh"
sudo env REDIS_INSTALL_LANG=zh_CN "$stage/redis/scripts/install.sh"
```

机器可读命令固定使用 C locale。部分启动校验和回滚安全诊断会使用固定双语或
locale 无关格式。

## 兼容性与测试

- x64 使用 `ubuntu-24.04` Runner；ARM64 使用 `ubuntu-24.04-arm`。
- 实际编译在按摘要固定的多架构 Rocky Linux 8 镜像内完成，glibc 基线为 2.28。
  Runner 只提供内核，不提供链接用 glibc。
- 所有 ELF 都检查架构、缺失依赖及最高 `GLIBC_*` 符号，不能超过
  `GLIBC_2.28`。
- 不使用 `-march=native`。
- 两个架构均执行官方源码校验、上游测试、PING/SET/GET 冒烟测试和生命周期集成
  测试。
- 生命周期集成测试在 GNU/Linux 用户态执行脚本；缺少 GNU `stat -c` 与
  `realpath -e` 的主机（例如 macOS）会跳过这些用例而不是报错。

Rocky 软件源依赖在构建时解析，因此会记录编译器/运行库信息，但不宣称压缩包可
逐字节复现。`ARM64-COW-BUG` 等内核警告不会被自动忽略。

## 发布自动化与不可变策略

[Linux 工作流](.github/workflows/build-linux.yml)只有手工和 `workflow_call`
入口，没有 push 或 schedule 触发器；发布默认关闭。

当前发布器只创建全新的纯数字 `X.Y.Z` Release 和 Tag：

1. 要求两个架构和完整、精确的 7 个产物；
2. 只允许从受保护默认分支经名为 `release` 的 GitHub Environment 运行；
3. 一次创建包含所有产物的草稿 Release；
4. 把每个远端产物的数字 ID、字节数和 GitHub SHA-256 摘要绑定到已校验本地文件，
   再下载并重新验证草稿；
5. 临发布前再次核对同一草稿及产物身份，并按数字 Release ID 发布；
6. 发布后再次回读产物身份、下载文件并执行语义及证明校验。

发布器绝不向已有 Release 添加、覆盖、删除产物，也不会补全或修复它。已有的残缺、
旧格式、草稿、预发布或额外产物 Release 都属于阻塞项，需要维护者检查。精确完整的
已有 Release 会被下载并重新校验内容及证明，然后跳过。该流水线策略无法阻止其他
获授权仓库写入者在最终 API 竞争窗口修改草稿，也不能阻止其以后修改可变的正式
Release。生产发布前**必须启用仓库级 Immutable Releases**，并把 Release 写权限
限制在经审查的发布路径和可信维护者。
手工指定 `force_rebuild` 可在完成上述校验后生成不发布的工作流产物，但不能重新
发布该 Release。

工作流文件无法创建仓库保护设置。管理员必须为默认分支配置保护规则或 ruleset，
为 `release` Environment 配置必需审核人和部署分支限制，并启用仓库级
Immutable Releases。

[计划工作流](.github/workflows/resolve-versions.yml)每日、相关控制器变更进入 `main`
或手工触发时运行。它先校验配置，把 `redis/redis-hashes` 的 `master` 解析为不可变
40 位提交，再从该提交下载哈希索引并把提交写入输出。它只生成计划，不能触发构建或
发布 Release。新的 Redis `X.Y` 系列必须经配置审查后登记。详见
[发布控制器说明](docs/RELEASE-CONTROLLER.md)。

## 本地构建 Linux 包

必须以非 root 用户在一次性 Rocky Linux 8 容器或等效受控 glibc 2.28 环境中执行。
Redis 构建和测试目标会运行上游源码中的程序，因此环境中不应存在无关凭据或生产
安装。脚本会拒绝 UID 0，也会在 `/usr/local/redis` 已存在时拒绝运行。

```bash
export REDIS_VERSION=7.4.11
export REDIS_SOURCE_SHA256=3c266ece0abd54ed3b1c912c6eb86b7508cf382cb690ee6649d3843f018f6357
export REDIS_HASHES_COMMIT=0123456789abcdef0123456789abcdef01234567
export PACKAGING_REVISION="$(git rev-parse HEAD)"
export PACKAGE_VARIANT=linux-glibc2.28
export GLIBC_BASELINE=2.28
export EXPECTED_MACHINE_ARCH="$(uname -m)"

case "$(uname -m)" in
  x86_64) export PACKAGE_ARCH=x64 ;;
  aarch64) export PACKAGE_ARCH=arm64 ;;
  *) echo "Unsupported architecture"; exit 1 ;;
esac

./scripts/linux/build-redis.sh
```

示例中的 `REDIS_HASHES_COMMIT` 必须替换为取得官方 SHA-256 时对应的真实提交。
`PACKAGING_REVISION` 必须是当前已审查打包源码的 40 位 Git 提交。两者都会写入
`BUILD-INFO`，并由 Release manifest 绑定。

## 许可证与商标

Redis 许可按版本不同：7.2.x 及更早版本使用 BSD-3-Clause；7.4.x 至 7.8.x 使用
RSALv2/SSPLv1 双许可；Redis 8 及以上可选择 RSALv2、SSPLv1 或 AGPLv3。应以包内
`LICENSE.txt` 和 Redis 官方[许可说明](https://redis.io/legal/licenses/)为准。
Redis 名称和标识仍受官方
[Redis Trademark Policy](https://redis.io/legal/trademark-policy/)约束。

本仓库自行编写的构建和打包代码使用 [MIT License](LICENSE)。适用时，
`UPSTREAM-CONTRIBUTOR-LICENSE.txt` 保留上游贡献者许可文本，
`UPSTREAM-DEPENDENCY-NOTICES.txt` 汇总对应已校验 Redis 源码中的依赖 notices，
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)记录项目引用和归属。这些材料仅供
说明，不构成法律建议；再次分发者应自行确认并履行对应 Redis 版本和使用方式的义务。
