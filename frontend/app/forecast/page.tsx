"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  ForecastAlertOut,
  ForecastLineOut,
  ForecastOut,
  ForecastSummaryOut,
} from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

function fmt(value: string): string {
  const n = Number(value);
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

export default function ForecastPage() {
  const token = useAuthToken();
  const [forecasts, setForecasts] = useState<ForecastOut[]>([]);
  const [selectedId, setSelectedId] = useState<string>("");
  const [summary, setSummary] = useState<ForecastSummaryOut | null>(null);
  const [alerts, setAlerts] = useState<ForecastAlertOut[]>([]);
  const [drillDownWeek, setDrillDownWeek] = useState<number | null>(null);
  const [drillDownLines, setDrillDownLines] = useState<ForecastLineOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh(currentToken: string, id: string) {
    const [summaryResult, alertsResult] = await Promise.all([
      api.getForecastSummary(currentToken, id),
      api.getForecastAlerts(currentToken, id),
    ]);
    setSummary(summaryResult);
    setAlerts(alertsResult);
  }

  useEffect(() => {
    if (!token) return;
    api
      .listForecasts(token)
      .then((list) => {
        setForecasts(list);
        if (list.length > 0) setSelectedId(list[0].id);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Unable to reach the API"));
  }, [token]);

  useEffect(() => {
    if (!token || !selectedId) return;
    refresh(token, selectedId).catch((err) => {
      if (err instanceof ApiError && err.status === 400) {
        setSummary(null);
        setAlerts([]);
      } else {
        setError(err instanceof ApiError ? err.message : "Unable to reach the API");
      }
    });
  }, [token, selectedId]);

  async function handleCalculate() {
    if (!token || !selectedId) return;
    setBusy(true);
    setError(null);
    try {
      await api.calculateForecast(token, selectedId);
      await refresh(token, selectedId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Calculation failed");
    } finally {
      setBusy(false);
    }
  }

  async function handlePublish() {
    if (!token || !selectedId) return;
    setBusy(true);
    setError(null);
    try {
      await api.publishForecast(token, selectedId);
      const updated = await api.listForecasts(token);
      setForecasts(updated);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Publish failed");
    } finally {
      setBusy(false);
    }
  }

  async function handleDrillDown(weekNumber: number) {
    if (!token || !selectedId) return;
    setDrillDownWeek(weekNumber);
    const lines = await api.getForecastLines(token, selectedId, weekNumber);
    setDrillDownLines(lines);
  }

  async function handleExport() {
    if (!token || !selectedId) return;
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(api.exportForecastUrl(selectedId), {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? "Export failed");
      }
      const blob = await response.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `forecast_${selectedId}.xlsx`;
      a.click();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setBusy(false);
    }
  }

  if (token === undefined) return null;
  if (token === null) {
    return (
      <div>
        <h1>13-Week Cash Flow Forecast</h1>
        <p>
          Please <Link href="/login">sign in</Link> to view the forecast.
        </p>
      </div>
    );
  }

  const selectedForecast = forecasts.find((f) => f.id === selectedId);
  const maxAbsClosing = summary
    ? Math.max(...summary.weeks.map((w) => Math.abs(Number(w.closing_cash))), 1)
    : 1;

  return (
    <div>
      <h1>13-Week Cash Flow Forecast</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Rolling 13-week liquidity projection, calculated from bank balances, expected
        collections/payments, recurring flows, and posted actuals.
      </p>

      {error && <p className="error-text">{error}</p>}

      <div className="card" style={{ marginTop: 16 }}>
        <div style={{ display: "flex", gap: 16, alignItems: "center", flexWrap: "wrap" }}>
          <label style={{ fontSize: 13 }}>
            Forecast:{" "}
            <select
              value={selectedId}
              onChange={(e) => setSelectedId(e.target.value)}
              style={{
                background: "var(--color-surface-alt)", color: "var(--color-text)",
                border: "1px solid var(--color-border)", borderRadius: 6, padding: "4px 8px",
              }}
            >
              {forecasts.map((f) => (
                <option key={f.id} value={f.id}>
                  {f.forecast_start_date} - v{f.version} - {f.scenario} - {f.status}
                </option>
              ))}
            </select>
          </label>
          {selectedForecast && (
            <>
              <span className="badge">{selectedForecast.reporting_currency_code}</span>
              <span className="badge">{selectedForecast.value_basis}</span>
              {selectedForecast.status !== "PUBLISHED" && (
                <button className="btn-primary" onClick={handleCalculate} disabled={busy}>
                  {busy ? "Working..." : "Calculate"}
                </button>
              )}
              {selectedForecast.status === "DRAFT" && summary && (
                <button className="btn-primary" onClick={handlePublish} disabled={busy}>
                  Publish
                </button>
              )}
              {summary && (
                <button className="btn-primary" onClick={handleExport} disabled={busy}>
                  Export to Excel
                </button>
              )}
            </>
          )}
        </div>
        {selectedForecast && selectedForecast.data_quality_warnings.length > 0 && (
          <div style={{ marginTop: 12 }}>
            {selectedForecast.data_quality_warnings.map((w, i) => (
              <p key={i} style={{ fontSize: 12, color: "var(--color-warning)", margin: "4px 0" }}>
                {"\u26A0"} {w}
              </p>
            ))}
          </div>
        )}
      </div>

      {summary && (
        <>
          <div style={{ display: "flex", gap: 16, marginTop: 20, flexWrap: "wrap" }}>
            {[
              ["Opening Cash", fmt(summary.opening_cash)],
              ["13-Week Net Cash Flow", fmt(summary.thirteen_week_net_cash_flow)],
              [
                "Lowest Projected Cash",
                `${fmt(summary.lowest_projected_cash)} (W${summary.lowest_projected_cash_week ?? "-"})`,
              ],
              [
                "Largest Funding Gap",
                summary.largest_funding_gap_week
                  ? `${fmt(summary.largest_funding_gap)} (W${summary.largest_funding_gap_week})`
                  : "None",
              ],
              ["Total Surplus", fmt(summary.total_surplus)],
            ].map(([label, value]) => (
              <div key={label} className="card" style={{ flex: "1 1 180px" }}>
                <div style={{ fontSize: 12, color: "var(--color-text-muted)" }}>{label}</div>
                <div style={{ fontSize: 22, marginTop: 4 }}>{value}</div>
              </div>
            ))}
          </div>

          <div className="card" style={{ marginTop: 20 }}>
            <h3 style={{ marginTop: 0 }}>Projected Closing Cash vs Minimum Liquidity</h3>
            <div style={{ display: "flex", alignItems: "flex-end", gap: 4, height: 160 }}>
              {summary.weeks.map((w) => {
                const closing = Number(w.closing_cash);
                const height = Math.max(4, (Math.abs(closing) / maxAbsClosing) * 140);
                return (
                  <div
                    key={w.week_number}
                    onClick={() => handleDrillDown(w.week_number)}
                    title={`Week ${w.week_number}: ${fmt(w.closing_cash)}`}
                    style={{
                      flex: 1, display: "flex", flexDirection: "column",
                      alignItems: "center", cursor: "pointer",
                    }}
                  >
                    <div
                      style={{
                        width: "100%", height,
                        background: closing < 0 ? "var(--color-danger)" : "var(--color-accent)",
                        borderRadius: "3px 3px 0 0",
                        alignSelf: closing < 0 ? "flex-start" : "flex-end",
                      }}
                    />
                    <div style={{ fontSize: 10, marginTop: 4, color: "var(--color-text-muted)" }}>
                      W{w.week_number}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="card" style={{ marginTop: 20, overflowX: "auto" }}>
            <h3 style={{ marginTop: 0 }}>Weekly Detail</h3>
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ textAlign: "right", color: "var(--color-text-muted)" }}>
                  <th style={{ textAlign: "left", padding: "4px 8px" }}>Metric</th>
                  {summary.weeks.map((w) => (
                    <th key={w.week_number} style={{ padding: "4px 8px" }}>
                      W{w.week_number}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {[
                  ["Opening Cash", "opening_cash"],
                  ["Inflows", "total_inflows"],
                  ["Outflows", "total_outflows"],
                  ["Net Cash Flow", "net_cash_flow"],
                  ["Closing Cash", "closing_cash"],
                  ["Minimum Liquidity", "minimum_required_liquidity"],
                  ["Surplus / Gap", "surplus_or_gap"],
                ].map(([label, key]) => (
                  <tr key={key} style={{ borderTop: "1px solid var(--color-border)" }}>
                    <td style={{ padding: "4px 8px" }}>{label}</td>
                    {summary.weeks.map((w) => {
                      const value = (w as unknown as Record<string, string>)[key as string];
                      const isGap = key === "surplus_or_gap" && Number(value) < 0;
                      return (
                        <td
                          key={w.week_number}
                          onClick={() => handleDrillDown(w.week_number)}
                          style={{
                            padding: "4px 8px", textAlign: "right", cursor: "pointer",
                            color: isGap ? "var(--color-danger)" : undefined,
                          }}
                        >
                          {fmt(value)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {drillDownWeek !== null && (
            <div className="card" style={{ marginTop: 20 }}>
              <h3 style={{ marginTop: 0 }}>Week {drillDownWeek} - What&apos;s driving this?</h3>
              <table style={{ width: "100%", fontSize: 13 }}>
                <thead>
                  <tr style={{ textAlign: "left", color: "var(--color-text-muted)" }}>
                    <th style={{ padding: "4px 8px" }}>Category</th>
                    <th style={{ padding: "4px 8px" }}>Direction</th>
                    <th style={{ padding: "4px 8px" }}>Amount</th>
                    <th style={{ padding: "4px 8px" }}>Source</th>
                    <th style={{ padding: "4px 8px" }}>Description</th>
                  </tr>
                </thead>
                <tbody>
                  {drillDownLines.map((line) => (
                    <tr key={line.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                      <td style={{ padding: "4px 8px" }}>{line.category_code}</td>
                      <td style={{ padding: "4px 8px" }}>{line.direction}</td>
                      <td style={{ padding: "4px 8px" }}>
                        {line.original_amount} {line.transaction_currency_code}
                      </td>
                      <td style={{ padding: "4px 8px" }}>{line.source_type}</td>
                      <td style={{ padding: "4px 8px" }}>{line.description}</td>
                    </tr>
                  ))}
                  {drillDownLines.length === 0 && (
                    <tr>
                      <td colSpan={5} style={{ padding: "8px", color: "var(--color-text-muted)" }}>
                        No cash flow lines this week.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}

          <div className="card" style={{ marginTop: 20 }}>
            <h3 style={{ marginTop: 0 }}>Alerts</h3>
            {alerts.length === 0 && (
              <p style={{ color: "var(--color-text-muted)", fontSize: 13 }}>No open alerts.</p>
            )}
            {alerts.map((a) => (
              <div
                key={a.id}
                style={{
                  padding: "8px 0", borderBottom: "1px solid var(--color-border)", fontSize: 13,
                }}
              >
                <span
                  className="badge"
                  style={{
                    background:
                      a.severity === "CRITICAL" || a.severity === "HIGH"
                        ? "var(--color-danger)"
                        : "var(--color-warning)",
                    color: "#fff",
                  }}
                >
                  {a.severity}
                </span>{" "}
                {a.message}
              </div>
            ))}
          </div>
        </>
      )}

      {!summary && selectedForecast && (
        <p style={{ marginTop: 20, color: "var(--color-text-muted)" }}>
          This forecast hasn&apos;t been calculated yet. Click Calculate above.
        </p>
      )}
    </div>
  );
}
