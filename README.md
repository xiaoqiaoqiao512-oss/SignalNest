# SignalNest

单用户、自托管、长期运行的个人校园信息助手，采用 Python 模块化单体。首个信息源为武汉大学本科生院“学生通知”。**采集、离线恢复、政策 v10、邮件计划/冻结、SMTP 发送恢复、后台邮件入口及基础历史搜索已实现**。使用外部定时模板触发有界单次运行，没有 Python 常驻调度器；邮件默认关闭，显式启用后可自动计划并发送。WSL/Linux 已完成 0008 升级、真实完整列表扫描、首页复核、持久成功时间和隔离恢复，见[升级验收](docs/validation/n2-wsl-upgrade-20261010.md)。此前受限实采、两封工程测试邮件的实际收件及外链阻断记录保持在[历史实机记录](docs/validation/n2-wsl-20261009.md)。生产通知及一周运行仍未验收；完整列表覆盖不表示正文积压已处理。

当前源码政策 v10 保留 v9 的申请主体修复，并修正并列报名对象、表格/带秒期限和分组截止；新增较早发布但仍开放的机会回顾，以及显式可选的模型语义复核。见[机会回顾用法](docs/open-opportunity-review.md)。最近生产核验仍是 v8/schema0008；**本轮未部署 v10、执行生产迁移或补发邮件**。既有实例需备份、显式迁移和政策更新，旧决策与冻结邮件保持不变。

2026-10-08 开始的新迭代 **N0 规则对齐 → N1 助教招聘 → N2 部署运行验收 → N3 基础搜索** 单列于[迭代记录](docs/iteration-20261008.md)。下文原邮件模块 N0–N4 编号继续保留。新 N1 已提供[助教招聘的离线判断与预览](docs/teaching-assistant.md)，含真实历史招聘正例和独立 EMS 详情 Parser；计算机学院本科教学的脱敏样本离线验收见[离线适配记录](docs/cs-undergrad.md)，后续已接入显式 CS 来源的有界采集、缓存、入库及独立定时模板，见[CS 采集说明](docs/cs-collection.md)。抓到历史助教正文不表示当期仍可申请，生产通知及连续运行验收仍独立进行。

## 安装与检查

支持 Python **3.12.x**。安装稳定版 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，在仓库根目录运行：

```sh
uv sync --locked
uv run --locked signalnest --help
uv run --locked signalnest config-check --config config.example.toml
uv run --locked pytest
uv run --locked ruff check src tests
uv run --locked ruff format --check src tests
# 另检查部署辅助程序。
uv run --locked ruff check deploy
uv run --locked ruff format --check deploy
```

`uv.lock` 锁定稳定依赖，禁止预发布版本。包名与 CLI 均为 `signalnest`，也可使用 `uv run --locked python -m signalnest`。技术栈为同步 HTTPX、Beautiful Soup（显式 `html.parser`）、Pydantic、同步 SQLAlchemy Core、Alembic、SQLite、argparse 和标准库 logging。

## 配置

```sh
cp config.example.toml signalnest.toml
uv run --locked signalnest config-check --config signalnest.toml
```

普通配置为 TOML，包含数据目录、数据库文件路径、信息源、HTTP 超时、请求间隔与 User-Agent。请求间隔是相邻物理请求的间隔，不是轮询周期；一次采集复用同一个 Fetcher，没有后台采集循环。可选 SMTP 配置只存服务、TLS、超时和凭据环境变量名；配置校验不读取凭据。只读 `ask` 使用独立查询 TOML；模型密钥在实际查询时从指定环境变量读取，采集和邮件不使用模型。

**data_dir 和 database 都相对于配置文件所在目录解析**，database 不相对于 data_dir。配置文件路径本身由调用者定位；绝对路径保持绝对路径，`~` 展开为主目录，符号链接配置以目标文件所在目录为准。

`config-check` 校验 TOML、未知字段、所选来源的 HTTPS 首页、正数且有限的超时/请求间隔；不测试网络可达性或目录可写性，不创建目录/数据库。导入模块及查看帮助同样不联网、不打开数据库、不启动任务。

`[source].parser` 明确选择 `whu-student-notices`（旧 UC 配置省略时的默认值）或 `cs-undergrad-notices`。它同时绑定列表/详情 Parser、解析版本及请求/重定向许可，不从主机、身份或解析失败猜选来源。CS 示例为 `config.cs.example.toml`；使用独立配置、数据目录、数据库、启用基线和实例锁，勿覆盖既有 UC 配置。`crawl-once`、`scheduled-run`、`import-page`、`reparse` 均使用这一绑定。EMS 仍仅离线预览。

当前 UC Parser v5 / CS v3，补充实际归档中已确认的正文包装，并限定文章层级以免正文内部重复 ID 误判。旧解析产物保留；规范化、身份和分页规则保持；本次政策 v10 不改 Parser。已归档失败正文可离线 `reparse`，不伪造新获取或更新邮件。专用 `signalnest-cs@.service` / `signalnest-cs-regular.timer` 每小时 :15 做小额度受限采集，读取可选 SMTP EnvironmentFile；邮件及通知仍须独立显式启用，见[CS 采集说明](docs/cs-collection.md)。

`[runtime]` 配置详情配额、复查日期档与有限错误延期；`[runtime.regular]` / `[runtime.full]` 为外部触发的单次任务预算。旧配置可以省略 runtime，使用新默认政策。定时频率由外部 timer 决定，不能用 HTTP 请求间隔设置。示例字段与行为见 [处理政策](docs/runtime-policy.md)。

## 显式初始化与升级

```sh
uv run --locked signalnest storage-init --config signalnest.toml
```

命令创建数据目录、`raw/`、数据库父目录，并通过 Alembic 升级至最新迁移（当前 `0009_opportunity_review`）。重复运行保留数据，后续安装新版本也用此命令升级；不使用 `create_all`，不提供清空或降级命令。成功退出码为 0，存储/处理错误为 1，配置、导入元数据或命令用法错误为 2。迁移失败回滚，已创建的目录或空数据库文件可能保留。导入和重新解析要求已初始化到最新迁移，不会隐式建库或升级。

可在临时目录验证（macOS / Linux）：

```sh
trial_dir="$(mktemp -d)"
cp config.example.toml "$trial_dir/signalnest.toml"
uv run --locked signalnest storage-init --config "$trial_dir/signalnest.toml"
uv run --locked signalnest storage-init --config "$trial_dir/signalnest.toml"
```

结果位于 `$trial_dir/data/`。初始化、升级、import-page、reparse、search-rebuild、notifications-activate、mail-plan / mail-drain、政策维护、后台邮件和采集入口共用数据库旁的 POSIX advisory 写入锁；默认并行写入立即拒绝，定时 full 的采集阶段可在原运行预算内有限等待。进程退出释放锁，锁文件保留。库调用者须使用同一 writer_lock 覆盖整个写入运行；`crawl_once` 自行持有整次运行的锁。升级个人数据前保留数据库与 raw 目录备份。

从早期节点升级：执行 `uv sync --locked` 并显式运行 `storage-init`；**继续使用原配置文件及原 database 路径**即可，不需要更名或搬动数据。0001/0002 保持冻结；0003 增加响应完整性/缓存绑定、首次发现来源和三个运行事实表，直接 ADD COLUMN，不重建旧表。既有响应不猜测类型或目标；重新解析这些旧记录时返回明确错误，可用已知元数据重新导入同一原文。0004 只新增通知启用/日期证据/基线/事件/决策及 planned 意图表，保留既有数据；升级默认未启用，不补发历史通知。0005 只新增冻结邮件、精确成员和计划阻断表，保留 N1 事件/意图，不自动计划；0006 新增投递、逐次尝试和政策维护操作状态，给旧冻结邮件登记 pending；升级不发送。0007 新增可重建的当前正文搜索索引，需 SQLite 支持 FTS5 trigram；旧库升级后执行下述 `search-rebuild`。0008 新增待适配列表引用及可空的完整扫描证据，不重建既有表，不抓外部正文；0001–0007 保持冻结。

## 单次采集

数据库须先显式初始化；以下采集命令会真实发送 HTTP 请求。默认测试使用离线模拟传输；临时目录的小额度实采与目标 WSL 的完整列表扫描分别留有验证记录：

```sh
uv run --locked signalnest crawl-once --help
# 明确受限扫描，只遍历一页并最多尝试两篇详情。
uv run --locked signalnest crawl-once --config signalnest.toml \
  --scan limited --max-pages 1 --max-details 2
# 尝试完整覆盖；实际页面证据和首页复核通过后才能报告 complete。
uv run --locked signalnest crawl-once --config signalnest.toml \
  --scan full --max-pages 64 --max-details 20 --max-requests 120 --run-seconds 600
```

`--scan {full,limited}` 必填。列表页上限默认 full=64、limited=2；详情默认 20，`--max-details 0` 可只处理列表。物理请求默认 120 次、运行预算 600 秒、每个资源预算 60 秒、完整正文上限 2 MiB，分别由 `--max-requests`、`--run-seconds`、`--resource-seconds`、`--max-body-bytes` 设置。首页复核、重定向、重试和完整获取回退都计入请求/时间预算；期限不是阻塞 DNS/read 的硬终止保证。

每次扫描从首页开始，沿实际 next 链校验请求/最终 URI、连续页码、总数与尾页，并提交每页全部条目。full 还重新请求首页（`Cache-Control: no-cache`，允许条件验证），比较最终 URI 和完整结构化列表（含未适配引用）。完整扫描保存原响应绑定、有序通知/引用行及首页复核证据；仅列表覆盖完整不代表所有正文已经处理。受限、循环、漂移、预算耗尽或失败均不推进完整扫描成功时间。主动选择 limited 时，即使到达末页也保留 limited；full 被页预算截断则报告 interrupted。列表 304 或普通列表失败后，仍从数据库查询详情待办；服务端冷却、全局请求/时间预算约束所有网络请求。

旧的 processed 通知若 `next_due_at` 为空，在运行开始事务中入队首次联网复查，不根据发布日期猜测。详情成功后 24 小时到期，普通失败后 15 分钟可重试；首次失败无成功版本，复查失败保留旧版本。第一次 complete 前发现来源为 bootstrap，之后为 regular，不生成邮件事件。详情处理按数据库有效到期时间（空值用首次发现时间，数据库 ID 仅用于平局）取有界批次，单篇普通 HTTP/解析失败继续处理其他篇；归档、数据库或失败登记不能可靠完成时终止。

