"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  ExcelTemplateOut,
  FreshnessEntry,
  ImportBatchDetailOut,
  ImportBatchOut,
} from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

export default function ExcelDataHubPage() {
  const token = useAuthToken();
  const [templates, setTemplates] = useState<ExcelTemplateOut[]>([]);
  const [history, setHistory] = useState<ImportBatchOut[]>([]);
  const [freshness, setFreshness] = useState<FreshnessEntry[]>([]);
  const [selectedTemplate, setSelectedTemplate] = useState<string>("");
  const [file, setFile] = useState<File | null>(null);
  const [batch, setBatch] = useState<ImportBatchDetailOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refreshLists(currentToken: string) {
    const [templatesResult, historyResult, freshnessResult] = await Promise.all([
      api.listTemplates(currentToken),
      api.listImportHistory(currentToken),
      api.getFreshness(currentToken),
    ]);
    setTemplates(templatesResult);
    setHistory(historyResult);
    setFreshness(freshnessResult);
    if (!selectedTemplate && templatesResult.length > 0) {
      setSelectedTemplate(templatesResult[0].code);
    }
  }

  useEffect(() => {
    if (!token) return;
    refreshLists(token).catch((err) => {
      setError(err instanceof ApiError ? err.message : "Unable to reach the API");
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  async function handleUpload() {
    if (!token || !file || !selectedTemplate) return;
    const template = templates.find((t) => t.code === selectedTemplate);
    if (!template) return;

    setBusy(true);
    setError(null);
    setBatch(null);
    try {
      const result = await api.uploadExcel(token, file, selectedTemplate, template.version);
      setBatch(result);
      await refreshLists(token);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Upload failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirm() {
    if (!token || !batch) return;
    setBusy(true);
    setError(null);
    try {
      await api.confirmImport(token, batch.id);
      const refreshed = await api.getValidation(token, batch.id);
      setBatch(refreshed);
      await refreshLists(token);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Import failed");
    } finally {
      setBusy(false);
    }
  }

  if (token === undefined) return null;
  if (token === null) {
    return (
      <div>
        <h1>Excel Data Hub</h1>
        <p>
          Please <Link href="/login">sign in</Link> to upload data.
        </p>
      </div>
    );
  }

  const selectedSpec = templates.find((t) => t.code === selectedTemplate);

  return (
    <div>
      <h1>Excel Data Hub</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Upload then Validate then Preview then Confirm Import. Nothing lands in the
        database until you confirm.
      </p>

      {error && <p className="error-text">{error}</p>}

      <div className="card" style={{ marginTop: 20 }}>
        <h3 style={{ marginTop: 0 }}>1. Upload</h3>
        <div className="form-field">
          <label htmlFor="template">Template</label>
          <select
            id="template"
            value={selectedTemplate}
            onChange={(e) => setSelectedTemplate(e.target.value)}
            style={{
              background: "var(--color-surface-alt)", color: "var(--color-text)",
              border: "1px solid var(--color-border)", borderRadius: 8, padding: "10px 12px",
            }}
          >
            {templates.map((t) => (
              <option key={t.code} value={t.code}>
                {t.name} (v{t.version})
              </option>
            ))}
          </select>
        </div>
        {selectedSpec && (
          <p style={{ fontSize: 13, color: "var(--color-text-muted)" }}>
            Required columns: {selectedSpec.required_columns.join(", ")}
          </p>
        )}
        <div className="form-field">
          <label htmlFor="file">Excel file (.xlsx)</label>
          <input
            id="file"
            type="file"
            accept=".xlsx,.xlsm"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          />
        </div>
        <button className="btn-primary" onClick={handleUpload} disabled={busy || !file}>
          {busy ? "Working..." : "Upload & Validate"}
        </button>
      </div>

      {batch && (
        <div className="card" style={{ marginTop: 20 }}>
          <h3 style={{ marginTop: 0 }}>2. Preview</h3>
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: 12 }}>
            <span className="badge">Total: {batch.total_rows}</span>
            <span className="badge">Valid: {batch.valid_rows}</span>
            <span className="badge">Invalid: {batch.invalid_rows}</span>
            <span className="badge">Warnings: {batch.warning_rows}</span>
            <span className="badge">Duplicates: {batch.duplicate_rows}</span>
            <span className="badge">Status: {batch.status}</span>
          </div>

          {batch.issues.length > 0 && (
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ textAlign: "left", color: "var(--color-text-muted)" }}>
                  <th style={{ padding: "4px 8px" }}>Row</th>
                  <th style={{ padding: "4px 8px" }}>Column</th>
                  <th style={{ padding: "4px 8px" }}>Severity</th>
                  <th style={{ padding: "4px 8px" }}>Message</th>
                </tr>
              </thead>
              <tbody>
                {batch.issues.map((issue, idx) => (
                  <tr key={idx} style={{ borderTop: "1px solid var(--color-border)" }}>
                    <td style={{ padding: "4px 8px" }}>{issue.row_number}</td>
                    <td style={{ padding: "4px 8px" }}>{issue.column_name ?? "-"}</td>
                    <td
                      style={{
                        padding: "4px 8px",
                        color:
                          issue.severity === "ERROR"
                            ? "var(--color-danger)"
                            : "var(--color-warning)",
                      }}
                    >
                      {issue.severity}
                    </td>
                    <td style={{ padding: "4px 8px" }}>{issue.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {batch.valid_rows > 0 &&
            !["IMPORTED", "PARTIALLY_IMPORTED", "FAILED"].includes(batch.status) && (
              <button
                className="btn-primary"
                style={{ marginTop: 16 }}
                onClick={handleConfirm}
                disabled={busy}
              >
                {busy ? "Importing..." : `Confirm Import (${batch.valid_rows} rows)`}
              </button>
            )}
        </div>
      )}

      <div style={{ display: "flex", gap: 20, marginTop: 20, flexWrap: "wrap" }}>
        <div className="card" style={{ flex: "1 1 320px" }}>
          <h3 style={{ marginTop: 0 }}>Data Freshness</h3>
          {freshness.map((entry) => (
            <div
              key={entry.template_code}
              style={{
                display: "flex", justifyContent: "space-between", padding: "6px 0",
                borderBottom: "1px solid var(--color-border)", fontSize: 13,
              }}
            >
              <span>{entry.template_code}</span>
              <span
                style={{
                  color:
                    entry.freshness_status === "CURRENT"
                      ? "var(--color-success)"
                      : entry.freshness_status === "STALE"
                      ? "var(--color-warning)"
                      : "var(--color-text-muted)",
                }}
              >
                {entry.freshness_status}
              </span>
            </div>
          ))}
        </div>

        <div className="card" style={{ flex: "1 1 320px" }}>
          <h3 style={{ marginTop: 0 }}>Import History</h3>
          {history.slice(0, 10).map((h) => (
            <div key={h.id} style={{ padding: "6px 0", borderBottom: "1px solid var(--color-border)", fontSize: 13 }}>
              <div>{h.file_name} <span className="badge">{h.template_code}</span></div>
              <div style={{ color: "var(--color-text-muted)" }}>
                {h.status} - {h.imported_rows}/{h.total_rows} imported
              </div>
            </div>
          ))}
          {history.length === 0 && (
            <p style={{ color: "var(--color-text-muted)", fontSize: 13 }}>No uploads yet.</p>
          )}
        </div>
      </div>
    </div>
  );
}
