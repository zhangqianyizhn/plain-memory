# AML 开源文本赛道：报名与 Smoke 首版设计

状态：2026-10-04 更新。首版源码、真实 Doubao 调用、Cloudflare HTTPS 公网链路和公开仓库均已完成验证。当前部署仍是报名/Smoke 版本，尚未切换或验证正式要求的 text-embedding-v4；容量与超时不能作为已经压测的承诺。

## 1. 范围和推荐结论

新建一个独立的小型 Python 项目，参考 OpenViking 的 HTTP 服务、请求身份上下文、写入完成后检索的组织方式，参考 DeepRead 的 embedding 调用和归一化向量检索。首版采用 FastAPI + SQLite + NumPy + rank-bm25 + tiktoken + HTTP embedding 服务。

对外只有 POST /add、POST /search 和轻量无鉴权健康检查；不生成答案。删除原系统中的 AGFS、viking URI、层级摘要、资源解析、语义记忆抽取、队列、多轮 Agent、重排器、图存储、管理后台和 Rust/C++/Go 构建依赖。这里的删减是提取所需职责后形成独立最小实现，不把完整 OpenViking 作为运行依赖，也不改动现有评测流水线。

按用户确认，现有 Linux 服务器为内网环境，不作为公网部署目标。优先在 Mac 运行，通过固定命名 Cloudflare Tunnel 提供稳定 HTTPS 入口；localhost 无法满足公网报名要求。系统名 PlainMemory，仓库名 plain-memory，项目位于 Desktop/plain-memory。

## 2. 参考依据与当前不确定项

依据本地文件：

- `/Users/zhangqianyi/Downloads/Agent记忆公开挑战赛说明.md`
- `/Users/zhangqianyi/Downloads/Agent Memory API接口文档.md`

已阅读的实现：

- OpenViking `openviking/server/routers/resources.py`：资源写入含 `wait` 选项，默认 false；不可直接沿用为 AML 成功返回条件。
- OpenViking `openviking/server/routers/search.py` 与 `openviking/service/search_service.py`：HTTP 路由与检索服务分开，基础 find 比含会话上下文的 search 更接近本方案。
- DeepRead `index/embedding.py`：HTTP embedding、批量编码和归一化；其 corpus/embedding/idmap 多文件存储改为 SQLite 原子写入。
- DeepRead `tool/vector_search.py`：余弦相似度检索。
- DeepRead `tool/bm25_search.py`、`tool/hybrid_search.py`：借鉴两路检索的职责；原有加权融合替换为参赛者要求的交替混排。

已只读获取官网 API 指南和规则页面。当前返回 HTML 含多个页面区块，本地文档中的 Add/Search 核心契约与所读取区块相符。两个本地文件使用不同官网域名，实际报名入口需以可用官网页面为准：

- https://agentmemories.ai/api-guide
- https://agentmemories.ai/rules
- https://agentmemories.ai/evaluation

官网有一处评测确认文字：`Open-source Methods entries are expected to use gpt-4o-mini during Add.` 另有正文称不限制 embedding model。这不足以确定纯切片与 embedding 方法是否必须调用生成模型；需核实该条适用范围。首版设计不为满足含义不明的文字增加生成模型调用，也不宣称已经满足全部参赛模型限制。

## 3. HTTP 与鉴权

拟提交的固定地址：

```text
Add:    https://<域名>/add
Search: https://<域名>/search
Health: https://<域名>/health
Auth:   Authorization: Bearer <MEMORY_API_TOKEN>
```

Bearer 是请求头的写法，不要求使用 JWT。生成至少 32 字节的加密随机值，服务通过环境变量读取，使用常量时间比较。密钥缺失或错误返回 401；服务缺少密钥配置则启动失败。Add/Search 共用一枚评测专用密钥，仅对应本部署的 AML 访问者。首版不承诺开放给多个不互信的客户。

