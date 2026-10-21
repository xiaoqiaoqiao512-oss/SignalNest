# 通知政策更新与有界重评

N4 提供显式、本地的政策更新和事件重评。它不建立新的内容事件、不重新采集、不发送邮件，也不修改已冻结邮件。所有写入由调用者持有实例写入锁；CLI 已统一加锁。预览只读，不获取写入锁。

先检查画像，再预览政策差异；`--at` 是显式 UTC Unix 秒。操作标识使用 1–64 个 ASCII 字母、数字或 `_.:-`，同一标识不可改参数，也不可同时用于政策更新和重评。以下时间占位替换为该操作的实际 UTC Unix 秒；示例事件 17/23 也须换成本实例核验后的明确事件 ID，不表示它们已获重评授权。

```sh
signalnest profile-check --profile profile.example.toml
signalnest notifications-policy-update --config config.toml \
  --profile profile.toml --operation-id policy-v9-selected --at '<实际UTC秒>' --preview
signalnest notifications-policy-update --config config.toml \
  --profile profile.toml --operation-id policy-v9-selected --at '<实际UTC秒>'
```

政策更新保存 `policy_manifest(profile)` 的不可变快照，复用相同摘要的既有 revision，并更新通道的后续决策政策。它不会扫描历史、创建投递资格或更改已有决策。政策 revision、通道指针和操作结果一起提交；其中任一步失败则一起回滚。重放已完成的更新返回原结果，不会把更晚的政策指针恢复成旧政策。代码规则升级后，可显式更新政策，以替换已经不能由当前代码执行的旧 manifest。

重评采用明确的事件 ID，最多 100 个输入 ID，排序去重后固定集合。事件 ID 可从数据库事件记录读取，不能按文章数值 ID 猜测。先预览原因，再决定是否登记新的投递资格：

```sh
signalnest notifications-reevaluate --config config.toml \
  --operation-id review-v9-selected --event-id 17 --event-id 23 --at '<实际UTC秒>' --preview
signalnest notifications-reevaluate --config config.toml \
  --operation-id review-v9-selected --event-id 17 --event-id 23 --at '<实际UTC秒>'
# 恢复原操作：省略事件集合与时间，保持原政策、原时钟和原输入。
signalnest notifications-reevaluate --config config.toml --operation-id review-v9-selected
```

当前持久化重评只接受以下事件：属于本次启用实例；对应通知当前版本和最后一次 live 内容事件；最终路线为 `none`；投递资格时间、立即意图和冻结邮件成员均为空；按固定评估时钟，仍在最近七个上海自然日内，或是具有可信期限且已经开放的机会。近期不是邮件资格本身，仍须由纯规则判断。近期但已经截止的事件可得到新的 `STORE_ONLY` 决策；很久以前且已经截止的事件不进入持久重评。已有 `immediate` 或 `digest` 资格的当前事件可以预览，不能重新登记资格；Digest 没有立即 outbox 也已经锁定。

首次创建操作时，读取确定的事件、正文版本、当前 live 观察证据和选中决策，关闭读取事务后提取事实并运行规则。政策、评估时钟、上海 Digest 时间、事件上下文、事实及完整决策一并冻结。正文全文不额外写入操作或决策；已有版本保存正文，快照保留有限原文证据。规则提取、决策与文件 I/O 不在业务写入事务中执行。

`notification_operations` 是一个小批操作日志，复用既有政策、事件和决策表，不是后台队列。它保存规范参数及摘要、带摘要的不可变输入/决策快照、成员结果和完成时间。每个成员各用一个短事务提交新的决策、选中决策、最终路线、必要立即意图和操作进展。决策的 `evaluation_key` 使用 `operation:<operation_id>`，避免与 N1 的 `initial` 保留值冲突；邮件身份仍属于原事件，不属于操作 ID。

恢复先返回已经完成成员的原结果；尚未完成的成员继续原集合、原政策和原时钟，不重新运行筛选或规则。显式重放参数不同返回 `notification_operation_parameters_mismatch`。未完成项的内容/观察/选中决策变化记为 `stale_input`；政策、模式、Digest 日历或地址变化记为 `stale_context`。发送暂停及跨过自然日或截止时间本身不会改变原快照。过期时间需重新判断时，使用新操作 ID 和新显式评估时间；不能改写原操作。

`evaluated_at` 始终是固定的决策时钟；重评操作的 `completed_at` 是实际完成观察时间，二者独立。可组合接口允许注入完成时钟做测试，时钟回退到评估时间之前会明确失败并回滚该成员。已完成成员不再次读取时钟。

`complete=true` 表示所有原成员已有终结结果，并不表示全部获准登记。`stale_input` / `stale_context` 需要查看原因并另建操作，CLI 返回非零退出码。数据库写入失败返回 `notification_database_write_failed`，未完成成员与操作保留用于恢复；不将它伪装成“没有变化”。成员在 SQL 故障前成功提交的结果保持有效，恢复只处理剩余成员。

可独立调用的入口位于 `signalnest.notifications.maintenance`：

