# 多角色电商智能客服平台

这是一个可在本地运行的电商智能客服演示项目，覆盖订单与物流查询、政策问答、售后申请、人工审核和模拟执行。Supervisor Agent 负责结构化计划与最终话术，After-sales Agent 负责售后判定；订单、知识检索和工单由受控领域服务处理。政策按“同名文件唯一当前版本”管理：重复内容幂等跳过，内容变化时原位替换分块。模型可运行在 Mock 模式，真实商城操作和支付退款目前仍使用 Mock 适配器。

## 架构

```text
Next.js 对话页 / 人工工作台
           │ HTTP + SSE
           ▼
FastAPI（登录鉴权、输入检查、消息持久化）
           │
           ├── PostgreSQL：消息、AgentRun、Outbox、业务数据
           ├── Redis：Session、短期上下文、SSE 事件
           └── MinIO：政策文件与售后凭证
           │
           ▼
Outbox Relay → RabbitMQ → Agent Worker
                            │
                            ▼
                 Supervisor Agent / 轻量状态机
                            │
             ┌──────────────┼──────────────┐
         订单服务         知识检索服务         工单服务
                            │（售后场景）
                       After-sales Agent
                            │
                    引用与业务结果校验

售后凭证上传 → Outbox → 售后 Worker → 多模态预审
人工审批 → Outbox → 售后 Worker → Mock OMS / Payment
```

Docker Compose 使用 RabbitMQ 模式：消息接口先把用户消息、AgentRun 和 Outbox 命令写入 PostgreSQL，再返回 `202 Accepted`；Relay 将命令发布到 RabbitMQ，由独立 Worker 生成回复。本地开发也支持 `AGENT_TASK_BACKEND=inline`，此模式在请求内执行 Agent，仍返回 `202` 状态码，但请求会等待处理完成。处理过程发布以下 SSE 事件：

- `user_message.created`
- `assistant.started`
- `assistant.delta`
- `assistant.completed`
- `assistant.failed`

Redis使用两类互相隔离的Key：Streams保留最近1000个会话事件，供SSE通过 `Last-Event-ID` 或 `cursor` 断线续传；List缓存最近 `AGENT_HISTORY_MESSAGES` 条会话上下文，默认采用24小时滑动TTL。每次读取或写入都会续期。PostgreSQL `messages` 永久保存完整对话，是事实源；Redis Key不存在、过期或读取失败时，系统从PostgreSQL加载最近消息并重新建立缓存。

经过订单归属校验的当前订单号和商品项编号另存于 PostgreSQL `conversations.context_data`。因此订单号即使已经超出最近消息和 Token 窗口，用户仍可用“这个订单”“查一下它的物流”等指代表达继续查询。结构化上下文按会话和客户共同隔离，只有订单或物流工具成功验证归属后才更新；它不依赖 Redis TTL，也不能绕过后续的订单归属复核。

Supervisor 的 Token 预算由应用层统一控制：计划生成和最终回复的最大输出均为 1024 tokens；历史上下文从最新消息开始向前选择，只保留完整消息，同时满足不超过 2560 个估算 tokens 和不超过 12 条；当前用户消息最多允许 4096 个估算 tokens。Kimi 的 OpenAI-compatible SDK 不提供本地 tokenizer，因此项目对中英文混合文本使用保守估算，模型侧仍会按实际 tokenizer 计费。

```env
SUPERVISOR_PLAN_MAX_TOKENS=1024
SUPERVISOR_RESPONSE_MAX_TOKENS=1024
AGENT_HISTORY_MAX_TOKENS=2560
AGENT_HISTORY_MESSAGES=12
AGENT_CURRENT_MESSAGE_MAX_TOKENS=4096
```

## 自动化评测

项目内置 200 条版本化且提示文本唯一的回归用例，覆盖订单、物流、政策、售后申请与进度、改址、催发货、发票、信息缺失、售后政策边界、越权攻击、提示注入和工具异常。Mock 回归会运行真实 Agent 编排与状态机，但使用隔离式 Fake 领域服务，不访问 Kimi 或业务数据库：

```powershell
.\.venv\Scripts\python.exe -m backend.app.evaluation --mode mock
```

