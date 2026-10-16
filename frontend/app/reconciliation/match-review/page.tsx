"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, ApiError, MatchProposalDetail, MatchProposalRow } from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";
import ReconciliationTabs from "@/components/ReconciliationTabs";

// PENDING = proposed by the engine, awaiting a decision (NOT reconciled). ACCEPTED = explicitly accepted.
// REJECTED = explicitly rejected; its transactions were returned to the Exceptions queue.
const STATUS_STYLE: Record<string, { label: string; colour: string }> = {
  PENDING: { label: "Proposed – awaiting decision", colour: "#0369a1" },
  ACCEPTED: { label: "Accepted", colour: "#15803d" },
  REJECTED: { label: "Rejected – exceptions raised", colour: "#b91c1c" },
};
const VIEWS: { label: string; statuses: string[] }[] = [
  { label: "Awaiting decision", statuses: ["PENDING"] },
  { label: "Accepted", statuses: ["ACCEPTED"] },
  { label: "Rejected", statuses: ["REJECTED"] },
  { label: "All decided", statuses: ["ACCEPTED", "REJECTED"] },
];

function StatusBadge({ status }: { status: string }) {
  const s = STATUS_STYLE[status] ?? { label: status, colour: "#64748b" };
  return <span className="badge" style={{ background: s.colour, color: "#fff" }}>{s.label}</span>;
}

const money = (amount: string, currency: string) => `${Number(amount).toLocaleString()} ${currency}`;

