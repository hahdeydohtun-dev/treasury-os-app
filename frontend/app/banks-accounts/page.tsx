"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, BankAccountOut } from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

export default function BanksAccountsPage() {
  const token = useAuthToken();
  const [accounts, setAccounts] = useState<BankAccountOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!token) return;
    setLoading(true);
    api
      .listBankAccounts(token)
      .then(setAccounts)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Unable to reach the API"))
      .finally(() => setLoading(false));
  }, [token]);

  if (token === undefined) return null;
  if (token === null) {
    return (
      <div>
        <h1>Banks &amp; Accounts</h1>
        <p>
          Please <Link href="/login">sign in</Link> to view bank accounts.
        </p>
      </div>
    );
  }

  return (
    <div>
      <h1>Banks &amp; Accounts</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Bank accounts across all legal entities you have access to. Account numbers
        are masked; add new accounts via the Excel Data Hub&apos;s Bank Accounts
        template or the API.
      </p>

      {error && <p className="error-text">{error}</p>}
      {loading && <p style={{ color: "var(--color-text-muted)" }}>Loading...</p>}

      <div className="card" style={{ marginTop: 20 }}>
        <table style={{ width: "100%", fontSize: 14, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--color-text-muted)" }}>
              <th style={{ padding: "6px 8px" }}>Account Name</th>
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
                  No bank accounts yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