真实模型与真实知识检索使用 `--mode live`；仅验证真实模型、保持确定性知识夹具时使用
`--mode live-fixture`。

命令校验意图、工具及参数、政策引用、结构化业务事实、敏感信息、退款承诺、人工转交、时延和 Token 预算，并在质量门禁失败时返回非零退出码。报告写入 `artifacts/evaluation/latest.json` 与 `latest.md`；完整使用方法见 [自动化评测说明](docs/evaluation-harness.md)。现有 GitHub Actions 仅在工作流列出的路径发生变化时运行 Mock 回归；它不执行真实模型评测，也不能用 Mock 通过率代表真实客服回答准确率。

当前 Agent 标准链路为：

```text
身份绑定与输入检查 → SupervisorPlan → 状态机 → 受控领域服务
→（售后场景）After-sales Agent → 引用校验 → Supervisor 最终话术
→ 流式回复 → Message / AgentRun / Trace 持久化
```

角色与服务之间只传递 Pydantic 结构；应用运行时统一控制单工具超时和一次 Run 的最大工具调用数。Kimi 不直接执行数据库写入，人工工单由状态机通过 TicketService 创建。SDK 自带远程 Trace 默认关闭，数据库仅记录脱敏后的计划、状态转换、工具和专家判定事件。

## 订单增值服务

Supervisor 现支持 `address_change`、`shipment_reminder`、`invoice_apply`、
`invoice_query` 和 `after_sales_status`。LLM 只负责路由，订单归属、订单状态、
重复申请和业务参数均由后端确定性校验：

- 修改地址：仅允许已付款未发货订单，客户在独立表单填写地址；地址不会进入 LLM 上下文。
- 催发货：仅允许已付款未发货订单，同一订单24小时内复用原催单结果。
- 发票：仅允许已付款且已完成、完成期不超过30天的订单；一个订单复用同一有效发票申请。
- 售后进度：直接读取案件、审核和执行状态，不由模型推测。

改址、催单和发票统一持久化到 `customer_service_requests`，通过请求号、
`customer_id`、订单号和幂等键完成审计与租户隔离。客户接口为：

- `GET /service-requests`
- `GET /service-requests/{request_no}`
- `POST /service-requests/{request_no}/address`
- `POST /service-requests/{request_no}/invoice`

## 订单领域

订单通过统一 `OrderGateway` 读取，运行时由 `ORDER_BACKEND` 选择实现：

- `mock`：内存演示数据，仅用于测试。
- `postgres`：当前本地默认值，从 `orders`、`order_items`、`shipments` 读取。
- `http`：调用外部 OMS 的 `GET /orders/{order_id}`，通过 `ORDER_OMS_BASE_URL` 和 `ORDER_OMS_API_KEY` 配置。

订单模型支持多商品和多物流包裹。商品使用稳定的大类 `product_category`，退款例外等特殊属性使用多值 `product_tags`；售后检索优先使用更具体的标签。订单查询始终同时约束 `order_no` 和当前 `customer_id`，越权与不存在统一返回 `ORDER_NOT_FOUND`。

开发演示数据通过幂等命令写入 PostgreSQL：

```powershell
docker compose exec backend python -m backend.app.orders.seed
```

重复执行只会报告 `skipped`，不会创建重复订单，也不会刷新已有订单的时间。演示订单的签收时间相对首次导入时刻生成；运行一段时间后，原本用于 2/48 小时举证窗口演示的订单会自然过期。需要重新演示时，应使用全新的演示数据库或新订单，不要对已有业务数据运行删除操作。

## 政策知识库

本地 `knowledge_docs` 批量导入：

```powershell
docker compose exec backend python -m backend.app.knowledge.cli /app/knowledge_docs
```

主要接口：

- `POST /knowledge/documents`：上传 UTF-8 `.md`、`.markdown`、`.txt`
- `GET /knowledge/documents`：文档列表
- `GET /knowledge/documents/{id}`：文档及来源元数据
- `POST /knowledge/search`：pgvector 余弦召回 + BM25 + RRF 融合，再由 cross-encoder 重排并过滤低相关结果

直接检索示例：

