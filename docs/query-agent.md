# 只读校园查询 Agent

本轮实现范围：一次显式 `ask` 可以在 UC、CS 两个现有实例中检索、读取通知、读取查询画像和来源诊断。模型只选择这几个固定工具，不接触 SQL、路径、Shell、采集、邮件或任意网址。使用已有 HTTPX 的同一个 Chat Completions 客户端连接 OpenAI 官方或显式配置的兼容接口，不新增框架、依赖、表或索引；生产通知政策、旧决策和冻结邮件不变。

## 接口与只读边界

独立查询 TOML 用实例 ID 引用已有实例配置，通知用 `(instance_id, document_id)` 定位，不能把不同数据库内相同整数 ID 混为一条。所有数据库使用 SQLite `mode=ro` 和 `query_only=ON`，不获取写入锁、隐式迁移、修复索引或保存对话。只读不能证明资料完整或实时；来源状态必须展示覆盖年龄与未成功积压。每个搜索/读取是短快照，跨库没有共同事务；已读取的正文版本在一次问题内固定。

普通 TOML 配置独立于采集和 SMTP，路径相对于查询配置文件；实例配置内存储路径仍相对于该实例配置。画像是操作者提供的查询上下文，不证明官方报名资格。帮助和 `ask-config-check` 不打开数据库、不读取环境密钥、不联网。

## 模型与预算

先固定 OpenAI Chat Completions 的函数调用消息交接，用同步 HTTPX，显式超时，无自动重定向、环境代理或底层重试。官方提供方保持固定 HTTPS 地址；兼容接口必须显式配置 `provider` 和 `base_url`，路径只接受 `/v1`，拒绝凭据、query和fragment。远程只接受HTTPS，HTTP仅允许字面回环地址127.0.0.1或::1；不将localhost的DNS解析当成回环保证。密钥只在实际模型查询时从指定环境变量读取。问题、工具返回的有限通知文本和查询画像会发送给选定提供方；`--local` 不调用模型。

一次查询限制逻辑模型轮数、实际 HTTP 次数、工具次数、返回字符量、通知数量、输出 token 和总运行时间。只有适配器拥有重试层；发送可能已发生的读/写失败不自动重试。`pricing_mode=token_rates`要求操作者填写输入/输出USD单价，每次物理请求前按JSON UTF-8字节数加余量预留费用；无有效usage按预留计费。该上限是应用估算，不是供应商账单保证。兼容代理不知道单价时，须显式选择`unpriced_proxy`并省略价格和USD预算：费用输出null，`price_source=unknown`，仍限制请求、时间与输出，不承诺美元或账户额度上限。超限停止，返回已有检索结果。

## 日期、来源与回答

程序捕获一次明确时钟，按配置时区处理有限日期表达式。发布日期与截止时间分别处理，模型不计算相对日期。未知或含糊表达式不猜测。回答以候选通知为单位，条件和截止时间直接引用已读取的原文；模型不供应网址、不自行宣称完整符合资格。未读取的通知、捏造的引用或原文以外的引文不能形成回答。

附件与图片仅提供引用，条件在附件中时保持未知。本轮不下载附件、不实现后台语义筛选、反馈、Embedding、向量索引、Reranker 或 Agent 框架；候选相关性仍需要真实模型与人工评价验证。模型失败返回有限搜索结果与错误分类，搜索结果不会被包装成已核验的报名机会。

## 使用

复制仓库中的 `ask.example.toml`，填写已有 UC、CS 配置与查询画像路径。它默认不含 `[model]`，没有模型或密钥时先用 `--local`；不会以查询之名创建空数据库。

```sh
uv run --locked signalnest ask-config-check --config ask.toml
uv run --locked signalnest ask --config ask.toml --local --question '助教招聘'
uv run --locked signalnest ask --config ask.toml --local --json \
  --at '2026-10-12T14:00:00+08:00' --question '最近一个月的科研机会'
```

官方模型使用`ask.example.toml`末尾的`[model]`示例，替换model并核对input/output的USD每百万token单价；示例数值**不是实际价格**。`openai-chat-completions`保持官方固定端点及`max_completion_tokens`。兼容接口使用[ask.proxy.example.toml](../ask.proxy.example.toml)，`openai-compatible-chat-completions`显式配置地址；`token_limit_parameter`可选择`max_tokens`或`max_completion_tokens`，只发送选定的一项，不在失败后偷偷换参数重发。该接口仍必须支持函数工具；不提供网页搜索、任意模型URL工具或远程MCP。API密钥用`api_key_env`指定的环境变量，官方默认`OPENAI_API_KEY`，代理示例使用`SIGNALNEST_PROXY_API_KEY`；不放TOML、不进入模型上下文或日志。

