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
  status: string;
  problem_type: string | null;
  problem_description: string | null;
  evidence_required: boolean;
  evidence_deadline_at: string | null;
  deadline_status: string | null;
};

function getApiBase(): string {
  if (typeof window === "undefined") return "/api/v1";
  return `${window.location.protocol}//${window.location.hostname}:8000/api/v1`;
}

function getConversationId(): string {
  const key = "customer-service-conversation-id-v2";
  const existing = window.localStorage.getItem(key);
  if (existing) return existing;
  const created = crypto.randomUUID();
  window.localStorage.setItem(key, created);
  return created;
}

function getCustomerId(): string {
  const configured = process.env.NEXT_PUBLIC_DEMO_CUSTOMER_ID;
  if (configured) return configured;
  const key = "customer-service-customer-id";
  const existing = window.localStorage.getItem(key);
  if (existing) return existing;
  const created = crypto.randomUUID();
  window.localStorage.setItem(key, created);
  return created;
}

export default function Home() {
  const [conversationId, setConversationId] = useState<string>();
  const [customerId, setCustomerId] = useState<string>();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [connected, setConnected] = useState(false);
  const [activeCaseNo, setActiveCaseNo] = useState<string>();
  const [evidenceFiles, setEvidenceFiles] = useState<File[]>([]);
  const [evidenceRequired, setEvidenceRequired] = useState(false);
  const [problemType, setProblemType] = useState("");
  const [problemDescription, setProblemDescription] = useState("");
  const [evidenceDeadlineAt, setEvidenceDeadlineAt] = useState<string>();
  const [caseAction, setCaseAction] = useState("");
  const [caseBusy, setCaseBusy] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setConversationId(getConversationId());
      setCustomerId(getCustomerId());
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!conversationId || !customerId) return;
    async function refreshActiveCase() {
      try {
        const params = new URLSearchParams({ conversation_id: conversationId! });
        const response = await fetch(
          `${getApiBase()}/after-sales/cases?${params.toString()}`,
          { headers: { "X-Customer-ID": customerId! } },
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
        setEvidenceRequired(active.evidence_required);
        setProblemType(active.problem_type ?? "");
        setProblemDescription(active.problem_description ?? "");
        setEvidenceDeadlineAt(active.evidence_deadline_at ?? undefined);
      } catch {
        // Chat remains usable if the optional case panel cannot be refreshed.
      }
    }
    void refreshActiveCase();
    const source = new EventSource(
      `${getApiBase()}/conversations/${conversationId}/events?customer_id=${customerId}`,
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
          ? current
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
  }, [conversationId, customerId]);

  useEffect(() => {
    // Some newer browsers return a Promise from scrollIntoView. An effect may
    // only return a cleanup function, so never return the browser result.
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const content = input.trim();
    if (!content || !conversationId || !customerId || sending) return;
    setInput("");
    setSending(true);
    try {
      const response = await fetch(
        `${getApiBase()}/conversations/${conversationId}/messages`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Customer-ID": customerId,
          },
          body: JSON.stringify({ content }),
        },
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const result = await response.json();
      setMessages((current) =>
        current.some((item) => item.id === result.message.id)
          ? current
          : [...current, { id: result.message.id, role: "user", content }],
      );
    } catch {
      setSending(false);
      setMessages((current) => [
        ...current,
        { id: crypto.randomUUID(), role: "assistant", content: "服务暂时不可用，请检查后端。" },
      ]);
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
          { method: "POST", headers: { "X-Customer-ID": customerId }, body },
        );
        if (!response.ok) {
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
    setCaseBusy(true);
    setCaseAction("");
    try {
      const detailsResponse = await fetch(
        `${getApiBase()}/after-sales/cases/${activeCaseNo}/materials`,
        {
          method: "PATCH",
          headers: {
            "Content-Type": "application/json",
            "X-Customer-ID": customerId,
          },
          body: JSON.stringify({
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
        { method: "POST", headers: { "X-Customer-ID": customerId } },
      );
      if (!response.ok) {
        const error = await response.json();
        throw new Error(error.detail || `HTTP ${response.status}`);
      }
      const result = await response.json();
      setCaseAction(`售后申请 ${result.case_no} 已提交。`);
    } catch (error) {
      setCaseAction(error instanceof Error ? error.message : "售后申请提交失败");
    } finally {
      setCaseBusy(false);
    }
  }

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
      </section>
    </main>
  );
}