stdout 返回 JSON 摘要：`scanned_entries` 是已提交遍历页的全部行数（可含跨页重叠，不含首页复核），`new_documents` 是本来源的真实新增身份；另含 `scanned_references`（未适配行观察数）、`new_references`（实际新增引用）和 `remaining_unadapted_references`；详情待办只含已适配通知。引用重复观察会计入扫描行数，同一 source/完整目标 URI 仅登记一次，不抓外部正文。查看用 `signalnest references-list --config signalnest.toml --limit 20 --offset 0`，详见[引用登记](docs/list-references.md)。另含详情尝试/成功/失败、实际请求、覆盖、运行结果及待办。`remaining_due` 为当前可处理数，`remaining_unprocessed` 为 discovered/failed 数，含保留旧成功版本的复查失败。succeeded 退出 0，其他运行结果/存储错误退出 1，参数错误退出 2，用户中断退出 130；limited 覆盖与成功运行可以同时成立。详见 [协调器设计与恢复边界](docs/crawling.md)。

## 本地导入与重新解析

在仓库根目录运行以下完整演示，所有操作离线，数据写到新临时目录：

```sh
trial_dir="$(mktemp -d)"
cp config.example.toml "$trial_dir/signalnest.toml"
uv run --locked signalnest storage-init --config "$trial_dir/signalnest.toml"
uv run --locked signalnest import-page --config "$trial_dir/signalnest.toml" \
  --metadata examples/offline/list-page1.json --file research/fixtures/student-notices-page1.html
uv run --locked signalnest import-page --config "$trial_dir/signalnest.toml" \
  --metadata examples/offline/list-page2.json --file research/fixtures/student-notices-page2.html
uv run --locked signalnest import-page --config "$trial_dir/signalnest.toml" \
  --metadata examples/offline/current-notice.json --file research/fixtures/current-notice-detail.html
uv run --locked signalnest import-page --config "$trial_dir/signalnest.toml" \
  --metadata examples/offline/legacy-notice.json --file research/fixtures/legacy-notice-detail.html
# 上述全新库的第三条响应为新式详情；已有库请使用导入返回的 response_id。
uv run --locked signalnest reparse --config "$trial_dir/signalnest.toml" --response-id 3
```

结果为 50 个通知身份、2 个成功版本和 48 个待处理通知。离线命令输出 JSON，保留 `response_id`、`document_id`、`version_id`、`discovered_count` 和 `next_page_url`，增加 `pagination` 证据对象、`reference_count` 和 `registered_row_count`；discovered_count 继续仅计本站通知条目，registered_row_count 计全部有效行。本地导入不自动遍历。详情和仅登记证据的 304 输出 `pagination=null`，不能把它当末页。重复导入不增加通知或相同规则/内容的版本；每次导入仍独立登记响应证据，相同 bytes 复用一个原始文件。重新解析复用原响应，不增加响应行或伪造获取时间。

元数据是简单 JSON：必须显式给出 `page_type`（list/notice）、`source_id`、`requested_url`、`final_url`、`fetched_at`（非负 UTC Unix 秒整数）和 `status_code`。notice 还必须给出 `source_document_id`，且该身份已由列表发现；不按文件名猜身份。可选头为 `content_type`、`etag`、`last_modified`、`vary`、`cache_control`、`content_encoding`；可明确给出 `body_state`（complete/unavailable）与 `request_profile`（user_agent/accept/accept_encoding=identity）。既有示例没有 profile，不自动取得缓存资格。source_id 必须与配置一致。CLI 的输入文件路径相对于调用工作目录，存储位置始终相对于配置文件。

示例时间沿用既有 [fixture 报告](research/whu-undergrad-notice-source.md) 的记录（当时依据响应 Date，非精确客户端观测时间）。真实导入须填写已有的获取时间，不用本地读取时间替代，也不由本命令推导 HTTP 元数据。`--processed-at` 可显式提供本次处理时间，默认才取当前时间；处理时间不得早于获取时间或相关最近处理时间。重新解析旧原文成功后会将它设为当前版本，这是显式操作，不按原获取时间自动忽略旧内容。

非 200、未完整读取或只保存元数据的响应允许省略 `--file`，会登记证据并以非零退出报告无法处理；unavailable 禁止带文件，complete 必须带文件。304 必须省略文件；CLI 普通导入不猜测绑定，仍输出 `outcome=evidence_only`，无绑定不能单独 reparse。服务层显式绑定的 304 可重新处理真实 200 原文。带 profile 的空 200 不发布正文，应只登记 unavailable；既有无 profile 离线空文件仍保留 `parse_empty_page` 失败证据。没有提供仅返回成功的采集命令。

原文为 `data_dir/raw/<sha256>.bin`。写入器先完整写同目录临时文件、校验并 fsync，再原子且不覆盖地发布；已有文件、重新解析读取都验证摘要。拒绝路径逃逸、符号链接和非普通文件，损坏不覆盖。当前文件实现面向 macOS/Linux POSIX；配置加载先解析路径别名，写入器拒绝解析后路径内再出现符号链接。

文件 I/O 和解析均在数据库事务外；详情的版本、当前版本指针、成功状态及响应处理摘要一起提交。失败保留最近成功版本，同一原文可以再次尝试。连失败状态都无法登记时返回 `failure_state_unavailable`，不声称已经恢复。文件发布后、数据库登记前失败可留下孤立文件，保留供人工核对；进程中断也可能留下 `.tmp-*`。停止所有写入后，以 raw_responses 的 body_path 对照文件识别孤立文件；不自动删除。fsync/原子发布不能证明断电持久性，文件与 SQLite 也不构成跨介质原子事务，详见 [设计说明](docs/design.md)。

## 基础历史搜索（新迭代 N3）

搜索已保存的**当前成功版本**，不抓取网站、不判断机会是否仍开放，不搜索未解析成功的通知或附件文件内容：

```sh
uv run --locked signalnest search --config signalnest.toml --query '助教招聘'
uv run --locked signalnest search --config signalnest.toml --query '科研训练'
uv run --locked signalnest search --config signalnest.toml --query '竞赛' \
  --from 2024-06-01 --to 2024-07-01 --source-id whu-undergrad-student
# 使用搜索返回的 document_id。
uv run --locked signalnest notice-show --config signalnest.toml --document-id 12
# 旧库升级、索引陈旧或恢复后显式重建；持实例写入锁，无网络/原文重解析。
uv run --locked signalnest search-rebuild --config signalnest.toml
```

搜索返回标题、站点日期、原始链接、纯文本片段、稳定身份与当前版本；`--limit` / `--offset` 支持有界分页。日期范围含两端，不猜“最近一个月”的锚点。多个空格分隔关键词为 AND，有限别名及版本在结果中明示。查询和详情使用 SQLite 只读连接，不获取写锁、迁移或修复；导入、采集与重解析成功时同步更新派生索引。复查失败仍可检索最近成功正文。说明及限制见[历史搜索](docs/search.md)。

[评估报告](docs/search-evaluation.md)使用 9 篇真实原文、15 个明确查询连接真实入库和生产索引。13 个非空工程相关集 Recall@1/3/5 为 0.852564/0.980769/1.000000，2 个预期空集正确；存在“竞赛”顺带提及误检。这些是可审查的工程标注，**不是用户确认的人工 gold 或全站准确率**。

定时完整扫描可在同一实例争锁时有限等待：`[runtime].full_lock_wait_seconds = 180.0`，等待计入原full运行预算；设0可立即拒绝。普通轮询、手动采集和邮件仍立即取得锁或明确失败。Windows保活的周期恢复、追加日志及维护步骤见[WSL部署](docs/wsl-deployment.md)。

## 只读校园查询 Agent

`ask` 在一次问题中查询 UC、CS 的已保存知识库；模型可调整关键词、分块读取通知、读取查询画像和来源状态，再用真实原文引句回答。通知由 **实例 ID＋document_id** 定位，回答链接和版本由程序填入。同一个客户端支持OpenAI官方或显式配置的Chat Completions兼容接口；模型失败时保留实际检索结果和有限错误，不猜报名资格或截止时间。

```sh
cp ask.example.toml ask.toml
# 编辑 [[instances]] 指向已有 UC/CS 实例配置；profile 指向核对过的查询画像。
uv run --locked signalnest ask-config-check --config ask.toml
# 先完全本地检索，不需要模型、密钥或网络。
uv run --locked signalnest ask --config ask.toml --local --question '助教招聘'
uv run --locked signalnest ask --config ask.toml --local --json \
  --at '2026-10-12T14:00:00+08:00' \
  --question '最近一个月有没有本科生可以参加的科研机会？列出报名条件和截止时间。'
# 选定模型、核对 USD 单价并填写 [model] 后，从指定环境变量提供 API 密钥。
uv run --locked signalnest ask --config ask.toml \
  --question '最近一个月有没有本科生可以参加的科研机会？列出报名条件和截止时间。'
```

官方[配置示例](ask.example.toml)默认没有启用模型；本机兼容代理另用[ask.proxy.example.toml](ask.proxy.example.toml)，只允许HTTPS或字面回环HTTP。价格未知时使用`unpriced_proxy`，美元费用保持未知，以请求、时间和输出上限约束。[查询说明](docs/query-agent.md)列出预算及错误语义。查询不抓校园网页、不修复索引、不写库或改通知政策；实际调用会把问题、有限通知文本及读取的查询画像发送给选定提供方。路径锚定规则不变，配置校验与帮助不打开数据库、不读取密钥、不联网。

“最近一个月”按查询时区回退一个日历月并夹到有效日期；“近30天”是包含当天的30个日期。发布日期筛选与报名截止分开；未知表达式不猜测。回答只引用实际读取的片段，未理解的资格、截止或附件条件保留未知。输出为纯文本或`--json`，退出码0为完成/本地检索，1为降级，2为配置错误。最初节点完全离线；10-13本机兼容代理六次受限模型请求验证了一份有引用的回答，其他案例未全验收，费用未知、人工质量待核对，见[试验记录](docs/validation/query-proxy-20261013.md)。

[查询回放与人工评价](docs/query-evaluation.md)提供九篇真实 UC/CS 原文上的九个工程情境，默认完全离线；显式模型开关另有整批时间/估算费用预算。候选相关集与回答依据分别标注，输入和产物冻结绑定；未标注项保持待确认，不据工程回归宣称真实回答质量。

实际知识库可从已校验的一致备份准备[只读查询快照](docs/query-snapshots.md)。副本暂停邮件、配置不含SMTP，保留原备份和派生数据库摘要；`ask`显示备份时间、真实列表/正文时间、覆盖和未处理限制，不将复制时间当获取时间。五个核心问题使用[独立预算回放](docs/query-snapshot-replay.md)，前题不会消耗后题的预留；模型结果与人工评价分别记录。

```sh
# 读取既有一致备份，发布全新的私有查询目录；不连接生产或采集网站。
uv run --locked python deploy/prepare_query_snapshot.py \
  --spec /private/backup-spec.json --output /private/new-query --profile /private/profile.toml
# 可选显式私有密钥文件；也可省略并沿用配置指定的环境变量。
uv run --locked python deploy/ask_saved.py --config /private/new-query/ask.toml \
  --api-key-file /private/credentials/proxy.key --question '计算机学院有哪些助教招聘？'
```

