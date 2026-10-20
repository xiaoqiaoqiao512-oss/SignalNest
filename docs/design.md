# SignalNest 设计与采集持久化

只读查询的模型连接复用同一同步Chat Completions客户端：官方提供方保持固定地址；兼容提供方显式配置HTTPS或字面回环HTTP的`/v1`地址，不跟随重定向，不由模型提供URL。未知价格显式使用`unpriced_proxy`，费用/美元预算为null，请求、时间、输出及引用校验继续生效；回放另有整批请求额度。该配置不参与采集缓存或生产政策，不改变业务schema。[接口与试验](query-agent.md)独立记录。

项目展示名为 SignalNest，Python 包和命令均为 signalnest。项目骨架、纯 Parser、原始文件存储、离线入库/重新解析、有界 HTTP Fetcher、单次采集协调器、处理政策、只读诊断、N0 本地决策、N1 通知事件持久化、N2 邮件计划/冻结、N3 SMTP 适配器与 N4 发送恢复/政策维护已完成。默认测试完全离线；另已完成真实子进程终止恢复与临时目录少量实采，证据见 [恢复验证记录](recovery-validation.md)。外部定时模板提供采集和独立邮件入口，没有 Python 常驻调度器；后台邮件默认关闭，显式启用后自动计划/发送。政策 v5 助教招聘的离线能力、独立 EMS 详情 Parser 与竞赛模板已接通，含邮件的备份专项校验见 [邮件恢复](mail-backup.md)。WSL 受限采集与两封工程验收邮件见[历史实机记录](validation/n2-wsl-20261009.md)；2026-10-10 已完成 0008 升级、真实完整列表扫描及恢复，见[升级验收](validation/n2-wsl-upgrade-20261010.md)，生产通知和持续运行仍未验收。当前源码政策 v10；WSL 仍运行已冻结的生产 v8，本轮不部署、不更新线上政策。未适配列表引用登记已接通，外部正文尚不抓取。

## 模块边界

- `config.py`：读取并校验普通 TOML，不创建存储。
- `cli.py`：配置、显式初始化、本地 JSON 元数据/HTML 导入、按响应 ID 重新解析、单次采集参数及退出码；帮助/配置校验不连接数据库或获取锁。
- `schema.py`：同步 SQLAlchemy Core 表定义，导入只构建内存元数据，不建表。
- `storage.py`：惰性 engine、SQLite 连接设置与 Alembic 初始化/升级。
- `rawstore.py`：原始 bytes 的摘要、原子发布与验证读取；不访问数据库。构造 RawStore 不操作文件。
- `ingestion.py`：响应证据登记、整页通知发现、版本/due/成功状态提交、失败登记及离线流程；提供可组合 Connection 入口。业务入口不依赖 CLI，不获取 HTTP。
- `cache.py`：固定请求 profile、精确 URI 候选、304 绑定校验与可恢复原文查询；没有 HTTP 缓存代理或网络请求。
- `ingestion_state.py`：来源/运行事实、扫描完成声明校验和待办查询；不决定遍历或调度。
- `instance_lock.py`：数据库旁的 POSIX advisory 写入锁；只在显式写入操作获取。
- `errors.py`：有限业务错误与时间/错误代码校验。
- `migrations/`：随 Python 包安装的固定历史迁移，升级与工作目录无关。
- `contracts.py`：不可变 Pydantic 输入/输出契约、规范化 JSON 与内容摘要，无 I/O。
- `fetching.py`：显式的有界同步 HTTP GET、固定 profile、流式上限、唯一重试层、手动跳转、缓存验证与持久冷却；不调用 Parser、不归档、不提交业务成功。构造不发送请求。
- `crawling.py`：`CrawlOptions`、`crawl_once` 与 `CrawlSummary`；持有整个运行的实例锁，复用 Fetcher/原文/缓存/业务接口，验证实际跨页覆盖，独立处理数据库详情待办并收尾。不隐式迁移，不保存分页续扫游标。
- `runtime_policy.py`：到期候选分组/公平批次、上海日历复查档及有限错误延期；显式保守重算旧成功 due 的短事务入口。不取网页、不改 Parser、不引入第二套调度真相。
- `status.py`：已有数据库的一致只读快照与有限诊断；不修正状态、不获取锁、不读取 raw 或发送请求。
- `search.py`：当前成功正文的派生词法索引、同事务增量同步、显式重建与只读搜索/详情；不重解析原文，不判断提醒资格。
- `querying/`：独立查询配置、明确时钟的有限日期规则、固定只读工具、证据校验及有界模型循环；跨 UC/CS 实例定位，不修改现有通知政策或持久状态。
- `model_client.py`：一个同步 OpenAI Chat Completions 适配器、唯一模型重试层和每次物理请求的保守费用预留；不依赖校园工具、数据库、采集或 SMTP，可供后续语义判断显式复用。
- `rollout.py`：只读部署准备与单次观察、明确允许文件的发布指纹/源码快照；本地通过不证明目标机运行、收件或连续在线。
- `parsing.py`：`html_tree`、`parse_list`、`parse_notice` 纯函数，按 UTF-8 解码并显式使用 `html.parser`；不联网、不访问存储，不配置 logger 或运行任务。
- `cs_parsing.py`：计算机学院本科教学的显式纯列表/详情 Parser，独立 cs-undergrad-notices-v2；有限支持实采确认的 vsb_content / vsb_content_501 正文包装，复用数据/错误契约与保守正文清洗。`ems_parsing.py` 继续提供 EMS 离线详情。
- `sources.py`：两个固定来源的绑定，配置明确选择列表/详情 Parser、版本及有限 HTTP 路由。惰性加载纯 Parser，不动态注册、猜来源或回退至另一 Parser。
- `notifications/contracts.py` / `profile.py`：严格不可变的个人画像、事实/证据/事件上下文和决策契约；画像读取只访问显式本地 TOML，不读取采集配置、环境密钥或数据库。
- `notifications/facts.py` / `decision.py`：从现有 NoticeContent 提取有限字面事实，使用明确的 Profile、EventContext 与 now 计算 Action、核对标记、路线及可重放摘要。没有时钟默认值、网络、持久化或邮件能力；不改变 Parser 或内容摘要规则。
- `notifications/state.py` / `service.py`：显式启用和真实日期证据，事务外比较/决策准备，同成功 Connection 提交独立 live 基线、事件、选中决策和必要 planned 意图；不渲染或发送邮件。
- `mail/contracts.py` / `rendering.py` / `planning.py`：纯文本邮件契约与纯渲染、事务外准备/装箱、短事务冻结和精确事件分配；不重新决策、读取原始文件或连接 SMTP。
- `mail/smtp.py`：显式消费 FrozenMessage，一次 TLS SMTP 会话、单一认证机制和单收件人事务；返回有限接受/可重试/未知/永久失败，不查库、重渲染或重试。
- `mail/sending.py`：同实例锁下串行发送；网络前后短事务登记尝试与结果，持久 due/暂停、一次人工许可与未知恢复；不改内容成功状态或重新渲染。
- `mail/background.py`：显式有界计划/发送运行及采集后邮件组合；普通已登记采集失败继续邮件，系统错误停止，默认关闭；独立 timer 推进冻结积压。
- `notifications/maintenance.py`：固定政策/时间/事件集合的有界预览、政策更新及可恢复重评；不会重路由已有邮件资格。
- `eventlog.py`：标准库 JSON 日志，CLI 显式启用；不在导入时配置日志。

