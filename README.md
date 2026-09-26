# 多角色电商智能客服平台

第4阶段已在 PostgreSQL + pgvector 政策知识库之上加入多角色编排：Supervisor Agent 负责结构化计划和最终话术，After-sales Agent 负责售后判定，Order、Knowledge、Ticket 保持为确定性领域服务。政策按“同名文件唯一当前版本”管理：重复内容幂等跳过，内容变化时原位替换分块。

## 架构

```text
Next.js chat UI
  ├─ POST /conversations/{id}/messages
  └─ SSE  /conversations/{id}/events
                     │
                  FastAPI
          ┌──────────┼──────────┐
      PostgreSQL       Redis          MinIO
       完整消息    短期上下文/SSE事件    文件
                     │
              Supervisor Agent
          结构化计划 / 最终话术
                     │
               轻量状态机
       ┌─────────────┼─────────────┐
  OrderService  KnowledgeService  TicketService
       └─────────────┬─────────────┘
                     │（售后场景）
             After-sales Agent
                结构化判定
                     │
             引用与结果确定性校验
```

POST接口先持久化用户消息并返回 `202 Accepted`。后台任务生成回复并依次发布：

- `user_message.created`
- `assistant.started`
- `assistant.delta`
- `assistant.completed`
- `assistant.failed`

Redis使用两类互相隔离的Key：Streams保留最近1000个会话事件，供SSE通过 `Last-Event-ID` 或 `cursor` 断线续传；List缓存最近 `AGENT_HISTORY_MESSAGES` 条会话上下文，默认采用24小时滑动TTL。每次读取或写入都会续期。PostgreSQL `messages` 永久保存完整对话，是事实源；Redis Key不存在、过期或读取失败时，系统从PostgreSQL加载最近消息并重新建立缓存。

Supervisor 的 Token 预算由应用层统一控制：计划生成和最终回复的最大输出均为 1024 tokens；历史上下文从最新消息开始向前选择，只保留完整消息，同时满足不超过 2560 个估算 tokens 和不超过 12 条；当前用户消息最多允许 4096 个估算 tokens。Kimi 的 OpenAI-compatible SDK 不提供本地 tokenizer，因此项目对中英文混合文本使用保守估算，模型侧仍会按实际 tokenizer 计费。

```env
SUPERVISOR_PLAN_MAX_TOKENS=1024
SUPERVISOR_RESPONSE_MAX_TOKENS=1024
AGENT_HISTORY_MAX_TOKENS=2560
AGENT_HISTORY_MESSAGES=12
AGENT_CURRENT_MESSAGE_MAX_TOKENS=4096
```

当前 Agent 标准链路为：

```text
身份绑定与输入检查 → SupervisorPlan → 状态机 → 受控领域服务
→（售后场景）After-sales Agent → 引用校验 → Supervisor 最终话术
→ 流式回复 → Message / AgentRun / Trace 持久化
```

角色与服务之间只传递 Pydantic 结构；应用运行时统一控制单工具超时和一次 Run 的最大工具调用数。Kimi 不直接执行数据库写入，人工工单由状态机通过 TicketService 创建。SDK 自带远程 Trace 默认关闭，数据库仅记录脱敏后的计划、状态转换、工具和专家判定事件。

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

重复执行只会报告 `skipped`，不会创建重复订单。

## 政策知识库

本地 `knowledge_docs` 批量导入：

```powershell
docker compose exec backend python -m backend.app.knowledge.cli /app/knowledge_docs
```

主要接口：

- `POST /knowledge/documents`：上传 UTF-8 `.md`、`.markdown`、`.txt`
- `GET /knowledge/documents`：文档列表
- `GET /knowledge/documents/{id}`：文档及来源元数据
- `POST /knowledge/search`：pgvector 余弦召回 + BM25 + RRF 融合检索

直接检索示例：

```json
{
  "query": "生鲜商品可以七天无理由退货吗",
  "top_k": 5,
  "policy_category": "refund",
  "product_category": "生鲜食品"
}
```