```json
{
  "query": "生鲜商品可以七天无理由退货吗",
  "top_k": 5,
  "policy_category": "refund",
  "product_category": "生鲜食品"
}
```

检索会分别执行 BGE 余弦向量召回和 Okapi BM25 召回，使用 RRF 按两路名次融合，随后以 `BAAI/bge-reranker-large` 对前 10 个候选做 query-document 交叉编码重排。低于校准阈值 `0.04` 的结果会被删除，因此最终可以少于 Top K，甚至返回空结果并触发安全转人工。结果包含 `bm25_score`、`vector_score`、`rerank_score`、两路排名、来源文件、政策标题、章节和分块序号，Agent 最终回答按 `【来源：文件名，章节】` 输出引用。

默认使用本地中文语义模型 `BAAI/bge-small-zh-v1.5`，在 CPU 上生成 512 维归一化向量。模型首次使用时下载到 Docker 的 `huggingface_cache` 持久卷，后续重启直接复用。短查询会按 BGE 官方建议增加中文检索指令，政策分块不加指令。

```env
EMBEDDING_MODE=bge
EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
EMBEDDING_DIMENSIONS=512
RERANKER_ENABLED=true
RERANKER_MODEL=BAAI/bge-reranker-large
RERANKER_CANDIDATE_LIMIT=10
RERANKER_THRESHOLD=0.04
```

RAG 参数基线与 reranker 阈值可重复评测：

```powershell
$artifactDir = (New-Item -ItemType Directory -Force -Path .\artifacts).FullName
docker compose run --rm --no-deps -v "${artifactDir}:/app/artifacts" backend python -m backend.app.rag_evaluation --report-dir /app/artifacts/rag-evaluation
docker compose run --rm --no-deps -v "${artifactDir}:/app/artifacts" backend python -m backend.app.rag_evaluation.reranker_cli --report-dir /app/artifacts/rag-evaluation
```

报告写入 `artifacts/rag-evaluation/`。当前数据集包含 142 条问题；修改政策文档、embedding、reranker 或召回参数后应重新运行，不应直接沿用 `0.04`。

如需改用 OpenAI-compatible Embeddings 服务：

```env
EMBEDDING_MODE=openai
EMBEDDING_API_KEY=replace-me
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=512
```

上传接口在本地开发环境可直接使用；对外部署前必须设置 `KNOWLEDGE_ADMIN_TOKEN`，并通过 `X-Admin-Token` 请求头传递。

订单读取会把 `order_id` 与当前 `customer_id` 一起提交给 OMS adapter；不属于当前客户的订单与不存在的订单统一返回 `ORDER_NOT_FOUND`，避免订单枚举。演示客户 `00000000-0000-4000-8000-000000000001` 拥有演示订单 `ORD-20260918-001`、`ORD-20260920-003`、用于 48 小时举证窗口测试的 `ORD-20260926-004`、用于未发货取消退款测试的 `ORD-20260926-005`，以及用于签收后凭证退款测试的 `ORD-20260926-006`。为该客户设置登录密码后，即可在前端使用这些演示订单。

客户接口现在从服务端验证的 Redis Session 取得 `customer_id`。先执行数据库迁移，再为已有客户设置登录账号；命令会交互式读取密码，不把密码留在 shell 历史中：

```powershell
docker compose up -d --build
docker compose exec backend python -m backend.app.security.provision demo 00000000-0000-4000-8000-000000000001
```

打开前端并使用该账号登录。再次执行账号命令可重置密码，旧 Session 会立即失效。`POST /auth/session` 验证账号密码后设置 HttpOnly Cookie；`GET /auth/session` 查询当前登录身份，`DELETE /auth/session` 注销。Session 存在 Redis，默认有效期 24 小时；生产环境 Cookie 使用 `Secure`，部署时必须通过 HTTPS 访问。客户接口与 SSE 都使用 Cookie，客户端传来的 `X-Customer-ID` 和 `customer_id` 查询参数不再作为身份依据。仅当 `APP_ENV=test` 且显式设置 `TEST_IDENTITY_HEADER_ENABLED=true` 时，旧 Header 才可用于集成测试。