HTTPS 保护密钥与评测文本在传输过程中的私密性；只绑定后端到 127.0.0.1，由 Caddy 或同等反向代理提供 HTTPS。无需账号、登录、OAuth、JWT、注册或密钥管理后台。无鉴权 GET /health 只给出就绪状态；可同时支持 GET /add、GET /search 返回相同轻量健康状态，兼容对绑定接口地址直接 GET 的探测。

不要混淆三种凭证：AML 发放的 Eval Key 用于登录评测平台；我们生成的 Memory System Key 用于平台调用本服务；embedding API key 用于本服务调用编码供应商。后者不得提供给 AML，也不得写进公开仓库或日志。

### Add

严格校验 request_id、user_id、session_id、messages 与 role/content/timestamp 的类型；role 仅 user/assistant；timestamp 是可选 Unix 毫秒整数。禁止把非法类型自动转换成字符串或静默删除非法消息。按 messages 数组顺序切片，保留原始 role、timestamp、message_index、chunk_index。

成功只返回：

```json
{"success": true, "request_id": "原值", "user_id": "原值", "session_id": "原值"}
```

整个请求完成 embedding、文本/向量/词项持久化及事务提交后才返回 HTTP 200。服务重启后立即能从同一数据库检索，不依赖后台队列。空白消息如何处置需用公开 Smoke 样例确认；默认可保留原始消息记录但不创建空白检索切片，禁止返回空 content。

### Search

校验 query 字符串、user_id 字符串、top_k 正整数和可选 options 字符串数组。选择题将 query 和 options 按原顺序拼成同一个检索文本，供两路检索使用；不预测选项、不生成答案。开放题使用 query 本身。

只返回 `{"data": [...]}`；空库或没有候选时返回 `{"data": []}`。每项只需稳定 id 和非空 content；可附 created_at。首版省略可选 score，避免把不同量纲的 BM25 分数和余弦分数当作统一相关性分数。返回顺序就是融合排名，不再额外按原始分数排序。正式 top_k=100，必须支持；结果可以少于 top_k，但不得更多。

created_at 有源 timestamp 时转为 UTC ISO-8601，否则使用持久化时间。返回 content 可加简短的 role/源时间标签后接原文片段，以保留时间推理线索；不加入生成的事实或回答指令。

### 失败行为

非法输入 400/422；无效凭证 401；超出明确声明的请求大小 413；embedding 暂时不可用或存储不可用 503；过载 429 且含 Retry-After。校验错误响应和上游错误日志不得回显评测正文或凭证。embedding 永久输入错误返回明确 4xx；内部配置错误作为服务故障处理。

不得静默截断消息、问题或选项。模型输入超限时明确拒绝并解释限制。建议先以单请求 16 MiB 作为待验证的服务限制，代理与应用设置一致；实际声明需通过公开样例和压测后确定。

## 4. 切片和 embedding

默认 `chunk_tokens=512`、`overlap_tokens=64`，步长 448；仅在单条消息内切片。短消息按原文存一片；不把不同 role、不同 session 的消息拼成一片。不拆改源 timestamp，不按 timestamp 重排数组。

沿用现有评测工程的 token 计数口径，使用 tiktoken cl100k_base，版本由依赖锁固定。token 边界可能位于中文或 emoji 的 UTF-8 字节内部，必须在合法字符边界小幅调整切点；不允许通过替换字符掩盖解码损坏。因此实际长度/重叠可在边界处有少量偏差，保证原文无损。

cl100k_base 是切片计数口径，不代表 embedding 供应商的 tokenizer 或最大输入长度。选定模型后单独验证最大输入、batch 大小和请求总 token 限制。建议 batch 初始值 32，供应商不支持批处理时在同一 HTTP 请求生命周期内分批编码，全部完成后才提交。

