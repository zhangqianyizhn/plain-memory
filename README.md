# PlainMemory

用于 AML 文本赛道接入的最小记忆服务。同步 Add；仅在请求 user_id 范围内做余弦向量与 BM25 检索；两路按向量第一、BM25 第一、向量第二、BM25 第二交替去重。Search 返回记忆证据，不生成答案。

当前版本：`0.1.0`，用于本地自测与 Smoke 准备。正式模型要求是 `text-embedding-v4`；当前冒烟模板使用 Doubao。密钥与实际运行配置只保存在本地。没有使用正式评测数据进行开发。

## 本机 Mac 启动

需要 Python 3.11+ 与 uv。macOS arm64 和 Linux amd64 使用相同源码与配置格式。Mac 自带 Python 3.9 不适用，可用 uv 管理独立解释器。

```bash
cd ~/Desktop/plain-memory
uv sync --frozen --python 3.13
uv run --frozen python scripts/init_config.py
```

依赖锁使用阿里云 PyPI 镜像；运行命令使用 `--frozen` 保持版本与锁一致，不隐式重新选择依赖。

`init_config.py` 生成权限为 0600 的 `config.local.yaml`，自动创建服务 Bearer 密钥，不覆盖已有配置。编辑该文件，将 `embedding.api_key` 替换为你的 Doubao key；base_url/model 也可按实际服务修改。不需要在聊天中提供 key。

```bash
uv run --frozen plain-memory --config config.local.yaml serve
```

默认只监听 `127.0.0.1:8000`，单 worker。无鉴权 `GET /health`、`GET /`、`GET /add`、`GET /search` 是轻量本地就绪检查，不承诺 embedding 供应商始终在线。POST /add 与 POST /search 均要求：

```http
Authorization: Bearer <config.local.yaml 中的 api_token>
```

服务密钥不是 AML 的 Eval Key，也不是 embedding API key。不要将密钥放在 URL 中。

## 配置与模型切换

`config.example.yaml` 是 Doubao 冒烟模板：配置只填写服务根地址 `https://ark.cn-beijing.volces.com/api/plan/v3`，代码根据 `doubao_multimodal` 协议追加 `/embeddings/multimodal`。每个切片单独请求一次向量，不把多个切片合并成一条 embedding。所有调用在同一次 Add 请求内完成后才提交数据库。

`config.text-embedding-v4.example.yaml` 是正式模型的配置骨架：只配置供应商的 `base_url`，代码为 `openai_compatible` 协议追加 `/embeddings`。协议和维度仍需按实际供应商确认。该协议支持批量 `input: [text, ...]` 和按返回 index 对齐。填写 dimensions 时会发送同名参数；供应商不支持该参数时保留 null，维度从成功写入结果确定。这里没有实现未确认的供应商专属协议。

切换模型、base_url、协议或切片配置时指定新数据库，例如 `data/text-embedding-v4.sqlite3`。服务拒绝把不同配置混入旧数据库，不会默默清空旧数据或重建。默认 512 token 切片、64 token 重叠，cl100k_base 计数；中文/emoji 字符边界会小幅调整。该计数器不等同于供应商的 tokenizer。

相对 database 路径基于配置文件所在目录。实际 `config*.yaml`、`*.local.yaml`、`*.local.yml`、数据文件、日志与 `.venv` 均在 `.gitignore` 中；公开配置仅有无凭证的 example。若你另起配置文件名称，应检查它被忽略后再提交。

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

Search 必填 query、user_id、top_k；可选 options 为字符串数组。选择题把 query 与 options 按顺序用于两路检索，不回答选择题。

```json
{"query": "What is my pet's name?", "user_id": "synthetic-user-1", "top_k": 100}
```

响应为 `{"data": [...]}`；条目包含稳定 id、非空 content、created_at，省略可选 score。content 带原 role 与存在时的源时间标签，随后是原文片段。无用户数据返回 `{"data": []}`。

先隔离 user_id，再计算向量和 BM25，包括 BM25 的 IDF。session_id 只是来源信息，Search 不包含它，因此同 user 可跨 session；不同 user/不同运行不能共享。Bearer 服务密钥供单一受信任 AML 调用者访问本部署，不是面向多个客户的权限系统。

默认并发上限 Add=16、Search=16；过载返回 429 和 `Retry-After: 1`。请求体默认最大 16 MiB，超限返回 413，不静默截断。非法输入 422，鉴权 401，临时 embedding/数据库故障 503。验证错误与上游错误不回显评测正文或上游响应。

Add/Search 的处理预算为 120/30 秒，HTTP 上游调用共享剩余预算，写入前再次检查；这是防止超时请求提交的保护，不是已验证的吞吐/延迟承诺。极端网络慢传输的实际返回时间仍受 HTTP 读取超时控制。正式部署容量需使用真实模型实测。

## 本地测试

```bash
uv run --frozen pytest -q
uv run --frozen ruff check .
```

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
docker build -t plain-memory:0.1.0 .
mkdir -p data
docker run --rm -p 127.0.0.1:8000:8000 \
  --mount type=bind,src="$PWD/config.local.yaml",dst=/app/config.local.yaml,readonly \
  --mount type=bind,src="$PWD/data",dst=/app/data \
  plain-memory:0.1.0
```

不要把宿主 .venv 复制到容器。数据库挂载本地持久化目录，不使用容器临时层或 NFS。Mac 用隧道转发宿主 8000；云服务器可用 HTTPS 反向代理。数据库+NumPy 每次在用户快照内计算，适合 Smoke，小规模性能不代表 Full 性能。

## 清理数据

服务不打印请求正文、密钥或 embedding 响应，默认关闭访问日志。停止服务后，通过明确 user_id 清除数据：

```bash
uv run --frozen plain-memory --config config.local.yaml purge --user-id 'EXACT_USER_ID' --yes
```

同时删除原消息、切片和幂等记录，并做安全删除、WAL 检查点和 VACUUM；另行删除备份和外部副本。评测结束 30 天内清理评测数据。这里只提供本地运维命令，没有公网删除接口。

## 来源与许可

Apache-2.0。设计参考 [OpenViking](https://github.com/volcengine/OpenViking)（ByteDance/Volcengine contributors）和 [DeepRead](https://github.com/zhangqianyizhn/DeepRead)（zhangqianyizhn 及仓库贡献者）。本项目独立实现最小服务，没有将上述工程作为运行依赖。参考职责、删减和融合改动见 [设计文档](docs/design.md)，当前部署容量、运行限制及完整改动披露见 [报名披露说明](docs/submission-disclosure.md)，来源说明见 [NOTICE](NOTICE)。当前实际模型、密钥、数据库及评测记忆不属于公开代码。
