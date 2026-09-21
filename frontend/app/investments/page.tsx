"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, InvestmentAlertOut, InvestmentLiquidityOut, InvestmentOut } from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

function fmt(value: string): string {
  const n = Number(value);
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

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

export default function InvestmentsPage() {
  const token = useAuthToken();
  const [liquidity, setLiquidity] = useState<InvestmentLiquidityOut | null>(null);
  const [investments, setInvestments] = useState<InvestmentOut[]>([]);
  const [alerts, setAlerts] = useState<InvestmentAlertOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!token) return;
    setLoading(true);
    setError(null);
    Promise.all([api.getInvestmentLiquidity(token), api.listInvestments(token), api.getInvestmentAlerts(token)])
      .then(([l, i, a]) => {
        setLiquidity(l);
        setInvestments(i);
        setAlerts(a);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Unable to reach the API"))
      .finally(() => setLoading(false));
  }, [token]);

  if (token === undefined) return null;
  if (token === null) {
    return (
      <div>
        <h1>Investments &amp; Fixed Deposits</h1>
        <p>
          Please <Link href="/login">sign in</Link> to view investments.
        </p>
      </div>
    );
  }

  return (
    <div>
      <h1>Investments &amp; Fixed Deposits</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Invested funds are never presented as immediately available cash - see Cash &amp; Liquidity
        for actual bank balances.
      </p>

      {error && <p className="error-text">{error}</p>}
      {loading && <p style={{ color: "var(--color-text-muted)" }}>Loading...</p>}

      {liquidity && (
        <div style={{ display: "flex", gap: 16, marginTop: 20, flexWrap: "wrap" }}>
          {[
            ["Total Invested", fmt(liquidity.total_invested)],
            ["Maturing (7d)", fmt(liquidity.maturing_7_days)],
            ["Maturing (30d)", fmt(liquidity.maturing_30_days)],
            ["Maturing (90d)", fmt(liquidity.maturing_90_days)],
            ["Expected Interest", fmt(liquidity.total_expected_interest)],
            ["Weighted Avg Rate", liquidity.weighted_average_rate ? `${liquidity.weighted_average_rate}%` : "N/A"],
            ["Investments", String(liquidity.investment_count)],
          ].map(([label, value]) => (
            <div key={label} className="card" style={{ flex: "1 1 150px" }}>
              <div style={{ fontSize: 12, color: "var(--color-text-muted)" }}>{label}</div>
              <div style={{ fontSize: 18, marginTop: 4 }}>{value}</div>
            </div>
          ))}
        </div>
      )}

      {alerts.length > 0 && (
        <div className="card" style={{ marginTop: 20 }}>
          <h3 style={{ marginTop: 0 }}>Alerts</h3>
          {alerts.map((a, i) => (
            <div key={i} style={{ padding: "6px 0", borderBottom: "1px solid var(--color-border)", fontSize: 13 }}>
              <span className="badge">{a.alert_type}</span> {a.message}
            </div>
          ))}
        </div>
      )}

      <div className="card" style={{ marginTop: 20 }}>
        <h3 style={{ marginTop: 0 }}>Investment Register</h3>
        <table style={{ width: "100%", fontSize: 14, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--color-text-muted)" }}>
              <th style={{ padding: "6px 8px" }}>Reference</th>
              <th style={{ padding: "6px 8px" }}>Type</th>
              <th style={{ padding: "6px 8px" }}>Currency</th>
              <th style={{ padding: "6px 8px" }}>Principal</th>
              <th style={{ padding: "6px 8px" }}>Rate</th>
              <th style={{ padding: "6px 8px" }}>Maturity</th>
              <th style={{ padding: "6px 8px" }}>Status</th>
            </tr>
          </thead>
          <tbody>
            {investments.map((inv) => (
              <tr key={inv.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                <td style={{ padding: "6px 8px" }}>
                  <Link href={`/investments/${inv.id}`} style={{ color: "var(--color-accent)" }}>
                    {inv.investment_reference}
                  </Link>
                </td>
                <td style={{ padding: "6px 8px" }}>{inv.investment_type_code}</td>
                <td style={{ padding: "6px 8px" }}>{inv.currency_code}</td>
                <td style={{ padding: "6px 8px" }}>{fmt(inv.principal_amount)}</td>
                <td style={{ padding: "6px 8px" }}>{inv.interest_rate}%</td>
                <td style={{ padding: "6px 8px" }}>{inv.maturity_date}</td>
                <td style={{ padding: "6px 8px" }}><StatusBadge status={inv.status} /></td>
              </tr>
            ))}
            {investments.length === 0 && !loading && (
              <tr>
                <td colSpan={7} style={{ padding: "12px 8px", color: "var(--color-text-muted)" }}>
                  No investments yet. Create one via the API or the Excel Data Hub&apos;s
                  Investment Master template.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
