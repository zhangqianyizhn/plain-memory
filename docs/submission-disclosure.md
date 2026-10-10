# AML 报名披露说明

## 已部署服务

PlainMemory 0.1.0 部署在本机 macOS arm64，通过固定 Cloudflare Tunnel 提供 HTTPS：

- Add：`https://plainmemory.de5.net/add`
- Search：`https://plainmemory.de5.net/search`
- 鉴权：`Authorization: Bearer <参赛者私下提交的服务 api_token>`

Add 在同一个 HTTP 请求内完成切片、全部 embedding 和 SQLite 事务提交后才返回成功，不使用后台写入。Search 仅返回记忆证据，不生成答案；检索范围先按完整 `user_id` 隔离，同一用户可跨 `session_id` 检索。服务密钥、embedding API key、Cloudflare 凭证、数据库和评测数据均不在公开仓库中。

当前在线部署用于报名连通性和 Smoke，实际使用 Doubao embedding 模型与 `https://ark.cn-beijing.volces.com/api/plan/v3` 协议。仓库提供 text-embedding-v4 的配置骨架和 OpenAI-compatible 适配器，但该正式模型尚未在当前部署中切换或验证；切换时将使用新数据库，避免混用不同模型的向量。

## 容量和运行限制

- 单机、单 Uvicorn worker、无高可用副本；Mac 必须保持供电、联网且不休眠，断电、断网、合盖、服务或 Tunnel 进程退出都会导致不可用。
- 本地 SQLite WAL/FULL 是唯一持久化存储；不使用分布式数据库、向量数据库或常驻向量/BM25 索引。
- Add 并发上限 16，Search 并发上限 16；超过上限立即返回 HTTP 429 和 `Retry-After: 1`。
- 单个 POST 请求体上限 16 MiB，超限返回 413；Add 服务截止时间 120 秒，Search 为 30 秒。
- 已验证 `top_k=100`；更大规模尚未压测。每次 Search 在目标用户的全部切片上执行 O(Nd) 的 NumPy 余弦计算并现场构建 BM25，因此定位为 Smoke/小规模服务，延迟和内存会随该用户切片数线性增长。
- 默认每条消息按 cl100k_base 计数切为 512 token，重叠 64 token；短消息直接保存。该计数口径不等于 embedding 供应商 tokenizer。
- embedding 是外部 HTTP 依赖；供应商限流、网络故障、模型限制或数据库故障会返回 4xx/503。当前没有经过 Full 规模、长期稳定性、Docker 或正式 text-embedding-v4 容量测试。

## 公开材料和原始方法

- PlainMemory 公开仓库：<https://github.com/zhangqianyizhn/plain-memory>
- 技术报告/设计说明：<https://github.com/zhangqianyizhn/plain-memory/blob/main/docs/design.md>
- 验证记录：<https://github.com/zhangqianyizhn/plain-memory/blob/main/docs/verification.md>
- OpenViking：ByteDance/Volcengine contributors，<https://github.com/volcengine/OpenViking>，Apache-2.0。
- DeepRead：zhangqianyizhn 及该仓库贡献者，<https://github.com/zhangqianyizhn/DeepRead>，Apache-2.0。

PlainMemory 是独立的最小 Python 实现；OpenViking 和 DeepRead 不是运行依赖。当前没有单独发表的 PlainMemory 学术论文，上述 `docs/design.md` 是本方法的技术报告。

## 相对参考实现的全部方法改动

1. 保留 OpenViking 的 HTTP 路由/服务职责分离、显式用户范围和写入后可检索思路；删除 AGFS/viking URI、层级目录、abstract/overview、资源解析、语义记忆抽取、会话总结、自迭代、多轮 Agent、异步队列、图关系、管理后台、MCP/CLI 以及 Rust/C++/Go 组件。
2. Add 改为强制同步：所有切片完成 embedding 并在单个 SQLite 事务中原子提交后才返回 200；新增 `(user_id, request_id)` 幂等、请求体哈希冲突检查、稳定 chunk id、失败回滚和重试安全。
3. 参考 DeepRead 的 HTTP embedding、L2 归一化和余弦检索；把 corpus/embedding/id-map 多文件存储替换为 SQLite 原始消息、切片、float32 向量、BM25 词项和模型元数据的原子持久化。
4. 新增固定 512-token/64-token-overlap 的逐消息切片，保留 role、timestamp、message/session 来源；不跨消息或跨 session 拼接，不进行 LLM 摘要或事实抽取。
5. 向量路和 BM25 路均先按完整 `user_id` 隔离，各自取 top-k；BM25 使用轻量英文词/数字与中文单字、双字词项。融合由 DeepRead 风格的加权混合改为稳定的 `V1, B1, V2, B2, ...` 交替去重，不使用加权分数、RRF、重排器或生成模型。
6. 新增 AML Add/Search 严格请求/响应契约、Bearer 鉴权、16 MiB 请求上限、并发信号量、120/30 秒截止时间、429/413/422/503 行为和不回显正文/凭证的错误处理。
7. Search 只返回带稳定 id、原 role/时间标签和原文片段的证据，不回答问题；`options` 只加入检索查询。不同用户和不同评测运行不共享数据，同一用户允许跨 session 检索。
