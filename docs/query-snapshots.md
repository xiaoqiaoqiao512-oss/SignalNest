# 真实知识库的只读查询副本

查询可同时读取 UC、CS 的独立数据库；生产采集仍在原实例运行。第一轮日常查询把**已校验的一致备份**传到模型代理所在机器，再产生隔离的查询副本。它不是实时数据库，不启动采集、邮件或 timer；默认不迁移。可用显式 `--upgrade-copy` 仅升级未发布的隔离副本，原库和原备份保持只读。

## 准备副本

先依照[一致备份](operations.md#一致备份)在原实例的同一 `writer_lock` 内使用 SQLite backup API，复制并验证 `raw/`。每个来源单独记录实际备份时刻，保留原备份不改。不能直接复制一个仍在使用 WAL 的 SQLite 主文件。`backup.complete` 只是备份生产者的完成标记，准备工具仍会重新校验数据库、外键、原文摘要、邮件和扫描证据；这不证明备份创建过程或断电耐久性。

仅把必要的一致备份传入受限目录，不复制 `smtp.env`、模型密钥或整个部署目录。原备份、派生数据库均含通知正文，完整 SQLite 还可能含个人地址、画像和历史邮件，不能放进仓库或公开报告。应限制为操作者可访问，并保留到期清理的人工责任。

在私有目录写准备清单。下面的 `snapshot_at` 是**备份生产者记录的实际 UTC Unix 秒**，须替换，不能照抄示例或以准备工具运行时间代填：

```json
{
  "format_version": 1,
  "instances": [
    {
      "id": "uc",
      "source_id": "whu-undergrad-student",
      "parser": "whu-student-notices",
      "backup_dir": "/private/tmp/signalnest-backup/uc",
      "database": "data.sqlite3",
      "snapshot_at": 1791963225
    },
    {
      "id": "cs",
      "source_id": "whu-cs-undergrad-teaching",
      "parser": "cs-undergrad-notices",
      "backup_dir": "/private/tmp/signalnest-backup/cs",
      "database": "data.sqlite3",
      "snapshot_at": 1791963225
    }
  ]
}
```

每个目录须含指定的数据库、`raw/` 和 `backup.complete`。清单路径和输出父目录应已存在且不能经过符号链接。输出必须是新目录；错误时不修改原备份，不发布半成品。

```sh
uv run python deploy/prepare_query_snapshot.py \
  --spec /private/tmp/query-spec.json \
  --output /private/tmp/signalnest-query \
  --profile /private/tmp/profile.personal.toml
uv run signalnest ask-config-check --config /private/tmp/signalnest-query/ask.local.toml
uv run signalnest ask --config /private/tmp/signalnest-query/ask.local.toml \
  --question "有没有助教招聘？" --local
```

`--profile` 可省略；提供时只复制并校验这一份显式画像，不从生产配置猜测。`ask-config-check` 保持配置检查，不打开数据库；实际 `ask` 才验证清单与数据库。准备工具不依赖模型、SMTP 环境变量或真实网络。

### 生产 0008 与新版 0009 的边界

当前新版代码的 schema head 为 `0009_opportunity_review`；旧生产库为 `0008_list_references`。新版查询同样要求所读数据库达到当前 head，不能直接连接旧生产库，也不能仅以配置校验通过作为可查询的证明。准备旧备份时默认返回 `snapshot_schema_upgrade_required`，不会自动迁移。

```sh
# 显式对全新隔离副本执行 0008 -> 0009；不写原库或原备份。
uv run --locked python deploy/prepare_query_snapshot.py \
  --spec /private/tmp/query-spec.json --output /private/tmp/signalnest-query-new \
  --profile /private/tmp/profile.personal.toml --upgrade-copy
uv run --locked python deploy/query_readiness.py \
  --config /private/tmp/signalnest-query-new/ask.local.toml
```

仅支持已知的 `0008 -> 0009` 转换；未知版本拒绝，不猜测迁移路径。临时副本先暂停通道，再通过现有 Alembic 事务迁移，并用新版完整备份校验验证关系、原文和邮件。成功后绑定新的数据库摘要，`transformation=pause_and_upgrade_0008_to_0009_v1` 明确记录这项转换；`prepared.json` 另存父版本、验证版本与 `schema_upgraded_in_copy`。相同 head 不执行迁移，旧的仅暂停清单继续可读。

原备份摘要在复制和处理后复核；任一来源失败不发布整个目录，并清理普通失败的暂存目录。升级不更新政策、不重新决策、不重新渲染或恢复发送；冻结邮件、成员、投递和尝试记录保留。进程被强制终止时可能留下 `.query-snapshot-*` 私有暂存目录，应先核对再人工处理，不能把它当作已发布查询副本。迁移与目录发布不是跨文件事务，也不提供断电保证。

输出父目录下先建立私有暂存目录，所有来源校验成功后发布完整目录。每个派生 DB 把 journal 模式固定为 DELETE，并在一个本地事务内将已有通知通道暂停为 `manual`，保留事件、冻结邮件、成员、尝试和正文成功状态。原备份和生产暂停状态不变。固定变换名为 `pause_notification_channels_v1`；清单分别记录父备份 DB SHA 与派生 DB SHA。派生 DB 为 `0400`，目录 `0700`，配置和元数据 `0600`。文件权限和只读查询不是防止所有者主动改写的安全边界；此目录应保持冻结，不与写入命令共用。

生成的最小实例 TOML 不含 SMTP 配置，且 `mail_runtime.enabled=false`；`raw/` 不会复制到查询副本。它可使用已经保存的规范化正文及图片、附件引用，不能用于重解析或恢复生产实例。保留的完整原备份才是恢复依据。

## 启用实际模型

先复制 `ask.local.toml` 为同目录的 `ask.model.toml`，按[模型配置](query-agent.md)加入显式模型块；代理位于运行查询进程的机器。密钥由启动环境提供，不写入清单、TOML 或命令参数。每个 `[[instances]]` 保留以下绑定：

```toml
[[instances]]
id = "uc"
config = "uc/instance.toml"
snapshot_manifest = "uc/snapshot.json"
```

随后用 `signalnest ask --config <ask.model.toml> --question <问题>` 查询。配置未给出 `snapshot_manifest` 的既有实例仍兼容，输出标为 `configured_database`，不能据此声称该库实时或来自已校验副本。

## 证据与新鲜度

`snapshot.json` 的 `signalnest-query-snapshot-v1` 契约绑定实例 ID、来源 ID、固定数据库文件名、父/派生摘要、备份时刻、派生时刻及以下实际数据库事实：

- 最近列表响应、条目登记和完整扫描的持久时间及运行 ID；没有完整扫描记录时保持未知。
- **当前成功版本**对应原文的最新获取时间及最新解析时间；这是各正文时间的最大值，不保证每条通知都同样新。
- 通知总数、保留成功正文数量、首次失败、已有成功版本的复查失败和未适配引用数。

查询开始时拒绝错来源/实例、摘要变化、事实不匹配、活动 journal、未暂停通道、缺失文件或符号链接；不会修补或退回另一个库。一个来源不可用时，另一个来源仍可提供结果，但输出保留有限错误和不完整警告。清单是本地来源声明与一致性检查，不是密码学签名或第三方认证。

`source_status` 和文本答案明确显示静态快照时间、当前成功正文的最新原获取时间、覆盖年龄和失败/待处理限制；覆盖年龄按提问的显式时钟及原持久完整扫描时间计算。备份、传输或重新打开数据库都不会推进它。站点发布日期仍用于发布日期筛选，不替代报名截止时间。原始正文取回、规范化解析、数据库备份和本次提问是四个不同的时点。

更新资料时重新取得一致备份，发布**另一个新目录**并显式选择新查询配置；不在原冻结目录中增量写入。UC、CS 的两个备份可来自不同时刻，输出分别声明；不承诺跨库同一瞬间快照。当前不自动定时同步、发送邮件或下载附件正文。
