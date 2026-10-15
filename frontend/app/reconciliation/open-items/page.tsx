"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  api, ApiError, AssignableUser, OpenItemCategory, OpenItemDetailOut, OpenItemEventOut, OpenItemStatus,
  ReconciliationOpenItemOut,
} from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

// Each exception type has its own label and colour so the queue never blurs "ambiguous" with "unmatched" or a
// variance. A successfully matched transaction is never an open item, so it never appears here.
const CATEGORY: Record<OpenItemCategory, { label: string; colour: string; hint: string }> = {
  BANK_ONLY: { label: "Unmatched bank", colour: "#b45309", hint: "On the bank statement, no ledger transaction found" },
  LEDGER_ONLY: { label: "Unmatched ledger", colour: "#7c3aed", hint: "In the ledger, not on the bank statement" },
  AMBIGUOUS_MATCH: { label: "Ambiguous match", colour: "#dc2626", hint: "Several ledger candidates tie; needs a human decision" },
  AMOUNT_VARIANCE: { label: "Amount variance", colour: "#0369a1", hint: "Matched pair whose amounts differ" },
  TIMING_DIFFERENCE: { label: "Timing difference", colour: "#0f766e", hint: "Matched pair whose dates differ" },
  FX_DIFFERENCE: { label: "FX difference", colour: "#4f46e5", hint: "Cross-currency difference" },
  DUPLICATE: { label: "Duplicate", colour: "#be185d", hint: "Possible duplicate" },
  BANK_CHARGE: { label: "Bank charge", colour: "#475569", hint: "Bank charge" },
  INTERNAL_TRANSFER: { label: "Internal transfer", colour: "#0891b2", hint: "Internal transfer" },
  ONE_TO_MANY: { label: "Ambiguous one-to-many", colour: "#dc2626", hint: "Competing one-to-many groupings" },
  MANY_TO_ONE: { label: "Ambiguous many-to-one", colour: "#dc2626", hint: "Competing many-to-one groupings" },
  BATCH_PAYMENT: { label: "Ambiguous batch", colour: "#dc2626", hint: "Competing batch groupings" },
  MISSING_LEDGER_ENTRY: { label: "Missing ledger entry", colour: "#b45309", hint: "Missing ledger entry" },
  MISSING_BANK_ENTRY: { label: "Missing bank entry", colour: "#7c3aed", hint: "Missing bank entry" },
  EXCEPTION: { label: "Exception", colour: "#64748b", hint: "Weak-evidence match needing review" },
};

const STATUS_VIEWS: { label: string; statuses: OpenItemStatus[] }[] = [
  { label: "Actionable (open + under review)", statuses: ["OPEN", "UNDER_REVIEW"] },
  { label: "Resolved", statuses: ["RESOLVED"] },
  { label: "Closed", statuses: ["CLOSED"] },
  { label: "Rejected", statuses: ["REJECTED"] },
  { label: "All history", statuses: ["OPEN", "UNDER_REVIEW", "RESOLVED", "CLOSED", "REJECTED"] },
];

function CategoryBadge({ category }: { category: OpenItemCategory }) {
  const c = CATEGORY[category];
  return <span className="badge" title={c.hint} style={{ background: c.colour, color: "#fff" }}>{c.label}</span>;
}

function AgeCell({ item }: { item: ReconciliationOpenItemOut }) {
  const actionable = item.status === "OPEN" || item.status === "UNDER_REVIEW";
  const days = item.age_days ?? 0;
  const colour = !actionable ? undefined : days > 30 ? "var(--color-danger)" : days > 7 ? "var(--color-warning)" : undefined;
  return <span style={{ color: colour, fontWeight: colour ? 600 : undefined }}>{days}d</span>;
}