准备工具默认生成`ask.local.toml`；真实查询需另按模型示例配置`ask.toml`。密钥文件须为本用户拥有的0400/0600普通文件，路径不得有符号链接或共享可写父目录；仅实际查询期间注入环境，帮助、`--check`和`--local`不读密钥。[发布收口](docs/query-release.md)提供固定公开允许集合、冻结归档、逐字节验证和重新安装，不将私人数据或凭据打包。

查询版本 v2 收紧截止引句：开始时间、区间起点、学院审核或普通报名说明不再借整篇的有效截止取得支持。独立校验完整声明和所选原句的同一截止边界，精度或指向不明仍待核对；生产提醒规则 v8、旧查询快照和冻结邮件不变。详见[截止证据与版本边界](docs/query-agent.md)。

当前查询版本为 `campus-query-v4`：最多两轮自主探索，预留正文读取与回答预算；原预算内最多一次引用纠正，保留原失败记录；精选短引文，工具或模型失败仍明确降级。空回答展示实际关键词、来源及匹配范围，不宣称全校无机会。真实知识库接入、模型调用、安装与已知限制另见[日常查询验收记录](docs/validation/query-daily-20261015.md)；副本是静态快照，不自动同步生产。

兼容代理节点全量2558项离线测试及Ruff/格式通过；历史[v2实际验证](docs/validation/query-deadline-v2-20261012.md)保留，原九项人工评价仍待确认。新回放独立绑定源码和输入，不继承旧标签。

## 上线检查与观察（新迭代 N2）

```sh
uv run --locked signalnest rollout-check --config signalnest.toml \
  --release-root "$PWD" --at "$(date -u +%s)"
uv run --locked signalnest observe --config signalnest.toml \
  --release-root "$PWD" --at "$(date -u +%s)"
# 产生源码实际字节与独立清单，目标必须新建且位于源码目录之外。
uv run --locked python deploy/release_snapshot.py \
  --release-root "$PWD" --output /private/tmp/signalnest-release.tar.gz
```

准备报告检查源码、数据库、积压、冷却和邮件状态；真实画像须显式传 `--profile`，核对后再声明 `--profile-confirmed`。它始终保留 `externally_verified=false`，不会发送邮件或开启定时器。`observe` 输出一行 JSON；新增 `deploy/observe_instance.py` 使用实际系统时间，供只读 systemd 观察模板每小时记录到独立 journal，不补造历史。模板默认使用明确标为测试的画像；确认个人画像后才用实例 drop-in 更换路径并加确认参数，见[上线说明](docs/rollout.md)。

WSL 已完成 UC 24 页、CS 4 页真实完整扫描及首页复核，10 月 11 日确认提醒画像后分别启用生产通知、邮件和观察 timer，见[上线起点](docs/validation/n2-production-wsl-20261011.md)。首条自然 Digest 于 10 月 12 日 09:00 到期，计划/SMTP 接受/实际收件须分别追踪；旧工程测试邮件实收不代替它。首周窗口固定从 **10 月 11 日 12:50:19 至 10 月 18 日 12:50:19（Asia/Shanghai）**，满七天后依据真实记录判断，当前尚未验收。UC、CS 分别使用数据库、完整扫描与独立邮件 timer；部署步骤见[后台邮件](docs/background-mail.md)。

新增[只读运行汇总](docs/observation-summary.md)从导出的观察记录计算积压净变化、覆盖过期和缺失区间，不读业务库、不联网、不修复；[人工评价入口](docs/human-review.md)独立保存输入摘要绑定的人工标签。六组 31 情境及 15 搜索查询仍是工程回归，未确认的案例保持 pending，不据此声称真实漏报率。

## 单次 HTTP 的离线衔接接口

这些证据/业务接口不发送 HTTP，详见 [持久状态设计](docs/ingestion-state.md)。`crawl_once` 将 Fetcher 与这些接口连接，详情到期时间仍只有 documents.next_due_at：

- `select_cache_candidate` 按 source + 实际请求完整 URI + 固定 RequestProfile 选择具体 200，并校验文件。无候选或原文丢失/损坏时返回 `requires_full_fetch`，不能取最近成功版本代替。
- `record_response(..., candidate=...)` 将无正文 304 绑定到请求前选定的 200。新的完整 200 即使解析失败也成为最新传输原文；旧成功内容保留。URI 查询不排序、不删除；旧路由与新路由、不同 profile 不共用验证器。
- `process_cached_response` 自动处理最新原文/绑定 304，返回 `body_response_id` 与观察的 `response_id`；无法安全复用时返回 `outcome=full_fetch_required` 与有限错误代码，获取器据此完整获取。解析/数据库失败仍抛错，不能当作无变化。
- `discover_page_in_transaction`、`save_notice_in_transaction`、`record_failure_in_transaction` 使用已有 Connection，与资源标记/详情 due/来源状态组合提交；便利入口仍自行包裹短事务。`record_failure` 可直接登记元数据失败，保留旧成功结果。
- 来源/运行状态接口记录列表尝试、有效正文证据、整页登记、完整/受限/中断覆盖及独立运行结果；`pending_documents` / `pending_resources` 从 SQLite 重建工作，详情 due 只有 documents.next_due_at。服务端 not_before 冷却持久化，HTTP 层负责遵守。

支持的 Vary 仅 User-Agent/Accept/Accept-Encoding；未知 Vary、`*`、no-store、压缩编码或无效验证器要求完整获取。不兼容 304 会持久阻断后续条件请求，直到新的完整 200。private/no-cache 可以条件验证；不会用 304 改写原 200 头和获取时间。显式历史 reparse 可以切换旧版本，自动缓存恢复会拒绝旧原文回退。

完整扫描状态接口校验传入分页/复核声明，新生产调用还校验响应绑定及通知/引用成员已登记，并与来源成功时间一起保存证据；`crawl_once` 在调用前实际提交全部页面、验证 URI 链/连续页码/总数/明确末页并完成首页复核。列表 complete 与运行 partial_failure 可以同时成立。旧记录的发现 origin 保持 unknown，不根据日期猜测。

写入锁支持 macOS/Linux POSIX；macOS 与 WSL/Linux 的真实子进程验证已分别记录，原生 Windows 写入未支持。数据库路径别名解析到同一锁，硬链接数据库和符号链接锁文件拒绝。帮助/config-check 不获取锁；外部不遵守 advisory lock 的程序不受保护。文件/Parser 均在事务外，正常回滚与锁释放不证明断电耐久性。

## 有界 HTTP 获取接口

`HttpFetcher(engine, raw_store, settings.http, source_id=...)` 是库入口；构造不发送请求，调用者持有 writer_lock、在一次运行内复用实例，并用上下文管理器关闭 Client。`fetch(FetchTarget(uri=..., page_type="list"))` 获取一个资源；详情显式指定 page_type="notice" 与 source_document_id。`crawl_once` 复用一个实例完成列表与详情。

`FetchResult.outcome` 为 complete、bodyless、transport_failure 或 deferred；`attempts` 保留每个实际 GET 的时间、ResponseInput、完整 bytes/None、304 的具体 candidate 与有限错误。调用者须按顺序登记所有已收到的响应，然后调用既有业务处理接口。Fetcher 自己只校验缓存文件和保存冷却，不归档响应或提交通知成功。

默认 profile 的 User-Agent 来自配置，Accept=text/html、Accept-Encoding=identity；也可显式提供 RequestProfile，实际三个请求头始终完全一致，拒绝不能直接发送的非 ASCII profile 值。Cookie/Auth 不发送；TLS 验证开启，环境代理关闭，底层重试为 0。手动跳转限本站 HTTPS 443、支持的列表路径或同身份详情路由，每跳重新选择自己的验证器。

完整 200 正文流式计数，默认最多 2 MiB，拒绝压缩编码、短读、空正文与非 HTML 类型；站点结构仍由 Parser 验证。默认两次额外尝试、三跳重定向、单资源 60 秒、全实例 600 秒/120 次物理请求；HTTP 超时按剩余预算收紧。异常 304 最多一次完整 GET 修复，共用额外尝试/间隔/运行预算。429 与适用的 Retry-After 保存完整 not-before，最多本次等 15 秒，其他同来源 URL 与重启也须遵守。期限是协作预算，不能承诺同步 DNS/read 内的精确硬终止。

`fetch(..., revalidate=True)` 用于首页复核；登记 304 后发现绑定文件再失效时，协调器调用 `repair(result)`，在同一次资源预算和唯一重试层内完整获取，不创建虚假原文或获取时间。交接表、错误分类、保守策略及限制见 [Fetcher 设计](docs/fetching.md) 和 [协调器设计](docs/crawling.md)。已完成真实子进程终止/重启及临时目录少量低频实采，具体证据、结果和验证边界见 [恢复验证记录](docs/recovery-validation.md)。默认测试仍完全离线。

## 定时运行与状态诊断

```sh
# 只读 JSON 诊断：不联网、不建库、不取得写入锁。
uv run --locked signalnest status --config signalnest.toml
# 以下两项会联网，使用 TOML 中对应的有界预算。
uv run --locked signalnest scheduled-run --config signalnest.toml --mode regular
uv run --locked signalnest scheduled-run --config signalnest.toml --mode full
# 首次采用新政策：先备份，再显式保守重算旧成功 due；不联网。
uv run --locked signalnest apply-recheck-policy --config signalnest.toml
```

到期详情分为前两页首次待办 F、其他首次积压 H、已有成功基线复查 R；默认保留 12/4/4，借用空槽并轮转，失败也计逻辑尝试。成功按上海日历年龄 0–7/8–30/>30 天分别 24 小时/7 天/30 天复查；未来日期按近期档并计数。失败按有限类别延期，始终服从来源冷却。due 与成功状态同事务提交；旧成功 due 不会自动批量重写，显式更新不会推迟逾期或清除失败退避。

`status` 区分列表尝试、有效响应、登记与完整扫描，展示首次积压、成功基线复查、冷却、最近运行及过期/预算提示。正常读到警告仍退出 0，存储错误 1、配置错误 2；它不把 running 行当活进程，也不从持久记录猜测本次 F/H 分组。新摘要和组日志提供分配、尝试、成功、失败、未服务、剩余 due 与最老逾期。

交付一个 **Linux/systemd 252+** 模板：普通每半小时，full 每日上海时间 01:15，均单次串行、同实例锁、无自动重启。首次或至少 24 小时停机恢复先执行一次有界 full；错过的普通周期不排队重放。模板已在目标 WSL 实例启用，本次保活与有界锁等待补丁的真实部署见[修复记录](docs/validation/n2-reliability-fix-20261014.md)；部署到其他机器仍须核对并验证本机配置。另提供每五分钟邮件 timer，显式启用后可自动推进计划与发送；三个服务共用实例锁。专用日志保留、一致备份与含邮件状态的只读恢复校验见 [部署与运维](docs/operations.md)；处理和诊断的准确语义见 [政策说明](docs/runtime-policy.md)。

