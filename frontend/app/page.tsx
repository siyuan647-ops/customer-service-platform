"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import Link from "next/link";

type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

type AfterSalesCaseSummary = {
  case_no: string;
  case_type: "refund" | "return" | "exchange" | "reship" | "repair" | null;
  status: string;
  problem_type: string | null;
  problem_description: string | null;
  evidence_required: boolean;
  evidence_deadline_at: string | null;
  deadline_status: string | null;
};

type CustomerServiceRequest = {
  request_no: string;
  order_id: string;
  request_type: "address_change" | "shipment_reminder" | "invoice_application";
  status: string;
  result_payload: Record<string, string>;
};

function getApiBase(): string {
  if (typeof window === "undefined") return "/api/v1";
  return `${window.location.protocol}//${window.location.hostname}:8000/api/v1`;
}

function getConversationId(customerId: string): string {
  const key = `customer-service-conversation-id-v3:${customerId}`;
  const existing = window.localStorage.getItem(key);
  if (existing) return existing;
  const created = crypto.randomUUID();
  window.localStorage.setItem(key, created);
  return created;
}

export default function Home() {
  const [conversationId, setConversationId] = useState<string>();
  const [conversationReady, setConversationReady] = useState(false);
  const [customerId, setCustomerId] = useState<string>();
  const [authChecking, setAuthChecking] = useState(true);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [authError, setAuthError] = useState("");
  const [authBusy, setAuthBusy] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState("");
  const [connected, setConnected] = useState(false);
  const [activeCaseNo, setActiveCaseNo] = useState<string>();
  const [evidenceFiles, setEvidenceFiles] = useState<File[]>([]);
  const [evidenceRequired, setEvidenceRequired] = useState(false);
  const [problemType, setProblemType] = useState("");
  const [caseType, setCaseType] = useState("");
  const [problemDescription, setProblemDescription] = useState("");
  const [evidenceDeadlineAt, setEvidenceDeadlineAt] = useState<string>();
  const [caseAction, setCaseAction] = useState("");
  const [caseConfirmation, setCaseConfirmation] = useState("");
  const [caseBusy, setCaseBusy] = useState(false);
  const [activeOperation, setActiveOperation] = useState<CustomerServiceRequest>();
  const [operationAction, setOperationAction] = useState("");
  const [address, setAddress] = useState({
    recipient: "", phone: "", province: "", city: "", district: "", detail: "",
  });
  const [invoice, setInvoice] = useState({
    invoice_type: "electronic_general",
    title_type: "personal",
    title: "",
    tax_number: "",
    email: "",
  });
  const bottomRef = useRef<HTMLDivElement>(null);

  async function restoreConversation(id: string) {
    const conversation = getConversationId(id);
    setConversationId(conversation);
    try {
      const response = await fetch(`${getApiBase()}/conversations/${conversation}`, {
        credentials: "include",
      });
      if (response.ok) {
        setMessages((await response.json()).messages);
        setConversationReady(true);
        return;
      }
    } catch {
      // Login remains valid if restoring an earlier conversation fails.
    }
    setMessages([]);
    setConversationReady(false);
  }

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void fetch(`${getApiBase()}/auth/session`, { credentials: "include" })
        .then(async (response) => {
          if (response.ok) {
            const id = (await response.json()).customer_id;
            await restoreConversation(id);
            setCustomerId(id);
          }
        })
        .catch(() => setAuthError("无法连接登录服务"))
        .finally(() => setAuthChecking(false));
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  async function login(event: FormEvent) {
    event.preventDefault();
    setAuthBusy(true);
    setAuthError("");
    try {
      const response = await fetch(`${getApiBase()}/auth/session`, {
        method: "POST", credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) throw new Error(response.status === 429 ? "登录尝试过多，请稍后再试" : "账号或密码错误");
      const id = (await response.json()).customer_id;
      await restoreConversation(id);
      setCustomerId(id);
      setPassword("");
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "登录失败");
    } finally {
      setAuthBusy(false);
    }
  }

  async function logout() {
    await fetch(`${getApiBase()}/auth/session`, { method: "DELETE", credentials: "include" });
    setCustomerId(undefined);
    setConversationReady(false);
    setConversationId(undefined);
    setMessages([]);
    setSendError("");
    setConnected(false);
    setActiveCaseNo(undefined);
    setActiveOperation(undefined);
    setCaseAction("");
    setOperationAction("");
    setEvidenceFiles([]);
  }

  useEffect(() => {
    if (!conversationId || !customerId || !conversationReady) return;
    async function refreshActiveCase() {
      try {
        const params = new URLSearchParams({ conversation_id: conversationId! });
        const response = await fetch(
          `${getApiBase()}/after-sales/cases?${params.toString()}`,
          { credentials: "include" },
        );
        if (!response.ok) return;
        const cases = (await response.json()) as AfterSalesCaseSummary[];
        const active = cases.find((item) =>
          ["WAITING_MATERIALS", "DRAFT"].includes(item.status),
        );
        if (!active) {
          setActiveCaseNo(undefined);
          setEvidenceRequired(false);
          setProblemType("");
          setProblemDescription("");
          setEvidenceDeadlineAt(undefined);
          return;
        }
        setActiveCaseNo(active.case_no);
        setCaseType(active.case_type ?? "");
        setEvidenceRequired(active.evidence_required);
        setProblemType(active.problem_type ?? "");
        setProblemDescription(active.problem_description ?? "");
        setEvidenceDeadlineAt(active.evidence_deadline_at ?? undefined);
      } catch {
        // Chat remains usable if the optional case panel cannot be refreshed.
      }
    }
    async function refreshActiveOperation() {
      try {
        const params = new URLSearchParams({ conversation_id: conversationId! });
        const response = await fetch(
          `${getApiBase()}/service-requests?${params.toString()}`,
          { credentials: "include" },
        );
        if (!response.ok) return;
        const rows = (await response.json()) as CustomerServiceRequest[];
        setActiveOperation(rows.find((item) => item.status === "DRAFT"));
      } catch {
        // Optional business forms must not make chat unavailable.
      }
    }
    void refreshActiveCase();
    void refreshActiveOperation();
    const source = new EventSource(
      `${getApiBase()}/conversations/${conversationId}/events`,
      { withCredentials: true },
    );
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false);
    source.addEventListener("user_message.created", (raw) => {
      const data = JSON.parse((raw as MessageEvent).data);
      setMessages((current) =>
        current.some((item) => item.id === data.message_id)
          ? current
          : [...current, { id: data.message_id, role: "user", content: data.content }],
      );
    });
    source.addEventListener("assistant.started", (raw) => {
      const data = JSON.parse((raw as MessageEvent).data);
      setMessages((current) =>
        current.some((item) => item.id === data.run_id)
          ? current.map((item) =>
              item.id === data.run_id ? { ...item, content: "" } : item,
            )
          : [...current, { id: data.run_id, role: "assistant", content: "" }],
      );
    });
    source.addEventListener("assistant.delta", (raw) => {
      const data = JSON.parse((raw as MessageEvent).data);
      setMessages((current) =>
        current.map((item) =>
          item.id === data.run_id ? { ...item, content: item.content + data.delta } : item,
        ),
      );
    });
    source.addEventListener("assistant.completed", (raw) => {
      const data = JSON.parse((raw as MessageEvent).data);
      setMessages((current) =>
        current.map((item) =>
          item.id === data.run_id
            ? { id: data.message_id, role: "assistant", content: data.content }
            : item,
        ),
      );
      void refreshActiveCase();
      void refreshActiveOperation();
      setSending(false);
    });
    source.addEventListener("assistant.failed", () => {
      setSending(false);
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "assistant", content: "处理失败，请稍后重试。" },
      ]);
    });
    return () => source.close();
  }, [conversationId, customerId, conversationReady]);

  useEffect(() => {
    // Some newer browsers return a Promise from scrollIntoView. An effect may
    // only return a cleanup function, so never return the browser result.
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    if (!caseConfirmation) return;
    const timer = window.setTimeout(() => setCaseConfirmation(""), 6000);
    return () => window.clearTimeout(timer);
  }, [caseConfirmation]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const content = input.trim();
    if (!content || !conversationId || !customerId || sending) return;
    setInput("");
    setSendError("");
    setSending(true);
    try {
      const response = await fetch(
        `${getApiBase()}/conversations/${conversationId}/messages`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content }),
        },
      );
      if (response.status === 401) {
        setCustomerId(undefined);
        throw new Error("登录已过期，请重新登录");
      }
      if (response.status === 429) {
        throw new Error(`发送过于频繁，请 ${response.headers.get("Retry-After") ?? "稍后"} 秒后重试`);
      }
      if (!response.ok) throw new Error(`发送失败（HTTP ${response.status}）`);
      const result = await response.json();
      setConversationReady(true);
      setMessages((current) =>
        current.some((item) => item.id === result.message.id)
          ? current
          : [...current, { id: result.message.id, role: "user", content }],
      );
    } catch (error) {
      setSending(false);
      setInput(content);
      setSendError(error instanceof Error ? error.message : "发送失败，请稍后重试");
    }
  }

  async function uploadEvidence() {
    if (!activeCaseNo || !customerId || evidenceFiles.length === 0 || caseBusy) return;
    setCaseBusy(true);
    setCaseAction("");
    try {
      for (let index = 0; index < evidenceFiles.length; index += 1) {
        setCaseAction(`正在上传 ${index + 1}/${evidenceFiles.length}…`);
        const body = new FormData();
        body.append("file", evidenceFiles[index]);
        const response = await fetch(
          `${getApiBase()}/after-sales/cases/${activeCaseNo}/evidence`,
          { method: "POST", credentials: "include", body },
        );
        if (!response.ok) {
          if (response.status === 429) {
            throw new Error(`上传过于频繁，请 ${response.headers.get("Retry-After") ?? "稍后"} 秒后重试`);
          }
          const error = await response.json();
          throw new Error(error.detail || `HTTP ${response.status}`);
        }
      }
      setCaseAction(`已上传 ${evidenceFiles.length} 个凭证，可以提交售后申请。`);
      setEvidenceFiles([]);
    } catch (error) {
      setCaseAction(error instanceof Error ? error.message : "凭证上传失败");
    } finally {
      setCaseBusy(false);
    }
  }

  async function submitAfterSalesCase() {
    if (!activeCaseNo || !customerId || caseBusy) return;
    if (!problemType.trim()) {
      setCaseAction("请填写问题类型。");
      return;
    }
    if (!caseType) {
      setCaseAction("请选择希望的售后处理方式。");
      return;
    }
    setCaseBusy(true);
    setCaseAction("");
    try {
      const detailsResponse = await fetch(
        `${getApiBase()}/after-sales/cases/${activeCaseNo}/materials`,
        {
          method: "PATCH",
          credentials: "include",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            case_type: caseType,
            problem_type: problemType.trim(),
            problem_description: problemDescription.trim() || undefined,
          }),
        },
      );
      if (!detailsResponse.ok) {
        const error = await detailsResponse.json();
        throw new Error(error.detail || `HTTP ${detailsResponse.status}`);
      }
      const response = await fetch(
        `${getApiBase()}/after-sales/cases/${activeCaseNo}/submit`,
        { method: "POST", credentials: "include" },
      );
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || `HTTP ${response.status}`);
      }
      const result = await response.json();
      setActiveCaseNo(undefined);
      setEvidenceFiles([]);
      setEvidenceRequired(false);
      setProblemType("");
      setCaseType("");
      setProblemDescription("");
      setEvidenceDeadlineAt(undefined);
      setCaseAction("");
      setCaseConfirmation(`售后申请 ${result.case_no} 已提交。`);
    } catch (error) {
      setCaseAction(error instanceof Error ? error.message : "售后申请提交失败");
    } finally {
      setCaseBusy(false);
    }
  }

  async function submitOperation() {
    if (!activeOperation || !customerId || caseBusy) return;
    setCaseBusy(true);
    setOperationAction("");
    try {
      const isAddress = activeOperation.request_type === "address_change";
      const payload = isAddress
        ? address
        : { ...invoice, tax_number: invoice.tax_number || undefined };
      const response = await fetch(
        `${getApiBase()}/service-requests/${activeOperation.request_no}/${isAddress ? "address" : "invoice"}`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || `HTTP ${response.status}`);
      }
      const result = (await response.json()) as CustomerServiceRequest;
      setOperationAction(
        isAddress
          ? `收货地址修改完成，申请号：${result.request_no}`
          : `发票已开具，申请号：${result.request_no}`,
      );
      setActiveOperation(undefined);
    } catch (error) {
      setOperationAction(error instanceof Error ? error.message : "业务申请提交失败");
    } finally {
      setCaseBusy(false);
    }
  }

  if (authChecking) return <main className="shell"><p>正在检查登录状态…</p></main>;
  if (!customerId) return (
    <main className="shell">
      <section className="chat-card" style={{ padding: 32, maxWidth: 440, margin: "auto" }}>
        <h1>登录智能客服</h1>
        <form onSubmit={login} style={{ display: "grid", gap: 12 }}>
          <label>账号<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" required /></label>
          <label>密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required /></label>
          <button type="submit" disabled={authBusy}>{authBusy ? "登录中…" : "登录"}</button>
        </form>
        {authError && <p role="alert">{authError}</p>}
      </section>
    </main>
  );

  return (
    <main className="shell">
      <section className="chat-card">
        <header className="chat-header">
          <div>
            <span className="eyebrow">COMMERCE COPILOT</span>
            <h1>智能客服</h1>
            <p>订单、物流与售后问题，我会一步步帮你处理。</p>
          </div>
          <span className={`connection ${connected ? "online" : ""}`}>
            <i /> {connected ? "实时连接" : "正在连接"}
          </span>
          <Link className="workbench-link" href="/admin">客服工作台</Link>
          <button type="button" onClick={() => void logout()}>退出登录</button>
        </header>
        <div className="messages" aria-live="polite">
          {messages.length === 0 && (
            <div className="welcome">
              <div className="welcome-mark">CS</div>
              <h2>今天需要什么帮助？</h2>
              <p>试试：“查询订单 ORD-20260918-001 的物流状态”</p>
            </div>
          )}
          {messages.map((message) => (
            <article key={message.id} className={`message ${message.role}`}>
              <span>{message.role === "user" ? "你" : "客服"}</span>
              <p>{message.content || "正在思考…"}</p>
            </article>
          ))}
          <div ref={bottomRef} />
        </div>
        {activeCaseNo && (
          <section className="evidence-panel" aria-label="售后凭证">
            <div>
              <strong>售后申请 {activeCaseNo}</strong>
              <span>
                {evidenceDeadlineAt
                  ? `系统已根据签收时间完成时效校验，举证截止：${new Date(evidenceDeadlineAt).toLocaleString("zh-CN")}。`
                  : "请上传照片或视频后提交。"}
              </span>
            </div>
            {evidenceRequired ? (
              <aside className="evidence-guidance">
                <strong>请尽量提供以下材料</strong>
                <ul>
                  <li>商品问题的清晰照片或视频</li>
                  <li>商品包装整体照片</li>
                  <li>快递面单照片</li>
                </ul>
                <p>至少上传一个文件；材料将由人工客服审核，请确保内容清晰、完整。</p>
              </aside>
            ) : (
              <aside className="evidence-guidance">
                <strong>此申请无需强制上传照片或视频</strong>
                <p>请填写问题类型和补充说明，确认后即可提交。</p>
              </aside>
            )}
            <label>
              <span>希望的处理方式（必填）</span>
              <select value={caseType} onChange={(event) => setCaseType(event.target.value)}>
                <option value="" disabled>请选择</option>
                <option value="refund">仅退款</option>
                <option value="return">退货退款</option>
                <option value="exchange">换货</option>
                <option value="reship">补发</option>
                <option value="repair">维修</option>
              </select>
            </label>
            <label>
              <span>问题类型（必填）</span>
              <input
                type="text"
                value={problemType}
                minLength={2}
                maxLength={100}
                placeholder="例如：运输破损、商品少件、无法正常使用"
                onChange={(event) => setProblemType(event.target.value)}
              />
            </label>
            <label className="problem-description">
              <span>补充说明</span>
              <textarea
                rows={2}
                value={problemDescription}
                maxLength={1000}
                placeholder="例如：签收后开箱时发现果实破损"
                onChange={(event) => setProblemDescription(event.target.value)}
              />
            </label>
            {evidenceRequired && (
              <>
                <label className="evidence-upload">
                  <span>上传照片或视频</span>
                  <input
                    type="file"
                    multiple
                    accept="image/jpeg,image/png,image/webp,video/mp4,video/quicktime"
                    onChange={(event) =>
                      setEvidenceFiles(Array.from(event.target.files ?? []))
                    }
                  />
                </label>
                <button
                  type="button"
                  disabled={evidenceFiles.length === 0 || caseBusy}
                  onClick={uploadEvidence}
                >
                  上传凭证
                </button>
              </>
            )}
            <button type="button" disabled={caseBusy} onClick={submitAfterSalesCase}>
              提交申请
            </button>
            {caseAction && <p>{caseAction}</p>}
          </section>
        )}
        {!activeCaseNo && caseConfirmation && (
          <section className="evidence-panel operation-panel" aria-live="polite">
            <p>{caseConfirmation}</p>
          </section>
        )}
        {activeOperation && (
          <section className="evidence-panel operation-panel" aria-label="业务申请表单">
            <div>
              <strong>
                {activeOperation.request_type === "address_change" ? "修改收货地址" : "申请发票"}
              </strong>
              <span>订单 {activeOperation.order_id} · {activeOperation.request_no}</span>
            </div>
            {activeOperation.request_type === "address_change" ? (
              <>
                {(["recipient", "phone", "province", "city", "district", "detail"] as const).map((field) => (
                  <label key={field}>
                    <span>{{ recipient: "收件人", phone: "手机号", province: "省份", city: "城市", district: "区县", detail: "详细地址" }[field]}</span>
                    <input
                      value={address[field]}
                      onChange={(event) => setAddress((current) => ({ ...current, [field]: event.target.value }))}
                    />
                  </label>
                ))}
              </>
            ) : (
              <>
                <label><span>发票类型</span><select value={invoice.invoice_type} onChange={(event) => setInvoice((current) => ({ ...current, invoice_type: event.target.value }))}><option value="electronic_general">电子普通发票</option><option value="vat_special">增值税专用发票</option></select></label>
                <label><span>抬头类型</span><select value={invoice.title_type} onChange={(event) => setInvoice((current) => ({ ...current, title_type: event.target.value }))}><option value="personal">个人</option><option value="company">企业</option></select></label>
                <label><span>发票抬头</span><input value={invoice.title} onChange={(event) => setInvoice((current) => ({ ...current, title: event.target.value }))} /></label>
                {invoice.title_type === "company" && <label><span>纳税人识别号</span><input value={invoice.tax_number} onChange={(event) => setInvoice((current) => ({ ...current, tax_number: event.target.value }))} /></label>}
                <label><span>接收邮箱</span><input type="email" value={invoice.email} onChange={(event) => setInvoice((current) => ({ ...current, email: event.target.value }))} /></label>
              </>
            )}
            <button type="button" disabled={caseBusy} onClick={submitOperation}>确认提交</button>
            {operationAction && <p>{operationAction}</p>}
          </section>
        )}
        {!activeOperation && operationAction && (
          <section className="evidence-panel operation-panel" aria-live="polite">
            <p>{operationAction}</p>
          </section>
        )}
        <form className="composer" onSubmit={submit}>
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
            placeholder="输入你的问题…"
            rows={2}
          />
          <button disabled={!input.trim() || !conversationId || !customerId || sending}>
            {sending ? "处理中" : "发送"}
          </button>
        </form>
        {sendError && <p role="alert">{sendError}</p>}
      </section>
    </main>
  );
}
