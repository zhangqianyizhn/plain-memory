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

未验证：text-embedding-v4 调用、Docker 构建、性能容量、官方 AML Smoke/Full。官方评测与材料由用户处理。
