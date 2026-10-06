# 本机负载测试

本目录使用现有 Docker 镜像启动隔离环境：独立的 PostgreSQL、Redis、RabbitMQ、MinIO 和 1 个 backend、1 个 agent-worker、1 个 after-sales-worker。容器固定使用 `AGENT_MODE=mock`、PostgreSQL 订单源和本地 BGE / reranker；只复用原项目的 Hugging Face 模型缓存卷。不会调用真实模型 API，也不会修改原项目数据库。

在仓库根目录运行：

```powershell
docker compose -f loadtests/docker-compose.yml up -d --no-build
docker compose -f loadtests/docker-compose.yml exec -T backend python -m backend.app.knowledge.cli /app/knowledge_docs
.\.venv\Scripts\python.exe -m loadtests.run --report-dir artifacts/loadtests/local
```

脚本创建 20 个独立测试账号，每个账号登录一次并复用 Session Cookie；查询阶段为 5、10、20 个并发用户，每档 180 秒；消息阶段为 0.2、0.5、0.8 条/秒，每档 180 秒。每条消息使用独立会话并通过 SSE 等待 `assistant.completed`。每 5 秒采样 RabbitMQ 就绪/未确认消息、Agent Outbox 待发布数和死信，每 10 秒采样容器 CPU/内存。发送期间积压超过 100 条即停止该档；每档结束后最多等待 240 秒清空。报告包含 API 与完整回复的 P50/P95/P99、状态码、排队和数据库任务核对。

结果保存在 `artifacts/loadtests/local/report.md` 和 `report.json`。这两个文件在 Git 忽略目录中；报告不保存登录密码。压测端也运行在同一台电脑上，因此结果是本机基线，不能直接推算多机器部署容量。

测试完成后停止隔离环境：

```powershell
docker compose -f loadtests/docker-compose.yml down
```

上述命令保留隔离数据卷，便于复查报告对应的数据。要比较 Worker 数量，可保持其余配置和脚本不变，再执行 `docker compose -f loadtests/docker-compose.yml up -d --scale agent-worker=2` 后重新运行测试。

## 小流量真实模型基线

`docker-compose.live.yml` 在同样隔离的数据服务上为 backend 和 agent-worker 启用 `AGENT_MODE=live`，从仓库根目录的 `.env` 读取模型密钥，但不会把密钥写入报告。下面的命令发送最多 20 条消息，约每 20 秒一条，避免把限流或大量排队混入单任务耗时：

```powershell
docker compose -f loadtests/docker-compose.yml -f loadtests/docker-compose.live.yml up -d --no-build
docker compose -f loadtests/docker-compose.yml -f loadtests/docker-compose.live.yml exec -T backend python -m backend.app.knowledge.cli /app/knowledge_docs
.\.venv\Scripts\python.exe -m loadtests.run --project cslive20261006 --mode live --users 5 --skip-read --rates 0.05 --duration 400 --queue-stop 5 --report-dir artifacts/loadtests/live
docker compose -f loadtests/docker-compose.yml -f loadtests/docker-compose.live.yml down
```

这轮会产生真实模型 API 调用。20 条样本的 P99 基本由最慢个案决定，只能作初步参考；它衡量低到达率下的端到端耗时，不代表最大并发容量。