## 本地画像与通知决策预览（N0）

`profile.example.toml` 明确标注为虚构示例，必须按自己的情况编辑；缺少的学校、层次、学院、专业和入学年保持未知，不推断为“符合资格”。Profile 独立于采集配置，不包含邮箱或凭据。至少配置一项关注主题或字面词组，拒绝未知字段、非法主题、重复项和数值隐式转换。

```sh
cp profile.example.toml profile.toml
uv run --locked signalnest profile-check --profile profile.toml
# 使用已有真实 fixture，纯离线；显式提供最终 URL 与决策时钟。
uv run --locked signalnest decision-preview --profile profile.toml \
  --file research/fixtures/notifications/notice-128291-20261005T133533Z.html \
  --url https://uc.whu.edu.cn/info/1517/128291.htm \
  --at 2026-10-05T20:00:00+08:00 --next-digest-at 2026-10-06T09:00:00+08:00
# 也可输入 NoticeContent JSON；不从数据库隐式取当前版本。
uv run --locked signalnest decision-preview --profile profile.toml \
  --notice-json /absolute/path/notice.json \
  --at 2026-10-05T20:00:00+08:00 --next-digest-at 2026-10-06T09:00:00+08:00
```

以上命令不需要 `--config`、数据库初始化或写入锁，不联网、不写数据库或原文、不发送邮件。画像路径、HTML/JSON 路径相对于调用工作目录；时间必须含时区，下一次 Digest 时间必须晚于决策时间。`--file` 必须给出实际最终详情 URL，不能根据文件名猜文章身份。JSON 输入完整字段见 [NoticeContent](src/signalnest/contracts.py)，不是数据库行或 ParsedNotice 包装。

stdout 为 JSON：事实、短原文证据、资格/时间三值判断、Action、独立 `needs_review`、理由、命中规则、未知项、版本和摘要。正文全文不重复输出。Action 为 `PUSH_NOW / DIGEST / STORE_ONLY / IGNORE`；有效路线为 `immediate / digest / none`。该命令的路线只是预览结果，不登记投递意图。N1 的生产成功事务可另行登记 planned 意图；N2 计划和冻结邮件，N4 独立登记发送尝试与结果。资格未知仍可提示核对可信紧迫的相关机会，不会声称用户已符合资格；近期日期本身不触发推送。

`--event-kind {new,update,activation_recent,historical}` 与 `--mode {hybrid,digest_only}` 仅声明预览上下文。update 可给 `--previous-notice-json` 和 `--previous-route`，缺少旧内容则标明比较未知。首启预览默认汇总，可信截止不晚于下一次 Digest 时保留紧急路线；显式 digest_only 仍优先。命令不证明实际发生新内容或此前已登记邮件资格。校验/参数错误退出 2，Parser 无法支持输入页面退出 1，成功预览退出 0。

政策 v8 补完两个原始行动表达的漏报，v9 保留该行为：固定“即日起接受报名”和“请登录学校系统报名辅修专业”及当天截止，修复前分别为 DIGEST、IGNORE，修复后均为 PUSH_NOW/deadline_soon。没有删除“接受”、添加“完成”或改画像让样本通过；完整本地输入、内容摘要和实际前后结果见[原句修复记录](docs/validation/notification-original-misses-20261010.md)。沿用 v6 的已绑定正文指令确认，不推造开始日期；菜单、历史、查询记录、缴费和退课仍被排除，新生选课的“辅修专业单独缴费”仍不触发辅修提醒。[生产评估](docs/validation/notification-production.md)分开记录 13 项真实情境、原 2 项合成情境、6 项助教情境、2 项有限指令边界、6 项 CS 情境及本次 2 项原句回归；工程期望不作为人工 gold。可离线重放：

```sh
uv run --locked python deploy/evaluate_notifications.py --check
uv run --locked python deploy/evaluate_notifications.py --check \
  --cases docs/validation/notification-production-synthetic-cases.json
uv run --locked python deploy/evaluate_notifications.py --check \
  --cases docs/validation/notification-current-instruction-cases.json
uv run --locked python deploy/evaluate_notifications.py --check \
  --cases docs/validation/notification-original-misses-cases.json
```

v7 引入的计算机学院“招募本科课程助教”、跨分号申请分支及年份冲突判断保持；真实本科招募进入待核对 Digest，不把缺少年份的月日补成紧急截止。[计算机学院离线验收](docs/cs-undergrad.md)提供 `profile.cs-undergrad.example.toml`、`decision-preview --parser cs-undergrad-notices` 和独立六情境回放；现已另行接通[显式来源采集](docs/cs-collection.md)。此前五组情境输入及 Action 保持不变，实际 v7/v8 快照另存；当前规则 v10 保留这些语法和主题优先级。

政策 v9 按声明区分本次申请主体、已完成的教师征题和未来受益学生；“在指导教师指导下”不再变成学生的教师资格要求。明确“项目负责人必须为本科生”形成本科约束，毕业年级、团队人数、参与项目互斥和导师资质仍需核对，月份区间不推成精确截止。两篇真实申报在固定时钟下为 DIGEST / unknown / needs_review，真正面向教师征题的反例仍排除 student，role 未填仍未知；混合当前主体无法区分时保留未知。见[完整回归与版本边界](docs/validation/notification-applicant-scope-v9.md)。

既有实例须显式更新政策；旧决策、已有 Digest 资格和冻结邮件不会自动撤回或重写。当前只支持有限词组与上下文、对象和时间；标题主题仍可表示普通相关信息，显式 `include_phrases` 仍是宽泛字面关注。不确定关联仅保留待核对，不凭当天截止补造关系。研究中原固定输入的 10 个 Action 已获人工确认，适配后的生产输入与工程期望另列，不能当成同一批人工准确率。未知资格和未支持表达式保留未知，图片/附件仅保留引用，不做 OCR 或下载解析。覆盖、限制、升级步骤及纯函数入口见 [原 N0 规则说明](docs/notifications.md)。[原 N1 设计](docs/notification-state.md)记录启用边界、独立 live 基线和原子成功事务。

## 通知启用与事件登记（N1）

升级不会自动开启通知。启用前须已有一次由生产协调器证明的完整列表扫描；详情部分失败仍可启用。下面邮箱地址仅为示例，正式启用请提供自己的地址；此启用命令不连接 SMTP。

```sh
uv run --locked signalnest storage-init --config signalnest.toml
# 只读预览：窗口、候选数量及最多 50 个近期身份和真实日期证据。
uv run --locked signalnest notifications-preview --config signalnest.toml \
  --profile profile.toml --activation-id first-enable \
  --sender notifications@example.org --recipient me@example.org
# 确认候选后显式启用，冻结边界、Profile/规则和地址。
uv run --locked signalnest notifications-activate --config signalnest.toml \
  --profile profile.toml --activation-id first-enable \
  --sender notifications@example.org --recipient me@example.org
uv run --locked signalnest notifications-status --config signalnest.toml
```

preview/status 只读数据库，不获取写入锁或联网；activate 使用统一实例锁和短事务。`--mode digest_only` 可以固定为汇总路线；默认 hybrid。`--digest-hour/--digest-minute` 默认上海 09:00，用于决策和邮件计划的下一档时刻判断；实际触发由外部 timer 决定。`--no-initial-recent` 关闭首启近期回顾。`--at` 为可选 UTC Unix 秒，默认本次处理时钟（与 N0 预览的 ISO 时间参数不同）；相同启用 ID、Profile 和参数重复执行始终复用原时间/集合，不重置，不同参数明确失败。

窗口固定为启用当日及前 6 个上海自然日，近期日期本身不能触发邮件资格。未知日期保持待核对，积压延期不丢已选候选；冲突保留原始选择和第一对证据。稳定启用集合不使用数据库整数 ID 水位。

之后生产采集显式登记 live 观察与事件；重复 200/304 不追加事件，A→B→A 记录两次 update。离线 import-page 和维护 reparse 可更新业务版本，但保持独立 live 基线与事件。版本/成功/due、资源处理标记、基线、事件、决策、选中路线及必要 planned 意图一起提交，通知登记失败整体回滚。这些意图和资格由 N2 计划；`crawl-once` 保持仅采集，显式启用的 `scheduled-run` 会在采集后执行邮件阶段。完整接口、比较未知的保守行为、迁移和恢复边界见 [N1 说明](docs/notification-state.md)。

## 邮件计划与预览（N2）

已升级并通过 N1 启用的实例，可以显式消费已保存的路线和决策，生成立即纯文本邮件或到期 Digest。无需 SMTP 配置，以下命令全部本地执行：

```sh
# 不写入的候选邮件预览。
uv run --locked signalnest mail-plan --config signalnest.toml --preview
# 默认每轮 5 封、每封 50 个事件、完整 MIME 128 KiB。
uv run --locked signalnest mail-plan --config signalnest.toml
# 更小的批次；用输出的 mail_ids 查看保存的内容。
uv run --locked signalnest mail-plan --config signalnest.toml \
  --max-messages 3 --max-events 20 --max-bytes 65536
uv run --locked signalnest mail-preview --config signalnest.toml --mail-id 1
```

`--at` 可明确给出 UTC Unix 秒，默认当前时间。JSON 区分本轮计划数、邮件 ID、阻断诊断、未分配立即/Digest、已到期和延后项；单项过大不会丢失事件，后续正常项可继续计划。重复计划不重新分配事件；立即与 Digest 排他，停机积压可分轮补计划。Digest 的待核对项单独展示，默认上海 09:00 沿用 N1 日历。

第一次冻结保存完整邮件 bytes、地址、Message-ID、Date、渲染版本、精确成员及决策理由；后续正文或 Profile 改变不影响它。两个预览的 stdout 有意包含地址、正文与证据，stderr 日志不包含这些内容。预览不取写入锁、不修改数据库。`notifications-status.planned_immediate` 仍是 N1 意图总数，未冻结积压看 mail-plan 的 remaining_*。**mail-plan 只冻结邮件**；可显式 `mail-drain`，或启用后台入口自动计划/发送。迁移、接口、额度与恢复边界见 [邮件计划说明](docs/mail-planning.md)。

## SMTP 适配器（N3）

标准库 `send_frozen(FrozenMessage, SmtpSettings) -> SendResult` 已实现，使用 N2 保存的地址和完整邮件字节。支持强制 STARTTLS 或隐式 TLS、证书/hostname 校验、环境变量凭据和有限超时；返回 accepted/retryable/uncertain/permanent，适配器自身不重试。

`[smtp]` 可以省略，示例默认注释禁用。配置只保存凭据环境变量名，检查配置和导入模块不读取其值或建立连接。正文后的最终 250 才算 accepted；之后 QUIT/close 失败不能触发反转或重发。DATA 边界断线/超时保留 uncertain，不猜测送达。

