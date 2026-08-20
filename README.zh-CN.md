# Redis 非官方构建

[English](README.md)

自动编译、测试并发布 Redis 非官方二进制包。本仓库统一管理不同操作系统、
Linux ABI 基线和 CPU 架构的构建，同一 Redis 版本的所有产物发布到同一个
GitHub Release。

> 本项目与 Redis Ltd. 无关联，也未获得其背书。Redis 二进制仍受每个压缩包
> 中附带的上游许可证约束。

## 当前支持

| 包类型 | 架构 | 运行要求 | 状态 |
| --- | --- | --- | --- |
| `linux-glibc2.28` | `x64` | Linux、glibc 2.28+，默认使用 systemd | 可用 |
| `linux-glibc2.28` | `arm64` | Linux、glibc 2.28+，默认使用 systemd | 可用 |
| `linux-glibc2.17-legacy` | `x64` / `arm64` | 旧版 Linux、systemd | 已设计 |
| `linux-musl1.2` | `x64` / `arm64` | Alpine/musl、OpenRC | 已设计 |
| `macos12` | `x64` / `arm64` | macOS 12+、launchd | 已设计 |
| `windows-msys2` | `x64` | Windows Service | 已设计，Windows 主后端 |
| `windows-cygwin` | `x64` | Windows Service | 已设计，兼容后端 |

当前 Linux 包固定安装到 `/usr/local/redis`。不需要为每个更高的 glibc 版本
重复打包：glibc 2.28 包面向 glibc 2.28 及以上系统。Alpine 使用 musl，必须
单独构建。

完整的平台矩阵、发布门禁和实施顺序见[多平台发布方案](docs/PLATFORM-DESIGN.zh-CN.md)。
首批多版本策略跟踪 Redis Open Source 官方所有当前 GA 系列的最新补丁版：6.2、
7.2、7.4、8.0、8.2、8.4、8.6、8.8 和 8.10。“已设计”与“已经发布”严格区分。

