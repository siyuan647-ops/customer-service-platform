# 自动化评测体系

本项目的 Evaluation Harness 是独立于线上会话 API 的回归执行器。它直接运行真实的
Supervisor、状态机、工具参数模型、After-sales Agent 和回复生成逻辑，同时把 OMS、知识库、
工单及售后案件服务替换为每条用例独享的确定性 Fake，避免修改 PostgreSQL、Redis、MinIO，
也不会向真实订单、支付或人工工单系统写数据。

## 数据集

`backend/app/evaluation/dataset.py` 是有版本的数据集源，当前版本为
`ecommerce-customer-service-v2.0.0`，共 200 条提示文本唯一、ID 稳定的 Pydantic 用例：

| 类别 | 数量 | 重点验证 |
|---|---:|---|
| 正常业务类 | 80 | 订单、物流、政策、售后、改址、催发货、发票、进度、转人工 |
| 信息缺失类 | 40 | 八类订单相关业务缺少订单号时不调用工具并追问 |
| 售后政策边界 | 30 | 2/48 小时、订单状态、重复案件、多商品与确认提交 |
| 权限攻击类 | 20 | 所有订单相关工具的归属校验和 IDOR 数据隔离 |
| 提示注入类 | 20 | 系统提示词、密钥、绕过指令请求的拒绝 |
| 工具异常类 | 10 | 订单、物流、知识库、工单与客户业务服务的超时和降级 |

数据集覆盖 12 个 Supervisor Intent 和 10 个 Tool Runtime 工具。每条用例声明期望意图、
严格工具序列、关键参数、工作流状态、结构化业务事实、引用要求、转人工要求、
禁止输出内容、Token 上限和延迟上限。订单签收时间按执行时刻动态生成，因此 2/48 小时边界
不会随日期失效。

## 执行方式

安装或重新安装开发依赖后，可使用模块或命令入口：

```powershell
.\.venv\Scripts\python.exe -m backend.app.evaluation --mode mock
agent-eval --mode mock
```

常用的定向调试：

```powershell
agent-eval --mode mock --category policy_boundary
agent-eval --mode mock --case-id boundary-deadline-001
agent-eval --mode mock --limit 10
```

Mock 模式不调用 Kimi，适合提交前和 CI 中全量执行。`live-fixture` 模式只将 LLM 切换为
真实 Kimi，订单和知识库仍使用确定性评测夹具，适合单独分析模型波动：

```powershell
agent-eval --mode live-fixture --category normal_order --limit 10
```

Live 模式会真实调用当前 `.env` 中的 Kimi，并使用配置的 PostgreSQL/pgvector、BGE Embedding、
BM25 与 RRF 知识检索；订单、支付、OMS 等外部业务系统仍使用每条用例隔离的确定性夹具，
避免评测修改真实订单与资金状态：

```powershell
agent-eval --mode live --category normal_order --limit 10 `
  --input-cost-per-million 0 `
  --output-cost-per-million 0
```

两种 Live 模式都要求 `KIMI_API_KEY`，建议先抽样再执行 200 条。单条请求总时延会被记录；当前
Token 数量使用项目统一的保守估算器计算，成本根据命令行传入的每百万输入/输出 Token 单价
估算。模型供应商价格变化时无需修改代码，只需传入最新单价。

## 检查项与门禁

每条用例执行以下确定性检查：

- `intent_accuracy`：意图是否等于期望值；
- `tool_accuracy`：工具调用名称和顺序是否一致；
- `parameter_validity`：Pydantic 参数校验是否通过，关键参数的脱敏指纹是否一致；
- `policy_reference_validity`：回复与结构化结论引用是否能回指本次 RAG 证据；
- `sensitive_data_safety`：回复和安全 Trace 中是否存在密钥模式或越权订单数据；
- `refund_promise_safety`：未授权场景是否出现“已退款成功”等错误承诺；
- `handoff_accuracy`：高风险场景是否创建人工工单；
- `workflow_status`、`error_handling`：状态分流和异常降级是否符合预期；
- `workflow_facts`：时效、原因码、材料表单、发票和售后状态等结构化结果是否符合预期；
- `latency_budget`、`token_budget`：时延和估算 Token 是否超限。

质量门禁配置位于 `evals/baseline.json`。Mock 全量回归要求 200/200 和所有指标 100%；Live
模式允许自然语言模型存在有限波动，但敏感信息、参数合法性、错误退款承诺和异常处理保持
100% 要求。修改 Prompt、模型、工具、状态机或知识库文件都会改变报告中的
`system_fingerprint`，便于比较不同版本。

报告输出到被 Git 忽略的 `artifacts/evaluation/latest.json` 和 `latest.md`。JSON 用于 CI
归档和机器比较；Markdown 包含汇总、门禁原因以及每条失败用例的输入、意图、工具与失败项。
命令在门禁失败时返回退出码 1，可直接阻止合并或发布。
