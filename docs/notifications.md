# N0 本地事实与规则决策

2026-10-06 的 N0 已实现纯本地计算与 CLI 预览，这些函数仍没有持久化副作用。2026-10-07 另行完成 [N1 启用与事件持久化](notification-state.md)；现已另行完成 [N2 本地邮件计划](mail-planning.md)；[N3 SMTP 适配器](smtp.md)已完成，[N4 发送协调](mail-sending.md)与[政策维护](notification-maintenance.md)已完成。

上述为旧邮件模块阶段编号。2026-10-08 开始的[新迭代 N0–N3](iteration-20261008.md)另行记录；本页当前源码规则为政策 v10；WSL 生产仍执行 v8，本轮不部署或更新线上政策。

## 可调用接口

```python
from datetime import datetime
from pathlib import Path
from signalnest.notifications.contracts import EventContext
from signalnest.notifications.profile import load_profile
from signalnest.notifications.facts import extract_facts, rule_manifest
from signalnest.notifications.decision import decide

profile = load_profile(Path("profile.toml"))
facts = extract_facts(notice_content)  # 已有 NoticeContent；不再取网页
context = EventContext(next_digest_at=datetime.fromisoformat("2026-10-06T09:00:00+08:00"))
decision = decide(profile, facts, context, now=datetime.fromisoformat("2026-10-05T20:00:00+08:00"))
```

`extract_facts` 和 `decide` 每次完整计算，输入不可变，没有缓存、随机数或系统时钟。`load_profile` 才执行显式本地文件读取。CLI 的 HTML 输入先调用既有 Parser，JSON 输入直接校验 NoticeContent；不读取数据库。未来协调器负责提供可信事件 provenance，本地 `EventContext` 只声明预览意图。

## Profile 与事实覆盖

Profile 是独立 UTF-8 TOML（最多 64 KiB）；`profile.example.toml` 是虚构示例。字段为固定 schema_version=1、profile_id=self、可空 institution/study_level/role/college/major/entry_year，以及 interest_topics/include_phrases/exclude_topics/high_value_topics/store_only_topics。`role` 仅接受 student/faculty，表示当前申报主体角色，独立于学习层次；缺省为 unknown，不由 undergraduate 或 faculty 学习层次自动推断。缺省个人事实为 unknown，入学年不转换成当前年级。拒绝额外字段、非法值、重复项、空/控制字符词组和高价值与仅保存主题重叠；至少有一个关注主题或字面词组。主题/词组集合规范排序，影响判断的值变化会改变 Profile 摘要。主题包括 exchange、scholarship、research、competition、course_enrollment、minor、recommendation、teaching_assistant。

`rule_manifest()` 返回固定规则清单，主题词组在 `facts.TOPIC_PHRASES` 中可检查；使用有限字面词组与上下文规则，没有打分/模型/DSL。`TopicMatch.context` 区分标题主题 `subject`、与入场行动相连的 `opportunity`、正文顺带提及 `incidental`、待核对关系 `uncertain`。所有出现位置都保留有限证据；正文裸词本身不命中主动兴趣或高价值机会。标题仍可表示普通相关信息；**只有标题明确主题命中**才能触发排除，避免正文提及另一主题就误杀。

v3 建立的同句关联规则继续保留：报名/申请/申报/征集/招募等行动与具体主题间隔最多 24 个字符；辅修要求直接的“辅修专业报名/申请”等关系，而且必须绑定该行动，不能从附近另一项选课或科研行动借用报名语义。缴费、退出、退课等办理对象、导航路径、明确否定、历史或已结束的报名不作为入场证据。判断针对局部关系，不因为整篇含缴费就屏蔽真正报名；逗号后独立的登录或收费步骤不否定前面的报名。逐个检查同一词组的所有出现，前文收费说明也不掩盖后文新报名。另一明确主题隔在行动和主题之间时不绑定，科研训练选课是已有真实样本支持的共享行动。

v4 补两类关联：只有一个标题主题、正文明确“即日起报名”或受支持的当前申请指令、该行动所在句没有另一个具名主题，且正文也没有竞争的另一项明确机会、标题不是结果/办理说明时，可以把正文行动关联到标题主题。`TopicMatch.evidence` 保留标题位置，`supporting_evidence` 单独保留正文指令位置，不把两字段拼成伪原文。正文具名辅修/选课等行动不会被科研标题借用，即使另一句再写泛指报名也不能借用；仅费用提及则不阻断真正机会。