Windows 方案明确参考 Apache-2.0 开源项目
[`redis-windows/redis-windows`](https://github.com/redis-windows/redis-windows)，
并把该项目公开 issue 转换为回归测试或清晰的范围决定。详见
[Windows issue 覆盖表](docs/WINDOWS-ISSUE-COVERAGE.md)和
[第三方引用说明](THIRD_PARTY_NOTICES.md)。在具备原生工具链及真实 ARM64 Windows
服务与数据测试前，不宣称支持原生 Windows ARM64。

## Release 产物

Redis 7.4.11 的 Linux 产物命名如下：

```text
Redis-7.4.11-linux-glibc2.28-x64.tar.gz
Redis-7.4.11-linux-glibc2.28-x64.tar.gz.sha256
Redis-7.4.11-linux-glibc2.28-arm64.tar.gz
Redis-7.4.11-linux-glibc2.28-arm64.tar.gz.sha256
```

压缩包顶层目录为 `redis/`：

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
└── README.txt
```

`PACKAGE-INFO` 记录版本、架构、libc、最低 glibc、服务后端和安装目录等机器
可读信息。安装和更新脚本会先校验这些信息，并实际执行安装包中的
`redis-server --version`，通过后才修改系统。

## Linux 安装

根据服务器架构下载对应包。不要直接覆盖 `/usr/local/redis`，应先解压到临时
目录：

```bash
sha256sum -c Redis-7.4.11-linux-glibc2.28-x64.tar.gz.sha256
mkdir redis-install
tar -xzf Redis-7.4.11-linux-glibc2.28-x64.tar.gz -C redis-install
sudo ./redis-install/redis/scripts/install.sh
```

默认安装流程：

- 首先检查 Linux、CPU 架构、glibc、二进制实际运行和 systemd；
- 安装到 `/usr/local/redis`；
- 复用已有 `redis` 账号，或者创建禁止登录的系统账号；
- 新安装的默认配置使用 `/usr/local/redis/data`；
- 注册并启动 `/etc/systemd/system/redis.service`；
- 记录账号和文件是否由本项目创建。

基础 systemd 单元遵循 Redis 配置中的 `dir`、`logfile`、Socket、TLS、模块、
ACL 和 include 路径，不会在启动命令中强制覆盖。严格加固配置仅作为可选示例，
因为其中的可写目录必须按用户实际配置调整。

常用选项：

```bash
# 注册并启用服务，但暂不启动
sudo ./redis-install/redis/scripts/install.sh --no-start

# 仅安装程序，不要求或注册 systemd
sudo ./redis-install/redis/scripts/install.sh --no-service

# 保留并接管已有配置或数据目录
sudo ./redis-install/redis/scripts/install.sh --adopt
```

如果 `redis.service` 属于其他软件，默认拒绝覆盖；只有明确确认时才使用
`--force-service`。

## Linux 更新和接管已有安装

更新脚本必须从新解压的安装包运行：

```bash
mkdir redis-update
tar -xzf Redis-7.4.11-linux-glibc2.28-x64.tar.gz -C redis-update
sudo ./redis-update/redis/scripts/update.sh
```

更新脚本会先验证新二进制，再停止 Redis；将旧程序、配置、脚本、服务单元、
元数据和安装状态备份到 `/usr/local/redis-backups/`，然后替换程序文件并完整
保留现有配置和数据。如果新服务启动失败，会自动恢复程序、服务状态和服务单元。

自动备份不会复制可能非常大的 Redis 数据目录。生产版本升级前仍应创建应用一致的
文件系统或存储快照；二进制回滚不能替代数据备份。

没有本项目状态文件的手工安装默认不会被接管。检查其服务账号、文件权限、配置
路径和备份后，显式迁移：

```bash
sudo ./redis-update/redis/scripts/update.sh --adopt
```

接管不使用 systemd 的安装时使用 `--adopt --no-service`。

## Linux 卸载

删除服务和程序，但保留配置、数据、安装状态和服务账号：

```bash
sudo /usr/local/redis/scripts/uninstall.sh
```

同时删除整个安装目录：

```bash
sudo /usr/local/redis/scripts/uninstall.sh --purge
```

只有安装状态能够证明 `redis` 用户或组由本项目创建，并且当前 UID/GID 仍然
一致时，`--purge` 才会删除对应账号。系统原有账号和
`/usr/local/redis-backups/` 始终保留。

## 中英文

维护脚本根据 `LC_ALL`、`LC_MESSAGES` 或 `LANG` 自动选择中文或英文，也可以
主动指定：

```bash
REDIS_INSTALL_LANG=en sudo -E ./redis/scripts/install.sh
REDIS_INSTALL_LANG=zh_CN sudo -E ./redis/scripts/install.sh
```

脚本使用命令退出码和机器可读属性，不解析本地化后的 `systemctl` 状态文本。

## Linux 兼容性与测试

- x64 GitHub Runner：`ubuntu-24.04`；ARM64：`ubuntu-24.04-arm`。
- 实际编译用户态：`rockylinux/rockylinux:8`，glibc 2.28。
- 检查所有 Redis ELF 文件，最高 glibc 符号版本不得超过 `GLIBC_2.28`。
- 不使用 `-march=native`，避免绑定 Runner 的 CPU 指令集。
- TLS 未启用，与默认 Redis `make` 构建一致。
- systemd 是默认服务后端；`--no-service` 支持仅安装程序。OpenRC 需要未来的
  musl/OpenRC 专用包。

Ubuntu 24.04 只提供 Runner 内核；Redis 在 Rocky Linux 8 容器中编译，不会
链接 Ubuntu 的 glibc。ARM64 运行时可能出现的 `ARM64-COW-BUG` 等内核警告
不会被自动忽略。

每个架构都会执行官方源码 SHA256 校验、`make test`、PING/SET/GET 冒烟测试、
ELF 架构检查、动态库检查和 glibc ABI 检查。打包后还会验证中英文帮助、错误
架构在修改系统前被拒绝、systemd 安装、自定义数据目录、更新和持久化、普通
卸载与重装、账号安全的彻底卸载以及 `--no-service` 安装。

## 自动构建

当前 [Linux 工作流](.github/workflows/build-linux.yml)接收一个确切的 Redis 官方
版本，并可选择 x64、ARM64 或两个架构。它只能手工触发，或者将来由可复用工作流
显式调用；没有 push 或 schedule 触发器。手工发布 Release 默认关闭。

[版本计划工作流](.github/workflows/resolve-versions.yml)会每日检查全部跟踪系列，
也可在控制器文件进入 `main` 或手工触发时运行。它只上传 JSON/Markdown 计划，绝不
调用 Linux 打包工作流。

多平台控制器已经实现为安全的 **仅生成计划（plan-only）** 模式，具体见
[发布方案](docs/PLATFORM-DESIGN.zh-CN.md)和
[发布控制器说明](docs/RELEASE-CONTROLLER.md)，跟踪系列声明在
[`config/release-lines.json`](config/release-lines.json)。已登记系列出现新的官方
SHA-256 后，控制器会生成缺少的版本/平台矩阵，但当前不会执行矩阵或发布 Release。
全新的 `X.Y` 系列会自动发现并报告为候选项，再通过一个小型策略 PR 审查许可证、
工具链和补丁集。

构建矩阵目前只供审核；只有以后明确加入独立、权限隔离的调度任务后才会执行。

## 本地构建 Linux 包

构建脚本应在 Rocky Linux 8 或其他受控的 glibc 2.28 环境运行：

```bash
export REDIS_VERSION=7.4.11
export REDIS_SOURCE_SHA256=3c266ece0abd54ed3b1c912c6eb86b7508cf382cb690ee6649d3843f018f6357
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

## 许可证

本仓库的构建脚本和工作流使用 [MIT License](LICENSE)。Redis 二进制、配置和
源码仍受安装包中附带的上游许可证约束。参考或合入的第三方工作统一记录在
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