export default function MatchReviewPage() {
  const token = useAuthToken();
  const [rows, setRows] = useState<MatchProposalRow[]>([]);
  const [viewIndex, setViewIndex] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<{ kind: string; id: string } | null>(null);
  const [detail, setDetail] = useState<MatchProposalDetail | null>(null);
  const [reason, setReason] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const load = useCallback(() => {
    if (!token) return;
    setError(null);
    api.listMatchProposals(token, VIEWS[viewIndex].statuses)
      .then(setRows)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Unable to reach the API"));
  }, [token, viewIndex]);
  useEffect(load, [load]);

  const loadDetail = useCallback((kind: string, id: string) => {
    if (!token) return;
    api.getMatchProposal(token, kind, id).then(setDetail)
      .catch((err) => setActionError(err instanceof ApiError ? err.message : "Unable to load the proposal"));
  }, [token]);

  function select(row: MatchProposalRow) {
    setSelected({ kind: row.kind, id: row.id });
    setActionError(null);
    setNotice(null);
    setReason("");
    loadDetail(row.kind, row.id);
  }

  async function decide(action: "accept" | "reject") {
    if (!token || !selected) return;
    setActionError(null);
    try {
      const result = await api.decideMatchProposal(token, selected.kind, selected.id, action, reason);
      setNotice(action === "accept"
        ? "Accepted. This match is now reconciled; no ledger or bank record was changed."
        : "Rejected. The transactions it covered are back in the Exceptions queue for investigation.");
      if (result.already_decided) setNotice("That decision had already been recorded; nothing changed.");
      setReason("");
      loadDetail(selected.kind, selected.id);
      load();
      setRefreshKey((k) => k + 1);
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "Action failed");
    }
  }

  const canDecide = detail?.status === "PENDING" && !detail.not_decidable_reason;

  return (
    <div>
      <h1>Match review</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Matches proposed by the reconciliation engine. A proposal is <strong>not</strong> a reconciliation until you accept it,
        however high its confidence. Accepting or rejecting records a decision only; it never posts or changes a ledger or bank
        record. Ambiguous and weak-evidence matches are exceptions and live in the other tab.{" "}
        <Link href="/reconciliation" style={{ color: "var(--color-accent)" }}>Back to runs</Link>
      </p>
      <ReconciliationTabs active="review" refreshKey={refreshKey} />

      <div className="card" style={{ display: "flex", gap: 12, alignItems: "end" }}>
        <label>Show<br />
          <select value={viewIndex} onChange={(e) => setViewIndex(Number(e.target.value))}>
            {VIEWS.map((v, i) => <option key={v.label} value={i}>{v.label}</option>)}
          </select>
        </label>
      </div>
      {error && <p style={{ color: "var(--color-danger)" }}>{error}</p>}

      <div style={{ display: "grid", gridTemplateColumns: selected ? "1.1fr 1fr" : "1fr", gap: 16, alignItems: "start" }}>
        <div className="card">
          <h2>{VIEWS[viewIndex].label} <span style={{ color: "var(--color-text-muted)", fontWeight: 400 }}>({rows.length})</span></h2>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ textAlign: "left", color: "var(--color-text-muted)" }}>
                <th style={{ padding: "6px 8px" }}>Proposal</th><th style={{ padding: "6px 8px" }}>Status</th>
                <th style={{ padding: "6px 8px" }}>Amount</th><th style={{ padding: "6px 8px" }}>Confidence</th>
                <th style={{ padding: "6px 8px" }}>Reference</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.kind}-${r.id}`} onClick={() => select(r)}
                    style={{ cursor: "pointer", background: r.id === selected?.id ? "var(--color-surface-alt, rgba(120,120,255,.12))" : undefined }}>
                  <td style={{ padding: "6px 8px" }}>{r.label.replace(/_/g, " ").toLowerCase()}{r.kind === "GROUP" ? ` (${r.member_count} txns)` : ""}</td>
                  <td style={{ padding: "6px 8px" }}><StatusBadge status={r.status} /></td>
                  <td style={{ padding: "6px 8px" }}>{money(r.amount, r.currency_code)}</td>
                  <td style={{ padding: "6px 8px" }}>{r.confidence ? Number(r.confidence).toFixed(0) : "–"}</td>
                  <td style={{ padding: "6px 8px" }}>{r.bank_reference ?? r.ledger_reference ?? "–"}</td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr><td colSpan={5} style={{ padding: "12px 8px", color: "var(--color-text-muted)" }}>
                  {viewIndex === 0 ? "No proposals are waiting for a decision." : "Nothing here."}
                </td></tr>
              )}
            </tbody>
          </table>
        </div>

        {selected && detail && (
          <div className="card">
            <h2>{detail.label.replace(/_/g, " ").toLowerCase()} <StatusBadge status={detail.status} /></h2>
            <p style={{ color: "var(--color-text-muted)" }}>{detail.reason}</p>
            {detail.matching_rule_version && <p>Rule version {detail.matching_rule_version} · confidence {detail.confidence ?? "–"}</p>}

            <h3>Bank statement transactions</h3>
            {detail.bank_transactions.length === 0 && <p>–</p>}
            {detail.bank_transactions.map((b) => (
              <p key={b.id}>{b.transaction_date} · {b.entry_type} · {money(b.amount, b.currency_code)} · {b.bank_reference ?? "no ref"} · {b.narration ?? ""}</p>
            ))}
            <h3>Ledger transactions</h3>
            {detail.ledger_transactions.length === 0 && <p>–</p>}
            {detail.ledger_transactions.map((t) => (
              <p key={t.id}>{t.event_date} · {t.direction} · {money(t.amount, t.currency_code)} · {t.reference ?? "no ref"}</p>
            ))}
            {detail.kind === "GROUP" && (
              <p>Bank total {detail.bank_aggregate_amount} · ledger total {detail.ledger_aggregate_amount} · difference {detail.difference} {detail.currency_code}</p>
            )}
            {detail.fx && (
              <p><strong>FX</strong> {detail.fx.source_currency} → {detail.fx.target_currency} at {detail.fx.rate} ({detail.fx.rate_type}, {detail.fx.rate_source}, dated {detail.fx.rate_date}), tolerance {detail.fx.tolerance_pct}%</p>
            )}

            {detail.decision && (
              <p><strong>Decision:</strong> {detail.decision.decision} on {new Date(detail.decision.decided_at).toLocaleString()}
                {detail.decision.reason ? ` – ${detail.decision.reason}` : ""}</p>
            )}
            {detail.not_decidable_reason && <p style={{ color: "var(--color-warning)" }}>{detail.not_decidable_reason}</p>}
            {detail.acceptance_conflict && (
              <p style={{ color: "var(--color-danger)" }}>
                <strong>Cannot be accepted:</strong> {detail.acceptance_conflict} You can still reject this proposal.
              </p>
            )}
            {actionError && <p style={{ color: "var(--color-danger)" }}>{actionError}</p>}
            {notice && <p style={{ color: "var(--color-accent)" }}>{notice}</p>}

            {canDecide && (
              <>
                <textarea rows={3} style={{ width: "100%" }} value={reason} onChange={(e) => setReason(e.target.value)}
                  placeholder="Reason (optional to accept; at least 10 characters to reject)" />
                <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                  <button className="btn-primary" disabled={!!detail.acceptance_conflict} onClick={() => decide("accept")}>Accept match</button>
                  <button className="btn-secondary" disabled={reason.trim().length < 10} onClick={() => decide("reject")}>Reject proposal</button>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
