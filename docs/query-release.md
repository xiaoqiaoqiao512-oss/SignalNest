# 查询候选的冻结、安装和比对

本轮查询候选包含本地查询 Agent、兼容代理配置，以及 Windows 保活和定时 full 锁等待修复。候选尚不等于生产已升级：当前生产是冻结 `7cdecf6` 加六个锁等待源码文件的精确补丁，内容指纹为 `9d55286ae2ec1883ee11a2f778233112cf3f56a6d726e43a9045412ac0feaf19`，schema 为 `0008`。查询/代理改动没有因此部署到生产。旧生产清单保留，不能用当前 checkout 的 Git HEAD 代替生产内容证据。

不创建提交、tag 或自动部署。允许集合中的未提交文件也会冻结；是否 dirty 单独记入旁边清单。先结束源码编辑，跑全部测试、Ruff 和格式检查，再冻结；真实数据快照和模型回放报告另放受限目录，不装入发布包。

当前查询行为版本为 `campus-query-v4`：沿用 v2 的截止证据校验，增加最多两轮自主探索及读取/回答预留，并在原预算内允许一次引用纠正，详见[查询协议](query-agent.md#两轮探索与收束)。这与 `signalnest-release-v1` 的发布序列化格式、`signalnest-query-snapshot-v1` 的数据绑定格式、`signalnest-query-snapshot-replay-v1` 的回放报告格式分别记录。旧 v1/v2/v3 查询报告和人工记录不被升级脚本重写，新候选重新冻结、回放，不继承旧标签。内容指纹绑定实际源码，版本字符串不能代替文件摘要。

## 允许集合

继续使用 `signalnest-release-v1` 和原有 `content_sha256 = SHA256(规范序列化的逐文件摘要映射)`，没有改变摘要算法。允许集合包含包源码、迁移模板、`pyproject.toml`、`uv.lock`、README、systemd 模板及明确列出的公共部署工具；本次补入：

- `deploy/windows/repair-keepalive.ps1`、`wsl-keepalive.ps1`。
- `deploy/evaluate_queries.py`、`evaluate_search.py`。
- `deploy/prepare_query_snapshot.py`、`replay_query_snapshot.py`、`ask_saved.py` ，以及查询接口、快照、回放和发布四份说明。
- `ask.example.toml`、`ask.proxy.example.toml`、两份普通配置示例、两份画像示例。
- `ask.wsl.example.toml`、`deploy/query_readiness.py`、[WSL 查询准备说明](wsl-query-setup.md)和其引用的一致备份运维说明：模型默认关闭，检查真实存储与索引，旧备份仅能显式升级独立副本。

示例只提供格式，不代表个人画像、模型价格、真实实例路径或已验收配置。实际密钥仅从配置指定的环境变量读取。绝不将密钥写进示例。允许集合没有扩大成 `*.toml`、`deploy/**` 或 `docs/**`：私人配置、SMTP 环境文件、数据库、raw、研究 fixture、验证报告和测试数据均不进入快照。解包后的 `RELEASE.json` 不会被再次收进内容摘要。只读数据准备和回放入口分别见[快照说明](query-snapshots.md)与[回放说明](query-snapshot-replay.md)。

允许集合扩展也会改变候选指纹，即使部分源码相同；每个清单都保留自己的确切文件集合。旧 v1 快照仍能校验，不重新计算或修改原生产证据。内容指纹只证明这组名称和 bytes；不证明单元测试、依赖安装、知识库内容、模型回答质量或发行者身份。

## 冻结和验证

输出必须在源码目录之外，且归档和 `.manifest.json` 都尚不存在。macOS 使用真实路径 `/private/tmp`，不用符号链接 `/tmp`；正式保存应选有受限权限且会备份的目录。

```sh
release_dir="$(mktemp -d /private/tmp/signalnest-query-release.XXXXXX)"
uv run --locked python deploy/release_snapshot.py \
  --release-root "$PWD" --output "$release_dir/signalnest-query.tar.gz"

# 验证压缩包、旁边清单、内部清单及全部文件；不解包、不写实例。
uv run --locked python deploy/release_snapshot.py \
  --verify "$release_dir/signalnest-query.tar.gz" --release-root "$PWD"
```

冻结命令输出 `content_sha256` 和 `archive_sha256`。将其单独记入交付记录，再使用 `--expected-content-sha256 <独立保存的内容指纹>` 验证；仅同时修改归档和它旁边的清单，并不能伪造那个独立记录。验证工具不是数字签名系统，不应把来路不明的包和它自带的摘要当成可信发布。

两件产物必须齐全：归档保存允许集合的实际 bytes 和确定的内部清单；旁边清单保存压缩包摘要、大小、Git HEAD/dirty、工具的 Python/zlib/SQLite 环境。相同名称与 bytes、相同 Python/zlib 生成环境下，归档可逐字节重建。Git/观察时间不进入归档 bytes。原子发布和 fsync 的边界仍见[发布说明](rollout.md#固定可重现版本)，没有新增断电保障承诺。

`--verify` 退出 0 表示对应校验通过；失败退出 1，仅打印有限错误代码。参数组合错误退出 2。缺件、坏摘要、重复/额外成员、链接/路径逃逸、过大的解压内容及不一致清单都会拒绝。验证压缩包最大 64 MiB、展开内容最大 128 MiB、每件文件最大 16 MiB，足够当前代码发布；不用于归档校园原文。帮助不读取源码、快照或实例。

## 重新安装和运行比对

在受限目录保留原始归档、独立摘要和旁边清单。先验证，再在空目录解包；解包后再次比对文件集合和逐文件摘要。以下命令仅安装查询候选，不恢复或启动生产采集、邮件、timer：

```sh
mkdir "$release_dir/unpacked"
# 仅解包上一步已验证且仍保存在受限目录中的归档。
tar -xzf "$release_dir/signalnest-query.tar.gz" -C "$release_dir/unpacked"
uv run --locked python deploy/release_snapshot.py \
  --verify "$release_dir/signalnest-query.tar.gz" \
  --release-root "$release_dir/unpacked/signalnest"

cd "$release_dir/unpacked/signalnest"
uv sync --locked --no-dev --no-editable
.venv/bin/python deploy/release_snapshot.py \
  --verify "$release_dir/signalnest-query.tar.gz" --release-root "$PWD"
.venv/bin/signalnest ask --help
```

解包目录校验返回 `source_matches=true`；通过新安装的解释器再次运行，应同时得到 `runtime_code_matches_release=true`。冻结工具运行旧环境时，源码匹配但运行代码不匹配是可区分的结果，不自动声称已安装。新增、缺少或改变允许集合中的文件都会得到 `release_source_mismatch`。私人配置应放在源码目录之外，查询配置指向隔离的 UC/CS 只读一致快照；不能用示例路径误连生产写入实例。

安装后可用 `.venv/bin/python deploy/ask_saved.py --config <快照目录>/ask.local.toml --check` 检查准备工具生成的配置，随后加 `--local --question <问题>` 做零模型检索。实际模型使用操作者另建的 `ask.model.toml`；批量回放与单题日常启动的参数和凭据入口见[回放说明](query-snapshot-replay.md)和[查询说明](query-agent.md#实际知识库的隔离查询)。保持使用新安装的解释器，不用其他 checkout 的命令来证明这一版本已运行。

版本回退只需要重新选取既有已验证的包并在独立目录安装；此工具不切换生产软链接、不迁移数据库、不更新政策或邮件，也不改模型配置。源码快照不包含依赖 wheel 缓存、完整测试/fixture 环境或私人实例备份；首次安装可能需要下载 `uv.lock` 指定的依赖。模型真实回放和只读知识库验收须另外记录实际查询版本指纹、数据快照时间和来源限制。