export default function OpenItemsPage() {
  const token = useAuthToken();
  const [items, setItems] = useState<ReconciliationOpenItemOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [viewIndex, setViewIndex] = useState(0);
  const [category, setCategory] = useState("");
  const [sort, setSort] = useState("oldest");
  const [minAmount, setMinAmount] = useState("");
  const [unassigned, setUnassigned] = useState(false);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<OpenItemDetailOut | null>(null);
  const [history, setHistory] = useState<OpenItemEventOut[]>([]);
  const [users, setUsers] = useState<AssignableUser[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [reference, setReference] = useState("");
  const [assignee, setAssignee] = useState("");
  const [dueDate, setDueDate] = useState("");

  const loadList = useCallback(() => {
    if (!token) return;
    setError(null);
    const params: Record<string, string | string[]> = { status: STATUS_VIEWS[viewIndex].statuses, sort, limit: "200" };
    if (category) params.category = category;
    if (minAmount) params.min_amount = minAmount;
    if (unassigned) params.unassigned = "true";
    api.listReconciliationOpenItems(token, params)
      .then(setItems)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Unable to reach the API"));
  }, [token, viewIndex, category, sort, minAmount, unassigned]);

  useEffect(loadList, [loadList]);

  const loadDetail = useCallback((id: string) => {
    if (!token) return;
    Promise.all([api.getReconciliationOpenItem(token, id), api.getReconciliationOpenItemHistory(token, id)])
      .then(([d, h]) => {
        setDetail(d);
        setHistory(h);
        api.listAssignableUsers(token, d.legal_entity_id).then(setUsers).catch(() => setUsers([]));
      })
      .catch((err) => setActionError(err instanceof ApiError ? err.message : "Unable to load the item"));
  }, [token]);

  function select(id: string) {
    setSelectedId(id);
    setActionError(null);
    setText("");
    setReference("");
    loadDetail(id);
  }

  async function act(
    action: "assign" | "start-investigation" | "comments" | "evidence" | "resolve" | "close" | "reject" | "reopen",
    body: Record<string, unknown> = {},
  ) {
    if (!token || !selectedId) return;
    setActionError(null);
    try {
      await api.actOnReconciliationOpenItem(token, selectedId, action, body);
      setText("");
      setReference("");
      loadDetail(selectedId);
      loadList();
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Action failed");
    }
  }

  const nameOf = (id: string | null) => (id ? users.find((u) => u.id === id)?.full_name ?? `${id.slice(0, 8)}…` : "—");
  const status = detail?.status;
  const actionable = status === "OPEN" || status === "UNDER_REVIEW";

  return (
    <div>
      <h1>Reconciliation work queue</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Outstanding reconciliation exceptions from executed runs. Successfully matched transactions are not listed.
        Resolving an item documents the investigation; it never posts or changes a ledger or bank record.{" "}
        <Link href="/reconciliation" style={{ color: "var(--color-accent)" }}>Back to runs</Link>
      </p>

      <div className="card" style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end" }}>
        <label>View<br />
          <select value={viewIndex} onChange={(e) => setViewIndex(Number(e.target.value))}>
            {STATUS_VIEWS.map((v, i) => <option key={v.label} value={i}>{v.label}</option>)}
          </select>
        </label>
        <label>Category<br />
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">All categories</option>
            {Object.entries(CATEGORY).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
          </select>
        </label>
        <label>Sort<br />
          <select value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="oldest">Oldest first</option>
            <option value="amount_desc">Highest amount</option>
            <option value="priority">Highest priority</option>
          </select>
        </label>
        <label>Min amount<br /><input value={minAmount} onChange={(e) => setMinAmount(e.target.value)} inputMode="decimal" style={{ width: 110 }} /></label>
        <label><input type="checkbox" checked={unassigned} onChange={(e) => setUnassigned(e.target.checked)} /> Unassigned only</label>
      </div>

      {error && <p style={{ color: "var(--color-danger)" }}>{error}</p>}

      <div style={{ display: "grid", gridTemplateColumns: selectedId ? "1.1fr 1fr" : "1fr", gap: 16, alignItems: "start" }}>
        <div className="card">
          <h2>{STATUS_VIEWS[viewIndex].label} <span style={{ color: "var(--color-text-muted)", fontWeight: 400 }}>({items.length})</span></h2>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--color-text-muted)" }}>
                <th style={{ padding: "6px 8px" }}>Type</th><th style={{ padding: "6px 8px" }}>Status</th>
                <th style={{ padding: "6px 8px" }}>Amount</th><th style={{ padding: "6px 8px" }}>Date</th>
                <th style={{ padding: "6px 8px" }}>Age</th><th style={{ padding: "6px 8px" }}>Priority</th>
                <th style={{ padding: "6px 8px" }}>Assigned</th>
              </tr>
            </thead>
            <tbody>
              {items.map((it) => (
                <tr key={it.id} onClick={() => select(it.id)}
                    style={{ cursor: "pointer", background: it.id === selectedId ? "var(--color-surface-alt, rgba(120,120,255,.12))" : undefined }}>
                  <td style={{ padding: "6px 8px" }}><CategoryBadge category={it.category} /></td>
                  <td style={{ padding: "6px 8px" }}>{it.status.replace("_", " ")}</td>
                  <td style={{ padding: "6px 8px" }}>{Number(it.amount).toLocaleString()} {it.currency_code}</td>
                  <td style={{ padding: "6px 8px" }}>{it.item_date}</td>
                  <td style={{ padding: "6px 8px" }}><AgeCell item={it} /></td>
                  <td style={{ padding: "6px 8px" }}>{it.priority === "HIGH" ? <strong style={{ color: "var(--color-danger)" }}>HIGH</strong> : it.priority}</td>
                  <td style={{ padding: "6px 8px" }}>{it.assigned_to_user_id ? "Assigned" : "—"}</td>
                </tr>
              ))}
              {items.length === 0 && (
                <tr><td colSpan={7} style={{ padding: "12px 8px", color: "var(--color-text-muted)" }}>
                  Nothing to work on here.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>

        {selectedId && detail && (
          <div className="card">
            <h2><CategoryBadge category={detail.category} /> {Number(detail.amount).toLocaleString()} {detail.currency_code}</h2>
            <p style={{ color: "var(--color-text-muted)" }}>{detail.description}</p>
            <p>Status: <strong>{detail.status.replace("_", " ")}</strong> · Age: <AgeCell item={detail} /> · Assigned to: {nameOf(detail.assigned_to_user_id)}
              {detail.due_date ? ` · Due ${detail.due_date}` : ""}</p>
            {detail.resolution_reason && <p>Resolution: {detail.resolution_reason}</p>}
            {detail.rejection_reason && <p>Rejection: {detail.rejection_reason}</p>}

            <h3>Source transactions</h3>
            {detail.bank_transaction && (
              <p><strong>Bank</strong> {detail.bank_transaction.transaction_date} · {detail.bank_transaction.entry_type} · {Number(detail.bank_transaction.amount).toLocaleString()} {detail.bank_transaction.currency_code} · {detail.bank_transaction.bank_reference ?? "no ref"} · {detail.bank_transaction.narration ?? ""}</p>
            )}
            {detail.ledger_transaction && (
              <p><strong>Ledger</strong> {detail.ledger_transaction.event_date} · {detail.ledger_transaction.direction} · {Number(detail.ledger_transaction.amount).toLocaleString()} {detail.ledger_transaction.currency_code} · {detail.ledger_transaction.reference ?? "no ref"}</p>
            )}
            {!detail.bank_transaction && !detail.ledger_transaction && <p>—</p>}

            {(detail.suggestions.length > 0 || detail.match_groups.length > 0) && (
              <>
                <h3>Engine context</h3>
                {detail.suggestions.map((s) => (
                  <p key={String(s.id)}>Suggestion {s.match_type ?? "—"} · confidence {s.confidence ?? "—"} · {s.status}<br />
                    <span style={{ color: "var(--color-text-muted)" }}>{s.reason}</span></p>
                ))}
                {detail.match_groups.map((g) => (
                  <p key={String(g.id)}>Group {String(g.relationship_type)} · {String(g.status)}{g.ambiguous ? " (ambiguous)" : ""}</p>
                ))}
              </>
            )}

            <h3>Actions</h3>
            {actionError && <p style={{ color: "var(--color-danger)" }}>{actionError}</p>}
            {actionable && (
              <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "end", marginBottom: 8 }}>
                <select value={assignee} onChange={(e) => setAssignee(e.target.value)}>
                  <option value="">Assign to…</option>
                  {users.map((u) => <option key={u.id} value={u.id}>{u.full_name}</option>)}
                </select>
                <input type="date" value={dueDate} onChange={(e) => setDueDate(e.target.value)} />
                <button className="btn-secondary" disabled={!assignee || text.trim().length < 3}
                  onClick={() => act("assign", { assignee_user_id: assignee, reason: text, due_date: dueDate || null })}>
                  {detail.assigned_to_user_id ? "Reassign" : "Assign"}
                </button>
                {status === "OPEN" && <button className="btn-secondary" onClick={() => act("start-investigation")}>Start investigation</button>}
              </div>
            )}
            <textarea rows={3} style={{ width: "100%" }} value={text} onChange={(e) => setText(e.target.value)}
              placeholder="Note / reason (resolution, rejection and reopening need a meaningful reason)" />
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
              <button className="btn-secondary" disabled={!text.trim()} onClick={() => act("comments", { note: text })}>Add comment</button>
              {status === "UNDER_REVIEW" && <button className="btn-primary" onClick={() => act("resolve", { reason: text })}>Resolve</button>}
              {status === "RESOLVED" && <button className="btn-primary" onClick={() => act("close", { note: text || null })}>Close</button>}
              {actionable && <button className="btn-secondary" onClick={() => act("reject", { reason: text })}>Reject</button>}
              {(status === "RESOLVED" || status === "CLOSED" || status === "REJECTED") &&
                <button className="btn-secondary" onClick={() => act("reopen", { reason: text })}>Reopen</button>}
            </div>
            <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
              <input value={reference} onChange={(e) => setReference(e.target.value)} placeholder="Evidence reference (e.g. journal or ticket no.)" style={{ flex: 1 }} />
              <button className="btn-secondary" disabled={!reference.trim()} onClick={() => act("evidence", { reference, note: text || null })}>Add evidence ref</button>
            </div>
            <p style={{ color: "var(--color-text-muted)", fontSize: 12 }}>
              Evidence is a reference only (no URLs or file paths). File upload is not available yet.
            </p>

            <h3>History</h3>
            <ol style={{ paddingLeft: 18 }}>
              {history.map((e) => (
                <li key={e.id} style={{ marginBottom: 6 }}>
                  <strong>{e.event_type.replace("_", " ")}</strong>
                  {e.from_status && e.to_status && e.from_status !== e.to_status ? ` (${e.from_status} → ${e.to_status})` : ""}
                  {e.assignee_user_id ? ` → ${nameOf(e.assignee_user_id)}` : ""}
                  <span style={{ color: "var(--color-text-muted)" }}> · {new Date(e.created_at).toLocaleString()}</span>
                  {e.reference && <div>Ref: {e.reference}</div>}
                  {e.note && <div>{e.note}</div>}
                </li>
              ))}
            </ol>
          </div>
        )}
      </div>
    </div>
  );
}