N4 已接入发送 CLI、持久尝试及恢复；mail-plan 和 crawl-once 不调用 SMTP，显式启用的后台入口会调用 N4。N3 本身不改数据库状态，直接重复调用适配器仍会发送一次；通过 N4 先登记尝试，再调用并保存结果。配置、有限契约、保守分类和测试范围见 [SMTP 说明](docs/smtp.md)。

## 发送、诊断与维护（N4）

先显式升级数据库并核对已冻结邮件。配置示例的 SMTP 默认禁用；启用 `[smtp]` 并提供对应环境变量后，`mail-drain` 可发起邮件连接；另外启用 `[mail_runtime]` 才允许后台命令自动计划/发送。本轮实现没有发送真实邮件。

```sh
uv run --locked signalnest storage-init --config signalnest.toml
uv run --locked signalnest mail-status --config signalnest.toml
uv run --locked signalnest mail-preview --config signalnest.toml --mail-id 1
# 会发送已冻结且到期的邮件，默认每轮最多 5 封；不会自动补计划。
uv run --locked signalnest mail-drain --config signalnest.toml --max-messages 5 --run-seconds 300
uv run --locked signalnest mail-pause --config signalnest.toml
uv run --locked signalnest mail-resume --config signalnest.toml
# 仅对 blocked 原邮件授予一次额外尝试，不清零次数或绕过未知结果冷却。
uv run --locked signalnest mail-retry --config signalnest.toml --mail-id 1
```

`mail-status` 只读，区分 pending/sending/retry/uncertain/accepted/blocked、到期积压、未计划意图、未分配 Digest、暂停原因和有限尝试诊断。最多展示 100 封，计数覆盖全部邮件；未解决未知与累计未知尝试分别报告。`mail-drain` 使用实际时钟，不接受伪造获取时间的 `--at`。运行预算在每封开始前检查，不能保证中止已进入 SMTP 的阻塞调用。普通单封失败继续；通道错误结束本轮，永久通道错误持久暂停；本地状态登记失败立即终止。drain 有需要处理的失败/暂停/未知返回 1，正常返回 0；参数/缺失 SMTP 配置返回 2。status 成功读出诊断返回 0，并不宣称邮件健康。

默认最多六次自动尝试，退避 5 分钟、15 分钟、1 小时、6 小时、24 小时；未知至少等待 30 分钟。配置 `[mail_sending]` 可缩小数量、时间和尝试上限，有限退避与未知冷却均有校验。遗留 sending 在持锁后一次性登记为未知，冷却跨重启保留。已提交 accepted 永不自动重发；SMTP 接受后本地提交前中断可能重复发送同一邮件，Message-ID 不提供通用外部去重。

政策更新与最多 100 个明确当前事件的重评独立于发送，使用稳定 operation ID；不自动补发历史，也不改已取得资格或已冻结邮件。恢复重评只给原 operation ID，复用原成员、政策、证据和决策时间：

```sh
uv run --locked signalnest notifications-policy-update --config signalnest.toml \
  --profile profile.toml --operation-id policy-20261008 --at 1791421200 --preview
# 核对后去掉 --preview；该操作只更新后续决策政策。
uv run --locked signalnest notifications-reevaluate --config signalnest.toml \
  --operation-id review-20261008 --event-id 17 --at 1791421200 --preview
# 显式执行后若中断，用同一 operation ID 恢复。
uv run --locked signalnest notifications-reevaluate --config signalnest.toml --operation-id review-20261008
```

预览不登记操作；首次执行必须给出事件与时间。已登记 Digest 资格即锁定，即使尚无立即 outbox。新规则时先更新政策；重评没有重新选择全部历史的隐含行为。接口、迁移、事务边界和恢复限制见 [发送说明](docs/mail-sending.md) 与 [政策维护说明](docs/notification-maintenance.md)。已有后台邮件 service/timer 模板，尚未安装或验证真实邮箱。

## 后台邮件

数据库升级、通知启用、政策及 SMTP 核对后，在 TOML 中明确设置 `[mail_runtime].enabled = true`，并保留 SMTP 凭据环境变量。默认每轮计划 5 封/每封 50 事件/128 KiB，发送 5 封/300 秒，可分别配置 `[mail_runtime]` 和 `[mail_sending]`。

```sh
# enabled=false 时只返回跳过；enabled=true 时会计划并发送。
uv run --locked signalnest scheduled-mail --config signalnest.toml
# 以下显式启用后台时，在一次采集后继续邮件阶段。
uv run --locked signalnest scheduled-run --config signalnest.toml --mode regular
```

普通采集失败或单项渲染阻断不妨碍已有冻结邮件；数据库、归档或状态登记等系统错误停止本轮。暂停允许计划与遗留 sending 恢复，禁止 SMTP。Linux 模板增加独立五分钟邮件 timer 和环境凭据文件，采集服务也接通邮件；锁冲突明确退出，下次周期继续。各阶段输出独立摘要和有限错误，见 [后台邮件说明](docs/background-mail.md)。恢复副本先暂停且不注入凭据；备份校验已专项检查冻结邮件、成员、尝试和投递状态，见 [邮件备份恢复](docs/mail-backup.md)。


## 助教招聘的本地预览（新 N1）

虚构画像 `profile.teaching-assistant.example.toml` 正式使用 teaching_assistant 主题。真实历史 EMS 通知可显式选择详情 Parser；以下是 **2024 历史回放**，不表示岗位现在开放：

```sh
uv run --locked signalnest profile-check --profile profile.teaching-assistant.example.toml
uv run --locked signalnest decision-preview \
  --profile profile.teaching-assistant.example.toml --parser ems-notices \
  --file research/fixtures/teaching-assistant/ems-notice-250571-20261008T102823953399Z.html \
  --url https://ems.whu.edu.cn/info/1588/250571.htm \
  --at 2024-09-20T09:00:00+08:00 --next-digest-at 2024-09-21T09:00:00+08:00
```

默认 Parser 仍为 whu-student-notices；EMS 只供显式 HTML 预览和库调用，不扩展采集来源或 import-page/reparse CLI。仅出现“助教”不触发提醒；真实申请当天截止可及时提示，并保留“原则上、本院、全日制、教师审核、附件”未知项。当前时间判断该历史岗位已过期。来源缺口、规则边界、离线入库与邮件预览证据、政策 v5 显式升级见[助教说明](docs/teaching-assistant.md)。

## 日志与当前边界

命令结果写 stdout；结构化事件日志写 stderr，包含 UTC 时间、级别、事件名和可选 source_id/run_id/document_id/response_id/mail_id/attempt_no/stage/error_code。错误诊断也写 stderr，因此错误输出不是纯 JSON 流。日志不包含原始配置、URL、异常正文或网页正文；只在 CLI 显式配置 SignalNest 的 logger，不修改 root logger。

已实现：安装/CLI/配置、数据库初始化与迁移、核心业务表、运行事实表及独立待适配引用表/约束、契约与稳定内容摘要、有界同步 HTTP Fetcher、WHU Parser、原文存储、整页幂等发现、版本/成功状态原子提交、失败登记、离线导入/重新解析、单次完整/受限扫描、独立详情补抓/复查、分组处理与到期政策、只读诊断、运行摘要、日志及外部定时模板、备份恢复校验程序，以及 N0 本地画像、事实提取和决策/路线预览、N1 启用/事件/决策/意图持久化，以及 N2 纯文本渲染、冻结、排他成员分配、分片和积压补计划，以及 N3 同步 SMTP 适配器、N4 发送尝试/有限重试/诊断/恢复与受限政策维护，以及政策 v9 的申请主体/历史阶段修复、v8 的接受报名/直接登录报名修复、v7 的 CS 助教申请与年份冲突判断、v6 的绑定报名指令确认、v5 的助教招聘/申请主体/时间判断、v4 的主题/行动关联与不确定证据、独立 EMS 离线详情 Parser、竞赛详情模板、后台自动邮件和含邮件状态的备份校验。

基础历史搜索、WSL 完整采集与工程邮件实收已另行验证；生产首周观察仍在原窗口内，尚未验收至少一周持续运行和当期助教自动发现。已实现有界只读查询 Agent、可配置模型适配器及一致快照查询，已有受限真实模型调用，人工质量评价仍待完成；没有后台模型判断、Embedding、向量索引或 Reranker。HTTP 必须显式调用 Fetcher 或采集入口；获取或 Parser 成功也不代表已经持久化成功。不增加用户系统、微服务、Redis、Celery、向量数据库、Docker、CI 或跨语言接口。

fixture 驱动的 Parser、离线闭环及三个采集交付均已完成：**有界 Fetcher → 单次采集协调器与 CLI → 整条恢复验证、真实终止实验及少量低频实采**。定时运行与诊断节点现提供政策和部署模板，后续在目标机观察；N1 已提供事件与投递资格，N2 已冻结本地邮件；N3 已提供 SMTP 适配器，N4 已实现发送登记、恢复与政策维护。既有实采验证限于记录中的边界和样本，不等于断电或全站扫描验证。普通运行处理增量/待补抓任务；首次历史导入建立基线；未来通知策略独立决定哪些事件发送邮件，不默认给所有历史通知发邮件。显式 mail-drain 或 SMTP 库调用可发送；`scheduled-mail` 与 `scheduled-run` 仅在明确启用后台邮件时发送。

列表 304 不代表没有待补抓详情或到期复查任务；“遇到已知通知就停止分页”不能保证完整性。模块边界、规范化规则、错误分类和后续集成约定见 [设计说明](docs/design.md)。

## 离线 Parser

```python
from pathlib import Path
from signalnest.contracts import PageInput
from signalnest.parsing import parse_list, parse_notice

page = PageInput(
    content=Path("research/fixtures/student-notices-page1.html").read_bytes(),
    page_url="https://uc.whu.edu.cn/tzgg/xstz.htm",  # 获取后的最终 URL
)
listing = parse_list(page)
assert len(listing.entries) == 25  # 此 fixture 的数量，不是 Parser 的固定限制
assert listing.pagination.current_page == 1
assert listing.pagination.total_pages == 24  # 从这一份页面的证据取得，未硬编码
assert not listing.pagination.is_last_page
notice = parse_notice(
    PageInput(
        content=Path("research/fixtures/legacy-notice-detail.html").read_bytes(),
        page_url="https://uc.whu.edu.cn/2022/show.jsp?wbtreeid=1517&wbnewsid=127581",
    )
)
print(notice.source_document_id, notice.content.content_sha256())
```

两个函数都完整解析并验证输入，使用 UTF-8 / `html.parser`，不联网、不执行脚本、不下载引用、不写文件或数据库，没有原始摘要缓存。两种文章路由统一为 `1517:文章ID`；列表沿实际“下页”链接，不因日期或已知条目提前停止。任一必要条目无效则整页失败。