发消息接口使用 Redis 双层滑动窗口：每位客户 60 秒最多 10 条，每个来源 IP 60 秒最多 60 条。售后凭证上传为每位客户 10 分钟最多 12 次、每个 IP 最多 60 次；这个额度允许一次申请上传最多 6 份材料并留出重试空间。任一额度耗尽时返回 `429` 和 `Retry-After`，不会创建消息或任务；`/health`、SSE 和普通读取接口不计入这些额度。登录另有 5 分钟内每个账号 5 次、每个 IP 20 次尝试的限制。若通过反向代理部署，在 `TRUSTED_PROXY_CIDRS` 中填入代理的 IP/CIDR JSON 列表，例如 `["172.20.0.10/32"]`；只有连接地址属于该列表时才读取 `X-Forwarded-For`。直接连接时保持 `[]`，转发头不能由客户端自行指定身份。

远程模型、HTTP OMS 和远程 Embedding 各自使用共享 Redis 熔断器：30 秒内出现 5 次超时或服务端故障，打开 30 秒；之后仅一个实例可试探恢复。模型熔断期间 Agent RabbitMQ 任务延迟 30 秒重试且不增加失败次数；售后任务同样延期。Mock 模型、本地 Embedding 和 PostgreSQL 订单源不经过远程熔断器。

## 售后申请闭环

具体订单的退款、退货、换货、补发或保修请求会先经过订单归属校验、商品选择、固定时效校验、政策检索和 AfterSales Agent 结构化判断。时效只依据订单商品的 `product_category` 和订单系统签收时间计算：`食品生鲜` 为 2 小时，其余当前商品分类为 48 小时；RAG 只负责政策解释和引用，不参与小时数计算。符合条件时系统保存 `WAITING_MATERIALS` 状态并立即显示材料窗口。用户必须在材料表中自行填写自由文本“问题类型”，系统和 LLM 均不推断该字段。窗口提示用户尽量提供商品问题照片或视频、商品包装整体照片和快递面单照片，后端只要求至少上传一个文件，具体材料有效性由人工客服审核。材料完整且用户点击提交或明确回复“确认提交”后才进入 `SUBMITTED`，否则保持 `WAITING_MATERIALS`。

售后流程在调用 Agent 前执行确定性订单状态校验：未付款订单不创建退款申请；已取消、已关闭、已退款订单拒绝重复申请；已付款未发货订单进入取消/仅退款申请且不使用签收时效、无需强制上传凭证；已发货未签收订单引导至物流异常、拒收或拦截流程；只有已签收订单才进入政策检索和 2/48 小时时效判断。同一客户的同一订单商品存在活动中或已提交的售后申请时，系统返回原申请，不重复创建。确认、补充材料和提交时会重新读取订单状态，防止处理期间订单状态变化。

活动案件范围覆盖从 `DRAFT` 到 `EXECUTION_FAILED` 的全部非终态，包括人工审核和外部执行阶段。应用查询与 PostgreSQL 部分唯一索引共同保证同一客户、订单、商品项最多只有一张活动案件；请求幂等键继续用于防止同一次请求重复执行。

售后接口：

- `POST /after-sales/cases`：创建售后草稿
- `GET /after-sales/cases`：查询当前客户的申请列表；聊天窗口携带 `conversation_id` 参数，只获取当前会话的申请
- `GET /after-sales/cases/{case_no}`：查询申请详情与状态日志
- `PATCH /after-sales/cases/{case_no}/materials`：保存用户选择的处理方式、问题类型与补充描述
- `POST /after-sales/cases/{case_no}/evidence`：上传 JPG、PNG、WebP、MP4 或 MOV 凭证到 MinIO
- `POST /after-sales/cases/{case_no}/submit`：校验凭证后提交申请

政策冲突或高风险判断会自动创建人工工单。已提交申请由人工工作台执行 `SUBMITTED → UNDER_REVIEW → APPROVED/REJECTED` 审批；审批通过后写入持久化 Outbox，由独立 Worker 按 `APPROVED → EXECUTING → CANCEL_PENDING（按需）→ REFUND_PENDING → COMPLETED` 执行。失败进入 `EXECUTION_FAILED`，可从工作台重试。每个 OMS 取消与支付退款动作都有稳定幂等键和独立操作记录，防止 Worker 重试造成重复处理。