“请/须/需/应…完成/进行/提交…报名/申请/申报/选课”的有限指令可以包含登录系统；必须绑定同一个行动，不能包含菜单/箭头路径、查看已有记录、缴费或退课等对象。这与仅“登录→报名申请→查询”不同。v6 仅在该正文指令通过原入场排除规则、且其具体行动位于已确认 opportunity 的正文证据或标题 supporting_evidence 内时确认 opening_confirmed，并保留指令在 time_evidence 中的原文位置；不虚构 opens_at。仅标题指令、关系 uncertain、另一主题的行动或仅截止日期不能取得这个确认。可信未来开始仍为 not_started，未支持的截止仍为 unknown。

原 Q01/Q02 均含“即日起”，输入保持不变。R01/R02 工程构造边界验证不含“即日起”的当前指令；修复前均 DIGEST，v6 均 PUSH_NOW/deadline_soon，保留[当时记录](validation/notification-rule-fix-20261010.md)。用户报告的原始表达现已单列 [O01/O02](validation/notification-original-misses-cases.json)：原样“即日起接受报名”“请登录学校系统报名辅修专业”加当天截止，v7 实际为 DIGEST/IGNORE，v8 均 PUSH_NOW。旧 Q/R 输入不覆盖；R 清单只更新过时的来源说明，原清单字节另存。

v8 的接受报名表达只扩展“即日起”后有限的“接受”，不从缴费、历史记录或裸“接受”推导机会。直接登录指令只接受“请/须/需/应登录(学校)系统”后立即接入场行动，仍需具名正文机会或唯一标题主题关联，并通过同一排除规则；不泛化为任意登录指令。直接登录只确认 opening_confirmed，opens_at 保持 null；“即日起”沿用已有站点日期依据。附加费用说明不会否决独立真实报名，菜单/查询/历史/缴费/退课对象仍不会命中。已有跨主题优先级、未知资格与未支持时间表达式保持，实际前后证据见[完成记录](validation/notification-original-misses-20261010.md)。

多标题主题配泛指报名，或无标题主题而正文中性“科研训练相关安排”与另一句泛指报名之间无法可靠绑定时，保留 `uncertain` 和 `topic_action_link_unknown`，含主题/行动原文证据。仅命中这种待核对兴趣时为 STORE_ONLY/relevance unknown，不制造邮件资格或借用截止升级；已取得邮件资格的条件更新仍按原跟踪约定提示核对。明确菜单、费用、退课、历史报名等负例不因此变成不确定机会。有限词组未覆盖所有自然语言，不宣称通用语义理解。

真实 128231 新生选课通知的“辅修专业单独缴费”是 incidental：关注辅修/科研且选课仅保存的画像得到 STORE_ONLY/none，只有辅修兴趣则 IGNORE/none。117011 辅修报名仍识别为 opportunity，临近可信期限时 PUSH_NOW，并保留资格未知。128291 科研选课仍可覆盖另一个主题的仅保存选择；同一主题同时关注和仅保存仍服从仅保存。`include_phrases` 是用户显式选择的宽泛字面关注，不作为正则，也不采用主题机会过滤，因此主动填写“辅修”仍可能收到仅提及它的通知；独立字面兴趣不会被另一保存主题压制。

v5 的助教主题使用更严格的当前招聘门槛：裸“助教/教学助理”仅 incidental，招聘标题需有可关联正文当前招聘/申请指令；没有当前关系保留 uncertain，不借另一项报名。工资、津贴、考核、历史及未开放不成为新岗位。实际本科教学受益者不推导申请层次，“原则上”的研究生对象不当作硬排除；院属、全日制、教师意见和附件继续待核对。仅对已观察初次申请表模板支持完整日期、相符周几和“下午13–23点前”，不取公布/考核日期。真实历史样本、本地预览和自动来源覆盖缺口见[助教招聘](teaching-assistant.md)。

资格只从明确“面向/仅限/报名对象”等对象声明提取学校、学习层次、学院、专业、20XX 入学年等有限条件。字段按显式 AND 作 match/mismatch/unknown；已知必要条件可靠不匹配为 ineligible，缺值/未支持/冲突为 unknown。GPA、语言分数、年级、全日制/在校状态、年龄、处分记录、项目成员身份及 OR/例外当前不能核准。识别“本科生”不能掩盖另外列出的条件；明确资格章节内尚未理解的编号条目保留未知，跨条目/跨句 OR 不按 AND 作可靠排除。没有对象声明不等于面向全体。eligible 仅表示支持的可见条件匹配，不是官方资格认定；不承诺理解任意通知的全部隐藏条件。

