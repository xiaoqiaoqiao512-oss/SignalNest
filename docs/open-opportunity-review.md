# 当前开放机会回顾与模型语义复核

本地 v10 补上“较早发布但现在仍可申请”的入口。首次启用的近七天发布窗口继续保留，回顾不改 activation_at、不伪造 new、不更改旧决策或冻结邮件。最近生产核验仍为 v8/schema0008；此交付未部署、迁移生产实例或补发邮件。

## 默认只读预览

```sh
signalnest notifications-review-open --config signalnest.toml --at '<实际UTC秒>' --preview
# 明确选择来源内稳定身份；source_id来自配置，不从文件名推断。
signalnest notifications-review-open --config signalnest.toml --at '<实际UTC秒>' \
  --source-document-id 'stable-source-id-a' --source-document-id 'stable-source-id-b' \
  --source-document-id 'stable-source-id-c' --preview
```

不带 `--apply` 就是预览，不取写锁，数据库使用只读连接。默认按站点发布日期回看90天，最多100条，可用 `--lookback-days 1..366 --limit 1..100` 调整；输出明确选择范围、数量与 truncated，**不承诺这个有界批次涵盖全部开放机会**。明确身份可超出该自动选择范围。

结果逐条区分：

- `open`：有当前报名行动与可信适用期限，主题相关，且没有已知资格冲突。个人学籍、组队、附件条件仍可能 unknown，摘要保留待核对提示，不宣称资格获官方确认。
- `unknown`：期限/当前阶段/适用组别或可追溯live证据不足；不自动授予投递资格。
- `excluded`：已截止、未开放、资格明确不符、非相关机会或仅结果/后续材料阶段。
- `already_notified`：已有投递资格、冻结成员或已登记的机会回顾，不重复提醒。
- `unavailable`：明确身份没有可用记录。

期限按有证据的适用组别提取。研究生或留学生的延期不能自动套用给本科生；画像缺少对应身份时保留核对。没有年份、未知阶段、复杂分支不能用“发布日期较近”或全篇最晚日期补齐。

## 显式登记与恢复

先检查预览。正式安装新代码时，应在现有实例锁/一致备份和停止写入的维护范围内显式 `storage-init` 升级至0009，再显式更新通知政策至v10；见[维护步骤](notification-maintenance.md)。迁移不自动回顾或创建邮件。

```sh
signalnest notifications-review-open --config signalnest.toml \
  --operation-id open-review-selected --at '<实际UTC秒>' \
  --source-document-id 'stable-source-id-a' --source-document-id 'stable-source-id-b' \
  --source-document-id 'stable-source-id-c' --apply
# 只凭原操作ID恢复，不改原选择、规则结果或评估时钟。
signalnest notifications-review-open --config signalnest.toml \
  --operation-id open-review-selected --apply
```

登记必须明确选择最多100个稳定身份，不能把自动90天选择直接整批投递。先独立保存冻结操作，然后短事务校验整组当前版本/live证据/政策，再一并写入开放条目的 `opportunity_review` 事件、决策、投递资格及必要立即意图。普通机会进入Digest；可信截止早于下次Digest且hybrid允许时使用既有立即路径，digest_only仍优先。此命令不调用SMTP、冻结或发送邮件；之后是否计划/发送由既有邮件流程和暂停状态决定。对实际已启用后台邮件的实例执行登记，会让后续timer有资格计划发送，因此要先检查并选择条目。

每安装/通知只保留一个序号0的回顾事件；new/update/activation_recent仍是正序号。唯一约束防止换操作ID重复投递，live比较基线和旧启用成员状态保持。业务提交失败整组回滚，冻结操作可恢复；输入或政策变化明确stale，不能借恢复替换原时钟。故障注入不是断电验证。

## 可选模型判断

```sh
signalnest notifications-review-open --config signalnest.toml --at '<实际UTC秒>' \
  --source-document-id 'stable-source-id-a' --preview --model-config ask.toml
```

`--model-config`复用[查询配置](query-agent.md)的model/limits，凭据在实际请求时从指定环境变量读取，不需要把密钥写进配置。不会打开该查询配置的其它实例，也不会用它的Profile覆盖当前通知政策。没有此选项则不读取密钥、不调用模型。模型配置仅允许预览，不能与 `--apply` 同用。

每篇只做一次结构化语义复核，共享整批调用/HTTP/时间/费用预算，复用已有Chat Completions客户端和唯一重试层。模型区分实际机会与提及、申请者与受益者、学生报名与行政阶段，返回按对象分组的时间原表达与未知。无需为已有正文运行一个自由浏览的Agent循环。

每项判断须引用title/body_text的准确Unicode区间；输出的时间表达须确实包含在引句中，不能凭空补年份或把最晚日期当所有人的期限。网页内容作为不可信数据处理，模型不能写库或发信。引文通过只证明文字存在，**不能证明模型理解正确**。当前模型结果是与规则并列的复核建议，不覆盖资格、当前投递路线或未知期限。预算、模型、无凭据或引据校验失败返回有限fallback，规则预览仍保留；显式模型复核降级时CLI退出1。

当前已接通可实际调用的可选模型入口，使用MockTransport验证协议、预算、引据和失败；本轮未调用真实提供方。下一步可以在经脱敏的案例上完成模型语义质量验收，再决定哪些有证据的判断进入持久政策，不能将离线模拟当真实模型效果。

## 规则与兼容性

facts/rules/decision版本为v10，Parser、NoticeContent摘要与依赖不变。新增ScopedDeadline空值省略，旧facts/decision序列化与封印继续可读；旧评估、人工标签、冻结邮件不会因升级被静默改写。0009增加窄操作表并保留循环外键数据地扩展事件约束，SQLite外键保持ON。仓库只保留小型合成回归输入；真实公告正文与逐条离线快照留在本地研究记录中，不随代码提交。