`ListPage.entries` 继续只含本站通知，顶层 `next_page_url` 访问方式不变；v4 增加 `references`、`row_count` 与 `ordered_rows()`，保留有效未适配目标及混合行顺序，详见[引用登记](docs/list-references.md)。已有必需的 `pagination: PaginationEvidence`，手工构造 ListPage 时也须提供证据。字段为 `current_page`、`total_pages`、`is_last_page`、`terminal_evidence`、`last_page_url`。非末页的总页数由最大可见数字锚与活动“尾页”链接一致性支持；末页必须当前页等于最大可见页码，且“下页”与“尾页”均为唯一、无锚的禁用标记，证据为 `disabled_next_and_last`。末页的 next/last URL 都为 null，非末页保留 HTML 中的真实链接。

既有首页、第二页及新增真实末页分别解析为 1/24、2/24、24/24，条目数为 25/25/13。文件名 `1.htm` 不代表第 1 页；不从文件名计算任何页码或下一页。缺失/重复 paginator、必要数字/控制缺失、状态冲突或活动链接损坏均抛 ParseError。当前没有可信的无分页单页模板，不能省略 paginator；少于 25 条、日期较旧或条目全已知也不证明末页。

分页 URL 限 HTTPS、`uc.whu.edu.cn` 默认 443、无凭据/查询/片段，路径为 `/tzgg/xstz.htm` 或 `/tzgg/xstz/<正整数>.htm`；下一页自链拒绝。可见 next 数字锚存在时须与活动“下页”一致。可见数字唯一、递增，跳过的数字须有实际省略号；明确 hidden、aria-hidden 或内联隐藏样式的必要证据拒绝，不计算外部 CSS 或执行脚本。原文模板不能可靠识别时失败。完整性由 `crawl_once` 校验跨页连续性、总页数漂移、请求/最终 URI 循环及尾页目标，所有页面成功登记并完成首页复核后才能判断扫描完成。单页的末页证据不等于一次完整扫描成功。

详情保留段落/表格、正文文本及 HTTP(S) 链接/图片/附件引用。HTML 中的 `a[href]` 和 `img[src]` 改为相对于最终页面 URL 的绝对地址；锚点、mailto、javascript 等链接不进入网页引用，并移除 href、保留可见文本。图片与附件仅有元数据；附件 access 保持 `not_checked`。脚本、已知统计节点、正文外区域不参与内容摘要。保留的 HTML **不是安全清洗产物**，后续展示不能直接信任它。

当前 Parser 为 `whu-student-notices-v4`，增加未适配列表引用登记，保留 v2 分页证据和 v3 有真实竞赛样本支持的 `#vsb_content_501` 正文模板；仍要求唯一容器与直接 `.v_news_content`，不猜测正文。沿用一个站点版本，列表和既有详情也标为 v4；旧模板提取、规范化和 NoticeContent 摘要规则不变，同内容的新版本产物可共存。live 对同一原文的规则升级不制造内容更新事件，维护 reparse 不生成邮件。原始字节摘要仅标识采集证据，不证明解析成功；parser_version 不进入内容摘要。

失败抛出 `ParseError`，提供 `code`、可选 `field` 和从 0 开始的 `item_index`：编码/空白输入、必要结构缺失、无效字段、身份不支持/含糊、异常空列表、无意义正文分别分类。空字节仍由 PageInput 拒绝。具体代码与规范化保证范围见 [设计说明](docs/design.md)。PageInput 不携带状态码；业务层仅把完整 200 原文交给 Parser，Fetcher 与缓存接口处理 HTTP 状态和 304 基线策略。

分页结构缺失/重复使用 `missing_structure`，非法整数或 URL 使用 `invalid_field`，相互矛盾的页码、数字链接或活动/禁用标记使用新增 `invalid_pagination`。离线入库仍按原规则保存证据和 `parse_` 错误代码，不登记部分条目，不改变原有事务与恢复语义；重新解析时从归档原文重新取得分页证据；单页导入不声明完整覆盖，完整扫描才在 coverage_evidence 保存实际响应/全部混合行/首页复核证据。

## 实际验证

以下按交付日期保留记录；当时尚未实现的能力，以后续节点为准。

2026-09-29，macOS / Darwin arm64，CPython 3.12.14，SQLite 3.53.1，uv 0.12.20。锁定的直接依赖为 Pydantic 2.13.5、SQLAlchemy 2.0.54、Alembic 1.20.0、HTTPX 0.28.1、Beautiful Soup 4.15.0；开发检查使用 pytest 8.4.2、Ruff 0.16.9。工具安装于临时隔离环境，未修改系统 Python。

**54 项离线测试全部通过，Ruff 检查与格式检查通过。** 测试覆盖配置和路径、错误退出、无副作用导入、迁移与重复初始化、唯一/外键/状态约束、升级失败回滚、数据契约与摘要、HTTP MockTransport、fixture HTML 后端、日志字段与敏感内容排除。测试默认离线；调研与 fixture 保持原样。sdist/wheel 构建通过；按锁文件在独立环境安装 wheel 后，已从临时工作目录验证帮助、配置校验不建库、两次初始化、迁移版本、错误退出与 JSON 日志。更名前的配置文件/数据库路径可继续使用，初始迁移内容未变。未验证其他操作系统/Python 次版本、并发迁移或校园网站实采。

2026-09-30 Parser 阶段：在上述 CPython 3.12.14 环境中完整执行 pytest，**136 项离线测试通过**（新增 82 项、原有 54 项），Ruff 检查和格式检查通过。使用四份既有 HTML 与内存构造样本，覆盖列表、身份、正文/媒体、字段错误、无意义正文、重复失败与确定性、统计/正文外修改不改变内容摘要、真实正文变化改变摘要。原报告、fixture 和用户未提交调研文件保持原样；契约、依赖、schema 和历史迁移未修改。没有实时抓取或运行上游故障实验。

2026-10-01 离线持久化阶段：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1，依赖未变。`uv sync --locked --offline` 成功，完整 pytest **199 项通过**；`ruff check src tests`、`ruff format --check src tests` 和 `git diff --check` 通过。另按本页命令在新临时库中初始化两次、导入四份 fixture、重新解析及重复导入，查询确认 50 通知/2 版本/48 待处理；重复导入共 8 条独立响应证据，仍仅 4 个原文文件，重新解析不增加响应。

新增验证使用真实文件/SQLite，覆盖身份/版本幂等、A→B→A、失败原文与状态保留、损坏/缺失/路径/符号链接、整页回滚、版本与成功状态一起回滚、文件发布与目录同步失败、提交失败、失败登记不可用、重新打开库继续处理、新版规则重解析、CLI 非零退出和无副作用帮助。中断场景是进程内 KeyboardInterrupt/数据库触发器/事件/I/O **故障注入**；没有真正终止进程、断电或真实 HTTP 实验，也未在 Linux/Windows 上运行验证。Parser、0001 和原有四份 HTML 的字节摘要核对不变；本任务未修改 research，保留并行调研文件。

另执行了全仓库 `ruff check .` 和 `ruff format --check .`：未通过，原因仅为另一项研究中的 `research/experiments/ingestion/capture_pages.py` 与 `offline_checks.py`（21 项 lint，2 个文件需格式化）。这些独立研究脚本不属于本次业务代码，按协作边界保留不修改；上面的开发命令检查全部项目源代码与测试。

2026-10-02 分页证据阶段：上述 Python 3.12.14 环境，完整 pytest **315 项离线测试通过**；项目代码 Ruff、格式检查及 `git diff --check` 通过。覆盖四份真实列表、分页结构/数字/控制破坏及明确隐藏的证据、来源 URL 与自链、非 25 条/旧日期/全已知页面、离线入库及 CLI/reparse 输出、v1→v2 同摘要详情产物。核对全部 research、schema 和历史迁移的文件摘要不变，两份详情的完整规范化内容及内容摘要也与 v1 一致。没有联网、遍历网站、改变数据库结构或提交 Git。全仓库 Ruff 仍只有上段的 21 项研究脚本问题和 2 个格式问题；保留未改。

2026-10-02 HTTP 衔接状态阶段：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1，依赖未变。完整 pytest **375 项离线测试通过**（保留 315 项、新增 60 项）；`ruff check src tests`、`ruff format --check src tests`（35 个 Python 文件）与 `git diff --check` 通过。0002→0003 保留旧通知/版本/原文/响应，旧缓存资格未知；重复初始化与迁移异常回滚通过。测试覆盖 304 精确绑定及头冲突、丢失/损坏原文的完整获取结果、新 200 解析失败不回退、规则升级/获取时间保留、业务/资源/due/冷却组合回滚、重开库重建待办、完整覆盖与运行部分失败独立，以及所有 CLI 写入争锁。

两个真实 POSIX 子进程验证争锁拒绝、正常退出及 SIGTERM 后锁可再次获取；这仅验证 OS 锁释放，不是数据库进程崩溃恢复实验。数据库中断仍是正常进程内 SQL 触发器/异常注入与关闭重开；未验证真实网络、HTTP 条件头发送/回退、数据库 kill/断电或 Linux/Windows。核对 28 个 research/Parser/rawstore/0001/0002 文件摘要不变，保留之前分页节点修改。全仓库 Ruff 再次执行，仍只有前述研究脚本 21 项 lint 与 2 个格式问题，按边界未修改；没有提交、推送或创建 PR。

2026-10-03 有界 Fetcher 交付：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1、HTTPX 0.28.1，依赖未变。`uv sync --locked --offline`、完整 pytest **530 项通过**（原有 375 项、新增 155 项）、`ruff check src tests`、`ruff format --check src tests`（36 个 Python 文件）及 `git diff --check` 通过；CLI 帮助/示例配置检查仍正确。新增测试使用 MockTransport、自定义 SyncByteStream 和假时钟，连接真实归档/缓存/SQLite 服务：精确 profile/条件头、Cookie/Auth 排除、响应关闭、原始流上限/部分失败、唯一重试层、手动目标校验/循环/累计跳数、共同请求/运行预算、304 完整回退、最新失败原文重处理、原获取时间/新规则保留、完整 Retry-After、冷却写入故障及关闭重开库后的冷却。

没有真实 HTTP 请求、TLS/DNS/slow-drip 测量、整条采集协调器、数据库杀进程或断电实验；协作 deadline 不是硬期限，损坏归档不会自动覆盖。research/fixture、Parser、rawstore、schema、历史迁移和锁文件保持不变，无新增依赖；下一交付再连接扫描、详情与运行摘要。全仓库研究脚本问题保留，开发检查范围仍为 src/tests。

2026-10-04 单次采集交付：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1、HTTPX 0.28.1、pytest 8.4.2，依赖未变。`uv sync --locked --offline`、安装入口的采集帮助/示例配置校验、完整 pytest **601 项通过**（保留 530 项、新增 71 项）、`ruff check src tests`、`ruff format --check src tests`（40 个 Python 文件）及 `git diff --check` 通过。

