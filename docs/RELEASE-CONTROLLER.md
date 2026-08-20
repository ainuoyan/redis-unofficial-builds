# Release controller / 发布控制器

## English

The multi-version controller is implemented in **plan-only** mode. It discovers
versions and missing assets, but it does not dispatch a build workflow, create
a tag, or publish a GitHub Release.

Configuration:

- `config/release-lines.json`: tracked Redis GA series, EOL dates, upstream
  hash/source locations, and new-series policy.
- `config/platforms.json`: complete target platform matrix. Only implemented
  platform rows may set `controller_enabled` to `true`.
- `scripts/release/resolve_versions.py`: standard-library-only resolver.
- `.github/workflows/resolve-versions.yml`: scheduled/manual plan generator.

The resolver downloads no source and executes no package code. Given the
official `redis-hashes` file and the repository's current Release inventory, it
creates:

- `release-plan.json`: decision and missing assets for every tracked series;
- `version-matrix.json`: Redis versions that need at least one asset;
- `build-matrix.json`: missing version/platform pairs;
- `new-series.json`: stable series above the configured discovery floor; and
- `summary.md`: human-readable GitHub job summary.

Run locally:

```bash
curl --fail --silent --show-error --location \
  https://raw.githubusercontent.com/redis/redis-hashes/master/README \
  --output redis-hashes.txt

printf '[]\n' >github-releases.json

python3 scripts/release/resolve_versions.py \
  --release-config config/release-lines.json \
  --platform-config config/platforms.json \
  --hashes redis-hashes.txt \
  --github-releases github-releases.json \
  --output-dir release-plan
```

The existing Linux workflow now has only two entry points: explicit manual
dispatch and reusable `workflow_call`. Manual publication defaults to `false`.
The plan workflow does not call it.

To enable builds later, implement and test a platform backend first, set that
row to `implemented` with a workflow name, and then add a separate,
permission-scoped orchestration job that consumes `build-matrix.json`. Changing
`controller_mode` away from `plan_only` is rejected by the resolver and the
workflow safety gate.

## 简体中文

多版本控制器当前已经实现，但固定运行在 **仅生成计划（plan-only）** 模式。它会
发现版本和缺少的 Release 产物，但不会触发打包工作流、创建 Tag 或发布 GitHub
Release。

配置与程序：

- `config/release-lines.json`：跟踪的 Redis GA 系列、EOL 日期、官方哈希/源码位置和
  新系列策略；
- `config/platforms.json`：完整目标平台矩阵，只有已经实现的平台才能把
  `controller_enabled` 设为 `true`；
- `scripts/release/resolve_versions.py`：只使用 Python 标准库的解析器；
- `.github/workflows/resolve-versions.yml`：定时或手工生成计划的工作流。

解析器不会下载 Redis 源码，也不会执行安装包代码。输入官方 `redis-hashes` 和当前
仓库 Release 清单后，输出：

- `release-plan.json`：每个跟踪系列的处理决定和缺失产物；
- `version-matrix.json`：至少缺少一个产物的 Redis 版本；
- `build-matrix.json`：缺失的版本/平台组合；
- `new-series.json`：高于发现基线的新稳定系列；
- `summary.md`：供 GitHub Job Summary 展示的摘要。

本地运行方法与上面的英文示例相同。现有 Linux 工作流现在只保留两个入口：显式
手工触发和可复用的 `workflow_call`，手工发布 Release 默认关闭；计划工作流不会
调用它。

以后启用实际构建时，应先完成并测试对应平台，把平台状态改成 `implemented` 并指定
工作流，然后另加一个权限隔离的调度任务来消费 `build-matrix.json`。解析器和 Actions
安全门都会拒绝把 `controller_mode` 从 `plan_only` 改成其他值。
