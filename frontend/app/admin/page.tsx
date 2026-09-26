"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";

type Evidence = {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  analysis_status: "NOT_REQUESTED" | "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED";
  analysis_result: {
    media_quality?: "clear" | "blurry" | "unusable";
    issue_visible?: boolean;
    detected_issues?: string[];
    product_visible?: boolean;
    package_visible?: boolean;
    waybill_visible?: boolean;
    extracted_tracking_number?: string | null;
    tracking_number_match?: boolean | null;
    summary?: string;
    risk_flags?: string[];
    confidence?: number;
  };
  analysis_model: string | null;
  analysis_prompt_version: string | null;
  analysis_error: string | null;
  analyzed_at: string | null;
};

type Review = {
  id: string;
  reviewer_id: string;
  decision: string;
  action: string | null;
  refund_amount: string | null;
  reason: string;
  created_at: string;
};

type Operation = {
  id: string;
  operation_type: string;
  provider: string;
  status: string;
  external_request_id: string | null;
  attempt_count: number;
  last_error: string | null;
};

type AdminCase = {
  case_no: string;
  customer_id: string;
  order_id: string;
  order_item_id: string;
  case_type: string;
  reason: string;
  problem_type: string | null;
  problem_description: string | null;
  status: string;
  reason_code: string | null;
  decision_reason: string | null;
  resolution_action: string | null;
  refund_amount: string | null;
  failure_reason: string | null;
  max_refund_amount: string;
  currency: string;
  created_at: string;
  submitted_at: string | null;
  evidence: Evidence[];
  reviews: Review[];
  operations: Operation[];
};

const STATUS_OPTIONS = [
  "",
  "SUBMITTED",
  "UNDER_REVIEW",
  "APPROVED",
  "EXECUTING",
  "CANCEL_PENDING",
  "REFUND_PENDING",
  "EXECUTION_FAILED",
  "COMPLETED",
  "REJECTED",
];

function apiBase() {
  if (typeof window === "undefined") return "/api/v1";
  return `${window.location.protocol}//${window.location.hostname}:8000/api/v1`;
}

function yesNo(value: boolean | undefined) {
  if (value === undefined) return "未判断";
  return value ? "是" : "否";
}

function qualityLabel(value: Evidence["analysis_result"]["media_quality"]) {
  if (value === "clear") return "清晰";
  if (value === "blurry") return "模糊";
  if (value === "unusable") return "无法识别";
  return "未判断";
}

function analysisStatusLabel(status: Evidence["analysis_status"]) {
  return {
    NOT_REQUESTED: "未启用预审",
    PENDING: "等待预审",
    PROCESSING: "预审中",
    COMPLETED: "预审完成",
    FAILED: "预审失败",
  }[status];
}

