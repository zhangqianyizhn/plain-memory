# 首版验证记录

验证环境：macOS arm64，Python 3.13.12，独立 plain-memory 虚拟环境。

- 26 项合成数据测试通过（约 2.1 秒）。
- Ruff 静态检查与格式检查通过。
- 已验证真实 loopback HTTP/CLI 启动、同步 Add 后立即 Search、响应丢失后重复请求的幂等基础、进程重启后稳定 id、用户与运行隔离、同用户跨 session、失败回滚、维度变化拒绝、Unicode 切片、top_k、options、并发过载与重试头、超时请求不提交。
- SQLite 写入故障由触发器模拟，原消息/切片/维度记录均回滚。
- 配置文件权限 0600；配置、隧道凭证路径及数据库被 Git 忽略。
- embedding 配置只接受 base_url；协议适配器分别追加 `/embeddings` 或 `/embeddings/multimodal`，并拒绝在 base_url 中重复填写端点路径。
- OpenViking 工作区已恢复干净；PlainMemory 已作为独立仓库推送到 `https://github.com/zhangqianyizhn/plain-memory`。

测试 embedding 是合成上游，不消耗用户 key；生产服务没有 fake 模型配置项。依赖中 Starlette TestClient 当前产生一个 httpx 弃用提示，测试通过，不影响生产 HTTP 调用。

2026-10-03 已使用 `https://ark.cn-beijing.volces.com/api/plan/v3` 完成真实 Doubao 调用：embedding HTTP 200，维度 2048；真实同步 Add/Search、相同请求重试和用户隔离冒烟测试通过。测试未记录或公开 API key。

2026-10-03 已验证 Cloudflare Tunnel 公网链路：`plainmemory.de5.net` 的 DNS 正常解析到 Cloudflare，命名 Tunnel 建立 4 条 QUIC 连接，Universal SSL 已激活，正式域名 HTTPS 健康检查返回 HTTP/2 200。

同日使用 Cloudflare 临时 HTTPS Tunnel 完成加密公网冒烟测试：健康检查、Bearer 鉴权、同步 Add、Search、幂等重试和用户隔离全部通过。临时地址仅用于测试，不作为报名地址。

同日通过正式地址 `https://plainmemory.de5.net` 完成最终加密公网冒烟测试：健康检查、Bearer 鉴权、同步 Add、Search、幂等重试和用户隔离全部通过。测试时 macOS/Python 在 VPN 切换后出现一次本地 DNS 解析异常；测试进程临时固定到已验证的 Cloudflare Anycast IP，TLS 仍按 `plainmemory.de5.net` 校验证书和主机名。

以上为旧版本历史记录；2026-10-28 起已删除旧供应商分支，以以下新版本验证为准。

## 2026-10-28 统一配置与真实模型验证

- 应用仅使用私密 `config.local.yaml`，阿里云 text-embedding-v4 和 NoneLinear gpt-4o-mini 的 Key 都从此文件加载，不读取其他项目的环境文件。
- 49 项测试通过，Ruff 与 diff 空白检查通过；涉及单文件生成、批量与顺序、双模型请求格式、错误脱敏、超时、隔离、幂等和诊断持久化。NoneLinear 默认超时延长至 180 秒，测试同时验证调用方更短的 deadline 仍然生效。
- 用户填写 Key 后，阿里云 text-embedding-v4 真实调用成功，维度 1024；使用新数据库，保留旧数据库。
- 新版服务的本机与正式公网 `https://plainmemory.de5.net` 合成 Smoke 均通过：健康检查、Bearer、同步 Add、Search、幂等重试和用户隔离。新库有 2 个独立合成 Add、2 个切片，持久化维度为 1024；这不等于官方 AML Smoke 已通过。
- 两轮 Smoke 的 12 条 POST 诊断记录包含 10 个 200 与 2 个预期鉴权拒绝 401，Add/Search 各 6 条。正文捕获关闭，元数据记录有效。
- NoneLinear 最初在 60 秒预算下两次超时，公共 DNS 路由和未鉴权 POST 探测也曾超时；这些结果不足以归因于 Key。随后用户在供应商控制台确认请求 trace，并授权延长等待时间。将本地配置和默认超时调整为 180 秒后，直接运行 `check-models`，阿里云 text-embedding-v4 与 NoneLinear gpt-4o-mini 均通过，后者返回预期的 `NONELINEAR_OK`。本次使用常规网络路径，没有固定 DNS、切换供应商或更换模型。

未验证：Docker 构建、Full 性能容量与新版本官方 AML Smoke/Full。旧版本官方 Smoke 得分由用户提供，新版本需重新验证。
