# WSL 查询新版的准备与接入

本步骤准备一个独立的只读查询安装。当前生产采集、邮件、政策和数据库不随之切换；后台模型通知判断仍未实现自动接入。查询 Agent 的上线与自动通知的语义判断是两项验收。

本指南按显式配置的 Chat Completions 兼容接口准备，不把显示名称当成已验证的 API 标识，也不假定提供方、价格、工具调用能力或可用性。模型标识和凭据由部署者在目标环境中核验与填写。

## 1. 独立安装已核对的发布包

沿用[发布冻结与校验](query-release.md)。保留归档、旁边清单，以及单独记录的 `content_sha256`。先在已信任的环境校验，再将归档解包到一个全新目录，例如 `/opt/signalnest-query/releases/<content_sha256>/signalnest`；不覆盖生产 `/opt/signalnest`，不安装或启用包内采集/mail timer。

在该独立源码目录中：

```sh
uv sync --locked --no-dev --no-editable
.venv/bin/python deploy/release_snapshot.py \
  --verify /private/release/signalnest-query.tar.gz \
  --release-root "$PWD" --expected-content-sha256 '<单独核对的内容指纹>'
.venv/bin/signalnest ask --help
.venv/bin/python deploy/query_readiness.py --help
```

核对 `source_matches=true` 和 `runtime_code_matches_release=true`，不要用旧生产解释器来证明新版本已运行。目标是 Ubuntu 24.04 / Python 3.12 的 POSIX 环境；实际 WSL 安装、依赖下载、权限和模型连通须在部署时验证。本地离线测试不能替代这些事实。

## 2. 准备真实知识库的隔离副本

生产 `0008` 不能直接供当前 `0009` 查询程序使用。先依现有[一致备份](operations.md#一致备份)取得两个来源的数据库与 raw，并分别记录真实备份时刻。备份沿用实例写入锁；不要为查询复制活动 WAL 主文件，也不要复制 SMTP 或模型凭据。

按[快照清单](query-snapshots.md)填写来源与原备份目录，使用真实 `snapshot_at`：

```sh
.venv/bin/python deploy/prepare_query_snapshot.py \
  --spec /private/query-spec.json --output /private/query-snapshots/new-copy \
  --profile /private/profile.personal.toml --upgrade-copy
.venv/bin/python deploy/query_readiness.py \
  --config /private/query-snapshots/new-copy/ask.local.toml
.venv/bin/python deploy/ask_saved.py \
  --config /private/query-snapshots/new-copy/ask.local.toml \
  --local --question '计算机学院有哪些助教招聘？'
```

`--upgrade-copy` 仅迁移未发布的副本 `0008 -> 0009`；原备份仍为 `0008`。不更新通知政策，不重判旧事件或发送邮件。派生通道暂停、普通配置不含 SMTP、查询 DB 为 `0400`。即便副本保留发送记录，也不会为历史 sending 任务执行恢复。副本是静态资料，后续更新须另建新目录。

`query_readiness.py` 检查各来源实际 schema、索引和可选快照绑定，不读密钥、不联网、不迁移、不修复索引。输出没有正文或凭据：

| 字段/退出码 | 含义 |
| --- | --- |
| `local_ready=true` | 配置、画像、所有来源的存储和索引通过本地检查 |
| `model_configured` | 仅表示普通模型配置存在且通过校验 |
| `model_runtime_verified=false` | 准备检查永远不声称已经实测模型 |
| `credentials_checked=false` / `network_checked=false` | 没有读取凭据或请求接口 |
| 0 | 本地准备通过；如指定 `--require-model`，还须存在合法模型块 |
| 1 | 存储/索引/清单异常，或要求模型配置但尚未填写 |
| 2 | 配置不可读或无效，错误只给有限代码 |

默认允许模型未配置，并明确返回 `model_not_configured`。加 `--require-model` 只提高配置要求，仍不验证鉴权、网络或工具协议。旧库返回 `query_schema_upgrade_required`；按上面步骤准备新副本，不对生产盲目执行 `storage-init`。

## 3. 模型稍后填写

保留准备工具生成的 `ask.local.toml`、实例绑定和画像行。复制为同目录下的 `ask.model.toml`，参考 [WSL 示例](../ask.wsl.example.toml) 加入预算和显式模型块。示例的模型块全部注释，默认不会产生模型请求；占位 endpoint/model 必须替换，不使用 `auto` 猜选模型。

- `base_url` 是从实际 WSL 查询进程可达的 `/v1` 地址。远程 HTTP 不会因局域网或改主机名而获得豁免；兼容客户端只允许远程 HTTPS 或字面回环 HTTP。
- `127.0.0.1` 指向查询进程所在网络环境。在 Windows 主机运行服务，不证明 WSL 内相同端口可达；届时实际核对网络模式与监听位置，不暗中降低地址校验。
- `model` 使用提供方的精确 API 标识，不要把显示名称或 `auto` 当成已核对的 API 标识。
- `api_key_env=SIGNALNEST_MODEL_API_KEY` 仅保存环境变量名称。实际值不进 TOML、命令参数、报告或仓库。
- 第一轮每题最多 6 次模型 HTTP 请求、90 秒、每次输出 2048 token，适配器最多 1 次尝试；批量回放另设整批上限。价格未知时 `unpriced_proxy` 保持金额未知，不承诺货币上限。

独立查询由拥有查询副本的账号执行。密钥可稍后保存为该账号拥有的 `0400` / `0600` 普通文件，放在不可被其他账号写入、且没有符号链接的私有目录中；用现有 `ask_saved.py --api-key-file` 临时注入配置指定环境变量。不要把密钥粘进聊天或脚本。帮助、`--check` 和 `--local` 不读取该文件；准备检查也不需要密钥。

```sh
.venv/bin/python deploy/query_readiness.py \
  --config /private/query-snapshots/new-copy/ask.model.toml --require-model

# 只在接口和凭据配置完成后实际验收；这会把问题、有限正文和画像交给提供方。
.venv/bin/python deploy/ask_saved.py \
  --config /private/query-snapshots/new-copy/ask.model.toml \
  --api-key-file /private/credentials/model.key \
  --question '最近一个月有没有本科生可以参加的科研机会？列出条件和截止时间。'
```

## 4. 真实验收与回退

逐题验证科研、CS 助教、学生报名与教师征题、模糊截止、无结果五个场景，每题独立预算。分别记录模型标识、发布指纹、备份时间、调用次数、引用与降级；模型广告名、成功 HTTP 或单次好答案均不等于工具和质量验收。沿用[受限回放](query-snapshot-replay.md)，人工标签保持独立 pending，不能将工程期望变成人工结论。

本地准备检查不能替代远程安装和模型验收；实际状态以带时间的部署记录为准。切换到旧的独立查询安装与旧快照即可回退查询；不降级生产数据库。不要为了试模型启用采集、SMTP、政策更新或额外任务。后台模型判断需要后续独立接入候选、持久证据与失败恢复，不能由 `ask` 的上线自动获得。

若模型网关也放在同一个 WSL 中，使用[CodeBuddy2API Docker 接入](codebuddy-wsl-gateway.md)，分别核对容器健康、账号授权、客户端密钥和模型工具调用；网关安装不等于查询已验收。

## 当前接入状态

独立 WSL 查询已完成基础模型与工具交接验证，使用暂停通知的静态快照；后台邮件和生产通知不会因此调用模型。人工回答质量标签仍待确认。脱敏的生产与查询状态见[上线摘要](validation/rollout-summary-20261021.md)；本指南保留通用准备步骤，不包含本机启动命令、查询内容或逐次调用记录。