正式模型要求是 text-embedding-v4；当前 Smoke 使用 Doubao，实际 base_url/model/key 均来自不入 Git 的本地 YAML。配置只保存服务根地址，具体 embedding 路径由协议适配器拼接：Doubao multimodal 使用 `/embeddings/multimodal`，通用协议使用 `/embeddings`。仅提供 doubao_multimodal 和 openai_compatible 两种所需协议，不开发多供应商插件框架。响应按供应商 index 对齐输入顺序，检查返回数量、维度、有限数值和非零向量。用 float32 保存，并做 L2 归一化；查询与入库必须使用同一模型、维度和规定的 query/document 输入方式。

数据库记录 embedding 模型与维度、切片和词法版本。配置不兼容则拒绝使用旧库。更换模型需要新库或显式重建，不能把不兼容向量混存。外部 API 首版减少本机模型依赖；若供应商的数据使用/留存条款不符合赛事要求，再采用私有部署 embedding，需提前确认。

## 5. SQLite 存储与同步一致性

SQLite 是唯一持久化事实来源；向量以明确 little-endian float32 BLOB 存储，bm25_terms 以 JSON 保存。避免首版的多文件更新、向量数据库服务和扩展安装。

最小三张表：

```text
add_requests:
  user_id, request_id, session_id, payload_hash,
  original_messages_json, committed_at
  UNIQUE(user_id, request_id)

chunks:
  id, user_id, session_id, request_id, message_index, chunk_index,
  role, source_timestamp_ms, content, bm25_terms_json,
  embedding_blob, created_at
  INDEX(user_id)

store_meta:
  schema_version, embedding_model, embedding_dimension,
  tokenizer_name, chunk_tokens, overlap_tokens, lexical_version
```

原始消息作为评测私密数据存储，仅用于无损持久化和必要审计，随切片同步删除。不是日志。可在不影响接口的后续版本简化存储，但本版本冻结后保持声明一致。

写入流程：认证/校验 → 查看已完成请求 → 切片/分词 → 同步编码 → 单一 SQLite 事务再次检查幂等性 → 写原始消息、所有切片与请求完成记录 → 提交 → 回显成功。embedding 网络调用不放进数据库写事务，避免长时间阻塞其他写入。

同一 user_id+request_id 且规范化请求体 hash 相同：直接回显成功，不追加切片。相同 key 而 session/body 不同：返回 422，说明幂等标识冲突；不用 409 制造平台的无效重复重试。hash 保留消息/选项数组顺序，不受 JSON 对象字段顺序影响，不修剪内容。chunk id 使用结构化字段 `(user_id, session_id, request_id, message_index, chunk_index)` 的哈希，禁止无分隔拼接产生歧义。

事务内所有切片与完成记录一起提交，失败全部回滚。提交后连接断开也不影响平台重试；重试识别已完成记录并返回成功。并发同 key 最终由唯一约束和事务内复查保证只提交一份，首版 Add 并发上限 1，避免无谓并行 embedding 开销。

单进程 Uvicorn，SQLite WAL、synchronous=FULL、busy_timeout；持久化目录在本地文件系统/持久化卷，不放在 NFS 或容器临时层。Search 一次读取仅含目标 user_id 的数据库快照，从这个快照同时构建两路检索。首版没有长驻向量或 BM25 缓存，故不存在缓存更新完成前就返回 Add 成功的问题。

## 6. 两路检索与隔离

向量路：先 `WHERE user_id = ?` 读取该用户切片，组装 NumPy 矩阵；查询归一化后计算矩阵点积即余弦相似度，稳定排序取前 top_k。

BM25 路：从同一用户快照中的词项构建 rank-bm25 索引，取词项匹配候选前 top_k。英文用小写词/数字分词，中文连续字符采用单字与双字词项，保证 Smoke 对中文可检索而不引入大型分词器。两端使用同一词法函数。空词项集合不建 BM25；不得填入与 query 没有词项匹配的任意条目。使用库时不简单以 `score > 0` 判断匹配，避免 BM25Okapi 在小语料上的非正 IDF 分数误删匹配项。

