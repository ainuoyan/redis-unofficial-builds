# Release controller / 发布控制器

## English

The multi-version controller is an enforced **plan-only** component. It
discovers official stable versions, compares expected names with GitHub
Release inventories, and writes review artifacts. It cannot dispatch a build,
create a tag, modify a Release, or publish assets.

`policy.patch_updates=auto_release` is a fixed policy marker. Configuration
validation requires this exact value, but no component currently consumes it
to publish automatically, and the resolver has no publication authority. Both
configuration validation and the workflow safety gate require
`policy.controller_mode=plan_only`.

### Configuration and trust inputs

- [`config/release-lines.json`](../config/release-lines.json) declares enrolled
  GA series, EOL dates, reviewed official source/hash locations, and
  new-series policy.
- [`config/platforms.json`](../config/platforms.json) declares the full design
  matrix. Only implemented, controller-enabled rows with a recognized workflow
  can enter a build matrix.
- [`scripts/release/resolve_versions.py`](../scripts/release/resolve_versions.py)
  performs strict, standard-library-only resolution.
- [`.github/workflows/resolve-versions.yml`](../.github/workflows/resolve-versions.yml)
  supplies the scheduled/manual GitHub plan.

After checkout, the workflow validates both configuration files before it
fetches upstream hash data or the Release inventory. It resolves
`redis/redis-hashes` `master` through the GitHub API to a lowercase 40-character
Git commit, then downloads `README` at that exact commit. The commit is
recorded as `hashes_commit` in the plan and workflow output. This prevents a
moving branch from changing the source-hash input after it has been selected.

The parser accepts only canonical stable `X.Y.Z` SHA-256 records with the
reviewed Redis download URL. Prerelease records and SHA-1 records are ignored.
A malformed line that claims to be a stable Redis record, a conflicting
duplicate, an unexpected URL, noncanonical version, oversized input,
duplicate JSON key, unknown configuration field, or non-finite JSON value
fails closed.

### Outputs

The controller writes:

- `release-plan.json`: one decision per selected series;
- `version-matrix.json`: new Redis versions eligible for platform planning;
- `build-matrix.json`: new version/platform rows for enabled backends;
- `new-series.json`: stable series above the configured discovery floor;
- `summary.md`: the same decisions for the GitHub job summary.

Experimental and design-only platforms are reported as controller-disabled and
never added to the matrices, even when an experimental row names its manual
artifact workflow. A new series is assigned `candidate_then_pull_request` and
requires reviewed configuration enrollment.

### Current exact Release inventory

For version `{version}`, the implemented controller expects exactly:

```text
Redis-{version}-linux-glibc2.28-x64.tar.gz
Redis-{version}-linux-glibc2.28-x64.tar.gz.sha256
Redis-{version}-linux-glibc2.28-arm64.tar.gz
Redis-{version}-linux-glibc2.28-arm64.tar.gz.sha256
SHA256SUMS
manifest.json
redis-unofficial-builds-{version}.spdx.json
```

Release-level metadata is part of the contract, not an optional supplement.
The resulting actions are:

| Action | Meaning | Matrix behavior |
| --- | --- | --- |
| `plan_new_release` | No Release exists; all enabled platform and release metadata assets are missing | Add version and platform rows |
| `skip_complete` | The published Release name inventory exactly matches all seven names | Add no rows |
| `blocked_nonfinal_release_state` | A numeric stable Release is a draft or prerelease | Report a blocking item; add no rows |
| `blocked_incomplete_immutable_release` | An existing Release lacks any required package or release-level metadata file | Report a blocking item; add no rows |
| `blocked_unexpected_immutable_release_assets` | All required names exist, but the Release also has an extra asset | Report a blocking item; add no rows |
| `blocked_no_official_stable_release` | A full run found no stable official SHA-256 record for an enrolled series | Report a blocking item; add no rows |
| `skip_eol` | The selected series is past its configured EOL date | Add no rows |
| `skip_no_enabled_platforms` | No platform is enabled | Add no rows |