- `preview_policy_update` / `update_policy`：只读政策比较与显式不可变政策更新。
- `prepare_reevaluation`：纯本地准备固定集合、证据、规则决策；不写数据库。
- `register_reevaluation_in_transaction`：使用调用者的 Connection 注册准备结果并验证输入 token。
- `reevaluate_events`：有界预览、创建并逐项应用、或只凭 ID 恢复。

限制：只重评明确选择的当前 live 事件；不提供候选查询语言、自动全历史重评、资格撤销、取消后改投或持续时钟驱动重评。旧政策可审计，不保证当前代码能执行任意旧引擎；已经冻结的决策和邮件不重新计算。测试覆盖真实 SQLite 事务故障和关闭重开后的操作恢复，这部分不宣称完成断电验证。

## 历史：政策 v6 的显式升级

该节点事实提取/规则/引擎更新为 v6，路线仍为 v1；本次仅补已绑定正文报名指令的开放证据，保留 v5 助教招聘、软条件与申请表时间、v4 标题/正文关联及登录提交指令、v3 机会/提及区分和 v2 跨主题优先级。EMS 当期自动发现未验收，详见[助教边界](teaching-assistant.md)。已有实例先保留备份，用新代码预览画像和真实样本，再显式 `notifications-policy-update --profile ... --operation-id ... --at ...`，使用新的 operation ID；不通过初始化或修改原 revision 自动更新。旧政策期间生产 live 处理会明确返回过期政策错误，应先更新后恢复采集。升级不补发历史、不改已有投递资格或冻结 bytes；旧误命中的 Digest 资格不会自动撤回。

已持久登记的 v1/v2/v3/v4/v5 维护操作按原 operation ID 单独恢复，复用原固定快照/时钟/决策，不调用新版规则；需要新版评估时另建明确操作。Profile/Facts 的新增可空字段未提供时省略，包括 TopicMatch.context 与空 supporting_evidence，原摘要继续可校验。规则升级步骤、样本结果和未知范围见 [政策 v6](notifications.md)。

## 历史：政策 v7 的显式升级

计算机学院真实样本验收节点的事实/规则/引擎为v7，routing仍v1；新增CS离线Parser不等于开启CS来源。先使用 `profile-check` 和 `decision-preview --parser cs-undergrad-notices` 核对真实样本及画像，再用新的 `policy-v7-...` operation ID 预览/保存政策更新；采集配置/source_id不因更新而改变。v1–v6既有政策、决定、投递资格和冻结邮件保持原样，未完成维护操作继续复用自己的固定快照。本次没有在已部署实例发布v7；旧激活政策仍须显式更新后才能用新版规则处理live正文。不得因规则更新自动补发历史或撤回既有Digest。详细规则和限制见[CS离线节点](cs-undergrad.md)。


## 历史：本地政策 v9 的显式升级

以下保留v9交付当时的未部署状态；当前生产v10发布记录见下一节。

v9 修复两篇真实学生项目申报被历史教师征题排除的问题，同时保留当前教师征题反例、v8 行动语法和跨主题优先级；毕业年级、团队人数/互斥、导师资质和月份时间仍待核对。事实提取/规则/决策引擎升至 v9，routing v1、Parser、schema 和 NoticeContent 内容摘要不变，见[修复验证](validation/notification-applicant-scope-v9.md)。

**WSL 生产仍执行 v8，本轮没有发布 v9 或更新实例政策。** 以后经授权发布时，先备份，核对新代码的 `profile-check` 和真实输入 `decision-preview`；再用本文的新 operation ID 与实际 `--at` 预览 `notifications-policy-update`，确认后以相同参数显式保存。只安装代码不改变已启用政策，旧 manifest 与当前代码不兼容的 live 决策会明确失败，不应靠重新初始化绕过。

政策更新只决定后续决策，不改旧决定、事件、已有投递资格或冻结邮件。若确需修复旧无投递资格事件，按本文 `notifications-reevaluate` 的现有有界入口选择符合窗口的当前 live 事件、先预览并另建操作；不全历史重评或自动补发。未完成的旧维护操作继续恢复其原快照、原时钟和原决定，不能借 v9 偷换既有操作。

## 当前源码：v10 与 document 级机会回顾

v10 保留 v9 历史主体修复，补完整并列报名对象、秒/截至/表格日期及适用组别。新库头为 `0009_opportunity_review`；已有实例先备份，停止写入并显式 `storage-init`，然后使用新的 `notifications-policy-update --operation-id policy-v10-selected --at <实际UTC秒> --preview` 核对/登记政策。版本不兼容期间不会自动以新版规则修改旧决定。2026-10-21已按该流程为UC／CS生产实例迁移至0009并显式启用v10，逐表行摘要、原画像和冻结邮件校验通过，见[上线记录](validation/rollout-summary-20261021.md)。历史v9交付时未部署的记录保留，不再表示当前生产状态。

`notifications-reevaluate`继续只处理真实已有事件，不声称能补回event=0的历史通知。新增 `notifications-review-open` 是独立document级回顾：默认只读，显式身份/操作ID/时间加`--apply`后冻结整组，确认开放者才取得一次投递资格。操作恢复不重新解释原事实；旧启用时间、成员状态、live内容比较基线及冻结邮件不改写。默认走Digest，短截止早于下次Digest可走既有立即意图；digest_only仍优先。详见[机会回顾](open-opportunity-review.md)。