时间接受有明确行动上下文的完整年份日期与支持的时刻，按 Asia/Shanghai 解释；显式小时按原文，只有日期的截止取当日 23:59:59，开始取 00:00:00，并保留原句。即日起/自通知发布之日起可使用输入的站点发布日期作开始依据。v2 仅在同一句明确报名/申请/申报/选课区间 `完整年份日期至/到月日` 中继承起点年份；不从发布日期补全年份、不跨句继承、不为跨年顺序自动加一年。精确 `24:00` 表示次日 00:00，24:01、秒、下午5点、未支持时区仍未知。

项目团队 `2024年2月25日前…提交` 是申请方期限，和后续导师审核、学院汇总的期限区分；不取全篇最后日期。`日前` 没有精确时刻，`deadline_lower_at` 保存当日日初，`deadline_at` 保存保守日末上界，并保留 `imprecise_deadline` 待核对，不声称精确截止，也不在该日日初就判已过期。17361 的“可提出立项补报申请”可确认当前开放，但不虚构 opens_at。紧迫判断采用下界；已经进入不确定区间则采用本次明确评估时刻，让 72 小时与首启紧迫例外及时提醒，而关闭判断仍使用上界。例如 24 日首启、下一次 Digest 是 25 日 09:00，不能等到可能已过期才汇总。区间内提醒始终保留 needs_review，用户应提前核对，日末上界不是可以拖到日末申报的保证。

未解决的同一申请“截止另行通知”等会取消可信期限；竞赛样本的开始日期不能冒充分赛道截止。117011 明确独立的“第二轮报名时间将另行通知”保留 `secondary_time_unknown`，不覆盖已声明的首轮区间；不泛化到任意附加时间声明。“报名截止后”是流程说明，不是另一个期限。可信期限已过为 closed；明确未来开始为 not_started；支持的开始依据或明确当前允许申报且尚未截止才能是 open。未知表达式不会被粗略截断后伪装成可信时间。

结果/结题/验收等标题按 reference 保存价值处理；仅办理缴费/退出等且标题没有入场行动的通知也为 reference。机会类别依据通过局部校验的入场词组，但紧迫/高价值规则还必须命中相关主题的机会证据，不能把另一个主题的截止借给顺带提及的兴趣。图片、附件只记录引用索引与 URL：没有 OCR、下载或附件正文解析。媒体型正文或明确“条件/时间见附件”等依赖保留 information_incomplete / unknown；正文信息充分、仅补充附件不会一律变资格未知。无法从媒体确认相关性时默认 STORE_ONLY + needs_review，可能漏掉媒体独有的机会。

证据位置为 **NoticeContent.title/body_text 中的 Unicode 字符索引，end 不包含**，摘录最多 120 字且与位置相符，不是原 HTML 字节偏移。媒体证据无虚构摘录，只记录引用。保留引用不意味着网页 HTML 可安全展示，后续界面仍须安全处理。没有可靠的全机会取消正样本，目前不启用取消提取；违规取消个人资格不等于活动取消。

## 决策与路线

| 输出 | 当前语义 |
| --- | --- |
| PUSH_NOW | 确认相关且已开放、可信截止在 72 小时内；或 recent new 命中高价值主题。未知资格仍明确待核对。 |
| DIGEST | 相关非紧急机会、普通信息或内容更新；可同时 needs_review。 |
| STORE_ONLY | 结果/参考内容、仅保存主题、相关已截止的首启/历史候选，或相关性不足的媒体信息。 |
| IGNORE | 可靠明确排除、充分可见文本无兴趣命中、无保留价值的已知不符合/已关闭机会；原业务数据仍保留。 |

近期按上海当日及之前 6 个自然日，仅配合高价值主题与机会/new 上下文；日期近期本身不触发提醒。未来发布日期标记待核对，不作为已发布紧急依据。旧日期但可信短截止的正常新机会仍可及时提示。needs_review 独立表示信息缺口；它既不等于资格符合，也不强制所有机会等到明天。

普通 update 默认汇总，不将任意正文变化猜成紧急条件变化。显式旧事实与登记路线表明此前已取得邮件资格，且支持的资格/开始/截止确实改变时，保留“条件变化”提醒，即使新条件不符合或截止已过；用户当前可靠排除仍优先。缺少比较证据显式标记未知；有限事实相同不能证明其他正文变化仅是排版。N0 不创建或去重实际事件。

