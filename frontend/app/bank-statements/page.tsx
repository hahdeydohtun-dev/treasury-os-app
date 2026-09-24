"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, BankStatementTransactionOut } from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

function fmt(value: string): string {
  const n = Number(value);
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

export default function BankStatementsPage() {
  const token = useAuthToken();
  const [transactions, setTransactions] = useState<BankStatementTransactionOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!token) return;
    setLoading(true);
    setError(null);
    api.listBankStatementTransactions(token)
      .then(setTransactions)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Unable to reach the API"))
      .finally(() => setLoading(false));
  }, [token]);

  if (token === undefined) return null;
  if (token === null) {
    return (
      <div>
        <h1>Bank Statement Evidence</h1>
        <p>
          Please <Link href="/login">sign in</Link> to view imported bank statement evidence.
        </p>
      </div>
    );
  }

  return (
    <div>
      <h1>Bank Statement Evidence</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        External bank evidence imported via the Excel Data Hub&apos;s Bank Statement
        template. This is read-only evidence for future reconciliation - it is
        never a treasury cash transaction and has no effect on cash balances.
        To import a new statement, use{" "}
        <Link href="/excel-data-hub" style={{ color: "var(--color-accent)" }}>
          Excel Data Hub
        </Link>{" "}
        and select the &quot;Bank Statement&quot; template.
      </p>

      {error && <p className="error-text">{error}</p>}
      {loading && <p style={{ color: "var(--color-text-muted)" }}>Loading...</p>}

      <div className="card" style={{ marginTop: 20 }}>
        <h3 style={{ marginTop: 0 }}>Imported Statement Transactions</h3>
        <table style={{ width: "100%", fontSize: 14, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--color-text-muted)" }}>
              <th style={{ padding: "6px 8px" }}>Date</th>
              <th style={{ padding: "6px 8px" }}>Type</th>
              <th style={{ padding: "6px 8px" }}>Amount</th>
              <th style={{ padding: "6px 8px" }}>Currency</th>
              <th style={{ padding: "6px 8px" }}>Reference</th>
              <th style={{ padding: "6px 8px" }}>Narration</th>
              <th style={{ padding: "6px 8px" }}>Statement Period</th>
            </tr>
          </thead>
          <tbody>
            {transactions.map((t) => (
              <tr key={t.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                <td style={{ padding: "6px 8px" }}>{t.transaction_date}</td>
                <td style={{ padding: "6px 8px" }}>{t.entry_type}</td>
                <td style={{ padding: "6px 8px" }}>{fmt(t.amount)}</td>
                <td style={{ padding: "6px 8px" }}>{t.currency_code}</td>
                <td style={{ padding: "6px 8px" }}>{t.bank_reference ?? t.external_transaction_id ?? "-"}</td>
                <td style={{ padding: "6px 8px" }}>{t.narration ?? "-"}</td>
                <td style={{ padding: "6px 8px" }}>
                  {t.statement_period_start} to {t.statement_period_end}
                </td>
              </tr>
            ))}
            {transactions.length === 0 && !loading && (
              <tr>
                <td colSpan={7} style={{ padding: "12px 8px", color: "var(--color-text-muted)" }}>
                  No bank statement evidence imported yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