人工工作台支持取消并退款、仅退款、退货退款、换货、补发和维修。退款动作调用 Mock Payment；换货、补发、维修和退货单创建调用 Mock OMS，并记录独立的幂等操作。当前适配器均为本地开发 Mock，不会修改真实商城或向真实用户打款。替换为真实系统时实现 `OmsAdapter`、`PaymentAdapter` 和 `InvoiceAdapter` 协议即可复用现有状态机与审批页面。

### 多模态售后材料预审

上传图片或视频后，系统会在同一事务中创建 `after_sales.evidence.analyze` Outbox 事件，由售后 Worker 异步调用 Kimi K2.6 进行预审。预审会结构化提取：

- 媒体清晰度，以及商品问题是否直接可见
- 商品、整体包装和快递面单是否可见
- 可见问题摘要和需要人工关注的材料异常
- 面单运单号；识别后由后端确定性地与 OMS/订单库运单号核对

预审结果只显示在人工工作台，客户接口仅返回 `analysis_status`。模型不会填写客户的“问题类型”，不会判断退款资格、责任归属或欺诈，也不会自动批准/驳回申请；预审失败同样不改变售后案件状态，人工仍可查看原始文件继续审核。模型输出的姓名、手机号和地址被提示禁止返回，服务不会记录原始媒体、模型请求体或 API Key，持久化错误只保留异常类型。

相关配置：

```env
EVIDENCE_ANALYSIS_ENABLED=true
EVIDENCE_ANALYSIS_TIMEOUT_SECONDS=90
EVIDENCE_ANALYSIS_PROMPT_VERSION=evidence-precheck-v1
```

`AGENT_MODE=live` 时调用配置的 Kimi 多模态模型；`mock` 时返回确定性模拟预审结果。图片支持 JPG、PNG、WebP，视频支持 MP4、MOV。材料预审和退款执行使用不同 `event_type`，不会互相误消费。

人工审批接口：

- `GET /admin/after-sales/cases`：按状态查看申请
- `POST /admin/after-sales/cases/{case_no}/start-review`：领取并开始审核
- `POST /admin/after-sales/cases/{case_no}/approve`：审批通过并写入执行 Outbox
- `POST /admin/after-sales/cases/{case_no}/reject`：驳回申请
- `POST /admin/after-sales/cases/{case_no}/retry`：重试失败任务
- `GET /admin/after-sales/evidence/{evidence_id}`：鉴权查看凭证

管理接口使用 `X-Admin-Token` 和 `X-Admin-ID`。开发环境可不配置 Token；生产环境必须设置 `AFTER_SALES_ADMIN_TOKEN`。

## 快速启动

需要 Docker Compose；以下命令在项目根目录运行，首次构建和加载本地 BGE 模型需要下载依赖。

1. 尚无 `.env` 时，从模板创建本地配置。示例已设置 `AGENT_MODE=mock`，不会调用付费模型；已有 `.env` 则先核对其中的 `AGENT_MODE`：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

2. 如需调用 Kimi，在本地 `.env` 中设置 `AGENT_MODE=live` 和 `KIMI_API_KEY`；保持 `mock` 可先验证页面和业务流程。

3. 启动完整环境：

```powershell
docker compose up -d --build --wait
```

4. 首次启动后导入政策与演示订单，并为演示客户创建登录账号。最后一条命令会交互式读取至少 12 位密码：

```powershell
docker compose exec backend python -m backend.app.knowledge.cli /app/knowledge_docs
docker compose exec backend python -m backend.app.orders.seed
docker compose exec backend python -m backend.app.security.provision demo 00000000-0000-4000-8000-000000000001
```

打开 `http://localhost:3000`，用 `demo` 和刚设置的密码登录。可以先询问“订单 ORD-20260918-001 的物流到哪了？”或“七天无理由退货规则是什么？”。重复执行账号创建命令会重置密码并使旧 Session 失效。导入过的演示订单不会刷新签收时间，时效类案例会随时间过期。

服务地址：

