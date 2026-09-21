"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  InvestmentEventOut,
  InvestmentOut,
  InvestmentTransactionOut,
  InvestmentVersionOut,
} from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

function fmt(value: string): string {
  const n = Number(value);
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

const TABS = ["Overview", "Commercial Terms", "Versions", "Transactions", "Events"] as const;
type Tab = (typeof TABS)[number];

function StatusBadge({ status }: { status: string }) {
  const danger = ["REJECTED", "CANCELLED"].includes(status);
  const warning = ["SUBMITTED", "UNDER_REVIEW", "PLACEMENT_PENDING", "PARTIALLY_TERMINATED"].includes(status);
  return (
    <span
      className="badge"
      style={{
        background: danger ? "var(--color-danger)" : warning ? "var(--color-warning)" : undefined,
        color: danger || warning ? "#fff" : undefined,
      }}
    >
      {status}
    </span>
  );
}

export default function InvestmentDetailPage() {
  const token = useAuthToken();
  const params = useParams();
  const investmentId = params.id as string;
  const [tab, setTab] = useState<Tab>("Overview");

  const [investment, setInvestment] = useState<InvestmentOut | null>(null);
  const [versions, setVersions] = useState<InvestmentVersionOut[]>([]);
  const [transactions, setTransactions] = useState<InvestmentTransactionOut[]>([]);
  const [events, setEvents] = useState<InvestmentEventOut[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !investmentId) return;
    Promise.all([
      api.getInvestment(token, investmentId),
      api.getInvestmentVersions(token, investmentId),
      api.getInvestmentTransactions(token, investmentId),
      api.getInvestmentEvents(token, investmentId),
    ])
      .then(([inv, v, t, e]) => {
        setInvestment(inv);
        setVersions(v);
        setTransactions(t);
        setEvents(e);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Unable to reach the API"));
  }, [token, investmentId]);

  if (token === undefined) return null;
  if (token === null) {
    return (
      <div>
        <h1>Investment Detail</h1>
        <p>
          Please <Link href="/login">sign in</Link> to view this investment.
        </p>
      </div>
    );
  }

  if (error) {
    return (
      <div>
        <h1>Investment Detail</h1>
        <p className="error-text">{error}</p>
      </div>
    );
  }

  if (!investment) {
    return <p style={{ color: "var(--color-text-muted)" }}>Loading...</p>;
  }

  const th: React.CSSProperties = { padding: "6px 8px", textAlign: "left" };
  const td: React.CSSProperties = { padding: "6px 8px" };

  return (
    <div>
      <Link href="/investments" style={{ color: "var(--color-text-muted)", fontSize: 13 }}>
        &larr; Back to Investments
      </Link>
      <h1 style={{ marginTop: 8 }}>{investment.investment_reference}</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        {investment.investment_type_code} &middot; <StatusBadge status={investment.status} />
      </p>

      <div style={{ display: "flex", gap: 4, marginTop: 16, borderBottom: "1px solid var(--color-border)", flexWrap: "wrap" }}>
        {TABS.map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            style={{
              padding: "8px 14px", background: "none", border: "none", cursor: "pointer",
              fontSize: 13, color: tab === t ? "var(--color-accent)" : "var(--color-text-muted)",
              borderBottom: tab === t ? "2px solid var(--color-accent)" : "2px solid transparent",
            }}
          >
            {t}
          </button>
        ))}
      </div>

      <div style={{ marginTop: 20 }}>
        {tab === "Overview" && (
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            {[
              ["Principal", fmt(investment.principal_amount)],
              ["Original Principal", fmt(investment.original_principal_amount)],
              ["Rate", `${investment.interest_rate}%`],
              ["Tenor (days)", String(investment.tenor_days)],
              ["Expected Interest", fmt(investment.expected_interest)],
              ["Accrued Interest", fmt(investment.accrued_interest)],
              ["Received Interest", fmt(investment.received_interest)],
              ["Currency", investment.currency_code],
              ["Maturity Date", investment.maturity_date],
              ["Placement Date", investment.placement_date ?? "Not yet placed"],
            ].map(([label, value]) => (
              <div key={label} className="card" style={{ flex: "1 1 150px" }}>
                <div style={{ fontSize: 12, color: "var(--color-text-muted)" }}>{label}</div>
                <div style={{ fontSize: 18, marginTop: 4 }}>{value}</div>
              </div>
            ))}
          </div>
        )}

        {tab === "Commercial Terms" && (
          <div className="card">
            <table style={{ fontSize: 14 }}>
              <tbody>
                <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Rate Type</td><td>{investment.rate_type}</td></tr>
                <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Day Count Convention</td><td>{investment.day_count_convention}</td></tr>
                <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Interest Payment Method</td><td>{investment.interest_payment_method}</td></tr>
                <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Early Termination Allowed</td><td>{investment.early_termination_allowed ? "Yes" : "No"}</td></tr>
                <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Partial Termination Allowed</td><td>{investment.partial_termination_allowed ? "Yes" : "No"}</td></tr>
                <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Rollover Allowed</td><td>{investment.rollover_allowed ? "Yes" : "No"}</td></tr>
                <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Version</td><td>{investment.version}</td></tr>
                {investment.previous_investment_id && (
                  <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Rolled From</td>
                    <td><Link href={`/investments/${investment.previous_investment_id}`} style={{ color: "var(--color-accent)" }}>View</Link></td></tr>
                )}
                {investment.rolled_to_investment_id && (
                  <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Rolled To</td>
                    <td><Link href={`/investments/${investment.rolled_to_investment_id}`} style={{ color: "var(--color-accent)" }}>View</Link></td></tr>
                )}
              </tbody>
            </table>
          </div>
        )}

        {tab === "Versions" && (
          <div className="card">
            <p style={{ fontSize: 12, color: "var(--color-text-muted)", marginTop: 0 }}>
              Every commercial-term change creates a new version; earlier versions are never overwritten.
            </p>
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead><tr style={{ color: "var(--color-text-muted)" }}>
                <th style={th}>Version</th><th style={th}>Effective Date</th>
                <th style={th}>Principal</th><th style={th}>Rate</th><th style={th}>Reason</th>
              </tr></thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                    <td style={td}>v{v.version}</td><td style={td}>{v.effective_date}</td>
                    <td style={td}>{v.terms.principal_amount ? fmt(String(v.terms.principal_amount)) : "-"}</td>
                    <td style={td}>{v.terms.interest_rate ? `${v.terms.interest_rate}%` : "-"}</td>
                    <td style={td}>{v.change_reason ?? "-"}</td>
                  </tr>
                ))}
                {versions.length === 0 && <tr><td colSpan={5} style={{ ...td, color: "var(--color-text-muted)" }}>No versions yet.</td></tr>}
              </tbody>
            </table>
          </div>
        )}

        {tab === "Transactions" && (
          <div className="card">
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead><tr style={{ color: "var(--color-text-muted)" }}>
                <th style={th}>Date</th><th style={th}>Type</th><th style={th}>Amount</th><th style={th}>Status</th>
              </tr></thead>
              <tbody>
                {transactions.map((t) => (
                  <tr key={t.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                    <td style={td}>{t.transaction_date}</td><td style={td}>{t.transaction_type}</td>
                    <td style={td}>{fmt(t.amount)} {t.currency_code}</td><td style={td}><StatusBadge status={t.status} /></td>
                  </tr>
                ))}
                {transactions.length === 0 && <tr><td colSpan={4} style={{ ...td, color: "var(--color-text-muted)" }}>No transactions yet - this investment has not been placed.</td></tr>}
              </tbody>
            </table>
          </div>
        )}

        {tab === "Events" && (
          <div className="card">
            {events.map((e) => (
              <div key={e.id} style={{ padding: "8px 0", borderBottom: "1px solid var(--color-border)", fontSize: 13 }}>
                <span className="badge">{e.event_type}</span> {e.description}
                <div style={{ color: "var(--color-text-muted)", fontSize: 11 }}>{e.event_date}</div>
              </div>
            ))}
            {events.length === 0 && <p style={{ color: "var(--color-text-muted)", fontSize: 13 }}>No events yet.</p>}
          </div>
        )}
      </div>
    </div>
  );
}
