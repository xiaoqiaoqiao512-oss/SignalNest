# WSL 内的 CodeBuddy2API 网关

这一步为 SignalNest 的只读查询提供本机 Chat Completions 接口。网关与 SignalNest 分开运行；部署网关不会迁移生产数据库、修改通知政策，或让后台邮件自动使用模型。

采用 [codebuddy2api 官方项目](https://github.com/orangeboyChen/codebuddy2api)的固定发布镜像及下述 Bun 运行时修复，部署配置为 [Compose 模板](../deploy/gateway/codebuddy2api.compose.yml)。模板要求先验证并导入镜像，再将实际本地镜像 ID 写入外部配置；不自动拉取 `latest`。构建源码的失败与采用官方发布镜像的记录分别保留，不能将两者当成同一次成功构建。

## 位置与访问

| 内容 | 位置 |
| --- | --- |
| Compose 配置 | `/opt/codebuddy2api-wsl/compose.yml` |
| 固定镜像 ID | `/etc/codebuddy2api-wsl/deployment.env` |
| 存储加密口令 | `/etc/codebuddy2api-wsl/storage.env`，root `0600` |
| SQLite 与其他持久资料 | `/var/lib/codebuddy2api-wsl/data` |
| 凭据目录 | `/var/lib/codebuddy2api-wsl/credentials` |
| WSL 查询接口 | `http://127.0.0.1:8002/v1` |
| 管理页面 | `http://127.0.0.1:8002/dashboard` |
| 存活检查 | `http://127.0.0.1:8002/health` |

容器内部监听 8001；主机只发布 **127.0.0.1:8002**。原 Windows 8001 服务独立保留。回环地址指进程自己的网络环境；须分别验证 WSL 查询进程和 Windows 浏览器，不能只凭其中一侧可达便宣称已接通。没有向局域网开放端口；回环绑定也不能代替本机用户及 Docker 网络内部的鉴权。

`--env-file deployment.env` 供 Compose 解析镜像 ID；服务的 `env_file: storage.env` 将加密口令提供给容器。不要打印完整解析后的 Compose、容器环境或秘密文件。检查配置使用 `docker compose ... config --quiet`。

## 首次登录

在部署机器的浏览器打开管理页面：

1. 按页面建立管理员账号与密码。
2. 打开 Credentials，点击 Start authentication，使用自己的 CodeBuddy 账号在授权页面登录。
3. 授权返回后确认凭据可用；创建名为 SignalNest 的 Access Key，绑定这份凭据。
4. 将客户端密钥保存到查询账号自己的私有文件中，不放进 TOML、聊天、命令参数或仓库。旧 Windows 网关的密钥不会自动适用于这个新实例。

新实例尚未建立 Access Key 时，项目按设计允许无密钥的 `/v1/*`。因此首次设置期间继续保持本机绑定。创建后还须验证未鉴权请求被拒绝、带密钥的模型列表可用，再测试实际模型和工具调用。`/health` 为 healthy 只证明服务和存储可用，不能证明账号、额度或模型能力。

SignalNest 的密钥文件须由**实际执行查询的账号**拥有，权限为 `0400` 或 `0600`，各级父目录不得为不可信账号所有或可被其他账号写入，不允许符号链接。root 创建的 `0600` 文件不能直接供普通查询账号使用。通过 `deploy/ask_saved.py --api-key-file` 仅在一次真实查询期间注入 `SIGNALNEST_MODEL_API_KEY`；配置检查、帮助和本地搜索不读密钥。

## SignalNest 配置与验收

在准备好的隔离查询快照旁添加模型配置，保留原有来源绑定与画像：

```toml
[model]
provider = "openai-compatible-chat-completions"
base_url = "http://127.0.0.1:8002/v1"
model = "EXACT_API_MODEL_ID"
api_key_env = "SIGNALNEST_MODEL_API_KEY"
timeout_seconds = 20.0
max_attempts = 1
max_completion_tokens = 2048
token_limit_parameter = "max_tokens"
pricing_mode = "unpriced_proxy"
```

在新容器中须通过已授权模型列表和真实请求核对精确 API 标识；不能沿用旧网关成功记录。不要改成显示名称或 `auto`。价格未知时保持费用未知，使用请求、时间和输出上限。查询会将有限问题、画像与通知片段交给选定模型链路。

后续依次验收：WSL 本机访问、管理登录、无密钥拒绝、授权模型列表、一次短生成、工具调用往返、真实知识库有引用回答。每一步单独记录，不将 HTTP 200 当成回答质量验收。安装和 `0008 -> 0009` **副本**准备见 [WSL 查询接入](wsl-query-setup.md)；不对生产执行迁移。后台通知的语义判断仍是另一项待接入功能。

## 重启、日志与备份

本部署使用原生 Ubuntu Docker Engine，非 Docker Desktop；服务由 systemd 启动。容器使用 `restart: unless-stopped`，Docker 日志限制为每份 10 MiB、最多三份；它不会启动已停止的 WSL，也不能保证 Windows 睡眠、注销或断电期间运行。仍需既有 Windows 保活与独立状态观察。不要再并行启用另一套 Docker Desktop 来管理相同实例。

持久目录按镜像实际运行 UID 配置，不能仅假定所有镜像的 `bun` 都是同一个 UID。SQLite 后端使用项目的加密文档存储；这不是整个 SQLite 文件的磁盘加密，管理员配置和其他元数据不能因此被当作秘密密文。持久存储口令与客户端 API Key 是两类不同秘密。

备份应在显式维护窗口停止网关后，以受限权限保存整个 data、credentials 和存储口令，随后启动原固定镜像；不要只复制活动 SQLite 主文件。密文恢复同时依赖口令和数据库中的 KDF salt，缺任一项均不可恢复。恢复副本先保持网络隔离，防止两实例同时使用同一上游账号。不要读旧版本无法解密的新格式库来做代码降级；升级前的一致备份才是回退依据。本任务不自动复制现有 SMTP 或 Windows 登录凭据。

## 本次验证发现的启动修复

官方应用 `1.4.2` 镜像使用 Bun `1.3.14` 与 better-sqlite3 `13.0.3`。在目标 WSL 上，原镜像启动 Next 后发生 `NAPI FATAL ERROR` 并重启；权限检查正常。Bun [1.4.1 官方发布说明](https://bun.com/blog/bun-v1.4.1#nodejs-compatibility-improvements)明确记录对此版本 SQLite 原生模块加载崩溃的修复。

本次用 [Dockerfile.bun-fix](../deploy/gateway/Dockerfile.bun-fix)在已校验的官方应用镜像上只覆盖 `/usr/local/bin/bun`：固定官方稳定版 `1.4.1` 的 Linux x64 baseline 资产，校验发布资产摘要与解压后二进制摘要。构建不联网，不重装应用依赖；核对派生镜像保留原应用所有层、启动配置和非 root 用户，另用受限无网络容器验证内存 SQLite `SELECT 1`。修复后再验证实际加密存储和 HTTP 健康。禁止 core dump，避免崩溃时留下可能含内存资料的大文件。

这是一份**本地派生镜像**，不能称为上游原镜像已直接运行成功。部署时应记录本地固定镜像 ID 和验证结果；本页说明可复用的配置与安全边界。后续升级应重新验证运行时兼容性，不能只替换浮动标签。


当前接入已完成基础鉴权、模型目录和工具交接验证。后台通知仍沿用既有规则，不会因网关可用就自动调用模型；查询只读静态快照，人工质量评价仍待确认。脱敏状态见[上线摘要](validation/rollout-summary-20261021.md)。