新增离线验证将 MockTransport 连接实际 Fetcher、归档、缓存、Parser 和 SQLite：两个真实列表共 50 个身份、重复新增 0、完整链及首页复核、已知首页后的新通知、循环/跳页/漂移/预算中止、304 重处理失败原文及文件丢失后的原预算修复、旧成功首次复查、普通详情失败继续与版本保留、按到期时间推进待办、冷却跨运行保留、数据库/失败登记/运行收尾故障明确终止。列表尝试意图、实际发送起点和响应接收时间分别验证。

本交付已完成协调器与 CLI，未进行真实网站请求、数据库杀进程或断电实验；故障注入与关闭重开库不能替代这些实验。research/fixture、Parser、rawstore、schema、历史迁移、依赖及锁实现未改，无 Git 提交或推送。下一交付再做真正的终止/重启恢复验证及少量低频实采，当前在此节点停止。

2026-10-05 恢复验证交付：同一 macOS 26.6.2 / CPython 3.12.14 环境，`uv sync --locked --offline`、CLI 帮助及示例配置校验通过。完整 pytest **615 项通过**（保留 601 项，新增 9 项跨运行集成测试及 5 项真实 SIGKILL/新进程恢复测试）；`ruff check src tests`、`ruff format --check src tests`（43 个 Python 文件）和 `git diff --check` 通过。测试仍完全离线；终止实验运行真实协调器、归档与 SQLite，但 HTTP 为模拟，不等于断电验证。

随后临时目录三轮低频实采共 **9 次真实 GET**，最短请求间隔 5.006 秒，得到 25 个通知身份、4 个成功版本、5 个不同原文文件和 21 条待办。首页 304 的 Vary 变化要求完整回退，第二轮达到请求预算后明确中断，第三轮在同一库继续；没有重复身份或版本，也没有误报完整扫描成功。结果、有限样本与实验边界见 [恢复验证记录](docs/recovery-validation.md) 和 [实采摘要](docs/validation/2026-10-05-live.json)。本交付未改生产代码，核对 32 个 research/Parser/schema/迁移/依赖文件摘要不变；无提交、推送或 PR。本轮三个交付全部完成，在此停止，尚未开展定时运行、邮件或长期观察。

2026-10-05 定时运行与诊断节点：同一 macOS / Python 3.12.14 环境，完整 pytest **715 项离线测试通过**（既有 615 项、新增 100 项：处理政策 51、状态/只读 CLI 28、配置/定时入口/日志 12、备份恢复 9）。三项旧恢复场景明确将无关首次待办延期，以继续测量成功基线的失败/回滚边界，不依赖已替换的全局 due 排序。连续十轮饱和批次每轮实际尝试 12/4/4；成功/due/资源标记回滚、政策升级不推迟逾期、304 当前处理时间、缺失/损坏原文恢复校验均通过。

`uv sync --locked --offline`、新命令帮助及示例配置校验、Ruff 与格式检查（src/tests 和 deploy/verify_backup.py，共 51 个 Python 文件）、`git diff --check` 通过。核对 33 个 research/fixture/Parser/schema/迁移/依赖文件摘要不变；未新增依赖或 schema。Linux/systemd 252+ 模板仅核对配置及官方语义，未在目标机启动、未启用定时任务，未进行新校园请求或断电实验；这些仍需部署后的验证与观察。交付处理政策、只读诊断、有限定时模板及独立日志/备份恢复说明后停止，无提交、推送或 PR。

2026-10-06 N0 与 N1 接口稿节点：macOS 26.6.2 arm64、CPython 3.12.14、Pydantic 2.13.5、SQLite 3.53.1、pytest 8.4.2，使用现有虚拟环境，未新增依赖或改锁文件。完整 `.venv/bin/pytest -q` **906 项离线测试通过**（既有 715 项，新增事实 90、决策 59、Profile/CLI 42）；`.venv/bin/ruff check src tests deploy/verify_backup.py`、格式检查（59 个 Python 文件）及 `git diff --check` 通过。

验证五份真实通知的事实/未知项、不同画像、短截止与日期边界、媒体/OR/否定/冲突、额外编号/年龄/处分条件、不支持的时刻/时区与未解决截止声明、首启路线例外、显式更新比较、版本口径校验、原文位置和跨子进程 hash seed 的完整决策确定性。安装入口的 profile-check 与真实 HTML decision-preview 在临时工作目录通过，不创建文件/数据库或获取锁；错误输入与当前 Parser 不支持的竞赛页面明确非零退出。未进行新网站/SMTP 请求、通知数据库迁移或 N1 事务实验；N1 只有明确标注的接口稿。Parser、schema、历史迁移、入库和依赖文件摘要不变；本实现未修改 research，保留并行 Agent 对三份通知/邮件报告的更新与新调研文件。无 Git 提交、推送或 PR，N1 持久化与 N2–N4 留后续交付。

2026-10-07 N1 启用与事件持久化节点：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1、Pydantic 2.13.5、pytest 8.4.2、Ruff 0.16.9，使用现有虚拟环境，依赖与锁文件不变。完整 `.venv/bin/pytest -q` **999 项离线测试通过**（原有 906 项，新增状态/升级 16、服务 38、CLI 37、生产协调器接入 2）。Ruff 检查、格式检查（66 个 Python 文件）及 `git diff --check` 通过。

验证启用重放和稳定身份、真实日期/冲突/延期候选、独立 live 基线、重复 200/304、A→B→A、离线和维护隔离、Parser 升级与比较未知追溯、事务外文件/Parser/N0、事件/决策/意图故障整体回滚、重开数据库、0003 升级保留和迁移失败回滚。生产协调器使用模拟 HTTP，直接连接真实归档/缓存/Parser/SQLite；通知登记故障明确终止，移除故障后绑定 304 可恢复。安装入口从不同工作目录在临时实例查看帮助、初始化、读取状态及预览通过，未满足完整扫描条件时明确返回 blocker。逐字节比较 69 份受保护的已跟踪文件：research/fixture、Parser、原契约/N0 规则、0001–0003、pyproject.toml 与 uv.lock 均未改变。

本轮没有新的网站或 SMTP 请求，不对个人实例自动启用。N1 新验证属于模拟 HTTP、故障注入及重开库，不是 N1 真实进程终止或断电实验。立即路线只有 planned 意图，Digest 只有登记资格；N2 冻结计划、N3 SMTP、N4 发送/政策更新与重评待后续交付。没有提交、推送或 PR。


2026-10-07 N2 邮件计划与冻结节点：同一 macOS 26.6.2 arm64 / CPython 3.12.14 / SQLite 3.53.1 环境，无新增依赖。完整 pytest **1112 项离线测试通过**（原有 999，新增渲染 37、计划/约束 44、CLI 28、迁移 4）；Ruff 检查、格式检查（75 个 Python 文件）及 git diff --check 通过。

实际 SQLite 验证重复/排他分配、数量和完整 MIME 字节分片、101 项边界、250 项以上积压、到期和停机补计划、待核对分组、冻结后正文/Profile/地址变化、过期/修改准备拒绝、第三条写入及诊断登记故障整体回滚、损坏检测与重开库。0004 升级保留真实 N1 事件/意图/原文，三个 DDL 故障位置回滚，升级不自动计划。安装入口从不同工作目录在新临时实例完成预览、首次一封/重复零封及保存文本/摘要一致；帮助和配置检查不创建存储。70 份 research/fixture、Parser、原契约/N0、0001–0004 与依赖文件逐字节不变。

N2 本批全部完成，仅有 pending 冻结邮件，不连接 SMTP，不发送、不自动调度计划；故障注入/重开库不称为新进程终止或断电实验。N3 可直接消费 load_frozen_mail 的地址和完整 bytes，返回有限发送结果；尝试登记、发送重试与恢复留 N4。没有提交、推送或 PR。详细能力与限制见 [N2 说明](docs/mail-planning.md)。


2026-10-07 N3 SMTP 适配器节点：macOS 26.6.2 arm64、CPython 3.12.14、OpenSSL 3.5.8、SQLite 3.53.1，依赖和锁文件不变。全套 **1237 项测试通过**（原有 N2 的 1112 项，新增配置 54、适配器/契约 36、实际 SMTP 协议 29、真实本地 TLS 6）。Ruff、格式检查（80 个 Python 文件）和 git diff --check 通过。

验证连接失败、单机制认证拒绝、MAIL/RCPT/DATA 明确 4xx/5xx、正文保存后断线/超时、异常/过长最终回复、最终 250 后 QUIT/close 失败、凭据缺失/非法、损坏冻结输入、无重渲染及有限结果。真实 N2 归档/Parser/SQLite 冻结邮件也通过本地服务的精确字节验证。两种 TLS 的真实握手与证书/hostname 拒绝均通过，失败不发送 AUTH/MAIL；另有透明 wrapper 和故障注入测试，明确区分验证层级。

从不同工作目录执行安装入口的帮助和带 SMTP 的配置检查通过，无凭据仍可校验，不创建存储；模块导入和配置加载不读凭据、不构造 SMTP 或 SSL context。70 份已跟踪的 research/fixture、Parser、原契约/N0、0001–0004 与依赖文件逐字节未变；保留 N2 未提交工作，N3 未新增数据库迁移。没有外部 SMTP/校园请求、真实账号投递或新终止/断电实验，没有提交、推送或 PR。N3 本批完成，N4 发送登记、诊断与恢复仍待交付。

配置与交接接口见 [N3 说明](docs/smtp.md)。


2026-10-08 N4 发送协调/诊断/恢复及政策维护交付：相同 macOS 26.6.2 arm64 / CPython 3.12.14 / SQLite 3.53.1 / OpenSSL 3.5.8 环境，依赖不变。全套 **1435 项离线测试通过**（保留 N3 1237 项，新增 N4 198 项）。Ruff 检查、格式检查（src/tests 与备份校验程序共 90 文件）和 git diff --check 通过。

验证真实 SQLite 的尝试/结果回滚、系统性错误停止、六次自动/一次人工许可、due/未知冷却、暂停、积压、有界诊断及政策操作恢复；真实 0005 升级保留冻结邮件/成员和全部旧事实，五个迁移故障点完整回滚。7 个真实 SIGKILL/新进程恢复场景连接父进程 SMTP 模拟服务独立接受记录，证实未知窗口可能重复、已提交 accepted 不重发；不冒充断电或公网投递验证。完整测试范围与接口见 [N4 说明](docs/mail-sending.md)。

安装入口在不同工作目录的帮助和配置检查通过，不创建存储；无 SMTP 的 drain 返回 2。75 份已跟踪的 research/fixture、Parser、原契约/N0/N1、rawstore/ingestion、0001–0004 与依赖文件逐字节未变；0005 未修改，保留既有 N2/N3 工作。未进行真实 SMTP/校园请求、真实账户发送、邮件定时部署或 Git 提交/推送/PR。N4 本批已经完成；既有计划只定义 N0–N4，N5 范围待明确。