HTTPX Client 保留 TLS 校验，显式设置 connect/read/write/pool 超时，限制为单连接，不自动跟随重定向或继承环境代理；Fetcher 校验目标 URL/每次跳转、收紧剩余超时并流式读取。依据 [HTTPX Client 文档](https://www.python-httpx.org/api/) 配置，重试只在 Fetcher 一层执行，协调器不另套重试。Beautiful Soup [显式指定后端](https://www.crummy.com/software/BeautifulSoup/bs4/doc/#specifying-the-parser-to-use)，避免本机装有 lxml 时改变结果；本源 fixture 为 UTF-8，解码失败必须报告错误。

已实现的站点函数签名为 `parse_list(page: PageInput) -> ListPage` 和 `parse_notice(page: PageInput) -> ParsedNotice`。页面输入包含内容字节与最终页面 URL，以最终 URL 解析相对地址；函数只返回结构化数据，不访问网络或数据库。

Fetcher 执行单次有界获取的请求间隔和重试；`crawl_once` 负责列表遍历、详情补抓与复查，复用已有归档/业务入口。没有插件工厂、任务平台或第二套业务成功状态。不在数据库事务内执行文件 I/O、Parser 或等待 HTTP。

UC/CS 使用各自 HTTPS 首页、栏目与旧式详情路由，所有请求和重定向均按本次绑定校验；源内身份不替代实际 URI，JSP 查询顺序/无关参数保留在缓存键。配置、运行、资源处理标记、正文版本、维护重解析和通知比较原文使用相同绑定。旧 UC 配置默认不变，CS 必须显式选择；不同来源建议独立数据库/目录/定时服务。现有通知表只支持每库一个启用通道，采集前发现另一来源已启用即明确拒绝，不先联网或登记半个运行。详情 due、事务、完整扫描证据及邮件启用规则复用，不增加 schema 或多来源调度平台。具体边界及部署见[CS 采集](cs-collection.md)。

有界获取的交接结构、默认预算、HTTP 错误分类与保守缓存策略见 [Fetcher 设计](fetching.md)，单次覆盖/待办/收尾见 [协调器设计](crawling.md)。Fetcher 的 complete 只表示完整非空 200 且 HTML 类型符合要求；Parser 与持久化仍须分别成功。Fetcher 阶段没有改变数据库 schema；N1 的新增表见下文，当前 Parser v4 与内容摘要规则见下文。RequestProfile 为可直接发送的 printable ASCII，保证实际请求头与缓存 profile 完全一致。

## 最小数据契约

- `PageInput`：原始页面 bytes 与 page_url；不包含 Client/Session 等运行依赖。
- `ListEntry` / `ListPage`：源内稳定身份、绝对详情 URL、标题、日期、本站条目 entries、独立 PendingReference 集合 references、顶层下一页 URL 和必需的 PaginationEvidence。row_count / ordered_rows() 保留实际混合行数与顺序；entries 仅含可用本站身份。source_id 由协调层从配置注入；本栏目空列表必须调查，不能默认为正常。entries / next_page_url 的读取方式不变，手工构造 ListPage 须补充分页证据。
- `ParsedNotice` / `NoticeContent`：稳定身份、页面 URL、parser_version，以及标题、日期、正文 HTML/文本、链接/图片/附件引用。HTML 保留结构但未做展示安全处理，不能直接当作可信网页渲染。图片型正文允许 body_text 为空，Parser 必须验证正文有可见文字或有效图片引用。
- `AttachmentReference`：名称、URL、可选源文件 ID（本站建议 owner:wbfileid）和访问状态。默认 not_checked，仅有证据时标为 manual_required；不下载或绕过验证码。
- `RawResponseReference`：URL、获取时间（UTC 秒）、HTTP 状态、白名单头和正文路径/摘要。路径与摘要必须成对出现，路径限定为相对的 raw/<sha256>.bin，304 禁止正文引用；契约不操作磁盘，由 RawStore 验证实际文件。
- `ResponseInput`：已取得原文的明确来源，包含页面类型、source_id、URL、fetched_at、状态码、完整性声明、请求 profile 和白名单响应头；详情必须给出源内身份、列表禁止给出目标。没有获取时间默认值，也不猜文件名。

契约拒绝未知字段、空必要字段、相对/非 HTTP(S)/含凭据 URL；引用集合使用 tuple，避免内容被随意修改。Parser 负责相对 URL 解析、路由身份提取和本站必要字段校验，不能只靠类型校验判断解析成功。

`NoticeContent.canonical_json()` 固定排序 JSON 键，使用 UTF-8 与紧凑序列化，保留正文内引用顺序；`content_sha256()` 对此编码求摘要。哈希包含标题、日期、正文和引用，排除来源身份、获取 URL/时间、Parser 版本及附件访问状态（后者是运行状态，不是公告更新）。数据库唯一键另含 parser_version。该序列化本身不等于 HTML 语义规范化；Parser 在序列化前按下面的固定规则处理 HTML。不同但任意语义等价的 HTML 不保证同一摘要。规则变化更新 Parser 版本。

## 日志

CLI 在参数解析后才配置 signalnest logger，帮助和包导入不触发配置。除配置/初始化事件，还记录 raw_archived、response_recorded、page_processed、processing_failed、fetch_started/retried/finished 和 crawl_started/finished；N1 成功提交后记录 notification_event_registered 或 notification_comparison_unknown；N2 CLI 记录 mail_planned / mail_previewed，不把预览内容写入日志。每次命令有 run_id；字段白名单为 time、level、event 及 source_id/run_id/document_id/response_id/stage/error_code。身份和阶段/错误仅接受有限长度安全字符；原始消息、args、异常栈、任意 extra、URL、网页和配置不进入日志。未知普通日志转成 unstructured_log，不回显内容。服务函数通过 signalnest logger 发出事件，调用者自行显式配置日志。

stdout 为命令结果，stderr 为事件日志和必要的用户错误诊断。日志只输出标准流，无文件 handler、后台线程或全局 root logger 改动。新增 status_read、policy_applied、detail_group_finished 事件；分组事件只增加 attempted/succeeded/failed/remaining_due/unserved/oldest_overdue_seconds 的非负整数白名单。字段白名单不是秘密识别器，调用者仍不得把密钥塞进身份字段。外部日志轮转和一致备份见 [运维文档](operations.md)。

## 已实现的数据模型

| 表 | 用途与关键约束 |
| --- | --- |
| `documents` | 稳定身份、详情 URL、发现时标题、发现/尝试/成功时间、最新处理状态、错误代码、下次到期时间，以及最近成功版本指针。`(source_id, source_document_id)` 唯一。 |
| `raw_responses` | 导入的响应证据：URL、状态、原获取时间、白名单头、原始路径/摘要、page_type、详情 document_id、最近处理时间/错误。列表无目标；详情绑定已发现通知。304 无正文。旧记录类型/目标可空。 |
| `http_resources` | source/完整 URI/profile 唯一、最新完整 200、禁止复用的 304 证据、成功处理原文/规则/时间；没有详情 due。 |
| `source_ingestion_state` | 列表尝试/正文证据/登记分别记录，完整扫描与 bootstrap 完成独立，持久冷却。 |
| `ingestion_runs` | 固定 origin/Parser、起止时间、独立覆盖与运行结果；完整扫描保存响应链、混合行及首页复核证据，旧运行证据为 NULL。遗留 running 可恢复为 interrupted。 |
| `discovered_references` | 未适配列表目标、标题/日期、首末列表原文/观察/run/位置/Parser 证据；source + 版本化完整 URI 候选键唯一。pending_adapter 不代表通知身份或正文成功，不进入详情待办。 |
| `notice_versions` | 通知的规范化内容 JSON、标题、发布日期、解析时间、解析器版本、规范化内容 SHA-256 和原始响应外键。`(document_id, content_sha256, parser_version)` 唯一。 |

`alembic_version` 是迁移工具的版本表，不是业务表。该表列出采集核心状态；另有下文的 N1 通知表和 N2 冻结表，搜索派生表另见后文，没有用户、推荐或反馈表；N4 投递与尝试表另见下文；信息源来自 TOML，暂不建立只有静态配置用途的 source 表。

本站 `source_document_id` 约定为 `栏目ID:文章ID`（如 `1517:128231`），Parser 已统一新旧链接。标题、URL 和内容摘要不替代稳定身份。原始字节摘要与规范化内容摘要分别计算，不能混用。

版本唯一键包含解析器版本：相同原文可用新 Parser 重解析，即使规范化内容恰好相同，也保留新解析器产物。相同 Parser 与相同内容不会产生重复版本。内容从 A 变为 B 后又回到 A 时，可复用 A 并更新 `current_version_id`；不能按最大版本 ID 判断当前正文。版本中的原始响应引用指向首次生成该产物的证据，并非每次复查事件；当前不提供完整处理尝试审计表；首次 discovery_origin/first_discovery_run_id 保持，旧记录 unknown。

`current_version_id` 通过复合外键保证属于该通知；为支持该双向引用，当前元数据的外键排序使用 `use_alter`，但 SQLite 初始迁移显式内联创建所有外键，不依赖不受支持的 ALTER ADD CONSTRAINT。`schema.py` 不用于 `create_all`。

## 状态与事务

- `discovered`：仅发现列表条目，尚未尝试详情处理；成功时间和版本指针均为空。
- `processed`：最近一次处理成功；必须有尝试时间、成功时间和属于该通知的版本指针，无错误代码。
- `failed`：最近一次处理失败；必须有尝试时间和错误代码。首次失败没有成功时间/版本；复查失败保留已有成功时间与版本。

所有操作时间为 UTC Unix 秒整数；发布日期为页面展示的日历日期，不从 HTTP Last-Modified 推导。`next_due_at` 是详情唯一到期来源；离线成功记录若为空，首次进入 `crawl_once` 时事务性设为运行开始时间，明确加入联网复查。成功用本次上海日历年龄选择 24 小时/7 天/30 天档，失败用有限错误延期，due 与业务状态同事务。旧 due 的政策更新必须显式保守重算，不推迟逾期或消除失败退避；不猜旧 discovery_origin。外部 timer 触发仍是有界单次运行。错误字段存简短分类代码，不存网页正文或可能含秘密的完整异常文本；规则与诊断见 [政策说明](runtime-policy.md)。

处理顺序已实现：先归档并独立登记响应证据，事务结束后读取/验证原文并解析，再用一个短事务提交幂等版本、通知成功状态和响应处理摘要；失败单独短事务记录错误，不覆盖成功产物。数据库约束验证引用、必要字段和幂等性；Parser 验证站点内容，RawStore 验证原始路径和摘要。列表全部本站条目、未适配引用、本站日期证据及该响应/资源处理摘要也在同一事务内提交；引用登记失败不能保留半页。

raw_responses.last_attempt_at 是最近一次处理完成/失败的时间，last_error_code 为该尝试的分类：两者为空表示只有证据、尚无已提交处理结果；时间非空且错误为空表示处理成功；错误非空表示失败。不是 HTTP 获取时间、成功游标或完整尝试审计。新的导入独立建立证据行，即使元数据和 bytes 完全相同；重新解析只更新原响应的处理摘要，不改 fetched_at、URL、头或正文引用。原始文件去重与响应行策略独立。

处理时间使用当前真实处理时刻，或调用者显式给出的 UTC 秒。不得早于 fetched_at、该响应或目标通知的最近处理时间，拒绝时不回退状态。首次 discovered_at 为第一次本地登记时间，重复发现保留。每次成功处理显式设置 current_version_id，包括重新解析历史原文；版本 parsed_at 保留首次创建该产物的时间，最新尝试时间另存通知和响应。

按照 [SQLAlchemy SQLite 事务说明](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html) 配置连接；通过 [Alembic 共享连接接口](https://alembic.sqlalchemy.org/en/latest/cookbook.html#sharing-a-connection-across-one-or-more-programmatic-migration-commands) 执行升级。每个应用连接开启 SQLite 外键检查，等待锁上限 5 秒，显式 BEGIN 使 DDL 和版本号更新一起回滚；不添加 WAL、后台队列或连接并发机制。迁移由 CLI 显式运行，导入和采集不迁移。生产升级应先备份，历史迁移冻结后通过新增 revision 演进；使用 SQLite batch migration 时需评估版本表与通知表的双向外键，不能盲目采纳自动生成代码。

## 原始文件与跨介质一致性

初始化建立 `data_dir/raw/`。RawStore 计算 SHA-256，正文固定为 `raw/<sha256>.bin` 相对于 data_dir；相同 bytes 复用文件，规范化 NoticeContent 另存数据库。图片、附件仅引用，不下载。304 不创建文件；真实 200 空 bytes 可归档为空原文证据，但不会通过 PageInput/Parser 成功验证。

发布协议（macOS/Linux POSIX）：在已打开的 raw 目录内以独占方式创建 `.tmp-<随机ID>`，完整写入/flush/fsync，重新读取验证摘要，再用同目录 `link` 原子地建立最终文件名。选择独占硬链接发布，避免 replace/rename 覆盖并发出现的损坏文件；遇到已存在的最终文件必须验证，并重新 fsync 文件/目录，不能覆盖或忽略上次目录同步失败。发布后 fsync 目录，移除本次临时名称并再次 fsync 目录；全部成功返回后才允许数据库引用。常规失败尽力清理本次临时文件；被强制终止时可能留下 `.tmp-*`。

读取也验证严格的路径/摘要格式、普通文件类型和实际 bytes 摘要。通过逐级目录 FD 与 O_NOFOLLOW 打开路径，拒绝路径组件及 blob 的符号链接，用 O_NONBLOCK 避免 FIFO 阻塞；不沿不可信相对路径或网页 URL 访问磁盘。配置加载先 resolve 既有路径别名；写入器在这个已解析的绝对路径上仍拒绝后续符号链接。data_dir 属于本地受信任的单用户进程，不能防御有目录写权限的其他进程恶意替换/移动存储；本阶段不支持 Windows 或多进程业务协调。

对正常进程中断，最终名称只暴露已写完文件；SQLite 未提交的业务事务不会推进成功版本。fsync 文件/目录是操作系统层的持久化请求，不能把原子命名等同于断电保障：文件系统、磁盘缓存和硬件仍影响结果，也未使用 macOS 专门的 F_FULLFSYNC。文件与 SQLite 没有共同事务。文件先成功、响应登记失败时允许出现孤立文件；后续读取再次校验，不信任单独存在的摘要记录。

恢复操作：文件发布前失败时不登记正文引用；响应证据登记成功而解析失败时，保留 response_id，修复规则后 `reparse`；版本/成功提交失败先回滚，再尝试独立失败事务。若失败事务也失败，抛 failure_state_unavailable，调用者不能宣称已登记恢复状态。突发中断可能只留下未处理证据或待处理通知，重新打开库后通过 documents.status 和 raw_responses.last_attempt_at 查询并继续处理。

暂不自动删除孤立文件或临时文件。停止所有写入并备份后，对照 `SELECT DISTINCT body_path FROM raw_responses WHERE body_path IS NOT NULL` 与 raw 目录中 `<64位小写摘要>.bin`；未引用的是孤立候选，`.tmp-*` 是未完成发布候选。先验证/调查再决定人工清理，不以文件修改时间或是否存在版本作为删除依据：解析失败和列表证据也需要保留。

0002 在 raw_responses 上增量 ADD COLUMN，避免启用外键时重建被 notice_versions 引用的表。page_type/document_id 的检查和外键保持目标明确；未迁移的旧记录保持 NULL，不凭 URL 自动赋予身份。最近处理时间/错误用于列表和详情的恢复诊断。0003 继续 ADD COLUMN，并增加缓存/来源/运行事实表，0001/0002 冻结；不补猜旧 profile 或原文完整性。具体字段、恢复行为及 SQLite 外键取舍见 [持久状态设计](ingestion-state.md)。

## 可复用的离线业务入口

| 入口 | 实际行为 |
| --- | --- |
| `open_initialized_engine(database)` | SQLite mode=rw 打开已有最新版库，缺失/旧迁移直接失败，不创建或升级。 |
| `RawStore.archive(bytes)` / `read(path, sha256)` | 完整归档或验证读取，不访问数据库。 |
| `record_response(engine, raw_store, evidence, bytes_or_none)` | 验证已发现的目标身份，发布文件后单独提交响应证据；返回 response_id，不表示解析成功。 |
| `discover_page(engine, source_id, ListPage, discovered_at)` | 每页全部本站条目及未适配引用事务性 upsert；最新列表 URL/标题覆盖，首次发现/处理状态/成功版本/错误不重置。返回条目处理数，不是新增通知数。 |
| `save_notice(engine, response_id, ParsedNotice, processed_at)` | 校验证据/目标/Parser 身份和最终 URL，按版本唯一键写入或复用，并与成功状态一起提交；低层入口要求调用者已验证原文并成功解析。 |
| `process_response(engine, raw_store, response_id, processed_at)` | 复用原获取证据，验证文件、完整解析、写入成功或失败状态；用于 reparse，也供 HTTP 层处理刚登记的响应。 |
| `import_page(engine, raw_store, ResponseInput, bytes_or_none, processed_at)` | 上述归档/登记/处理的组合；无显式绑定的 304 返回 evidence_only。 |

process_response 可注入纯 notice_parser/list_parser 函数以支持规则升级和离线测试，无工厂/插件配置。默认仍使用本站固定 Parser。每次调用执行完整解析，原文相同不会跳过失败或新版本规则。两个入口类型、获取时间与通知身份均显式提供；CLI 不等于又发生一次 HTTP 请求。

列表处理的 ProcessingResult 与 CLI JSON 均增加 pagination，保留原 next_page_url；它是 Parser 通过验证的同一个证据对象。详情与 evidence_only 的 304 没有列表证据，pagination 为 None/null，不表示末页。重新解析归档列表时重新验证并返回证据；完整扫描收尾在 ingestion_runs.coverage_evidence 保存实际响应、分页和全部有序行的证明，单页导入不伪造完整扫描。新增 reference_count / registered_row_count，原 discovered_count 仍仅为本站条目处理数。证据错误发生在条目事务之前，原文与分类错误可保留，不能登记半页或清除既有成功状态。

预期错误统一为 IngestError，提供 code/stage/response_id/document_id；Parser 分类加 `parse_` 前缀，文件错误为 raw_path_invalid/raw_missing/raw_digest_mismatch/raw_not_regular/raw_io_or_unsafe_path。身份不一致为 identity_mismatch；数据库读/写失败为 database_read_failed/database_write_failed；失败登记失败为 failure_state_unavailable，运行收尾无法登记为 run_finalization_unavailable。未发现目标、类型未知、无正文、非 200、来源不符或时间倒退都有明确代码。只存代码，不存整段异常。未经预期的程序缺陷仍抛出原异常，不转换为解析成功或空结果；不能据此假定失败状态已经保存。

业务函数中的程序缺陷直接传播；CLI 最后边界将非预期异常转为非零退出和无异常正文的 unexpected_error 诊断，明确不确认失败登记已完成。

没有完整的处理尝试审计：通知/响应只保留最近状态，同内容版本指向首次生成它的证据。历史原文 reparse 成功会切换当前版本，不自动判断哪个 fetched_at 更新；未来批量规则重算需先定义历史产物与当前指针的更新策略。

## 对调研建议的调整

列表返回 304 不代表没有待补抓详情或到期复查任务。列表检查与详情处理队列必须独立推进。

“遇到已知通知就停止分页”不能作为完整性的保证；置顶、顺序变化和分页移动会使重叠页策略漏报。当前每次扫描明确选择 full/limited，沿实际 next 继续处理已知页；外部 timer 分别触发普通两页轮询和每日完整核对，不据有限运行推断 full 尝试。

普通运行关注增量及待处理任务；首次历史导入建立历史基线，不能默认把所有旧通知当作新提醒。通知策略独立决定历史导入、首次发现和正文更新的路线；离线导入与维护重解析不制造邮件。发送仅消费已冻结且到期的资格。

## 已完成的 WHU Parser

`PARSER_VERSION = "whu-student-notices-v4"`。v2 提供严格分页证据，v3 增加真实竞赛样本正文容器，v4 将有效但未适配的列表目标登记为独立引用；沿用一个站点规则版本，不另建版本框架。列表和既有详情均标为 v4，分页、详情提取、规范化与 NoticeContent 摘要规则均未变化；两份既有详情内容与摘要保持一致。重解析可保留同内容摘要的新版产物；live 同原文升级不会制造更新事件，maintenance 不改变 live 基线或产生邮件。选择器、提取语义与规范化规则固定在代码中；修改影响结果的规则时更新版本。不读取运行中可变配置，也没有原始摘要短路。

列表只读取唯一的 `div.list_txt > ul.am-list`，逐条验证其 li / a / span / i。条目失败时整页抛错，item_index 标记位置；不返回部分成功。空列表异常；不硬编码页长 25。保留原始条目顺序和置前旧日期通知，不去重或按日期过滤。分页须独立通过下面的源级证据验证；next_page_url 仅在确认末页时返回 None，缺失结构或活动链接损坏绝不当作末页。

文章身份限本源 `uc.whu.edu.cn` 的 `/info/1517/<正整数>.htm` 和 `/2022/show.jsp?wbtreeid=1517&wbnewsid=<正整数>`。查询顺序与无关参数不影响身份；必要参数缺失、空值、重复参数（即使重复值相同）、新路由查询身份冲突明确失败。详情的其他栏目/主机/路径仍失败；列表中有效但未适配目标进入 PendingReference， 损坏的已知文章路由不降级。旧路由 urltype 若提供，必须为 news.NewsContentUrl。普通正文外链不受文章身份规则限制。

详情确认外层 `.news_show`、直接标题区 `.title_nei` 及其中唯一 b / i，并在该区域读取唯一 `#vsb_content` 或 `#vsb_content_501`，容器内恰有一个直接 `.v_news_content`。竞赛页面模板由真实 18135 样本验证；两个候选同时出现或层级不符均失败。fixture 内有嵌套的 .news_show，所以不要求正文直接属于最外层。标题需有文字/数字，日期严格来自“时间：YYYY-MM-DD”可见字段并校验日历有效性。缺少、重复或不识别的结构不会用全页标题/其他日期猜测。

附件只取通知区 `.fj > ul` 的每个 li 中唯一 a，记录可见名称及绝对 URL。WHU download.jsp 从唯一 owner 与 wbfileid 提取 `owner:wbfileid`；不强制 wbfileid 长度为 32（真实样本有不同长度），但要求字母/数字。普通附件链接可保留而无源文件标识。access 一律 not_checked，看到链接不证明可下载或需要验证码，不抓取文件。

## 列表分页证据与覆盖边界

依据 [采集设计研究](../research/ingestion-design.md) 与四份现有列表原文：旧/新首页为 1/24，旧第二页为 2/24，新末页为 24/24，条目分别 25/25/25/13。最大可见数字并非通用总页数标准；只在本模板的数字/尾页控制相互支持时接受。不会从静态文件名（末页恰为 1.htm）、条目数量、发布日期或通知是否已知推断页码/终止。

PaginationEvidence 为不可变 Pydantic 契约：

| 字段 | 来源与含义 |
| --- | --- |
| current_page | 唯一无锚 span.p_no_d 的可见正整数。 |
| total_pages | 经尾页控制验证的最大可见数字，当前页不得超过它。 |
| is_last_page | 当前数字等于 total，并同时具备禁用 next/last 证据；不是扫描完成标记。 |
| terminal_evidence | 末页固定为 disabled_next_and_last，非末页为 None。 |
| last_page_url | 非末页活动“尾页”的实际 URL；末页禁用无链接，为 None。 |

next_page_url 保留在 ListPage 顶层，避免两个字段重复储存同一事实。契约拒绝“末页有 next”“非末页无 next”、错误页码范围或与 is_last_page 不一致的证据；不能手工构造缺证据的伪成功结果。

源级验证规则：

1. 列表所在父区域内恰有一个 .page，其直接子节点恰有一个 .p_pages；缺失、重复（包括嵌套重复）或未知层级失败。当前没有可信的无分页单页模板，不自动补成 1/1。
2. span.p_no / span.p_no_d 数字为严格正整数，唯一且按显示顺序递增，从 1 开始；数字间有跳跃时须有实际 p_dot 省略号。当前标记唯一、无锚，其他数字有唯一直接链接，不能将两个数字指向同一 URL 或将活动数字指向当前页。没有总页数常量或每页条目限制。
3. 非末页须 current<total，唯一活动 next/last 的文本分别为“下页”/“尾页”。last URL 须与最高数字的锚一致；current+1 数字可见时，next URL 须与它一致，不能改指另一可见页。next 目标来自实际 href，绝不按文件编号计算。若下个数字未显示，具体跳页最终由协调器验证。
4. 末页须 current=total，唯一 p_next_d / p_last_d 同时无锚且文本匹配；活动/禁用类冲突、禁用节点带锚、缺少任一控制或与数字声明矛盾均失败。13 条仅是该 fixture 的行数，不是证据。
5. 所有分页目标以及输入最终列表 URL 都要求 HTTPS、uc.whu.edu.cn、默认 443、无凭据/query/fragment，且路径精确为 /tzgg/xstz.htm 或 /tzgg/xstz/<正整数>.htm；拒绝 next 自链和反斜杠等不支持形式。先检查原始 href，避免 URL 拼接抹去空查询/片段。普通正文引用仍沿用原 HTTP(S) 契约。
6. 分页区域及祖先、必要数字/控制/省略号及其后代若明确 hidden、aria-hidden=true 或内联 display:none / visibility:hidden，不能作为可见证据，抛 invalid_pagination。不计算外部 CSS，不执行脚本，无法承诺浏览器布局意义上的可见性。

这些证据只说明这一份页面的声明有效。`crawl_once` 从首页开始，沿 next 实际目标发送请求；维护跨页实际请求/最终 URI 集合（包含重定向路径），拒绝重复；要求 current 从 1 连续到 total、总数和活动尾页目标一致，并核对实际末页路径包含初始尾页目标。条目提交成功后才计入页面覆盖；后续发现跨页漂移不会撤销已正确登记的条目，但不能宣布 complete。

full 模式还以 `revalidate=True` 发送首页复核，比较最终 URI 与完整 `ListPage`（有序本站身份及引用目标/href/位置、标题/日期/URL、下一页及分页证据）；失败、变化或预算不足均不推进完整扫描成功时间。全部通过后才构造 `ScanCompletion`，再用短事务提交覆盖。limited 从不声明 complete，详情批次独立于扫描覆盖：列表中断/304 后仍查询 SQLite；服务端冷却或全局网络预算则停止所有请求。首页复核不能证明站点同一时刻的一致快照，也不能发现扫描期间其他页短暂变化。早期临时目录实采只运行 limited；2026-10-10 的目标 WSL 已另行验证真实完整列表覆盖，详见[实机升级](validation/n2-wsl-upgrade-20261010.md)，长期观察仍待后续。单次策略与统计定义见 [协调器设计](crawling.md)。

## 固定规范化规则与范围

1. 只对选出的正文子树操作；标题/日期和附件另行提取。页外导航、footer、统计及 HTTP 元数据不进入正文。正文内移除 script/style/noscript/template、nav/footer/form、已知 fj/title_nei/p_pages 页面组件，以及 nattach 数字 ID、dynclicks ID、clickTimes 统计节点；不按“错误”“登录”“已下载”等词语删除普通通知文字。
2. 移除明确 hidden / aria-hidden=true / 内联 display:none 或 visibility:hidden 的节点。正文根容器若隐藏，视为无意义。注释、脚本只有字符串或空白/空 br/hr 都不能证明有效正文。明确 width=height=1 的图片不计作通知图片；其他有效 HTTP(S) 图片引用可单独构成图片型正文，不假装已检查图片内容。
3. 普通文本节点将空白连续段（含换行、tab、NBSP）折叠为一个空格，清除块元素边界的格式空白和块间空白节点；保留行内元素之间的必要空格，不给 Word 拆分的 span 人工加分隔符，以免将 2026 拆成 202 6。pre/code 子树的 HTML 文本不折叠。正文文本按块/换行分行，表格单元格用 tab 分隔；HTML 保留段落、表格、强调等结构。
4. 正文 a.href / img.src 用 PageInput 的最终 page_url 解析并改写为绝对 HTTP(S) URL；输出链接/图片引用按正文顺序排列，不限制外链域名，不解释全页 base 标签。空链接、片段锚点、mailto/javascript/tel/data 等不作为网页引用；移除它们的 href 而保留锚文本。显式但格式无效的 HTTP(S) 链接抛 invalid_field。图片缺失/无效 src 则失败，不悄悄丢失媒体。
5. 删除动态事件属性和 VSB 图片辅助属性（vurl/orisrc/vsbhref/vheight/vwidth），以及未支持的 srcset（仅保留 src 回退）；保留普通属性、内联样式与数字/日期。Beautiful Soup 确定性序列化属性，再由 NoticeContent 做稳定 JSON 与 SHA-256。不会任意删除 CSS、font/span 或标点，也不合并不同但语义等价的任意树。

清洗后必须有文字/数字或有效非装饰图片引用；附件的存在不能替空正文证明成功。HTML 保留与这里的规范化**不是展示安全清洗**，iframe 或其他未专门处理属性仍可能存在；未来界面必须采用独立的可信展示方案。没有 OCR、附件正文提取、CSS 布局计算，也未验证图片 URL 指向的实际字节。若站点模板/编码/附件参数形式改变，应失败并用新 fixture 更新规则及版本。

## 错误分类

`ParseError.code` 是 ParseErrorCode 字符串枚举，`field` 为有限字段名，`item_index` 为可选的零基条目位置。消息不包含输入值、URL、整页 HTML 或任意异常文本；仅捕获预期的 URL/日期/结构校验错误，程序缺陷仍抛出原异常。

| code | 含义 |
| --- | --- |
| invalid_utf8 | 不可按 UTF-8 严格解码 |
| empty_page | 非空字节却只有空白；空字节由 PageInput 拒绝 |
| missing_structure | 必要结构缺失或存在多个候选，不能可靠选择 |
| invalid_field | 标题、日期、URL、附件参数等必要字段无效 |
| unsupported_identity | 非本源/本栏目/支持路由，或身份形式不受支持 |
| ambiguous_identity | 身份参数重复或与路径身份冲突（包括附件标识） |
| empty_list | 本来源列表容器没有条目 |
| meaningless_body | 清洗后没有有意义文字或有效图片引用 |
| invalid_pagination | 数字声明、链接对应关系或活动/禁用状态相互矛盾，或必要证据明确隐藏 |

分页缺失/重复节点为 missing_structure，非法整数或来源 URL 为 invalid_field；field 可为 pagination/current_page/page_numbers/next_page_url/last_page_url/page_url。错误对象不携带整页 HTML。离线业务层继续保存 parse_<code>，保持原证据、短事务与失败恢复语义。

Parser 不检查 HTTP 200/403/304，PageInput 未扩展状态码。200 错误页因缺少本站结构/字段失败，而非因全页含某个关键词失败。

## 调研启示与后续持久化契约

[可靠性报告](../research/changedetection-io-reliability.md) 和 [SignalNest 决策报告](../research/changedetection-io-signalnest-decisions.md) 提醒必须分开三个概念：原始字节摘要只标识取得的采集证据；规范化内容摘要标识解析产物；parser_version 标识规则。原文相同不证明上次解析成功。报告中关于上游崩溃窗口/raw-checksum shortcut 的后果是源码推断，尚未运行验证；本实现借鉴风险边界，不宣称复现上游缺陷，也未复制上游代码。

离线业务层与单次 HTTP 协调层均遵守：

- 原始响应入档仅表示取得采集证据，不推进成功游标或成功状态。
- Parser 返回表示解析与字段验证成功；协调层仍须将版本及成功状态提交后才算持久化成功。
- 解析失败记录分类并保留最近成功版本；同一原文以后仍可重试，规则升级后仍可重新解析。
- 如将来跳过重复处理，必须有对应原文和 parser_version 的已成功解析结果，不能只看原始摘要。内容摘要不含 parser_version；现有数据库唯一键另含版本，所以新版规则产物即使摘要相同也可保留。

现有离线原文、短事务及最小传输/运行事实已足够连接协调器，无需新增 schema。没有任务队列或分页续扫游标；N1 的 planned 意图与 N2 的冻结邮件独立于采集状态。研究建议精简为三张运行事实表，省去双份详情 due、持久 run 计数/最后页游标、通用任务/租约、列表/扫描调度字段；计数在持锁的本次运行中计算。完整扫描状态接口保留旧声明兼容；新生产 ScanCompletion 含响应和成员记录，提交时核验每行登记及绑定，并与来源成功时间同事务保存。实际链验证仍由 `crawl_once` 在调用前完成。0008 仅为引用恢复/完整覆盖增加必要表和证据列，详见[引用登记](list-references.md)。

## 已完成的恢复验证与后续边界

新增 9 项跨运行集成回归连接真实归档、缓存、Parser 与 SQLite；5 项真实 SIGKILL 实验在原文登记后、业务提交前、覆盖提交前和运行收尾前终止生产协调器，再由全新进程重建待办，验证成功版本、组合事务与覆盖事实。HTTP 在这些测试中仍是模拟的，不能称作断电验证。

随后临时目录低频实采三轮，共 9 次真实 GET。首页 304 的 Vary 缩减触发保守完整回退，第二轮请求预算中断，第三轮沿用原库继续处理。最终 25 个身份、4 个版本、21 条待办，未登记完整扫描成功；观察已固化为离线回归。实验边界、持久结果和复现方式见 [恢复验证记录](recovery-validation.md)。

采集三交付完成后已增加 [处理政策与诊断](runtime-policy.md) 及 [一个定时部署模板](operations.md)。没有新增 schema、持久配额/优先级、扫描模式字段或后台 Python 调度器；后续在目标机观察；Email 发送与恢复已另由 N2–N4 完成，本轮接通外部邮件定时入口。没有安装定时器或发送真实邮件。

## N0 决策与 N1 通知持久化

N0 的调用链为 `NoticeContent → extract_facts → decide(Profile, Facts, EventContext, now=...)`。采集处理成功不证明报名资格；画像缺值、未支持的 OR/条件、时间/媒体缺口保留未知。相关性、资格、时间分别判断，`PUSH_NOW / DIGEST / STORE_ONLY / IGNORE` 与 `needs_review` 分开。纯路线合成也在 N0：digest_only、首启默认汇总及可信短截止例外进入显式输入和决策证据，后续计划器不能重新读取当前 Profile 来改已选路线。

政策 v2 将事实提取/规则/引擎升至 v2，路线仍 v1。不同主动主题不能被另一仅保存主题压住；申报主体的 role 与受益学生的学段分开，缺省 role 保持未知。支持同句完整年份继承、24:00 及有界“日前”精度；按下界及时提醒、按上界判断关闭并保留待核对。不用发布日期补年份，不将导师/学院期限当学生期限。新可空字段未提供时不写入标准快照，保证 v1 已存摘要兼容；新 live 决策必须先显式更新政策，既有冻结邮件不重评，ID-only 恢复复用原固定决策。

政策 v3 将事实提取/规则/引擎升至 v3，路线仍 v1，保留上述 v2 行为。主题证据增加 subject/opportunity/incidental；正文顺带提及不成为兴趣，紧迫或高价值命中须有对应机会证据。辅修与具体报名/申请行动直接绑定，局部排除缴费/退出、导航、否定、历史和结束声明；不因整篇出现费用说明而否决真正报名，也不借用另一项报名。标题主题仍可表示普通信息，显式字面关注保持宽泛；已获邮件资格的条件更新不被另一保存主题压制。有限语法不是通用语义理解。旧 context 未提供时省略序列化，v1/v2 已保存事实与决策摘要不变。原文、Parser v3、正文摘要和 schema 均未改；真实生产重放与修复前后结果见 [评估记录](validation/notification-production.md)。

政策 v4 补唯一标题主题与正文当前行动的跨字段关联，支持真实登录系统完成申请的指令；菜单、记录、费用、退课、历史和另一个具名机会不能借用此规则。TopicMatch.supporting_evidence 保留独立正文位置，空值省略以保持 v3 已存摘要。无法可靠绑定时 context=uncertain 并保存 topic_action_link_unknown，仅待核对的兴趣保留 STORE_ONLY/relevance unknown；已登记资格的更新仍按跟踪规则提示核对，不因不确定关联升级紧急。原 13 个真实案例与 2 个新合成案例分开评估，人工原标签、适配和工程期望逐例映射；历史快照不覆盖。新迭代编号见[迭代记录](iteration-20261008.md)，与旧邮件 N0–N4 独立。

政策 v6 将已绑定机会的正文当前报名指令作为 opening_confirmed 的证据：具体行动须通过原入场排除并位于 opportunity 的正文证据或标题 supporting_evidence 中。保留指令的原文位置到 time_evidence，不补造 opens_at；可信未来开场与未知截止的判断不变。仅标题、歧义主题、菜单/历史/缴费和另一项行动不能取得此确认。事实/规则/引擎升 v6，路线、Parser、正文摘要和 schema 不变；真实 v5 快照和 seal 保留，旧决策及冻结邮件不自动改写。[当时修复记录](validation/notification-rule-fix-20261010.md)保留 R01/R02 工程边界来源；随后原句漏报已单独冻结并补完。

政策 v8 补两个有限语法：`即日起` 后允许 `接受` 再接入场行动；`请/须/需/应登录(学校)系统` 后立即接报名/申请/申报/选课，无须额外“完成”。仍须绑定具体机会、通过相同菜单/记录/历史/办理对象排除；不放宽任意登录文本或仅含报名词语的说明。原样行动表达的 O01/O02 在 v7 实际为 DIGEST/IGNORE，v8 均为 PUSH_NOW；内容、Profile、事件和时钟不变。直接登录指令只确认当前申请，不补造 opens_at；未知截止与资格继续未知。事实/规则/决策引擎升 v8，路线 v1、三个 Parser、正文摘要、schema 0008 不变，实际 v7 评估与全量 seal 保留。实例必须显式更新政策，不撤回旧资格或重写冻结邮件，见[原句修复验证](validation/notification-original-misses-20261010.md)。

政策 v9 按每条声明区分当前申请主体、已完成教师征题和未来受益学生，不把历史“共征集了”或导师指导描述当成学生的教师资格门槛；明确项目负责人本科要求单独生成约束。当前教师征题仍保持 faculty role，受益本科生不改写本次主体；无法区分的当前教师/学生冲突保留 unknown。毕业年级、人数、项目互斥和导师资质保留原文待核对，月份阶段仍无精确截止。两份完整真实学生 NoticeContent 与既有教师 fixture 构成回归，不改 Parser/规范化/schema/正文摘要。事实提取、规则、决策引擎分别升 v9，routing 仍 v1，v8 的语法、主题优先级及负例保持。新版本尚未发布到 WSL；旧决定和冻结邮件不自动重算，已有实例需显式政策预览/更新，受限重评另行选择事件与时钟，见[本轮验证](validation/notification-applicant-scope-v9.md)与[维护入口](notification-maintenance.md)。

资格只核对少量明确对象条件，不用“研究生”等全页裸词推断对象；发布日期不代替开放或截止。短截止相关机会可在资格未知时提示核对，理由明确不构成资格确认。图片/附件只保留未解析引用；不从 alt、文件名补造事实。具体规范、版本、证据范围和真实样本限制见 [规则说明](notifications.md)。

Profile、事实、政策和决策输入摘要用于追溯 N0 规则输入，不替代原字节/规范内容摘要，也不是内容事件或发送去重身份。N0 纯函数不写这些快照；预览 kind/previous_route 只是用户声明。N1 根据已验证的 live 运行和响应、启用身份、真实日期与独立基线构造生产 context，并保存政策、事实短证据及完整决策；采集 due 仍只来自 documents.next_due_at。

[N1 实现说明](notification-state.md)定义已可调用的 prepare/commit 和列表证据接口。0004 只新增八张通知表，升级保留旧数据且默认未启用。生产协调器显式传 live；本地导入默认 offline，CLI reparse 为 maintenance，不改变 live 基线。成功事务一起提交版本/状态/due/资源标记、基线、事件、决策和必要 planned 意图；Parser、文件读取及 N0 计算均在事务外。任何登记失败回滚，有限系统性错误终止采集。

重复 200/304 不重复事件，A→B→A 以序号记录两次更新；相同字节的 Parser 升级静默建立新口径，不同原文先用当前规则重解析旧实际正文。比较不能证明时静默推进新成功基线并保留最近未知诊断，可能漏掉同时发生的真实更新。日期证据不从发现时间推导；首启选择和冲突保留。N2 已完成本地冻结计划，N1 planned 仍是立即投递意图；N3 SMTP 适配器已实现，N4 已另行完成发送登记与重评恢复。


### 新迭代 N1：助教招聘

这是 2026-10-08 的新编号，不替换以上旧 N1 持久化设计。政策 v5 正式增加 teaching_assistant，要求招聘/招募/选聘与当前申请证据，不把介绍、工资/津贴、考核、历史或尚未开放的招聘当机会。标题与泛指正文申请的关联不越过其他具名机会；缺少关联保留 uncertain 和原文核对证据。真实 EMS “本科教学课程”是受益课程，“原则上从本院全日制研究生”是未完全核准且带软限定的申请对象；教师签署和附件条件继续 unknown。样本初次申请表的完整日期/周几/下午16点前被有限识别，公布和考核不抢占截止。

`ems_parsing.parse_ems_notice` 版本 ems-notices-v1，仅支持已观察的1588详情模板；复用正文清洗、引用和摘要规则，未改变本科生院 Parser v3。CLI decision-preview 可显式 --parser ems-notices；库 import_page/process_response 已有 notice_parser 注入点可复用。没有 EMS 列表、当期匿名正文或生产来源适配；原 Fetcher 白名单不变。离线入库保留真实获取时间，不创建 live 事件或邮件任务；显式历史回放另走纯决策/邮件渲染，不制造生产证据。

事实/规则/引擎升 v5，路线仍 v1，不新增 schema 或依赖。实际 v4 seal 和评估快照在升级前保留，旧决定和冻结字节不自动变更；实例需显式政策更新。实现、恢复边界与后续来源任务见[助教招聘](teaching-assistant.md)。

## N2 本地邮件计划与冻结

[邮件计划说明](mail-planning.md)描述新增 `0005_mail_planning` 及三个小表：mail_messages 保存完整冻结 bytes/头/地址/档次与渲染版本，mail_message_members 保存顺序和完整决策快照并以 event_id 唯一，mail_plan_errors 保留有限单项阻断。保留 N1 的八张表和所有历史迁移，避免重建现有循环外键。升级不自动计划或启用。

准备读取已选路线、事件版本和实际正文响应 URL，不用当前 Profile、通知 current_version_id 或日期重新决策；Digest 资格使用当时 context.next_digest_at，旧积压归入最近已到上海档次。普通/待核对组分开，立即与汇总共享唯一成员分配。默认每轮 5 封、每封 50 事件及完整编码 128 KiB；按真实 RFC 5322 大小分片，单项阻断保留待办，后续可继续。

只读准备关闭事务后纯渲染，提交重验不可变 token 与档次/分片并原子登记整组邮件、成员和阻断。文件、网络、Parser、N0 不在写事务内。已冻结邮件的 bytes、地址、Message-ID、时间和成员不重新渲染，load_frozen_mail 校验保存摘要与头字段，为 N3 提供明确交接；不承诺外部 exactly-once。计划 CLI 持统一写锁，预览只读且明确输出地址/正文，日志不输出这些值。

N2 的 mail_messages.state 继续仅标记冻结产物为 pending；N4 的 mail_delivery 是唯一发送状态来源。N3 显式库接口独立返回观察，N4 负责持久化。对 N2 的故障注入/重新打开数据库测试不称为真实进程终止或断电验证。


## N3 SMTP 有限结果接口

[SMTP 说明](smtp.md)固定 SendResult：accepted/retryable/uncertain/permanent、有限阶段/错误码、实际回复码及通道/邮件范围和清理诊断。send_frozen 只用 N2 bytes 和地址；默认 verified TLS，凭据在实际调用时读取一次，单机制认证，无网络重试。最终 250 后清理错误不反转接受；进入 DATA 后未取得可信最终回复为 uncertain，不复制完整 smtplib 实现或根据异常名字猜测未发送。

可选 SmtpSettings 只包含传输配置。适配器不更新投递或通知成功状态；N4 网络前提交尝试、网络后提交结果，本地登记失败时停止继续发送。适配器不证明收件箱送达、外部去重或崩溃恢复。本地 socketpair/真实 TLS 握手验证和异常注入不称为公网投递或断电实验。


## N4 发送事务与政策维护

[发送说明](mail-sending.md)固定新的 0006 迁移：mail_delivery / mail_attempts / notification_operations，加原通道 pause_reason/paused_at。旧冻结 bytes、成员及通知不重建；旧邮件得到 pending 投递状态，升级本身不发送。N2 新计划将 pending 投递状态同邮件/成员一起提交，SMTP 状态不重复写入旧冻结标签或 N1 意图。

尝试序号终身递增；一次人工许可在网络前消费，失败即再次 blocked。未知最小冷却保存于原尝试，即使额度耗尽或配置变化，人工重试也不能绕过它。恢复结束旧 sending 并一次持久化未知 due，已提交 accepted 不再自动发送。状态诊断计数全部积压、有限展示 100 封，历史未知次数保留；不读取正文或密钥。

SMTP 接受与本地提交无法原子完成。真实 SIGKILL 测试通过父进程模拟服务的独立接受日志证明未知窗口可能重复；同 Message-ID 不能证明外部去重。结果写入失败终止整个 drain，内容处理成功独立保留。详细平台与测试边界见发送说明。

[政策维护](notification-maintenance.md)只更新不可变政策并显式重评最多 100 个当前 live 事件；不自动历史补发。固定原集合/时钟/证据，每项决策/路线/意图/操作进度原子提交。Digest 资格已登记即锁定，即使 outbox 为空。旧政策可审计，当前代码不会假装执行不兼容的旧规则。

## 后台邮件与备份一致性

定时完整扫描的采集入口允许有限锁等待，默认180秒，使用原采集单调截止时间扣除等待与启动开销。等待只重复取得OS互斥锁，不重复执行受保护业务；等待期间无运行登记、数据库连接、Parser、文件归档或网络请求。regular、手动采集和邮件维持立即拒绝；等待或预算耗尽明确失败，系统错误不转成重试。无需新schema或持久重试队列。

`run_mail_pass` 同实例锁内先计划再发送，`run_scheduled_cycle` 先采集再邮件；两阶段依次持锁，争锁明确拒绝。普通采集/渲染问题必须已有可靠诊断才可继续，系统错误立即停止。后台配置默认关闭，配置校验不读密钥或开库；发送暂停、due、未知恢复继续以原 N4 表为唯一事实，不新增调度表或第二套重试层。

外部 timer 为上海半小时采集、每日 full 和五分钟邮件，服务注入可选环境凭据文件。采集 20 分钟、邮件 10 分钟服务上限提供部署终止边界；接受后中断的不确定窗口仍存在。详见 [后台邮件](background-mail.md) 和 [目标部署](operations.md)。

一致备份沿用停写、统一锁、SQLite backup API 与 raw 复制。只读校验器增加冻结 MIME/头/摘要、精确成员与历史政策绑定、唯一投递状态、逐次尝试与暂停检查，不运行现行政策或重渲染。恢复副本先暂停、禁用后台、无凭据，避免原副本同时发送；sending 一次恢复为 uncertain，accepted 不重发。校验不能恢复快照之后的外部接受事实，也不是恶意改写认证或断电证明。

## 新迭代 N2 / N3：部署证据与历史搜索

新编号独立于上述邮件 N0–N4。新 N2 的本地部署检查、源码实际字节快照与观察入口已接通，
最初远端只读核验为原生 Windows、未见 WSL 发行版；这是历史准备状态。用户后来安装 WSL，当前已在 Linux 升级 0008、完成真实 full 和隔离恢复，生产邮件与七天运行仍未验收，见[最新实机记录](validation/n2-wsl-upgrade-20261010.md)。
`rollout-check` / `observe` 只读采集/邮件诊断，不获取锁、创建库、恢复 sending 或验证 SMTP。
准备、凭据存在性与真实外部验收分别表达；实际机器服务、邮箱收件和七天记录须独立取得。
发布只归档明确允许的源码/锁文件/模板，逐文件摘要覆盖未提交代码，不夹带个人配置。
新快照拒绝覆盖、路径逃逸和符号链接；两文件发布、fsync 及压缩确定性的保障范围见[上线说明](rollout.md)。

新 N3 新增迁移 0007：`search_documents` 保存当前成功正文的规范化文本及准确版本指针，
`search_fts` 是 SQLite FTS5 trigram 派生索引，触发器与普通行一起事务提交。
详情成功事务末尾同步索引，包括离线导入和维护 reparse；索引失败使版本、成功状态、资源标记、
live 通知事件/决策/意图整组回滚。业务失败后旧成功版本继续可查，A→B→A 正确跟随 current_version_id。
旧库升级不自动建索引内容，用 `search-rebuild` 持统一写锁从规范化 JSON 全事务重建，无 HTTP/Parser/原文读取。

查询与单条详情使用 mode=ro/query_only，不修复、不取写锁。索引缺失/陈旧明确失败。
只检索当前成功标题/正文，含站点日期范围与来源过滤、片段及原始 URL；不搜索附件文件或所有历史版本。
有限固定别名与当前查询版本在结果明示，不修改 NoticeContent 摘要或通知政策。
FTS 三字候选与短词 instr 回退、排序、重建及备份限制见[历史搜索](search.md)；
9 篇真实语料和 15 查询的生产 Recall@K 见[工程评估](search-evaluation.md)，不称人工确认的 gold。

## 计算机学院离线验收与政策 v7

[真实样本离线适配](cs-undergrad.md)复用 ListPage/PaginationEvidence、PendingReference、NoticeContent 与归档/短事务入口。三个实际列表为 1/4、2/4、4/4，共43个已观察身份；第3页未取得，离线导入不登记完整覆盖。新的 Parser 不放宽本科生院 URL、Fetcher 或默认 CLI 导入规则。

真实64481保留2026学年、可见发布时间与2025落款冲突、本科/研究生申请分支、85分/年级/教师审核和纯文本外部申请渠道。政策v7只补有证据的有限词形、对象分支和风险；不硬排本科、不宣称硕士符合，不补截止年份，不以考核时间制造紧迫提醒。假设本科画像得到待核对Digest。事实/规则/引擎升v7，routing仍v1；原Parser版本、正文摘要与schema不变，历史决策/资格/冻结邮件不改写。已有实例须显式更新政策。自动采集是下一节点，仍须接来源隔离、目标校验、缓存和完整扫描证明。

## 首周证据工具与有限 Parser 恢复

原始运行、状态和归档仍是恢复真相，不新增业务表。只读[观察汇总](observation-summary.md)从实际 JSONL 提取净积压、26 小时覆盖过期与缺失样本；窗口长度不证明七天在线或实际收件。独立[人工评价](human-review.md)冻结 Profile/上下文/时钟/原文或检索语料的输入摘要，人工标签不复用工程期望，不改生产决定和冻结邮件。

UC v5 / CS v3 只补实机已归档的正文包装，以及 UC 正文内部重复 ID 的文章层级区分；[16 个脱敏结构样本与恢复边界](parser-failure-recovery.md)另记。分页/身份/规范化和政策 v8 保持；单个来源 Parser 版本同时标记列表与详情。旧版本行保留，同一原文重新解析不伪造 fetched_at，维护操作不制造 live 更新事件；成功后最近错误字段会清除，此前失败以原备份/诊断记录追溯。未支持跳转不放宽目标许可。

## 只读校园查询节点

`ask` 通过[独立查询配置](query-agent.md)统一 UC/CS 的既有搜索、详情、画像与来源状态。使用实例＋本地通知 ID 定位；读到的版本、内容摘要与 Parser/事实版本随回答返回。固定工具没有写库、抓取、发送、Shell、文件路径或任意网址接口；只读引擎与短快照复用现有实现，原始归档、业务状态、派生索引、政策和冻结邮件都不修改。

OpenAI Chat Completions 只有一个同步适配器，处理有限状态、唯一重试层、输出/运行/请求/费用预算；不引入 SDK 或框架。模型选择关键词与分块读取，最终提交已读取原文的连续引句，链接和资格/时间规则事实由程序填入。规则事实只是内存预览，不产生 N1 内容事件或决策写入。无证据的条件与截止保持未知；明确时钟只决定有限发布日期范围和预览，不伪造历史数据库状态。

范围、原文分块和引用均在程序侧验证，源不可用或模型失败保留实际取得的候选和有限诊断。精确引句不证明语义选择正确；真实模型效果与人工评价留后续验证，没有将工程期望变成 gold。模型配置默认为空；实际调用才读取 API 环境变量并向 OpenAI 发送有限资料，`--local` 始终离线。此节点不升级正在观察的 WSL 冻结版本，不重置首周窗口。

[查询评价](query-evaluation.md)使用独立临时实例和完整冻结语料，显式绑定问题、画像、时钟、版本、模型设置及预算。整批预算与单次 `ask` 预算组合，不增加数据库状态。人工相关集与回答依据分开，新增查询标签 v2，旧规则/搜索 v1 继续兼容；未经真人确认的统计保持 pending/null。工程候选检查、模拟模型协议、真实模型效果分别记录。

查询 `campus-query-v2` 单独校验时间引句的用途：原 NoticeFacts.time_evidence 混合开始、截止与行政步骤，不能因整篇有 deadline_at 就支持任意片段。该查询节点当时复用 v9 事实提取及其时间规则；当前源码已另行升级到 v10，保留同样的证据校验边界，对完整时间声明和所选原句分别做有限内存重放，排除其他时间声明；二者须给出同一上界及精度下界，多处同文有歧义仍保持未知。新增工具字段 deadline_evidence 提供有限完整声明，不保存探针内容或更改摘要、通知政策与数据库。旧 v1 评价保留，新回放与源码摘要另存；不能把规则支持解释为官方资格或任意中文语义核验。

## document 级开放机会回顾（本地 v10）

首次七天发布筛选仅是启用时防历史群发策略，不代表全部仍开放机会。新增有界、默认只读的回顾服务，按来源稳定身份/current版本/live响应证据/Profile/显式时钟判断，分为open、unknown、excluded、already_notified、unavailable。相关机会个人资格unknown允许附核对提示；当前报名/期限unknown不自动登记。

0009新增一张冻结回顾操作表，并扩展事件CHECK：`opportunity_review`独占每安装/通知的`event_seq=0`；new/update/activation_recent继续为正序号。唯一键使不同操作名也不重复授资格，live observation的seq0仍表示没有live内容事件，既有比较基线不更改。操作先独立冻结、再在一个短事务内验证全部输入并提交整组事件/决策/必要立即意图/结果；故障回滚整组，可复用原操作。SQL事务内不做模型、Parser或文件I/O。

迁移重建events的两CHECK，但保留已有循环FK/ID。SQLite临时defer FK、重建后显式全库foreign_key_check，再清除废弃父表的deferred计数；FK保持ON，全部DDL在显式事务。非空循环图与冻结邮件保持测试覆盖；这不是生产升级或断电耐久性证明。

可选语义复核复用Chat Completions客户端和共享查询预算，输出申请者/受益者/分组时间原表达/未知及精确引文。仅可在回顾预览显式启用；默认离线。引文校验证明文字存在，不证明模型语义正确。邮件发送、状态与去重继续由程序管理，没有新增Agent框架。详见[接口与用法](open-opportunity-review.md)。