`effective_route` 在决策中一并计算：STORE_ONLY/IGNORE→none，DIGEST→digest；PUSH_NOW + hybrid 通常 immediate，digest_only→digest。activation_recent 默认降为 digest，但可信开放截止不晚于 next_digest_at 保留 immediate；digest_only 是显式用户选择，仍优先。historical 不产生主动路线。所有时钟显式传入且带 UTC offset，next_digest_at 必须晚于 now；输出 evaluated_at 统一 UTC。路线不代表邮件已经计划或发送。

## 申请主体与历史阶段（政策 v9）

两篇真实学生申报中的“学校面向全校教师共征集了……”是已完成的选题征集；“面向本科生的、在指导教师指导下……”描述受益学生及指导关系。v9 保留这些原句的未知项及精确位置，不把它们变成当前学生的教师 role 或 study_level 硬门槛。明确“项目负责人必须为本科生”单独形成本科约束；带否定、OR 或未支持附加条件时不能硬匹配。当前教师征题与明确当前学生申报同时存在而无法可靠区分时，相关申请约束保留 unsupported/unknown，不静默选教师。

毕业年级不能负责、团队人数、同时只能参与一个项目和指导教师职称/学位等条件继续保留原文核对证据，不凭 Profile 的本科身份宣称全部符合；月份阶段不生成某日或 23:59 截止。固定输入和时钟下，两篇真实学生申报均为 DIGEST / unknown / needs_review。14147 明确“现面向全校教师征集”的当前主体仍是教师，后续本科生选题不改写本次主体；student 仍 IGNORE/ineligible，role 未填仍 unknown。有限历史、主体及指导模板不代表通用中文语义理解；v8 的接受报名/直接登录语法、跨主题优先级和菜单/历史/缴费负例保持，见[修复验证](validation/notification-applicant-scope-v9.md)。

## 版本、摘要与边界

当前固定版本分别为 `whu-notice-facts-v10`、`notification-rules-v10`、`notification-decision-v10`、`notification-routing-v1`。v10补并列报名对象、截至/秒/表格和分组期限；v9历史主体修复及此前跨主题、助教、报名语法继续保留，旧版本回放独立保存。既有事件的路线行为保持；新的opportunity_review沿用首启式默认Digest及短截止例外，digest_only仍优先。UC Parser v5、CS v3、EMS离线Parser及NoticeContent摘要不变；本地schema新增0009，生产尚未升级。变更规则时显式升版，不读取运行中可变配置。`policy_manifest(profile)`冻结Profile、事实规则、决策顺序/阈值、路线与版本；input_sha256另绑定原始提取facts、EventContext和显式时钟。派生组别时间用于判断，不替换封印的原提取。decide拒绝旧提取器facts直接混用，新评估须显式重提取。

原 bytes 摘要、`NoticeContent.content_sha256()`、Parser 版本保持原职责；事实摘要不替代正文摘要，规则版本不加入正文摘要。相同输入、上下文与时钟产生同一决策；新时钟/画像/上下文会产生新输入摘要，N0 纯函数本身不持久化这些结果。版本标识加固定规则清单是追溯约定，不保证跨规则改动忘记升版仍能安全恢复。

真实 fixture 离线覆盖科研训练选课、辅修 OR/绩点条件、大创中期多时间/项目身份、教师选题征集、结题结果及 18135 竞赛模板。14147 本次申报主体是教师，后续受益本科生不成为本次申请者约束：role=student 明确不匹配，当前实现 IGNORE/none 而保留业务正文；role 未填则资格未知。仅提到教师指导不构成角色排除。117011、17361、128291 临近可核对期限会 PUSH_NOW 并保留资格/材料/日期精度未知；127511 结题结果只保存；18135 的分赛道截止不明，普通竞争兴趣得到 DIGEST+needs_review，正文个别违规者“取消参赛资格”不会变成整场取消。

当前[生产评估](validation/notification-production.md)分别重放 13 个固定真实情境和 2 个新合成最小情境，保留 v2/v3/v4 历史结果，并单列 6 个真实助教/跨主题情境。原研究 S01–S09、S11 的 10 个 Action 已于 2026-10-08 人工确认，适用原固定输入，不包括所有事实、核对标记、路线或生产实现；“研究标签全仍待确认”的旧描述已过时。生产 Profile/事件经过显式适配，逐例映射注明无完全相同的整组输入；P05/P07 原人工 STORE_ONLY 与工程 IGNORE 的语义差异单列待重新确认。合成例没有人工标签。不能将工程回归通过率当成人工准确率。真实样本没有覆盖全部主题、资格或可靠取消正例；新 N1 助教已完成离线解析、判断与邮件预览，自动发现当期机会仍未验收。

