# 当前通知规则的离线评估

评估器调用项目实际 Parser、事实提取和决策函数；离线工程期望用于回归，不是人工准确率、全站召回率或资格判定。固定输入与合成输入分开保存，所有 Profile 均为示例配置，不代表当前用户。

## 当前记录

- [固定输入清单](notification-production-cases.json)与[真实通知汇总](notification-production-current.json)
- [合成输入清单](notification-production-synthetic-cases.json)与[合成当前结果](notification-production-synthetic-current.json)
- [助教回归清单](teaching-assistant-cases.json)与[当前结果](teaching-assistant-current.json)
- [有限指令回归清单](notification-current-instruction-cases.json)与[当前结果](notification-current-instruction-current.json)
- [原始漏报表达清单](notification-original-misses-cases.json)与[当前结果](notification-original-misses-current.json)
- [CS 离线输入](cs-offline-cases.json)与[紧凑汇总](cs-offline-current.json)
- [申请主体规则与合成回归说明](notification-applicant-scope-v9.md)

历史材料保留聚焦的 v5/v7 失败回归、清单变更记录和一个精简 v6 决策兼容样本。重复的多版本全量输出已从待推送内容中裁掉；测试继续比较固定输入、关键 Action 和版本边界。

## 版本与运行

当前源码 facts/rules/decision 为 v10，routing 为 v1；WHU、EMS 和 CS Parser 保持自身版本。开放机会回顾新增迁移0009；UC／CS生产已于2026-10-21在一致备份后显式升级并更新v10政策。旧决策与冻结邮件经逐表行摘要核对保留，见[上线记录](rollout-summary-20261021.md)。这些生产证据独立于本页固定时钟和示例画像的离线评估，不把工程结果转换成人工标签。

从仓库根目录运行评估器时，为每份输出指定一个尚不存在的路径：

    uv run --locked python deploy/evaluate_notifications.py --check --output /tmp/signalnest-notification-current.json
    uv run --locked python deploy/evaluate_notifications.py --check --cases docs/validation/notification-original-misses-cases.json --output /tmp/signalnest-notification-misses.json

不传 --output 时结果写到标准输出；已有输出文件不会被覆盖。详情见[通知规则说明](../notifications.md)和[维护入口](../notification-maintenance.md)。

## 独立人工评价进度

原六组31个工程情境与人工评价进度分开维护；工程期望不等于真人评价。v9申请主体修复由短合成文本回归覆盖，原公告正文、逐条评估快照和机器运行记录留在本地研究记录中，不随代码提交。

新增[人工入口](../human-review.md)绑定完整输入与冻结回放证据，[当前待确认结果](human-review-pending-20261011.json)为 31 个通知情境加 15 个搜索查询，0 confirmed、46 pending。未替用户创建标签；漏报/误报率在无人工分母时均为 null。工程回归和后续人工子集统计分别报告，旧决策和已冻结邮件不改写。

当前源码与UC／CS生产政策均为v10。分组期限、开放机会回顾和后续阶段边界由代码测试覆盖；真实正文和完整逐条快照仍不公开。本轮三条生产机会的回顾状态见[脱敏上线摘要](rollout-summary-20261021.md)。旧人工评价包和既有决策保持原样，不把新增工程判断转换成人工标签。
