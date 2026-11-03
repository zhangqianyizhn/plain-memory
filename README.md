# PlainMemory

用于 AML 文本赛道接入的最小记忆服务。同步 Add；Search 在请求 user_id 的语料快照内执行余弦向量、BM25 和 LLM 规划的有序正则搜索，按“向量、BM25、正则”逐名次交替去重。LLM 不可用时恢复原两路检索。Search 返回原文记忆证据，不生成最终答案。

当前版本：`0.2.0`，用于本地自测与 Smoke 准备。Embedding 固定为阿里云百炼 `text-embedding-v4`（1024 维），生成模型固定为 NoneLinear `gpt-4o-mini`。只有一份应用配置 `config.local.yaml`，不读取其他项目的 `.env`。没有使用正式评测数据进行开发。

## 本机 Mac 启动

需要 Python 3.11+ 与 uv。macOS arm64 和 Linux amd64 使用相同源码与配置格式。Mac 自带 Python 3.9 不适用，可用 uv 管理独立解释器。

```bash
cd ~/Desktop/plain-memory
uv sync --frozen --python 3.13
uv run --frozen python scripts/init_config.py
```

依赖锁使用阿里云 PyPI 镜像；运行命令使用 `--frozen` 保持版本与锁一致，不隐式重新选择依赖。

`init_config.py` 从代码默认值直接生成权限为 0600 的 `config.local.yaml`，自动创建服务 Bearer 密钥，不依赖配置模板，也不覆盖已有文件。编辑这一个文件，填写 `embedding.api_key`（北京地域百炼）和 `llm.api_key`（NoneLinear）；首次生成配置时还需将 `embedding.base_url` 中的 `YOUR_WORKSPACE_ID` 替换为业务空间 ID。已经准备好的本机配置保留实际工作空间地址。不需要在聊天中提供 key。

填写后统一检查两个模型：

```bash
uv run --frozen plain-memory --config config.local.yaml check-models --config-only
uv run --frozen plain-memory --config config.local.yaml check-models
```

前一条只检查本地配置；后一条发起一次合成文本 Embedding 和一次简短 NoneLinear 推理，不读评测数据。任一模型失败即退出，不自动切换供应商或模型。

```bash
uv run --frozen plain-memory --config config.local.yaml serve
```

默认只监听 `127.0.0.1:8000`，单 worker。无鉴权 `GET /health`、`GET /`、`GET /add`、`GET /search` 是轻量本地就绪检查，不承诺 embedding 供应商始终在线。POST /add 与 POST /search 均要求：

```http
Authorization: Bearer <config.local.yaml 中的 api_token>
```

服务密钥不是 AML 的 Eval Key，也不是 embedding API key。不要将密钥放在 URL 中。

## 统一模型配置

`config.local.yaml` 的 `embedding` 使用百炼北京工作空间根地址 `https://YOUR_WORKSPACE_ID.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`，代码固定追加 `/embeddings`。鉴权为 `Authorization: Bearer <embedding.api_key>`。请求使用字符串数组 `input`、`dimensions: 1024`、`encoding_format: float`，每个切片得到独立向量，按响应 `index` 对齐。没有 `provider` 或 `protocol` 选择项。