对 BM25 语料数、平均长度、IDF 和向量候选全部先按完整 user_id 隔离；不能先全库 top_k 后过滤，也不能共用所有用户的 BM25 统计量。user_id 是不透明字符串，保持完整值，不能删去 eval:<run_id> 前缀来共享旧运行的数据。

session_id 只组织来源。Search 契约不含 session_id，所以同一个 user_id 内可跨 session 检索；不能只搜最新 session，也不能把 session_id 单独作为全局键。不同 user 下同名 session、不同 run 的 user_id 都不共享。

只有一个受信任的 AML 调用者时，固定 Bearer 密钥允许访问本部署中的评测 user_id；这不是面向多个客户的用户授权系统。未来多客户场景必须增加 credential→namespace 映射，再按 namespace+user_id 隔离，首版不提前引入。

按用户要求融合：遍历 `V1, B1, V2, B2, ...`，按稳定 chunk id 去重，保留第一次出现位置，收集到 top_k 即停止。一条路不足时继续另一条路。

```text
V = [A, B, C, D]
B = [B, E, A, F]
交替并去重 = [A, B, E, C, D, F]
top_k=4 时返回 [A, B, E, C]
```

这里只消除两路召回的同一切片；不会合并不同时间、role、session 中恰好相同的文本，以免丢失独立事件。BM25 与向量分数不做加权，初版不增加 RRF 或模型重排。

代价：每次 Search 都有 O(Nd) 向量计算与用户内 BM25 构建开销；适合 Smoke 和小语料，不能据此承诺 Full 性能。例：每用户 10,000 切片、1,024 维，向量矩阵约 39 MiB，尚未包含文本、数据库和临时数组。未来按实际瓶颈再增加有界的用户缓存或索引，不作为首版依赖。

## 7. 部署评估

2026-10-02 只读检查结果：本机是 Darwin arm64；未取得本机内存信息。指定服务器可通过 SSH 登录，是 Linux x86_64、56 逻辑 CPU、约 64 GB RAM（约 60 GB available）、所在磁盘约 826 GB 可用，Python 3.10.12。当前 PATH 中未找到 Docker、Podman、uv、Caddy、Nginx、nvidia-smi；80/443 未见监听。命令未找到不代表软件在整台机器上绝对不存在，也不能据此判断是否有 GPU。

SSH 可达只证明 SSH 通路可用，不证明 AML 评测机器可访问 HTTP/HTTPS，也不证明拥有开放端口或安装软件的权限。

| 位置 | 对首版的判断 | 必须补齐的条件 |
|---|---|---|
| 本机 Mac | 开发、自测适合；公网报名有条件可用 | 固定公网入口或命名隧道、稳定 TLS、持续供电/不休眠、稳定网络；临时隧道地址不适合固定版本 |
| 现有 Linux | 计算资源充分，但用户已确认内网限制，当前不采用 | 若以后迁移，需要新的公网入口条件 |
| 租赁云服务器 | 现有服务器的网络/权限条件不满足时采用 | 固定公网 IP、域名/TLS、出站可达；2–4 vCPU、4–8 GB RAM 是 Smoke 起点，Full 规格待压测 |

独立项目支持 Python 3.11+，本机验证使用现有 Python 3.13 独立虚拟环境；Mac 系统 Python 3.9 不适用。源码已脱离原有 workspace，未修改原工程解释器或依赖。

当前默认使用 Mac 原生 Python/uv + 固定隧道；不安装或部署到内网服务器。Dockerfile 只是后续迁移的可选路径，尚未在当前无 Docker 的环境验证构建。Apple Silicon 使用 arm64 镜像，服务器使用 amd64 镜像，分别构建或发布双架构镜像；不拷贝 Mac 虚拟环境到 Linux。

不依赖 GPU、host.docker.internal、特定 CPU 指令或宿主绝对路径。连接 embedding API 从容器网络实际验证。依赖版本与 token 编码器缓存随发布准备好，避免首次启动隐式下载失败。