Blocked rows require maintainer review but do not suppress decisions for other
series. The resolver never treats an incomplete Release as ordinary missing
work and never emits a build row that would complete it.

The resolver reads asset **names only**. `skip_complete` is therefore a
planning result, not proof of integrity. The publish-capable Linux workflow
downloads an exact existing Release, validates archives and aggregate
metadata, checks the tag's packaging revision, and verifies all required
attestations before it skips a build.

Canonical stable Release tags are numeric `X.Y.Z`. Stable aliases such as
`vX.Y.Z`, `redis-X.Y.Z`, and `redis-vX.Y.Z` in the Release inventory fail
closed instead of being merged. Draft and prerelease states on a canonical
stable version are blocking conditions; unrelated non-stable Release tags are
ignored.

### Local execution

The following example uses an empty Release inventory:

```bash
hashes_commit="$(gh api \
  -H 'X-GitHub-Api-Version: 2022-11-28' \
  repos/redis/redis-hashes/commits/master \
  --jq .sha)"

gh api \
  -H 'Accept: application/vnd.github.raw+json' \
  -H 'X-GitHub-Api-Version: 2022-11-28' \
  "repos/redis/redis-hashes/contents/README?ref=$hashes_commit" \
  >redis-hashes.txt

printf '[]\n' >github-releases.json

python3 scripts/release/resolve_versions.py \
  --release-config config/release-lines.json \
  --platform-config config/platforms.json \
  --hashes redis-hashes.txt \
  --hashes-commit "$hashes_commit" \
  --github-releases github-releases.json \
  --output-dir release-plan
```

`--series X.Y` can be repeated to select tracked series. `--version X.Y.Z`
selects an exact official version in a tracked series. Series and exact-version
filters are mutually exclusive. `--as-of YYYY-MM-DD` makes EOL decisions
reproducible.

### Separation from publication

The [Linux build workflow](../.github/workflows/build-linux.yml) has explicit
manual and `workflow_call` entry points. The plan workflow does not call it.
Direct publication requires both architectures, the exact seven assets, a
protected default-branch ref, and the protected GitHub Environment named
`release`.

The publisher starts only when neither the numeric Release nor tag exists. It
creates a new draft targeted at the packaging commit, uploads all seven files
together, validates attestations, reads back the draft's REST
`target_commitish` and exact inventory, binds remote asset IDs, sizes, and
GitHub SHA-256 digests to the verified local files, then downloads and
semantically revalidates every file. Immediately before publication it
rechecks the same draft identity, state, tag OID, and asset records, then
publishes by numeric Release ID. After publication it requires the new tag to
resolve to the packaging commit, reads back the same asset records, downloads
the files again, and repeats semantic and attestation verification. It never
fills, overwrites, deletes, or repairs
assets in an existing Release. Existing incomplete, legacy, draft,
prerelease, or extra-asset Releases block publication.
A manual `force_rebuild` is limited to nonpublishing Actions artifacts after
an exact existing Release has been revalidated; it cannot republish that
Release.

Workflow YAML cannot create branch or Environment protection. Repository
administrators must separately configure a default-branch protection
rule/ruleset and `release` Environment required reviewers plus deployment
branch restrictions, enable repository-level Immutable Releases before
production publication, and restrict Release writes to the reviewed workflow
and trusted maintainers.

`manifest.json` binds the source SHA-256, immutable `redis-hashes` commit,
packaging revision, patch-set hash, and both packages. `SHA256SUMS` covers the
other six Release assets. The SPDX 2.3 file is explicitly
release-package-level rather than a complete transitive component SBOM.
GitHub Artifact Attestations provide SLSA provenance for all seven assets and
an SPDX predicate for both archives. Adjacent checksums remain in the same
GitHub Release trust boundary and are not independent signatures.

## 简体中文

