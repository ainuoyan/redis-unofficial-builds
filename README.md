# Redis Unofficial Builds

自动编译、测试并发布 Redis 非官方二进制包。这个仓库统一管理不同操作系统、Linux 兼容基线和 CPU 架构的构建；所有产物发布到同一个 Redis 版本的 GitHub Release。

> 本项目与 Redis Ltd. 无关联，也未获得其背书。Redis 二进制仍受上游许可证约束。

## 当前支持

| 平台标识 | 架构 | 构建基线 | 状态 |
| --- | --- | --- | --- |
| `linux-glibc2.28` | `x64` | Rocky Linux 8 / glibc 2.28 | 可用 |
| `linux-glibc2.28` | `arm64` | Rocky Linux 8 / glibc 2.28 | 可用 |
| Windows | - | 待确定 | 计划中 |
| macOS | - | 待确定 | 计划中 |

当前 Linux 包的安装位置固定为 `/usr/local/redis`。后续可以在本仓库继续增加面向其他 Linux ABI、发行版或操作系统的工作流，不需要另建仓库。

## Release 产物

Redis 7.4.11 的 Linux 产物命名如下：

```text
Redis-7.4.11-linux-glibc2.28-x64.tar.gz
Redis-7.4.11-linux-glibc2.28-x64.tar.gz.sha256
Redis-7.4.11-linux-glibc2.28-arm64.tar.gz
Redis-7.4.11-linux-glibc2.28-arm64.tar.gz.sha256
```

压缩包内部顶层目录为 `redis/`：

```text
redis/
├── bin/
├── conf/
│   ├── redis.conf
│   └── sentinel.conf
├── BUILD-INFO
├── LICENSE.txt
└── README.txt
```

## Linux 安装

根据服务器架构下载对应包。x86_64 示例：

```bash
sha256sum -c Redis-7.4.11-linux-glibc2.28-x64.tar.gz.sha256
sudo tar -xzf Redis-7.4.11-linux-glibc2.28-x64.tar.gz -C /usr/local
```

启动并检查版本：

```bash
/usr/local/redis/bin/redis-server /usr/local/redis/conf/redis.conf
/usr/local/redis/bin/redis-server --version
/usr/local/redis/bin/redis-cli --version
```

升级前请备份配置和数据，并先停止已有 Redis 进程。压缩包附带上游默认配置，直接解压会覆盖 `/usr/local/redis/conf` 中的同名文件；生产环境建议先解压到临时目录，再按需合并配置。

## Linux 兼容性

- GitHub Runner：x64 使用 `ubuntu-24.04`，ARM64 使用 `ubuntu-24.04-arm`。
- 实际编译用户态：`rockylinux/rockylinux:8` 容器，glibc 2.28。
- 构建脚本检查全部 Redis ELF 文件，最高 glibc 符号版本不得超过 `GLIBC_2.28`。
- 不使用 `-march=native`，避免绑定 GitHub Runner 的特定 CPU 指令集。
- TLS 未启用，与原来的直接 `make` 打包方式一致。

Ubuntu 24.04 只提供 Runner 和内核；Redis 在 Rocky Linux 8 容器用户态中编译，不会链接 Ubuntu 24.04 的 glibc 2.39。glibc 2.28 及以上系统通常具备用户态 ABI 条件，但发布前仍应在实际目标系统验证。ARM64 的 `ARM64-COW-BUG` 警告取决于运行时内核，本项目不会默认忽略。

## 自动构建

[Linux 工作流](.github/workflows/build-linux.yml)支持：

1. 合并构建脚本或工作流到 `main` 时构建当前最新的 Redis 7.4.x，并发布 Release。
2. 在 Actions 页面手动输入完整版本，例如 `7.4.11`。
3. 每天检查 Redis 官方 `redis-hashes`；发现新的 7.4.x 版本后自动构建和发布。

每个架构都会执行官方源码 SHA256 验证、`make test`、`PING` 与 `SET/GET` 冒烟测试、ELF 架构检查、动态库检查和 glibc ABI 检查。

## 本地构建 Linux 包

脚本应在 Rocky Linux 8 或其他 glibc 2.28 构建环境中运行：

```bash
export REDIS_VERSION=7.4.11
export REDIS_SOURCE_SHA256=3c266ece0abd54ed3b1c912c6eb86b7508cf382cb690ee6649d3843f018f6357
export PACKAGE_VARIANT=linux-glibc2.28
export EXPECTED_MACHINE_ARCH="$(uname -m)"

case "$(uname -m)" in
  x86_64) export PACKAGE_ARCH=x64 ;;
  aarch64) export PACKAGE_ARCH=arm64 ;;
  *) echo "Unsupported architecture"; exit 1 ;;
esac

./scripts/linux/build-redis.sh
```

## 许可证

本仓库中的构建脚本和工作流使用 [MIT License](LICENSE)。Redis 二进制、配置文件和源码仍受 Redis 上游许可证约束，发布包会包含对应版本的 `LICENSE.txt`。Redis 7.4 使用 RSALv2/SSPLv1 双许可证，使用或分发前请自行确认适用条款。