2026-10-08 政策 v2、后台邮件及邮件备份节点：同一 macOS 26.6.2 arm64 / CPython 3.12.14 / SQLite 3.53.1 / OpenSSL 3.5.8 环境，依赖和锁文件不变。最终完整 `.venv/bin/pytest -q --tb=short` **1578 项测试通过**；Ruff 检查、格式检查（src/tests 与备份程序共 98 文件）和 git diff --check 通过。

真实通知样本验证跨主题优先级、同句年份继承/24:00、项目团队与学院期限区分、日前紧迫下界及精度未知、教师申报主体与学生受益者区分、竞赛正文及分赛道期限未知。Parser 升至 whu-student-notices-v3，事实/规则/引擎升至 v2，路线仍 v1；旧模板正文与摘要规则不变。保存的真实 v1 manifest 与一致的 v1 序列化样本连接实际归档/SQLite，验证 ID-only 操作恢复、显式政策升级、旧决策和冻结邮件兼容；没有假称执行旧规则引擎或使用外部历史数据库。

后台集成连接实际采集/通知/计划/发送服务，验证自动分批积压、普通采集失败/单项阻断继续已有邮件、系统错误停止、暂停及有限 CLI 错误。新增 5 项真实子进程验证争锁/释放、环境凭据被 SMTP 适配器读取、supervisor 超时终止及暂停恢复；SMTP 为本地 socketpair，进程用例的 TLS wrapper 透明，既有 N3 真实本地 TLS 测试仍通过。不是实际 systemd 或公网邮件验证。

邮件备份及旧备份回归共 60 项通过：真实 SQLite backup API 与 raw 复制保留 pending/accepted/sending/uncertain/暂停、冻结 bytes/成员/尝试；只读校验拒绝 MIME、关联、304 基线、数值类型或状态损坏，恢复副本先暂停，遗留 sending 一次变未知，accepted 不重发，原实例不受影响。

安装入口从不同临时工作目录执行后台帮助、示例配置/画像校验、disabled 邮件及竞赛预览均成功且无存储创建；enabled 缺 SMTP 返回 2。68 份已跟踪 research/fixture、原契约、rawstore/ingestion/crawling、0001–0004 和依赖文件与 HEAD 逐字节一致；0005/0006 沿用既有 N4 文件，本轮没有新增迁移或修改它们。保留已有未提交工作，未进行 Git 提交、推送或 PR。

本轮三项实现已经完成。后台默认关闭，已有实例政策须显式更新，部署须先核对配置和冻结邮件；目标 Linux 的 unit/calendar 验证、EnvironmentFile 加载、实际服务超时及真实账户投递仍待受控验证。本轮没有新的校园/公网 SMTP 请求，没有安装或启用生产服务，不宣称断电耐久性、长期运行或人工标注准确率。

2026-10-08 政策 v3 机会识别修正：同一 macOS 26.6.2 arm64 / CPython 3.12.14 环境，完整 `.venv/bin/pytest -q --tb=short` **1637 项通过**（保留 1578 项，新增主题上下文 47 项、生产评估 12 项）。Ruff 检查与格式检查覆盖 src/tests 和两个部署辅助程序，共 101 个 Python 文件；`git diff --check` 通过。

真实选课负例、辅修报名正例、科研跨主题优先级，以及局部收费/导航/历史/否定、逐次出现、已跟踪关闭/资格收紧、v1/v2 事实摘要兼容通过。生产入口离线重放 8 份真实原文的 13 个固定情境，修正 3 个误入 Digest 的情境，保留原有 5 个立即路线；修复前后原文、画像、上下文、时钟和正文摘要一致，Parser 源码摘要不变。[评估文件](docs/validation/notification-production.md)保留实际版本、源码摘要、工程期望和未知项，不是人工准确率评估。

安装入口从临时工作目录预览上述三个真实负/正/跨主题样本，分别得到 STORE_ONLY/none、PUSH_NOW/immediate、PUSH_NOW/immediate，均保留待核对且未创建存储。研究/fixture、Parser、schema、历史迁移与依赖保持本轮开始时的内容；既有未提交工作保留。未联网、发送邮件、更新个人实例政策或提交 Git。v3 规则有限，标题主题仍可表示普通相关信息，显式字面关注仍可匹配顺带提及；旧 Digest 资格与冻结邮件不自动撤回，已有实例须按[升级步骤](docs/notifications.md#已有实例显式升级政策)显式启用新政策。

2026-10-08 新迭代 N0 规则对齐节点：同一 macOS 26.6.2 arm64 / CPython 3.12.14 环境，完整 `.venv/bin/pytest -q --tb=short` **1683 项通过**（原 1637 项，新增 v4 回归 37 项、评估检查 9 项）。Ruff、格式检查覆盖 102 个项目/测试/部署 Python 文件，`git diff --check` 通过。未新增依赖或迁移。

政策 v4 修复标题/正文行动断开及登录提交指令漏报，保留菜单/历史/缴费/退课负例、科研/辅修优先级及已跟踪变化提醒；不确定关系保留原位置证据，不能借另一项明确机会的报名或截止升级。原 13 个真实情境全部符合原工程期望，新增两个明确开放且当天截止的合成情境均 PUSH_NOW/deadline_soon，分别保存 v3 基线与 v4 当前快照。当前快照与实际源码/输入/决策强一致性、v1/v2/v3 旧摘要及旧决定读取通过，旧冻结邮件不改写。研究 10 个 Action 已人工确认，适配后的两项 STORE_ONLY/IGNORE 差异独立留待确认，不再表述为全体标签未确认。

安装入口从另一临时工作目录预览两个最小情境均得到立即路线，没有创建存储。74 份既有研究/Parser/存储/schema/迁移/依赖文件与本轮起点逐字节一致；N1 并行研究另增三份官方完整 HTML、获取元数据和报告，未改旧证据。有限官网 GET 仅用于新助教样本研究，未接入生产来源；N0 测试与评估仍离线。本轮没有更新个人政策、部署定时器、发送真实邮件或提交 Git。N0 已完成，N1 仅完成样本准备，N2/N3 仍待后续交付。

2026-10-08 新迭代 N1 离线助教节点：同一 macOS 26.6.2 arm64 / CPython 3.12.14 环境，完整 `.venv/bin/python -m pytest -q --tb=short` **1806 项通过**（90.74 秒；新增 123 项），Ruff 与格式检查覆盖 107 个 Python 文件，`git diff --check` 通过。未增加依赖或迁移。新增 EMS 详情 Parser 48 项、招聘规则 48 项、CLI 15 项、归档/邮件预览集成 3 项、评估 8 项与实际 v4 seal 兼容 1 项。

真实历史助教招聘在 2024 截止当天 PUSH_NOW/deadline_soon，并保留软资格、院属、学籍、教师意见与附件未知；2026 判断为已关闭而不提醒。两个官方介绍负例、历史/津贴/考核/菜单/未开放、未知时间、跨主题优先级及真正申请旁带工资说明均覆盖。旧 13 例与 N0 两例继续通过；另存 6 个助教/跨主题工程情境，不称人工准确率。升级前实际 v4 报告与完整 seal 保留，旧决策/冻结邮件不静默改写。

真实 EMS 原文保留获取元数据的 2026 时间，验证归档、重复导入、维护重新解析和重开 SQLite；离线不创建 live 事件或邮件任务。显式 2024 回放单独验证确定的纯文本 MIME 预览，不冒充生产冻结或投递。安装入口从另一临时目录完成画像检查、帮助及历史/当前预览，没有创建存储；88 份研究/fixture、本科生院 Parser、核心存储/获取/schema/迁移范围及依赖受保护文件摘要未变，既有工作保留。本轮完全离线，没有修改个人政策、启用定时器、发送真实邮件或提交 Git。

**本轮 N1 离线交付已完成，当期助教自动发现仍未验收**：EMS 当前正文匿名请求的登录限制沿用既有研究证据，未在本轮重新请求。下一项来源适配需公开当期正文、可靠列表分页与单来源实例边界的独立验证；不能仅放宽主机白名单。N2 部署及一周观察、N3 搜索尚未开展。

2026-10-09 新迭代 N2 准备 / N3 搜索：macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1、uv 0.12.20，全部 **1950 项离线测试通过**（108.08 秒，新增 144 项）。Ruff/格式覆盖 src、tests、deploy 共 119 个 Python 文件，差异检查通过；锁定依赖离线同步通过。覆盖真实归档/Parser/SQLite 搜索、过滤、增量/A→B→A/重建、包含通知意图的事务回滚、旧库升级、只读 CLI、发布确定性/FIFO 子进程及备份索引结构校验。真实语料生产回放和另一临时目录 CLI 演示通过。research/fixture、原 Parser、政策 v5、0001–0006 与依赖文件未改；未提交 Git。N2 远端仅只读核验，未安装 Linux、启用 timer、校园实采、发送邮件或开始一周观察。详见[本轮验证](docs/validation/n2n3-20261009.md)。

2026-10-10 未适配引用节点已完成：Parser v4、迁移 0008、整页引用登记与完整扫描证据已接通，新增只读 references-list 和独立待适配计数；外部正文不抓取。最终 **2038 项测试通过**（132.08 秒，新增 65 项），Ruff/格式检查覆盖 123 文件，差异检查通过。研究/fixture 和旧迁移保留；通知政策 v6 评估与搜索结果保持一致，旧快照保留。真实第 6/7 页离线导入为 48 个本站通知和 2 个引用，未进行远端升级或实采。详见[本轮验证](docs/validation/list-references-20261010.md)。

2026-10-10 原始漏报补完：政策 v8，实际环境仍为 macOS 26.6.2 arm64、CPython 3.12.14、SQLite 3.53.1。完整 **2242 项离线测试通过**（116.20 秒，保留原 2201 项、增加 41 项），Ruff/格式覆盖 129 个 Python 文件。两条原样行动表达的实际 v7 DIGEST/IGNORE 失败已冻结，同一输入在 v8 均 PUSH_NOW；原 13 例、助教/CS 与 Q/R 回归保持。六组31情境重新评估，实际 v7 报告和全量政策 seal 保留，安装入口在仓库外预览通过且无存储副作用。未改研究/fixture、Parser、数据库迁移或依赖；未执行 WSL 升级、实采、实例政策更新或邮件，见[完整记录](docs/validation/notification-original-misses-20261010.md)。本轮第一项已完成，发布与目标升级为下一个交付。

较早发布但仍开放的通知可用 `notifications-review-open --config signalnest.toml --at <UTC秒> --preview` 本地回顾；默认只读。明确选择通知后才用 `--apply` 登记，模型仅在预览显式提供 `--model-config ask.toml` 时调用。完整命令、来源证据和迁移边界见[说明](docs/open-opportunity-review.md)。

0009为机会回顾新增操作表并扩展事件约束，保留已有事件/循环外键和冻结邮件，不自动补发。首次启用验收应同时检查“近期发布候选”和“仍开放机会回顾”；空邮件队列不证明没有漏掉机会。生产升级和登记仍是显式操作。