2026-10-13在用户提供的本机兼容代理完成一次GET模型清单与两轮受限回放，合计六次模型POST；`auto`形成了一份有实际原文引用的回答，其他案例受预算限制未全部完成，详见[真实试验记录](validation/query-proxy-20261013.md)。`auto`是代理别名，其实际后端未固定；模型清单是该代理声明，不证明所有模型均已测试或官方可用。代理127.0.0.1属于运行查询进程的机器，不能直接用于另一台WSL主机；生产部署尚未连接这个代理。

真实调用需要运行进程能读到 `api_key_env` 指定的环境变量；离线回放、`--local`、帮助和配置校验不需要密钥。当前客户端不会自动寻找或加载 `.env` 文件。若只在 WSL 的 Bash 终端试用，可在同一终端输入密钥而不把值写进命令行：

```sh
read -r -s -p 'OpenAI API key: ' OPENAI_API_KEY
printf '\n'
export OPENAI_API_KEY
```

然后在该终端执行查询或固定样本回放。变量只供这个 shell 及其子进程使用，其他 SSH 会话和 systemd 服务不会自动继承。需要交给另一会话运行时，由对应启动环境显式注入；仓库外的凭据文件应限制为所有者可读写，不使用命令参数传递密钥。操作者只需告知提供方、模型名、环境变量名或受限文件路径，不把密钥贴到聊天、TOML、测试快照或日志。环境变量用法见 [OpenAI 官方快速入门](https://developers.openai.com/api/docs/quickstart)。

固定公开原文和合成画像可用`deploy/evaluate_queries.py`显式验证。已知价格时设置`--max-total-cost-usd 0.05 --run-seconds 180`；未知代理价格时改用`--max-total-requests 4 --run-seconds 90`，不能再宣称0.05美元预算。每项只有剩余的整批请求额度，重试也计入。取得真实结果后再填写独立人工评价；此步骤不部署查询版本到生产实例，也不更新采集、通知政策或邮件。

程序调用接口为 `ask(QuerySettings, Question) -> QueryReport`；`Question.at` 必须为有 UTC offset 的显式时刻。CLI 默认仅捕获一次实际时间，`--at` 可重放日期计算。`QueryTools.call` 只接受固定 Pydantic 参数：

`--at` 是问题的计算时钟；数据库仍读取本次执行时的资料，不提供历史数据库快照。工具轨迹只保留受校验的实例/通知 ID、工具名称与错误，不保存模型参数原文。

| 工具 | 输入/输出与边界 |
| --- | --- |
| `search` | query、可选 instance_id、offset；范围在程序中固定，各来源独立返回有限结果及 total，保留词法别名，不跨库比较 BM25 分数。 |
| `read_notice` | instance_id＋document_id＋正文字符 offset；返回固定版本、有限正文分块、next_offset、规则内容类别/资格/时间状态及证据、媒体引用，不发送 body_html。直接读取 ID 也校验已解析的发布日期范围。 |
| `read_profile` | 无参数；返回显式查询画像或缺失，不读取生产通道、收件人、SMTP 配置或环境。 |
| `source_status` | 无参数；按问题时钟读取所有实例的覆盖年龄、首次积压、到期复查、失败和有限诊断，不能证明持续在线。 |
| `answer` | 实际读取的 reference＋连续原文引句；结束循环。网址/发布日期/版本/规则结果由程序补全，模型不能提供替代链接或自行声明符合资格。 |

原文引句必须出现在本轮**实际返回给模型**的正文分块、标题或规则证据中；读取整条后仅返回首块，不能借此引用未交付的末尾。一次查询内重复读取固定最初读取的正文版本，即使期间数据库有新提交；输出版本和内容摘要可追溯，但不承诺跨来源同一时刻快照。未知工具、路径、额外字段、任意 URL 和 SQL 均不属于接口。

当前源码查询预览使用事实/资格规则 v10，仅做纯内存计算，没有通知事件、路线更新或入库。资格比较始终标注不是完整官方核验；无画像、规则不理解的条件、附件未读等不默认符合。精确引文校验保证可追溯，**不能证明模型选择某句话的语义一定正确或检索没有漏报**。

当前查询协议为 `campus-query-v4`，沿用 v2 的截止引文校验。`time_evidence` 沿用事实契约，其中包含报名开始、行政审核、报名指令等，不能全部当截止。`facts.deadline_evidence` 仅返回可以独立支持已提取截止边界的完整时间声明；开始日期、区间起点、裸日期和普通报名说明不能借另一句的截止取得 `rule_supported`。

查询层现复用 v10 纯函数（旧 v4 回放保留自己的版本）；期限规则已补截至、秒、有限表格关联和分组截止，工具同时展示 scoped_deadlines，不把各组最晚日期作为全局截止。截止引句校验仍遵循以下限制：先在只保留非时间标题上下文的内存输入中，单独重放原时间声明，再重放模型选择的引句；两者都须得到与原通知一致的截止上界及 lower boundary。不会修改、保存或计算这些探针为新的正文版本。保留完整原声明作校验，不能通过删去“各学院”把行政提交时间改成学生报名时间；同一引句在该字段的多处出现须全部属于支持的声明，否则缺少位置的引句保持未知。正文时间不能借标题另一句的日期。明确包含报名截止语境的标题仍可支持其自己的引句，普通标题日期不行。

为保持有界，最多检查前8条时间证据、返回4条完整声明，每条不超过500字符；同一引句最多核对8处出现。120字符的旧事实摘录不是完整声明，校验使用原字段中的完整句段。长句、超出范围、证据不足或删去“日前”等造成精度变化均保留 `deadline_evidence_status=unknown` 与 `deadline_quote_scope_unknown`，文字输出为“时间原文待核对”。`extracted_deadline_at` 仍是原规则从整篇提取的结果，**不表示所选引句已获支持**；“日前”的上下边界和 imprecise_deadline 继续返回，支持不意味着精确时刻已核实。

v1、v2、v3 已保存的回放、独立人工记录和版本字段保持原样；v4 另存新报告并绑定查询源码摘要，不回写旧报告、不继承旧输入的人工标签。查询 v4 节点当时没有更新生产政策、事实提取版本、Parser、schema、决策或冻结邮件；其旧回放和版本事实不由本轮 v9 修复改写。

## 两轮探索与收束

当前 v4 沿用 v3 的最多两轮自主模型调用，模型可以调整主题关键词、选择来源或读取正文，也可以提前调用 `answer`。探索次数不等于完整查询的模型调用总数：程序须保留读取正文和形成答案的空间，预算较紧时会提前结束探索。

已有搜索候选但还没有实际交付过正文时，程序保留 `read_notice` 与 `answer` 两个步骤；已经交付正文时保留 `answer`。没有候选时可以直接收束为 `answer` 的空结果。读取/回答所需的逻辑调用、实际请求和工具槽位不足时明确降级，不继续用检索占满额度。每次探索或收束读取还会临时收紧本轮请求上限和时间截止，防止适配器重试占用后续请求/时间；费用上限、慢请求或工具错误仍可能导致降级，预留不是完成保证。

收束阶段仅向模型提供当前目标工具。官方提供方发送指定函数的 `tool_choice` 对象；兼容提供方发送单一工具定义和 `tool_choice="required"`，不会在拒绝后换参数重试。`required` 本身允许一个或多个调用，不能作为“恰好一次”的保证；客户端与 Agent 都校验返回恰好一个且名称正确的工具调用，不符合则以 `model_required_tool_ignored` 降级。接口依据见 [Chat Completions 的 tool_choice 定义](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。这一有限兼容策略不证明任意代理均支持协议，也不代表五题真实模型验收已通过。

强制读取失败会保留有限错误和已有候选，不再次进入探索循环。空 `answer` 只说明本次实际检索范围没有选出有依据的通知；输出同时列出来源、关键词、偏移及匹配数，不能改写为“全校没有机会”。`model_usage.exploration_rounds` 记录进入探索的轮数，`forced_tools` 记录尝试收束的工具名，不能把尝试记录当成工具已成功执行。

## 日期范围

范围含两端，作用于站点发布日期，不能以此判断机会仍开放：

| 表达式 | 程序含义 |
| --- | --- |
| 最近/近/过去一个月 | 回退一个日历月到同日；月底夹到前月有效日期。 |
| 近30天 | 查询当天及此前29天。 |
| 最近一周/近7天 | 查询当天及此前6天。 |
| 本月、上个月、今年、今天 | 按配置 IANA 时区的日历计算，结束不超过相应范围。 |
| YYYY-MM-DD至/到YYYY-MM-DD | 严格日期校验，范围有序。 |

“上周”“近期”“去年”、多个冲突日期等当前不猜测，报告有限 unknown；明显用于“本月截止”等报名期限的表达式不会转成发布日期过滤。有限句式不能覆盖任意中文时间关系，命中范围会始终在报告和文字回答中展示。模型可以换关键词或来源/偏移，不能绕过已解析的发布日期范围。

## 实际限制与费用口径

默认最多 6 轮模型、8 次 HTTP、16 次工具（**包括程序的来源状态和1–2次初始检索**）、90秒、累计60,000个工具结果字符、12篇不同通知、每块6,000字符、每源5条结果。JSON `model_usage` 明示物理请求、工具次数、字符、未知用量及按配置单价计算的消耗。最多读2,000,000字符的单篇正文；图片/附件引用最多各8项，详细信息仍可用已有 `notice-show` 核对。

已知单价时每个物理请求按完整请求JSON的UTF-8字节数＋4,096估算输入token，加配置输出上限预留。有效usage按配置单价结算，缓存优惠不额外假定；缺失usage、错误响应、断线保留预留。供应商报告超出估算时记录较大消耗并停止。`unpriced_proxy`只报告请求与已返回的usage，不将未知费用写成0，不根据模型名字套用OpenAI价格；缺失usage仍保留uncertain_requests，超出token上限也停止。没有价格自动查询、额外token-count请求或隐藏SDK重试；`cost_is_invoice=false`。

只重试连接建立失败、429、500/502/503/504，最多2个物理请求；Retry-After 只接受0–3秒的秒数，长等待/日期值直接降级。读/写/协议失败可能已执行 POST，不自动重试。请求体、响应错误文本与密钥不记日志。请求 identity 编码，拒绝意外压缩而不先解压；响应流上限256KiB，所有路径关闭响应。逻辑调用、HTTP、费用或运行时间用尽就停止。运行时间是单线程合作式预算，I/O 前后及流块间检查；不能强制抢占 DNS、SQLite/文件/CPU 操作，阻塞时仍受 HTTP 阶段超时/SQLite已有超时限制，不承诺恰在90秒硬终止。预算仅约束一次查询，没有跨进程/按天的供应商账户消费账本。

## 错误与回退

`QueryReport` 有 answered、fallback、local，包含真实候选、来源状态、工具轨迹与有限错误；工具轨迹不保存问题参数、整页正文或秘密。模型未配置/凭据缺失、认证拒绝、超时、有限重试失败、无效响应、输出截断、预算耗尽各自有 `model_*` 分类。预期工具错误包括 storage_unavailable、既有 search_index_stale / notice_not_processed、invalid_tool_arguments、unknown_tool、notice_outside_publication_window、citation_not_read / citation_quote_invalid、tool_call_budget / tool_character_budget。

已读取但未完成回答的正文也以候选元数据进入回退；回退片段不被包装成核验过的报名条件。一个来源不可用不清除另一个结果，全部存储不可用时不浪费模型请求。正常无命中不证明没有机会；缺失/过期覆盖、失败复查和待处理积压在文字和 JSON 中明示。程序缺陷不通过宽泛异常捕获伪装成成功。

退出码：0 为正常回答/明确本地检索，1 为模型降级或工具不完整（报告仍输出），2 为配置或问题/时间参数错误。stdout 为答案/报告，stderr 仅结构化 ask_started / ask_finished 与有限诊断；不持久化查询或改变既有日志保留策略。

官方协议依据：[Chat Completions创建接口](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)及[函数调用交接](https://developers.openai.com/api/docs/guides/function-calling)。最初节点完全离线；后续兼容代理有受限真实HTTP验证，尚无OpenAI官方调用、真实费用、完整人工模型质量或生产查询部署验收；首周观察仍按原窗口进行。

后续已补[固定样本回放与独立人工评价入口](query-evaluation.md)：本地默认零模型请求，显式模型开关有整批预算；候选召回与回答证据分别核对，不把工程回归期望填成真人判断。

## 实际知识库的隔离查询

使用[一致备份准备工具](query-snapshots.md)生成带摘要与来源事实的静态副本，`instances.snapshot_manifest`可选，旧查询配置继续兼容。启用时先核对来源、数据库摘要、真实持久时间和副本暂停状态，再向模型提供快照标识；异常来源保留有限错误，其他来源仍可查询。备份时间、派生时间、最近完整列表和当前成功正文的原获取时间分别显示，快照不会自动跟随生产更新。

`deploy/ask_saved.py`是显式日常启动入口，沿用同一CLI/Agent。`--api-key-file`仅在实际模型查询期间将本用户0400/0600普通文件读入配置指定的环境变量，finally恢复；不自动搜索`.env`，不把密钥当命令参数、报告或模型输入。`--local`、`--check`和帮助不读它。也可省略文件并沿用当前环境；普通`signalnest ask`的凭据读取行为不变。

```sh
# 使用准备工具实际生成的配置；检查、本地模式不读取密钥文件。
uv run --locked python deploy/ask_saved.py \
  --config /private/signalnest-snapshot/ask.local.toml --check
uv run --locked python deploy/ask_saved.py \
  --config /private/signalnest-snapshot/ask.local.toml --local --question '助教招聘'

# ask.model.toml 由操作者在同一快照目录中加入明确模型块，不由工具猜测。
uv run --locked python deploy/ask_saved.py \
  --config /private/signalnest-snapshot/ask.model.toml \
  --api-key-file /private/signalnest-credentials/model.key \
  --question '最近一个月有没有本科生可以报名的科研机会？'
```

[五题快照回放](query-snapshot-replay.md)对每题保留完整请求、时间和可用费用预算，整批不足在请求前拒绝；每题子进程有墙钟限制，不把异常终止的用量写成0。报告绑定数据库/配置/画像/快照及查询源码，回答、回退、未完成与人工pending分别保存。它不替换旧九篇样本的历史记录或独立人工评价。[查询发布说明](query-release.md)区分候选源码内容与生产版本，安装候选不升级WSL采集或邮件。

## v3 的真实回放修正边界

真实代理回放曾出现强制阶段仍继续搜索或一次返回多个调用。收束阶段在首条系统说明之外，另在消息末尾追加程序指令，要求恰好一次目标调用，读取只选一篇，回答不再探索。仍由客户端和协调层校验单个同名调用；不执行违约的替代工具，不自动重发。共享关键词也不足以证明主题相关，学生报名、教师征题、成果推荐和选拔应分别判断；弱关联候选可以拒选。提示只能改善模型行为，不能替代语义质量的人工核验。

短引文和至多三篇的选择是模型提示，契约仍有自己的字段/数量硬上限。输出截断、超时或无效引用均保留失败与检索候选；不得把一次成功回放当作所有后续调用必定成功。当前实际配置与各轮结果由独立验证记录绑定，内部试验的同版本变体通过源码摘要区分。

资格展示将“本人资格未核验”和有限规则输出分开。`official_eligibility_verified=false`，不把规则的 eligible/ineligible 当成官方结论；不匹配时返回最多四条 `rule_mismatch_evidence`（字段和原句，不含画像 actual 值），便于核对该句是否针对申请人。旧真实快照与 v8 回放曾发现教师征题历史描述被用于学生资格；本轮 v9 已在事实提取代码修复这两篇真实学生申报，并保留当前教师征题反例、未知条件和原文位置，见[修复验证](validation/notification-applicant-scope-v9.md)。当前源码查询会用 v10 重新做内存预览，但旧回放、决策和冻结邮件不改写，旧已安装查询版本也不会自动更新。WSL 生产仍为 v8，本轮不更新线上政策；未支持的混合主体或历史表达仍须核对，不能将该有限修复解释为完整官方资格核验。

## v4：一次引用纠正

在原查询预算内，仅当 answer 返回 `citation_not_read` 或 `citation_quote_invalid` 时，允许一次新的语义纠正轮。程序提供已读 reference 白名单和有限错误，要求原样选择已读证据并只调用 answer；不重新搜索、不读新通知、不自动改写引句。重复错误、无剩余额度或改用其他工具仍降级。原失败 trace 保留，model_usage.answer_corrections 与警告区分纠正后的回答。它是一个计数的模型步骤，不是新的 HTTP 重试层；客户端仍是唯一传输重试层。旧 v1/v2/v3 报告及人工标签保持原样，不把旧成功转成 v4 验收。

纠正成功仍保留先前工具错误及 `tool_results_incomplete`，因此可能 `status=answered` 而 CLI/回放退出1、`degraded_cases` 仍包含该题。应分别核对最终引用、纠正次数和原失败，不能将其记为无错误验收。