多版本控制器被强制限制为**仅生成计划（plan-only）**。它发现官方稳定版本、比较
预期名称与 GitHub Release 清单并生成审查产物；不能触发构建、创建 Tag、修改
Release 或发布产物。

`policy.patch_updates=auto_release` 是固定的策略标识：配置校验强制要求该取值，
但当前没有任何组件据此自动发布，解析器也不具备发布权限。配置校验和工作流
安全门都要求 `policy.controller_mode=plan_only`。

### 配置与信任输入

- [`config/release-lines.json`](../config/release-lines.json)声明 GA 系列、EOL、
  经审查的官方源码/哈希位置和新系列策略；
- [`config/platforms.json`](../config/platforms.json)声明完整设计矩阵，只有已实现、
  启用控制器并使用受支持工作流的平台才能进入构建矩阵；
- [`scripts/release/resolve_versions.py`](../scripts/release/resolve_versions.py)使用
  Python 标准库执行严格解析；
- [`.github/workflows/resolve-versions.yml`](../.github/workflows/resolve-versions.yml)
  提供定时和手工计划。

完成 checkout 后，工作流会在获取上游哈希数据或 Release 清单前校验两个配置文件。
它通过 GitHub API 把 `redis/redis-hashes` 的 `master` 解析为小写 40 位 Git 提交，
再读取该提交上的 `README`，并把提交记录为计划和工作流输出中的
`hashes_commit`。因此，选定输入后移动分支不能改变对应源码哈希。

解析器只接受规范 `X.Y.Z` 稳定版、SHA-256 和经审查 Redis 下载 URL。预发布记录和
SHA-1 记录忽略。伪装为稳定版但格式错误的记录、冲突重复项、异常 URL、非规范版本、
过大输入、重复 JSON 键、未知配置字段或非有限 JSON 数值均会直接失败。

### 输出

控制器生成：

- `release-plan.json`：每个选定系列的决定；
- `version-matrix.json`：可为平台规划的新 Redis 版本；
- `build-matrix.json`：启用后端的新版本/平台行；
- `new-series.json`：高于配置发现基线的稳定系列；
- `summary.md`：供 GitHub Job Summary 使用的同一组决定。

实验性和仅设计平台都会以控制器禁用状态列入报告，即使实验性行指向手工 artifact
工作流也绝不进入矩阵。新系列标记为
`candidate_then_pull_request`，必须经配置审查后登记。

### 当前精确 Release 清单

对于版本 `{version}`，已实现控制器恰好要求：

```text
Redis-{version}-linux-glibc2.28-x64.tar.gz
Redis-{version}-linux-glibc2.28-x64.tar.gz.sha256
Redis-{version}-linux-glibc2.28-arm64.tar.gz
Redis-{version}-linux-glibc2.28-arm64.tar.gz.sha256
SHA256SUMS
manifest.json
redis-unofficial-builds-{version}.spdx.json
```

Release 级元数据属于强制约定，不是可选附件。动作含义如下：

| 动作 | 含义 | 矩阵处理 |
| --- | --- | --- |
| `plan_new_release` | Release 不存在，全部启用平台与元数据产物均待创建 | 加入版本和平台行 |
| `skip_complete` | 正式 Release 的名称清单精确匹配 7 个名称 | 不加入 |
| `blocked_nonfinal_release_state` | 纯数字稳定 Release 是草稿或预发布 | 报告阻塞项，不加入 |
| `blocked_incomplete_immutable_release` | 已有 Release 缺少任一包或 Release 级元数据 | 报告阻塞项，不加入 |
| `blocked_unexpected_immutable_release_assets` | 必需名称均存在，但 Release 还含额外产物 | 报告阻塞项，不加入 |
| `blocked_no_official_stable_release` | 完整运行时，已登记系列没有任何官方稳定版 SHA-256 记录 | 报告阻塞项，不加入 |
| `skip_eol` | 系列已超过配置 EOL | 不加入 |
| `skip_no_enabled_platforms` | 没有启用平台 | 不加入 |