百炼 v4 每批最多 10 条、每条最多 8192 模型 token；`batch_size` 默认为 10，配置校验拒绝超过 10 的值。维度 1024 是官方默认值；若修改维度，应使用新数据库。所有批次在同一次 Add 请求内完成后才提交数据库。切片计数器与供应商 tokenizer 不同；供应商拒绝超长问题时明确失败，不静默截断。接口规则见[官方同步向量 API 文档](https://help.aliyun.com/zh/model-studio/text-embedding-synchronous-api)。

同一个文件的 `llm` 固定为 `https://api.nonelinear.com.cn/v1` 和 `gpt-4o-mini`，调用 `/chat/completions`，Bearer 鉴权，`stream: false`，默认 `temperature: 0`、`max_tokens: 2048`、超时 180 秒；若调用方传入更短的 deadline，仍使用剩余预算。调用方式与另一会话已验证的 NoneLinear 接入一致，使用现有 httpx 直接请求，无额外 SDK 依赖。接口见[NoneLinear 官方说明](https://docs.nonelinear.com.cn/api/chat-openai)。空回答、输出截断、返回不同模型或上游错误会明确失败，不记录请求正文或上游错误正文。当前 Add/Search 的 120/30 秒截止时间保持不变；Search 的正则规划使用更短的阶段预算，不能占用完整 180 秒。总超时会取消网络请求，不遗留后台推理线程。

v0.2 在 Search 中调用 LLM 生成有序正则，仅发送 query 与 options，不把整个用户语料发送给生成模型。正则在本地的目标用户语料中执行，返回内容仍来自原始切片；Add 不调用生成模型，不做事实抽取或摘要。检索效果需要通过下一轮官方 Smoke 对照验证，平台仍负责最终 Answer。

改变 embedding base_url、维度或切片配置时指定新数据库，例如 `data/text-embedding-v4.sqlite3`。服务拒绝把不同配置混入旧数据库，不会默默清空旧数据或重建。默认 512 token 切片、64 token 重叠，cl100k_base 计数；中文/emoji 字符边界会小幅调整。该计数器不等同于供应商的 tokenizer。

相对 database 路径基于配置文件所在目录。`config.local.yaml`、数据文件、日志与 `.venv` 均在 `.gitignore` 中；公开仓库用初始化脚本代替多份模型配置模板。Cloudflare 隧道的部署配置及凭证独立于应用模型配置。

## 接口契约

Add 必填 `request_id`、`user_id`、`session_id`、`messages`；message 包含 `role`（user/assistant）、字符串 content、可选 Unix 毫秒 timestamp。按数组顺序处理，不按时间重排。

```json
{
  "request_id": "synthetic-request-1",
  "user_id": "synthetic-user-1",
  "session_id": "synthetic-session-1",
  "messages": [{"role": "user", "content": "My pet is named Orchid.", "timestamp": 1704067200000}]
}
```

成功响应严格回显：

```json
{"success": true, "request_id": "synthetic-request-1", "user_id": "synthetic-user-1", "session_id": "synthetic-session-1"}
```

先编码全部切片，再在一个 SQLite WAL/FULL 事务里保存原始消息、向量、词项与请求完成记录。提交后才返回成功，不依赖后台任务。相同 user_id/request_id 的相同请求直接返回成功；相同标识但内容或 session 不同返回 422。空白消息持久化但不生成空白检索结果。

Search 必填 query、user_id、top_k；可选 options 为字符串数组。选择题把 query 与 options 按顺序用于三路检索，不回答选择题。

```json
{"query": "What is my pet's name?", "user_id": "synthetic-user-1", "top_k": 100}
```

响应为 `{"data": [...]}`；条目包含稳定 id、非空 content、created_at，省略可选 score。content 带原 role 与存在时的源时间标签，随后是原文片段。无用户数据返回 `{"data": []}`。

先隔离 user_id，再计算向量和 BM25，包括 BM25 的 IDF。session_id 只是来源信息，Search 不包含它，因此同 user 可跨 session；不同 user/不同运行不能共享。Bearer 服务密钥供单一受信任 AML 调用者访问本部署，不是面向多个客户的权限系统。

## 有序正则检索与降级

在唯一的 `config.local.yaml` 中配置：

```yaml
regex_search:
  enabled: true
  max_patterns: 5
  timeout_seconds: 18
  attempt_timeout_seconds: 8
  max_attempts: 2
  match_timeout_seconds: 0.5
```

正常三路模式中，x 为有效且去重后有序正则的个数（1–5）。每个正则按切片原始顺序取 `floor(top_k / (3*x))` 个匹配；各正则结果按表达式优先级轮流去重，正则路至多 `floor(top_k/3)` 条。向量路与 BM25 路也各至多 `floor(top_k/3)` 条，最终按 `V1, B1, R1, V2, B2, R2, ...` 去重返回。按稳定切片 id 去重，不合并内容相同但来源不同的记忆；名额不足或重复时不跨路补足。

例如 `top_k=100, x=5`：每个正则最多 6 条，正则路最多 30 条；向量和 BM25 各最多 33 条，所以去重前最多 96 条。结果少于 top_k 是约定行为。top_k 小于 3 时不调用 LLM，使用原两路检索以保留小 top_k 的有效结果。

LLM 默认最多尝试 2 次、每次最多 8 秒、阶段总预算 18 秒；瞬时网络错误、超时、限流、临时上游错误或非法计划可重试，鉴权等永久错误立即降级。退避也计入阶段预算。总预算受 Search 剩余时间约束，并预留至少 1 秒用于最终混排；网络超时会取消本次请求。表达式最多 256 字符，严格解析 JSON 并检查编译、空匹配，实际匹配既有单次时间限制也有 0.5 秒总预算。

关闭该模式、LLM 未配置、规划最终失败、剩余预算不足或正则执行超时，均恢复原两路各取 top_k 后混排去重，至多返回 top_k 条。合法正则没有匹配时仍保留三路名额，不视作模型不可用。模型故障不会单独导致 Search 503；Embedding 或数据库故障继续按原接口处理。

诊断元数据记录 `regex_search` 的状态、尝试次数、耗时、表达式数量与哈希、每个表达式命中数，以及最终 `fusion_mode`。正则字符串仅在限时私密正文捕获启用时保存。不同 user 和 run 不共享计划、缓存或匹配结果。

默认并发上限 Add=16、Search=16；过载返回 429 和 `Retry-After: 1`。请求体默认最大 16 MiB，超限返回 413，不静默截断。非法输入 422，鉴权 401，临时 embedding/数据库故障 503。验证错误与上游错误不回显评测正文或上游响应。

Add/Search 的处理预算为 120/30 秒，HTTP 上游调用共享剩余预算，写入前再次检查；这是防止超时请求提交的保护，不是已验证的吞吐/延迟承诺。极端网络慢传输的实际返回时间仍受 HTTP 读取超时控制。正式部署容量需使用真实模型实测。

## 本地测试

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
```

如果只运行本地进程内测试、不监听端口：`uv run --frozen pytest -q --ignore=tests/test_cli_http.py`。当前线上服务已由用户迁移到服务器，本地代码通过测试不代表服务器已经升级。

单元/契约测试只使用合成向量，不消耗你的 API key，也不等于官方 AML Smoke。生产服务没有 fake 模型配置选项。

配置 key 并启动服务后，在另一个终端运行真实模型自测：

```bash
uv run --frozen python scripts/smoke.py --config config.local.yaml
```

脚本创建独立合成 user_id，检查健康、401、Add 后立即 Search、重复 Add 和用户隔离；不打印凭证或文本。可用 `--base-url https://你的域名` 检查公网链路。

## Mac 公网入口

推荐 Cloudflare 固定命名 Tunnel：Mac 无需公网 IP 或入站端口，隧道提供 HTTPS。需要你拥有可接入 Cloudflare 的域名并登录自己的账号。参考 [Cloudflare Tunnel 文档](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)。

```bash
brew install cloudflared
cloudflared tunnel login
cloudflared tunnel create plain-memory
cp deploy/cloudflared.example.yml deploy/cloudflared.local.yml
```

填入刚生成的 tunnel UUID、credentials-file 和自己的 hostname，然后建立 DNS 并运行：

```bash
cloudflared tunnel route dns plain-memory memory.YOUR_DOMAIN
cloudflared tunnel --config deploy/cloudflared.local.yml run plain-memory
```

用自己的域名替换示例，提交地址为 `https://memory.YOUR_DOMAIN/add` 和 `/search`。隧道只转发到本机 127.0.0.1:8000；HTTPS 在 Cloudflare 边缘终止，因此评测数据也经过该服务。不要给平台加 Cloudflare Access 登录页面或机器人挑战。

临时调试可运行 `cloudflared tunnel --url http://127.0.0.1:8000`，但随机 trycloudflare URL 会变化，不用于固定报名地址。没有域名时先本地验证，等选定稳定入口再提交地址。

运行期间可用 `caffeinate -i uv run --frozen plain-memory --config config.local.yaml serve` 阻止空闲休眠。保持供电、网络和屏幕盖打开；caffeinate 无法保证断电、合盖、断网后的服务可用。正式长期运行时使用 launchd 或其他现有进程管理器，并单独保活隧道。

## Linux/容器兼容

没有 Docker 也可使用独立 Python/uv 运行相同命令。Dockerfile 提供可选 Python 3.13 容器路径，Mac 使用 arm64，Linux 使用 amd64；此环境尚未验证 Docker 构建。

```bash
docker build -t plain-memory:0.2.0 .
mkdir -p data
docker run --rm -p 127.0.0.1:8000:8000 \
  --mount type=bind,src="$PWD/config.local.yaml",dst=/app/config.local.yaml,readonly \
  --mount type=bind,src="$PWD/data",dst=/app/data \
  plain-memory:0.2.0
```

不要把宿主 .venv 复制到容器。数据库挂载本地持久化目录，不使用容器临时层或 NFS。Mac 用隧道转发宿主 8000；云服务器可用 HTTPS 反向代理。数据库+NumPy 每次在用户快照内计算，适合 Smoke，小规模性能不代表 Full 性能。

## 清理数据

服务默认记录私密诊断元数据，关闭访问日志和正文捕获；同一配置文件中的 `diagnostics` 管理诊断数据库、保留期和可选正文捕获。默认保留 7 天，启动及运行中每分钟清理过期记录。只为允许调试的 Smoke 启用 `capture_content: true`，并填写带时区的 `capture_until`；到期后后续请求自动恢复为元数据记录。记录不含 HTTP 鉴权头或上游响应正文。日志故障会报告到 stderr，不会把已提交的 Add 改为失败。

查看 Add/Search 状态码、耗时和正文捕获状态：

```bash
uv run --frozen python scripts/inspect_diagnostics.py --config config.local.yaml
```

需要导出获准调试的 Smoke 时，显式提供 `--output data/private-smoke.jsonl --include-content`，可附 `--user-id 'EXACT_USER_ID'` 限定范围。导出文件为 0600，不回显正文到控制台，不覆盖已有文件；导出副本需单独删除。

停止服务后，通过明确 user_id 清除数据：

```bash
uv run --frozen plain-memory --config config.local.yaml purge --user-id 'EXACT_USER_ID' --yes
```

同时删除原消息、切片、幂等记录和该用户的诊断记录，并做安全删除、WAL 检查点和 VACUUM；另行删除备份和外部副本。评测结束 30 天内清理评测数据。这里只提供本地运维命令，没有公网删除接口。

## 来源与许可

Apache-2.0。设计参考 [OpenViking](https://github.com/volcengine/OpenViking)（ByteDance/Volcengine contributors）和 [DeepRead](https://github.com/zhangqianyizhn/DeepRead)（zhangqianyizhn 及仓库贡献者）。本项目独立实现最小服务，没有将上述工程作为运行依赖。参考职责、删减和融合改动见 [设计文档](docs/design.md)，当前部署容量、运行限制及完整改动披露见 [报名披露说明](docs/submission-disclosure.md)，来源说明见 [NOTICE](NOTICE)。当前实际模型、密钥、数据库及评测记忆不属于公开代码。
