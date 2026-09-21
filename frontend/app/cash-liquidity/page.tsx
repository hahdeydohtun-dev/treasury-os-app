"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, BankAccountOut, CashPositionOut } from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

export default function CashLiquidityPage() {
  const token = useAuthToken();
  const [position, setPosition] = useState<CashPositionOut | null>(null);
  const [accounts, setAccounts] = useState<BankAccountOut[]>([]);
  const [currencyFilter, setCurrencyFilter] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setLoading(true);
    setError(null);

    Promise.all([
      api.getCashPosition(token, currencyFilter ? { currency_code: currencyFilter } : undefined),
      api.listBankAccounts(token),
    ])
      .then(([positionResult, accountsResult]) => {
        if (cancelled) return;
        setPosition(positionResult);
        setAccounts(accountsResult);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Unable to reach the API");
      })
      .finally(() => !cancelled && setLoading(false));

    return () => {
      cancelled = true;
    };
  }, [token, currencyFilter]);

  if (token === undefined) return null;
  if (token === null) {
    return (
      <div>
        <h1>Cash &amp; Liquidity</h1>
        <p>
          Please <Link href="/login">sign in</Link> to view cash position data.
        </p>
      </div>
    );
  }

  return (
    <div>
      <h1>Cash &amp; Liquidity</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Daily cash position foundation, built from imported bank balances and posted
        treasury transactions. Full liquidity/funding optimization is a later module.
      </p>

      {error && <p className="error-text">{error}</p>}

      <div style={{ display: "flex", gap: 20, marginTop: 20, flexWrap: "wrap" }}>
        <div className="card" style={{ flex: "1 1 260px" }}>
          <h3 style={{ marginTop: 0 }}>Group Cash Position</h3>
          {loading && <p style={{ color: "var(--color-text-muted)" }}>Loading...</p>}
          {position && (
            <>
              <p style={{ fontSize: 28, margin: "8px 0" }}>
                {position.total_cash} <span className="badge">TOTAL</span>
              </p>
              <table style={{ width: "100%", fontSize: 14 }}>
                <tbody>
                  <tr>
                    <td>Available</td>
                    <td style={{ textAlign: "right" }}>{position.available_cash}</td>
                  </tr>
                  <tr>
                    <td>Overdraft</td>
                    <td style={{ textAlign: "right" }}>{position.overdraft}</td>
                  </tr>
                  <tr>
                    <td>Net Cash</td>
                    <td style={{ textAlign: "right" }}>{position.net_cash}</td>
                  </tr>
                  <tr>
                    <td>Accounts</td>
                    <td style={{ textAlign: "right" }}>{position.account_count}</td>
                  </tr>
                </tbody>
              </table>
              <p style={{ fontSize: 12, color: "var(--color-text-muted)" }}>
                As of {position.as_of}. Figures across currencies are summed as
                reported, without FX conversion - use the currency filter below for
                a single-currency view.
              </p>
            </>
          )}
        </div>

        <div className="card" style={{ flex: "1 1 260px" }}>
          <h3 style={{ marginTop: 0 }}>Cash by Currency</h3>
          <div style={{ marginBottom: 12 }}>
            <label style={{ fontSize: 13, color: "var(--color-text-muted)" }}>
              Filter:{" "}
              <select
                value={currencyFilter}
                onChange={(e) => setCurrencyFilter(e.target.value)}
                style={{
                  background: "var(--color-surface-alt)",
                  color: "var(--color-text)",
                  border: "1px solid var(--color-border)",
                  borderRadius: 6,
                  padding: "4px 8px",
                }}
              >
                <option value="">All currencies</option>
                {position &&
                  Object.keys(position.by_currency).map((code) => (
                    <option key={code} value={code}>
                      {code}
                    </option>
                  ))}
              </select>
            </label>
          </div>
          {position &&
            Object.entries(position.by_currency).map(([code, amount]) => (
              <div
                key={code}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  padding: "6px 0",
                  borderBottom: "1px solid var(--color-border)",
                }}
              >
                <span>{code}</span>
                <span>{amount}</span>
              </div>
            ))}
        </div>
      </div>

      <div className="card" style={{ marginTop: 20 }}>
        <h3 style={{ marginTop: 0 }}>Bank Accounts</h3>
        <table style={{ width: "100%", fontSize: 14, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--color-text-muted)" }}>
              <th style={{ padding: "6px 8px" }}>Account</th>
              <th style={{ padding: "6px 8px" }}>Number</th>
              <th style={{ padding: "6px 8px" }}>Currency</th>
              <th style={{ padding: "6px 8px" }}>Type</th>
              <th style={{ padding: "6px 8px" }}>Status</th>
            </tr>
          </thead>
          <tbody>
            {accounts.map((account) => (
              <tr key={account.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                <td style={{ padding: "6px 8px" }}>{account.account_name}</td>
                <td style={{ padding: "6px 8px" }}>{account.account_number_masked}</td>
                <td style={{ padding: "6px 8px" }}>{account.currency_code}</td>
                <td style={{ padding: "6px 8px" }}>{account.account_type_code}</td>
                <td style={{ padding: "6px 8px" }}>{account.status}</td>
              </tr>
            ))}
            {accounts.length === 0 && !loading && (
              <tr>
                <td colSpan={5} style={{ padding: "12px 8px", color: "var(--color-text-muted)" }}>
                  No bank accounts yet. Add one via the Excel Data Hub or the API.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