阻塞行需要维护者审查，但不会抑制其他系列的决定。解析器绝不会把残缺 Release 当作
普通缺失任务，也不会生成用于补全它的构建行。

解析器只读取产物**名称**，因此 `skip_complete` 是计划结果，不是完整性证明。具备
发布能力的 Linux 工作流会下载已有精确 Release，校验包和聚合元数据、检查 Tag
打包提交并验证全部证明后，才跳过构建。

稳定 Release Tag 必须是纯数字 `X.Y.Z`。Release 清单中的 `vX.Y.Z`、
`redis-X.Y.Z`、`redis-vX.Y.Z` 等稳定别名会直接失败，不会合并身份。规范稳定版
上的草稿或预发布状态属于阻塞项；无关的非稳定 Tag 会忽略。

### 本地执行

以下示例使用空 Release 清单：

```bash
hashes_commit="$(gh api \
  -H 'X-GitHub-Api-Version: 2022-11-28' \
  repos/redis/redis-hashes/commits/master \
  --jq .sha)"

gh api \
  -H 'Accept: application/vnd.github.raw+json' \
  -H 'X-GitHub-Api-Version: 2022-11-28' \
  "repos/redis/redis-hashes/contents/README?ref=$hashes_commit" \
  >redis-hashes.txt

printf '[]\n' >github-releases.json

python3 scripts/release/resolve_versions.py \
  --release-config config/release-lines.json \
  --platform-config config/platforms.json \
  --hashes redis-hashes.txt \
  --hashes-commit "$hashes_commit" \
  --github-releases github-releases.json \
  --output-dir release-plan
```

`--series X.Y` 可重复选择已跟踪系列；`--version X.Y.Z` 选择已跟踪系列中的确切
官方版本。系列和确切版本筛选互斥。`--as-of YYYY-MM-DD` 可复现 EOL 判断。

### 与发布分离

[Linux 构建工作流](../.github/workflows/build-linux.yml)具有明确手工和
`workflow_call` 入口，计划工作流不会调用它。直接发布要求两个架构、精确 7 个
产物、受保护默认分支 ref 和名为 `release` 的受保护 GitHub Environment。

发布器只在对应纯数字 Release 和 Tag 均不存在时开始。它创建目标为打包提交的新草稿，
一次上传 7 个文件、校验证明、通过 REST 回读草稿的 `target_commitish` 和精确清单，
把远端产物 ID、字节数和 GitHub SHA-256 摘要绑定到已校验本地文件，再下载并按语义
复验每个文件。临发布前重新核对同一草稿身份、状态、Tag OID 和产物记录，然后按数字
Release ID 发布。发布后还要求新 Tag 解析到打包提交，回读相同产物记录，再次下载并
重复语义及证明校验。它绝不补充、覆盖、删除或修复已有 Release 中的产物。
已有残缺、旧约定、草稿、预发布或额外产物 Release 会阻塞发布。
手工 `force_rebuild` 只可在精确已有 Release 重新校验后生成不发布的 Actions
产物，不能重新发布该 Release。

工作流 YAML 无法创建分支或 Environment 保护。仓库管理员必须另外配置默认分支
保护规则/ruleset、`release` Environment 的必需审核人和部署分支限制；生产发布前
还必须启用仓库级 Immutable Releases，并把 Release 写权限限制在经审查的工作流和
可信维护者。

`manifest.json` 绑定源码 SHA-256、不可变 `redis-hashes` 提交、打包提交、补丁集
哈希和两个包；`SHA256SUMS` 覆盖其他 6 个 Release 产物。SPDX 2.3 明确是 Release
包级清单，不是完整传递组件 SBOM。GitHub Artifact Attestations 为 7 个产物提供
SLSA 来源证明，并为两个压缩包提供 SPDX predicate。相邻校验和仍处于同一 GitHub
Release 信任边界，不是独立签名。
