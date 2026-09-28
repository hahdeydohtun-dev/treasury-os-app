"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, ReconciliationRunOut } from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

function StatusBadge({ status }: { status: string }) {
  const danger = ["FAILED", "CANCELLED"].includes(status);
  const warning = ["RUNNING", "DRAFT"].includes(status);
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

export default function ReconciliationPage() {
  const token = useAuthToken();
  const [runs, setRuns] = useState<ReconciliationRunOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const [entityId, setEntityId] = useState("");
  const [bankAccountId, setBankAccountId] = useState("");
  const [periodStart, setPeriodStart] = useState("");
  const [periodEnd, setPeriodEnd] = useState("");
  const [formError, setFormError] = useState<string | null>(null);

  const load = () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    api.listReconciliationRuns(token)
      .then(setRuns)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Unable to reach the API"))
      .finally(() => setLoading(false));
  };

  useEffect(load, [token]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!token) return;
    setFormError(null);
    try {
      await api.createReconciliationRun(token, {
        legal_entity_id: entityId,
        bank_account_id: bankAccountId,
        period_start: periodStart,
        period_end: periodEnd,
      });
      setEntityId("");
      setBankAccountId("");
      setPeriodStart("");
      setPeriodEnd("");
      load();
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Unable to create the run");
    }
  }

  async function handleExecute(id: string) {
    if (!token) return;
    try {
      await api.executeReconciliationRun(token, id);
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to execute the run");
    }
  }

  async function handleCancel(id: string) {
    if (!token) return;
    try {
      await api.cancelReconciliationRun(token, id);
      load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to cancel the run");
    }
  }

  if (token === undefined) return null;
  if (token === null) {
    return (
      <div>
        <h1>Reconciliation</h1>
        <p>
          Please <Link href="/login">sign in</Link> to view reconciliation runs.
        </p>
      </div>
    );
  }

  return (
    <div>
      <h1>Reconciliation</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        A reconciliation run defines a scoped reconciliation job (entity, bank account,
        period) and tracks its own lifecycle. Stage 5B provides the run and configuration
        infrastructure only - no matching engine exists yet, so an executed run simply
        counts the bank statement evidence in its own scope. See{" "}
        <Link href="/bank-statements" style={{ color: "var(--color-accent)" }}>
          Bank Statements
        </Link>{" "}
        for the underlying evidence.
      </p>

      {error && <p className="error-text">{error}</p>}

      <div className="card" style={{ marginTop: 20 }}>
        <h3 style={{ marginTop: 0 }}>Create Reconciliation Run</h3>
        <form onSubmit={handleCreate} style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "flex-end" }}>
          <label>
            Entity ID
            <input value={entityId} onChange={(e) => setEntityId(e.target.value)} required style={{ display: "block" }} />
          </label>
          <label>
            Bank Account ID
            <input value={bankAccountId} onChange={(e) => setBankAccountId(e.target.value)} required style={{ display: "block" }} />
          </label>
          <label>
            Period Start
            <input type="date" value={periodStart} onChange={(e) => setPeriodStart(e.target.value)} required style={{ display: "block" }} />
          </label>
          <label>
            Period End
            <input type="date" value={periodEnd} onChange={(e) => setPeriodEnd(e.target.value)} required style={{ display: "block" }} />
          </label>
          <button type="submit">Create Run</button>
        </form>
        {formError && <p className="error-text">{formError}</p>}
      </div>

      <div className="card" style={{ marginTop: 20 }}>
        <h3 style={{ marginTop: 0 }}>Reconciliation Runs</h3>
        {loading && <p style={{ color: "var(--color-text-muted)" }}>Loading...</p>}
        <table style={{ width: "100%", fontSize: 14, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--color-text-muted)" }}>
              <th style={{ padding: "6px 8px" }}>Period</th>
              <th style={{ padding: "6px 8px" }}>Status</th>
              <th style={{ padding: "6px 8px" }}>Statement Txns</th>
              <th style={{ padding: "6px 8px" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                <td style={{ padding: "6px 8px" }}>{run.period_start} to {run.period_end}</td>
                <td style={{ padding: "6px 8px" }}><StatusBadge status={run.status} /></td>
                <td style={{ padding: "6px 8px" }}>{run.statement_transaction_count}</td>
                <td style={{ padding: "6px 8px" }}>
                  {run.status === "READY" && (
                    <>
                      <button onClick={() => handleExecute(run.id)}>Execute</button>{" "}
                      <button onClick={() => handleCancel(run.id)}>Cancel</button>
                    </>
                  )}
                </td>
              </tr>
            ))}
            {runs.length === 0 && !loading && (
              <tr>
                <td colSpan={4} style={{ padding: "12px 8px", color: "var(--color-text-muted)" }}>
                  No reconciliation runs yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