### 已有实例显式升级政策

升级代码不会改写旧政策、事件、决定或被冻结邮件。新可空字段未提供或 supporting_evidence 为空时从规范快照省略，旧 `TopicMatch.context=None` 表示历史未知而非本次新分类，v1–v8 已保存的 Profile/Facts/Decision 原摘要仍可校验；旧 frozen mail 和固定输入的重评操作可继续读取。代码新增实时决策时，旧激活政策返回 `notification_policy_outdated`，须先显式预览和更新：

```sh
signalnest profile-check --profile profile.toml
# 显式填写 role（若已知），核对真实样本的预览后再发布政策。
signalnest notifications-policy-update --config config.toml \
  --profile profile.toml --operation-id policy-v10-applicant-scope --at '<实际UTC秒>' --preview
signalnest notifications-policy-update --config config.toml \
  --profile profile.toml --operation-id policy-v10-applicant-scope --at '<实际UTC秒>'
```

政策更新不自动补发历史。希望用 v10 重评未取得投递资格、仍符合窗口的当前 live 事件时，按[政策维护](notification-maintenance.md)显式选择 event ID、新 operation ID 与时钟。已有邮件资格及冻结邮件保持原路线、理由和字节；旧误命中的 Digest 资格不会因升级自动撤回，已冻结邮件也不会自动重写。旧未完成重评恢复使用旧快照，不能偷换成 v9 规则。命令中的时间占位须替换为本次操作的实际 UTC Unix 秒，同一 operation ID 的预览和应用使用相同参数；本轮没有在 WSL 执行这些命令。

本实现参考[通知决策调研](../research/notification-decisions.md)最新 Action+needs_review 口径，拆开认知缺口和优先级；规则仍以代码和已验证样本为准，调研中的旧验收表/IMMEDIATE/REVIEW 术语不作为实现接口。独立 live 基线、稳定启用身份、A→B→A 事件与原子 planned 意图已由 [N1](notification-state.md)实现并另行测试；N0 纯函数测试本身不证明数据库事务、SMTP、重启投递或断电恢复。

### 计算机学院真实样本（政策 v7）

见[离线适配](cs-undergrad.md)与[六情境实际回放](validation/cs-offline-current.json)。真实申请分支跨分号，不能只按一处“面向研究生”硬排本科；学年与独立落款年份冲突及缺少年份的申请月日均保留原文位置，阻止伪造开放/紧急期限。当前假设本科画像得到DIGEST+needs_review，不是当前可申请的资格确认。菜单/缴费/历史报名、科研跨主题优先级及既有Action边界保持。原 v6/v7/v8 快照另存，当前 v9 保留既有 Action 语义；未新增人工 gold。本次 v9 修复不改变已有 CS 自动采集或目标实例政策。

## 当前规则 v10：开放机会与申请组别

当前 facts/rules/decision 为 v10，routing v1。完整“本科生和研究生均可报名”形成同一个允许集合，不能只匹配后半句。仅有“或”、例外、未知资格/学籍/附件条件仍保留未知。时间支持明确“截至”与合法整数秒，同一表格行关联有限报名阶段；孤立时间标题本身不使后面的有效截止失效。未知年份、非法秒/偏移/分数秒、未理解时间声明仍不能冒充精确日期。

`NoticeFacts.scoped_deadlines`保留本科、研究生、国际生等有证据的分支；无分支时省略以保留旧序列化。`deadline_for_profile`创建只供判断的时间视图，不改变原事实/hash。未知画像或同组冲突不选择最早/最晚日期；本科不借用研究生或国际生延期。国际生例外仍待核对。新规则对两个真实竞赛的有限当前征集模板结合报名安排确认开放，不把裸未来截止或近期发布日期当开放。

首启七天边界保留防止历史群发，较早发布但仍开放的通知由独立[机会回顾](open-opportunity-review.md)进入评估；不是改成30天，也不伪造new。模型可在回顾预览中辅助语义判断，但目前不会覆盖规则资格、修改政策或直接发信。历史报告和人工标签保持原样；本地v10未部署，正式实例最近核验仍v8。