- 对话页面：http://localhost:3000
- 售后人工工作台：http://localhost:3000/admin
- FastAPI文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- MinIO控制台：http://localhost:9001
- PostgreSQL宿主机端口：`15432`
- Redis宿主机端口：`16379`

对话接口同时保留 `/api/v1` 前缀别名，供当前前端与后续版本化客户端使用。容器内仍使用 PostgreSQL `5432` 和 Redis `6379`。

当前前端会访问同一主机的 `8000` 端口，以上步骤面向本机演示。若使用域名、HTTPS 或反向代理部署，需要先调整前端 API 地址与代理、CORS、Cookie 配置；这份 Compose 配置不提供完整的生产部署方案。

> MinIO 上游社区镜像已于 2026 年 9 月撤下。本项目为本地开发固定使用 `bitnamilegacy/minio:2025.7.23-debian-12-r5`；生产部署前应重新评估受维护的 S3 兼容存储或商业镜像。

## 本地开发

项目目标运行时为 Python 3.12。后端安装与运行：

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d postgres redis minio
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
alembic upgrade head
uvicorn backend.app.main:app --reload --port 8000
```

模板中的数据库、Redis 和 MinIO 地址使用宿主机端口，因此后端可以直接在宿主机运行；默认 `AGENT_TASK_BACKEND=inline`，不需要单独启动 RabbitMQ Agent Worker。如果已有 `.env`，应核对其中的连接地址与 `AGENT_MODE`。完整的异步队列、售后执行与预审链路请使用上面的 Docker Compose 快速启动。

前端：

```powershell
cd frontend
npm install
npm run dev
```

## 测试

```powershell
.venv\Scripts\python.exe -m pytest
cd frontend
npm run lint
npm run build
```

默认测试使用 SQLite、内存事件 Broker 和 Mock Agent，不依赖外部服务，也不会调用真实模型。Redis 集成测试仅在设置隔离的 `REDIS_TEST_URL` 时运行，否则跳过。

## 数据库迁移

```powershell
alembic upgrade head
alembic downgrade -1
```

首个迁移创建：

- `conversations`
- `messages`

第二个迁移新增：

- `conversations.customer_id`
- `agent_runs`（状态、模型、工具次数、脱敏 Trace）
- `human_tickets`

第三个迁移启用 `vector` 扩展并新增：

- `knowledge_documents`
- `knowledge_chunks`
- 512维余弦距离 HNSW 索引

后续迁移新增：

- `orders`、`order_items`、`shipments`
- `after_sales_cases`
- `after_sales_evidence`
- `after_sales_action_logs`
- `after_sales_reviews`
- `after_sales_operations`
- `outbox_events`
- 售后凭证多模态预审状态、结构化结果、模型及提示词版本
- 同一客户、订单、商品项的活动售后案件部分唯一索引

## 目录

```text
backend/app/           FastAPI应用
  agents/              Supervisor、工具契约、运行时与输入检查
  api/                 登录、会话、售后、知识库与SSE接口
  knowledge/           文档解析、向量生成、混合召回与重排
  messaging/           Outbox 与 RabbitMQ 命令
  security/            Session、限流与熔断
  services/            业务领域服务
  workers/             Agent 回复与售后执行 Worker
  evaluation/          Agent 回归评测
  rag_evaluation/      知识检索评测
frontend/              Next.js对话页面与人工工作台
migrations/            Alembic迁移
tests/                 后端单元与集成测试
docs/                  评测、队列与项目说明
knowledge_docs/        演示政策文档
loadtests/             负载测试脚本
```

## 安全

- `.env`、本地评测产物、构建产物和依赖目录均已通过 Git 忽略；脱敏 Agent Trace 存储在数据库中。
- `.dockerignore` 阻止本地密钥进入Docker构建上下文。
- `.env.example` 包含本地开发用的固定数据库、RabbitMQ 和 MinIO 口令，以及外部 API 的空值或占位值；不得写入真实密钥。对外部署前必须更换这些固定口令并配置管理员 Token。
- 默认使用 Mock Agent；需要真实模型时显式设置 `AGENT_MODE=live` 和 `KIMI_API_KEY`。OMS、支付和发票适配器目前仍为 Mock。