检索会分别执行 BGE 余弦向量召回和 Okapi BM25 召回，再使用 RRF 按两路名次融合。结果包含 `bm25_score`、`vector_score`、两路排名、来源文件、政策标题、章节和分块序号，Agent 最终回答按 `【来源：文件名，章节】` 输出引用。

默认使用本地中文语义模型 `BAAI/bge-small-zh-v1.5`，在 CPU 上生成 512 维归一化向量。模型首次使用时下载到 Docker 的 `huggingface_cache` 持久卷，后续重启直接复用。短查询会按 BGE 官方建议增加中文检索指令，政策分块不加指令。

```env
EMBEDDING_MODE=bge
EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5
EMBEDDING_DIMENSIONS=512
```

如需改用 OpenAI-compatible Embeddings 服务：

```env
EMBEDDING_MODE=openai
EMBEDDING_API_KEY=replace-me
EMBEDDING_BASE_URL=https://api.openai.com/v1
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=512
```

上传接口在本地开发环境可直接使用；对外部署前必须设置 `KNOWLEDGE_ADMIN_TOKEN`，并通过 `X-Admin-Token` 请求头传递。

订单读取会把 `order_id` 与当前 `customer_id` 一起提交给 OMS adapter；不属于当前客户的订单与不存在的订单统一返回 `ORDER_NOT_FOUND`，避免订单枚举。Docker 前端默认使用演示客户 `00000000-0000-4000-8000-000000000001`，该客户拥有演示订单 `ORD-20260918-001`、`ORD-20260920-003`、用于 48 小时举证窗口测试的 `ORD-20260926-004`、用于未发货取消退款测试的 `ORD-20260926-005`，以及用于签收后凭证退款测试的 `ORD-20260926-006`。本地单独运行前端时可在 `frontend/.env.local` 配置 `NEXT_PUBLIC_DEMO_CUSTOMER_ID`。

直接调用 API 时，POST 与会话查询必须携带 `X-Customer-ID: <UUID>`；SSE 因浏览器 EventSource 不能设置自定义 Header，使用 `customer_id=<UUID>` 查询参数。当前 UUID 仅用于开发期的会话及模拟订单归属验证；生产环境必须从服务端验证过的 JWT/Session 中取得客户身份，不能信任客户端自行提交的 `X-Customer-ID`。

## 售后申请闭环

具体订单的退款、退货、换货、补发或保修请求会先经过订单归属校验、商品选择、固定时效校验、政策检索和 AfterSales Agent 结构化判断。时效只依据订单商品的 `product_category` 和订单系统签收时间计算：`食品生鲜` 为 2 小时，其余当前商品分类为 48 小时；RAG 只负责政策解释和引用，不参与小时数计算。符合条件时系统保存 `WAITING_MATERIALS` 状态并立即显示材料窗口。用户必须在材料表中自行填写自由文本“问题类型”，系统和 LLM 均不推断该字段。窗口提示用户尽量提供商品问题照片或视频、商品包装整体照片和快递面单照片，后端只要求至少上传一个文件，具体材料有效性由人工客服审核。材料完整且用户点击提交或明确回复“确认提交”后才进入 `SUBMITTED`，否则保持 `WAITING_MATERIALS`。

售后流程在调用 Agent 前执行确定性订单状态校验：未付款订单不创建退款申请；已取消、已关闭、已退款订单拒绝重复申请；已付款未发货订单进入取消/仅退款申请且不使用签收时效、无需强制上传凭证；已发货未签收订单引导至物流异常、拒收或拦截流程；只有已签收订单才进入政策检索和 2/48 小时时效判断。同一客户的同一订单商品存在活动中或已提交的售后申请时，系统返回原申请，不重复创建。确认、补充材料和提交时会重新读取订单状态，防止处理期间订单状态变化。

活动案件范围覆盖从 `DRAFT` 到 `EXECUTION_FAILED` 的全部非终态，包括人工审核和外部执行阶段。应用查询与 PostgreSQL 部分唯一索引共同保证同一客户、订单、商品项最多只有一张活动案件；请求幂等键继续用于防止同一次请求重复执行。