export default function AdminPage() {
  const [cases, setCases] = useState<AdminCase[]>([]);
  const [selectedNo, setSelectedNo] = useState<string>();
  const [status, setStatus] = useState("");
  const [token, setToken] = useState("");
  const [credentialsReady, setCredentialsReady] = useState(false);
  const [reviewerId, setReviewerId] = useState("demo-agent");
  const [action, setAction] = useState<"" | "cancel_and_refund" | "refund_only">("");
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("材料审核通过");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setToken(window.localStorage.getItem("after-sales-admin-token") ?? "");
      setReviewerId(
        window.localStorage.getItem("after-sales-reviewer-id") ?? "demo-agent",
      );
      setCredentialsReady(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  const headers = useCallback(
    (json = false) => ({
      ...(json ? { "Content-Type": "application/json" } : {}),
      ...(token ? { "X-Admin-Token": token } : {}),
      "X-Admin-ID": reviewerId,
    }),
    [token, reviewerId],
  );

  const loadCases = useCallback(async () => {
    try {
      const params = status ? `?status=${encodeURIComponent(status)}` : "";
      const response = await fetch(`${apiBase()}/admin/after-sales/cases${params}`, {
        headers: headers(),
        cache: "no-store",
      });
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || `HTTP ${response.status}`);
      }
      const data = (await response.json()) as AdminCase[];
      setCases(data);
      setSelectedNo((current) =>
        current && data.some((item) => item.case_no === current)
          ? current
          : data[0]?.case_no,
      );
      setMessage("");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "加载失败");
    }
  }, [headers, status]);

  useEffect(() => {
    if (!credentialsReady) return;
    const initial = window.setTimeout(() => void loadCases(), 0);
    const timer = window.setInterval(() => void loadCases(), 2500);
    return () => {
      window.clearTimeout(initial);
      window.clearInterval(timer);
    };
  }, [credentialsReady, loadCases]);

  const selected = cases.find((item) => item.case_no === selectedNo);
  const effectiveAction = action || (
    selected?.reason_code === "PRE_SHIPMENT_REFUND_ALLOWED"
      ? "cancel_and_refund"
      : "refund_only"
  );
  const effectiveAmount = amount || selected?.refund_amount || selected?.max_refund_amount || "";

  async function mutate(path: string, body?: object) {
    if (!selected || busy) return;
    setBusy(true);
    setMessage("");
    try {
      const response = await fetch(
        `${apiBase()}/admin/after-sales/cases/${selected.case_no}/${path}`,
        {
          method: "POST",
          headers: headers(body !== undefined),
          body: body === undefined ? undefined : JSON.stringify(body),
        },
      );
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || `HTTP ${response.status}`);
      }
      await loadCases();
      setMessage("操作成功，执行状态会自动刷新。");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  async function viewEvidence(item: Evidence) {
    try {
      const response = await fetch(
        `${apiBase()}/admin/after-sales/evidence/${item.id}`,
        { headers: headers() },
      );
      if (!response.ok) throw new Error("凭证加载失败");
      const url = URL.createObjectURL(await response.blob());
      window.open(url, "_blank", "noopener,noreferrer");
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "凭证加载失败");
    }
  }

  return (
    <main className="admin-shell">
      <header className="admin-topbar">
        <div>
          <h1>售后人工工作台</h1>
          <p>审核材料并通过 Mock OMS / Mock Payment 执行业务。</p>
        </div>
        <Link href="/">返回客服对话</Link>
      </header>

      <section className="admin-toolbar">
        <select value={status} onChange={(event) => setStatus(event.target.value)}>
          {STATUS_OPTIONS.map((item) => (
            <option key={item || "all"} value={item}>
              {item || "全部状态"}
            </option>
          ))}
        </select>
        <input
          value={token}
          placeholder="管理员 Token（开发环境可留空）"
          onChange={(event) => {
            setToken(event.target.value);
            window.localStorage.setItem("after-sales-admin-token", event.target.value);
          }}
        />
        <button type="button" onClick={() => void loadCases()}>刷新</button>
      </section>

      <section className="admin-grid">
        <aside className="case-list">
          {cases.length === 0 && <p>当前筛选条件下暂无售后申请。</p>}
          {cases.map((item) => (
            <button
              type="button"
              key={item.case_no}
              className={`case-row ${selectedNo === item.case_no ? "active" : ""}`}
              onClick={() => {
                setSelectedNo(item.case_no);
                setAction("");
                setAmount("");
                setMessage("");
              }}
            >
              <span className="case-row-head">
                <strong>{item.case_no}</strong>
                <i className="status-pill">{item.status}</i>
              </span>
              <span>{item.order_id} · {item.problem_type || item.case_type}</span>
              <small>{new Date(item.created_at).toLocaleString("zh-CN")}</small>
            </button>
          ))}
        </aside>

        <article className="case-detail">
          {!selected ? (
            <p>请选择一条售后申请。</p>
          ) : (
            <>
              <h2>{selected.case_no}</h2>
              <div className="detail-grid">
                <div className="detail-card"><span>状态</span><strong>{selected.status}</strong></div>
                <div className="detail-card"><span>订单</span><strong>{selected.order_id}</strong></div>
                <div className="detail-card"><span>商品项</span><strong>{selected.order_item_id}</strong></div>
                <div className="detail-card"><span>问题类型</span><strong>{selected.problem_type || "未填写"}</strong></div>
                <div className="detail-card"><span>处理动作</span><strong>{selected.resolution_action || "待审核"}</strong></div>
                <div className="detail-card"><span>最大退款</span><strong>{selected.max_refund_amount} {selected.currency}</strong></div>
              </div>

              <section className="detail-section">
                <h3>客户描述</h3>
                <p>{selected.problem_description || selected.reason}</p>
                {selected.decision_reason && <p>系统结论：{selected.decision_reason}</p>}
                {selected.failure_reason && <p>执行失败：{selected.failure_reason}</p>}
              </section>

              <section className="detail-section">
                <h3>凭证材料</h3>
                {selected.evidence.length === 0 ? <p>未上传凭证。</p> : (
                  <div className="evidence-review-list">
                    {selected.evidence.map((item) => (
                      <article className="evidence-review-card" key={item.id}>
                        <header>
                          <div>
                            <strong>{item.filename}</strong>
                            <small>{Math.ceil(item.size_bytes / 1024)} KB · {item.content_type}</small>
                          </div>
                          <span className={`analysis-pill ${item.analysis_status.toLowerCase()}`}>
                            {analysisStatusLabel(item.analysis_status)}
                          </span>
                          <button type="button" onClick={() => void viewEvidence(item)}>
                            查看原始材料
                          </button>
                        </header>

                        {(item.analysis_status === "PENDING" || item.analysis_status === "PROCESSING") && (
                          <p className="analysis-note">AI 正在预审。本结果仅辅助人工查看，不影响案件提交和人工审批。</p>
                        )}
                        {item.analysis_status === "FAILED" && (
                          <p className="analysis-note warning">AI 预审暂不可用，请直接查看原始材料并人工判断。</p>
                        )}
                        {item.analysis_status === "COMPLETED" && (
                          <div className="analysis-result">
                            <p>{item.analysis_result.summary || "未生成摘要"}</p>
                            <div className="analysis-facts">
                              <span><small>材料清晰度</small><strong>{qualityLabel(item.analysis_result.media_quality)}</strong></span>
                              <span><small>问题可见</small><strong>{yesNo(item.analysis_result.issue_visible)}</strong></span>
                              <span><small>商品可见</small><strong>{yesNo(item.analysis_result.product_visible)}</strong></span>
                              <span><small>包装可见</small><strong>{yesNo(item.analysis_result.package_visible)}</strong></span>
                              <span><small>快递面单可见</small><strong>{yesNo(item.analysis_result.waybill_visible)}</strong></span>
                              <span><small>识别运单号</small><strong>{item.analysis_result.extracted_tracking_number || "未识别"}</strong></span>
                              <span>
                                <small>运单与订单匹配</small>
                                <strong>
                                  {item.analysis_result.tracking_number_match === undefined || item.analysis_result.tracking_number_match === null
                                    ? "无法核对"
                                    : item.analysis_result.tracking_number_match ? "匹配" : "不匹配"}
                                </strong>
                              </span>
                              <span><small>模型置信度</small><strong>{Math.round((item.analysis_result.confidence ?? 0) * 100)}%</strong></span>
                            </div>
                            {(item.analysis_result.detected_issues?.length ?? 0) > 0 && (
                              <div className="analysis-list">
                                <strong>可见问题</strong>
                                <ul>{item.analysis_result.detected_issues?.map((value) => <li key={value}>{value}</li>)}</ul>
                              </div>
                            )}
                            {(item.analysis_result.risk_flags?.length ?? 0) > 0 && (
                              <div className="analysis-list warning">
                                <strong>人工关注项</strong>
                                <ul>{item.analysis_result.risk_flags?.map((value) => <li key={value}>{value}</li>)}</ul>
                              </div>
                            )}
                            <small className="analysis-meta">
                              {item.analysis_model || "未知模型"} · {item.analysis_prompt_version || "未知提示词版本"}
                              {item.analyzed_at ? ` · ${new Date(item.analyzed_at).toLocaleString("zh-CN")}` : ""}
                            </small>
                          </div>
                        )}
                      </article>
                    ))}
                  </div>
                )}
              </section>

              <section className="detail-section">
                <h3>执行记录</h3>
                {selected.operations.length === 0 ? <p>尚未执行外部操作。</p> : selected.operations.map((item) => (
                  <div className="operation-row" key={item.id}>
                    <strong>{item.operation_type}</strong> · {item.provider} · {item.status}
                    <div>尝试次数：{item.attempt_count}</div>
                    {item.external_request_id && <div>外部单号：{item.external_request_id}</div>}
                    {item.last_error && <div>错误：{item.last_error}</div>}
                  </div>
                ))}
              </section>

              <section className="admin-actions">
                <label>
                  <span>客服工号</span>
                  <input value={reviewerId} onChange={(event) => {
                    setReviewerId(event.target.value);
                    window.localStorage.setItem("after-sales-reviewer-id", event.target.value);
                  }} />
                </label>
                <label>
                  <span>处理动作</span>
                  <select value={effectiveAction} onChange={(event) => setAction(event.target.value as typeof action)}>
                    <option value="refund_only">仅退款</option>
                    <option value="cancel_and_refund">取消订单并退款</option>
                  </select>
                </label>
                <label>
                  <span>退款金额</span>
                  <input type="number" min="0.01" step="0.01" max={selected.max_refund_amount} value={effectiveAmount} onChange={(event) => setAmount(event.target.value)} />
                </label>
                <label className="full">
                  <span>审核理由</span>
                  <textarea rows={3} value={reason} onChange={(event) => setReason(event.target.value)} />
                </label>

                {selected.status === "SUBMITTED" && (
                  <button disabled={busy} type="button" onClick={() => void mutate("start-review")}>开始审核</button>
                )}
                {selected.status === "UNDER_REVIEW" && (
                  <>
                    <button disabled={busy || !effectiveAmount || !reason.trim()} type="button" onClick={() => void mutate("approve", { action: effectiveAction, refund_amount: effectiveAmount, reason })}>审批通过并执行</button>
                    <button className="danger" disabled={busy || !reason.trim()} type="button" onClick={() => void mutate("reject", { reason })}>驳回申请</button>
                  </>
                )}
                {selected.status === "EXECUTION_FAILED" && (
                  <button className="secondary" disabled={busy} type="button" onClick={() => void mutate("retry")}>重试执行</button>
                )}
                {message && <p className="admin-message">{message}</p>}
              </section>
            </>
          )}
        </article>
      </section>
    </main>
  );
}