拟定 Smoke 并发声明：Add=1、Search=2，过载即时 429+Retry-After，不进入后台排队后回成功。进程内并发约束需覆盖 worker/thread 的实际执行，单 worker 即可。拟定 Add 服务截止 120 秒、代理读取超时 150 秒、Search 服务截止 30 秒、代理读取超时 45 秒；这些值需结合供应商和平台实际请求超时校准，不能宣称来自官方硬性要求。

## 8. 验证、公开仓库和数据生命周期

实施交付物保持少量文件：api.py（路由/协议/鉴权）、store.py（事务与读快照）、retrieval.py（两路检索与混排）、text.py（切片/词法）、embedding.py（实际供应商调用）、config.py；配套 pyproject/依赖锁、README、.env.example、Dockerfile、最小部署说明与协议测试。

必要验收：

1. Add HTTP 200 后立刻 Search 能命中；重启后仍能命中且 id 不变。
2. 同 request 重放、并发重放和提交后响应丢失不重复写入；冲突 payload 明确失败。
3. embedding 中途失败/SQLite 写失败后没有半条请求；重试能完整写入。
4. 两用户、同名 session、不同 run 互相搜不到；同 user 跨 session 可检索。
5. 长短消息、中文/emoji、重叠与 timestamp/数组顺序无损。
6. 向量/BM25 各 top_k、交替顺序、重复跳过、空库、options 和 top_k=100 满足协议。
7. 鉴权 401、无鉴权 Health、无正文的错误日志、过载 Retry-After、明确拒绝超限 payload。
8. 真实 embedding 模型端到端自测；至少从部署机器外验证公网 HTTPS。官方 Smoke 需要已签发 Eval Key，不能用本地契约测试替代平台 Smoke。

公开 GitHub 仓库采用固定 commit/tag（例如 v0.1.0-smoke），README 明确方法、来源、删减、模型及部署容量。OpenViking 和 DeepRead 本地 LICENSE 均为 Apache-2.0；复用代码时保留许可、版权、适用 NOTICE 与修改说明，不仅在 README 放一个链接。新仓库默认建议 Apache-2.0，具体所有者和名称待提供。

公开仓库只含代码、配置模板与人工合成测试样例，不含密钥、sqlite/db-wal/db-shm、embedding、副本、原始评测数据、请求内容日志。日志限耗时、状态码、计数和必要脱敏标识。

提供本地运维清理命令，按 user_id 或明确列出的 run 清单删除原消息、切片和幂等记录；API 契约没有 end-run 通知，需要由评测结束时间驱动清理。评测结束 30 天内同时清除数据库中的内容、WAL、备份与缓存；仅执行 DELETE 不保证文件中没有残留，需规划检查点与压缩/安全删除，私密卷需访问控制。不新增公网 Delete API。

首版只用于报名和 Smoke，不自动启动 Full。Full 通常 0.5–2 天且次数/冷却有限，必须在容量与规则核实后单独推进。按提供的说明，接口需持续可用至 2026-11-04，Full 受理后版本冻结；后续迭代准备新版本，不在正在运行的申报版本上更换模型、鉴权或协议。

## 9. 用户已确定的推进边界

- 正式 embedding 模型要求 text-embedding-v4；当前 Smoke 用 Doubao。本地私密配置可更换供应商与模型，模板不含真实 key。
- 系统名 PlainMemory，仓库名 plain-memory，独立建立在 Desktop；旧项目只移除本次新增设计文档，不改业务代码。
- 用户创建远程 GitHub 仓库；本地项目先初始化 Git，不自动推送。
- 优先本机 Mac，现有内网服务器不提供公网服务。
- 报名、联系人、团队、Eval Key 及平台提交材料由用户负责，本项目不处理。
- 当前验证边界为合成数据协议测试与本机 HTTP 链路。用户填入 Doubao key 后运行 scripts/smoke.py；域名/固定隧道配置后再验证公网。

以 README 与实际实现为准；本文保留参考和设计理由，未验证项不能当作已交付能力。首版不运行官方 Smoke/Full，也不会自动启动评测。