售后接口：

- `POST /after-sales/cases`：创建售后草稿
- `GET /after-sales/cases`：查询当前客户的申请列表；聊天窗口携带 `conversation_id` 参数，只获取当前会话的申请
- `GET /after-sales/cases/{case_no}`：查询申请详情与状态日志
- `PATCH /after-sales/cases/{case_no}/materials`：保存用户填写的问题类型与补充描述
- `POST /after-sales/cases/{case_no}/evidence`：上传 JPG、PNG、WebP、MP4 或 MOV 凭证到 MinIO
- `POST /after-sales/cases/{case_no}/submit`：校验凭证后提交申请

政策冲突或高风险判断会自动创建人工工单。已提交申请由人工工作台执行 `SUBMITTED → UNDER_REVIEW → APPROVED/REJECTED` 审批；审批通过后写入持久化 Outbox，由独立 Worker 按 `APPROVED → EXECUTING → CANCEL_PENDING（按需）→ REFUND_PENDING → COMPLETED` 执行。失败进入 `EXECUTION_FAILED`，可从工作台重试。每个 OMS 取消与支付退款动作都有稳定幂等键和独立操作记录，防止 Worker 重试造成重复处理。

当前执行适配器为本地开发用 Mock：Mock OMS 会真实更新本项目 PostgreSQL 中的订单状态，Mock Payment 会更新支付状态，并返回可追踪的模拟外部单号，但不会向真实用户打款。替换为真实系统时只需实现 `OmsAdapter` 和 `PaymentAdapter` 协议，状态机与审批页面无需改动。

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

1. 从模板创建本地配置：

```powershell
Copy-Item .env.example .env
```

2. 默认保持 `AGENT_MODE=mock`，避免开发期间产生模型费用。如需调用Kimi，将其改为 `live` 并只在 `.env` 中填写密钥。

3. 启动完整环境：

```powershell
docker compose up --build
```

4. 首次启动后导入政策与演示订单：

```powershell
docker compose exec backend python -m backend.app.knowledge.cli /app/knowledge_docs
docker compose exec backend python -m backend.app.orders.seed
```

服务地址：

- 对话页面：http://localhost:3000
- 售后人工工作台：http://localhost:3000/admin
- FastAPI文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health
- MinIO控制台：http://localhost:9001
- PostgreSQL宿主机端口：`15432`
- Redis宿主机端口：`16379`

对话接口同时保留 `/api/v1` 前缀别名，供当前前端与后续版本化客户端使用。容器内仍使用 PostgreSQL `5432` 和 Redis `6379`。

> MinIO 上游社区镜像已于 2026 年 9 月撤下。本项目为本地开发固定使用 `bitnamilegacy/minio:2025.7.23-debian-12-r5`；生产部署前应重新评估受维护的 S3 兼容存储或商业镜像。

## 本地开发

项目目标运行时为 Python 3.12。后端安装与运行：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
alembic upgrade head
uvicorn backend.app.main:app --reload --port 8000
```

模板中的数据库和 Redis 地址已经使用宿主机端口 `15432`、`16379`，因此后端也可以直接在宿主机运行。

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

测试使用 SQLite、内存事件Broker和Mock Agent，不依赖外部服务，也不会调用真实模型。

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
  api/                 健康检查、会话和SSE路由
  knowledge/           文档解析、向量生成、导入与混合召回
  services/            Agent与会话处理服务
frontend/              Next.js对话页面
migrations/            Alembic迁移
src/kimi_agent_spike/  第0阶段Kimi兼容性验证
tests/                 后端单元与集成测试
docs/                  阶段报告
```

## 安全

- `.env`、Trace、构建产物和依赖目录均已忽略。
- `.dockerignore` 阻止本地密钥进入Docker构建上下文。
- `.env.example` 只能包含占位值，不得写入真实密钥。
- 默认使用Mock Agent；生产环境显式设置 `AGENT_MODE=live`。
