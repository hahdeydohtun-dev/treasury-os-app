"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  api, ApiError, ReportCatalogEntry, ReportDrill, ReportKpi, ReportResult, ReportTable,
} from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

// Which filters make sense for each report. Anything else is not sent, so a filter can never silently change a report
// that does not use it. The server derives the visible entities from the signed-in user's permissions; the entity and
// account pickers only NARROW that result.
const APPLIES: Record<string, string[]> = {
  overview: ["period", "entity", "currency"],
  "run-summary": ["period", "entity", "account", "currency", "run_status"],
  "matching-performance": ["period", "entity", "account", "currency"],
  exceptions: ["period", "entity", "account", "currency", "category", "status", "assigned", "txn", "age"],
  ageing: ["as_of", "period", "entity", "account", "currency", "category", "assigned", "txn", "age"],
  assignment: ["period", "entity", "account", "currency", "category", "status", "assigned"],
  "bank-accounts": ["period", "entity", "account", "currency"],
  "entity-dashboard": ["period", "entity", "account", "currency"],
  "group-dashboard": ["period", "entity", "account", "currency"],
};
const CATEGORIES = ["BANK_ONLY", "LEDGER_ONLY", "AMBIGUOUS_MATCH", "AMOUNT_VARIANCE", "TIMING_DIFFERENCE", "ONE_TO_MANY", "MANY_TO_ONE", "BATCH_PAYMENT", "EXCEPTION"];
const STATUSES = ["OPEN", "UNDER_REVIEW", "RESOLVED", "CLOSED", "REJECTED"];
const RUN_STATUSES = ["DRAFT", "READY", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"];
const MULTI = new Set(["category", "exception_status", "run_status"]);

type Filters = Record<string, string>;

function queryString(filters: Filters, applicable: string[], extra: Record<string, string> = {}): string {
  const wanted = new Set<string>();
  const map: Record<string, string[]> = {
    period: ["date_from", "date_to"], as_of: ["as_of"], entity: ["legal_entity_id"], account: ["bank_account_id"], currency: ["currency_code"],
    run_status: ["run_status"], category: ["category"], status: ["exception_status"], assigned: ["unassigned"],
    txn: ["txn_date_from", "txn_date_to"], age: ["min_age_days", "max_age_days"],
  };
  applicable.forEach((a) => (map[a] ?? []).forEach((k) => wanted.add(k)));
  const q = new URLSearchParams();
  Object.entries({ ...filters, ...extra }).forEach(([k, v]) => {
    if (v === "" || v === undefined) return;
    if (!wanted.has(k) && !(k in extra)) return;
    if (MULTI.has(k)) q.append(k, v); else q.set(k, v);
  });
  return q.toString();
}

function Cell({ value }: { value: unknown }) {
  if (value === null || value === undefined || value === "") return <span style={{ color: "var(--color-text-muted)" }}>–</span>;
  return <>{String(value)}</>;
}

function KpiCard({ kpi, onDrill }: { kpi: ReportKpi; onDrill: (d: ReportDrill) => void }) {
  const clickable = !!kpi.drilldown;
  return (
    <div className="card" title={`${kpi.definition}\nSource: ${kpi.source}`}
         onClick={() => kpi.drilldown && onDrill(kpi.drilldown)}
         style={{ minWidth: 170, flex: "1 1 170px", cursor: clickable ? "pointer" : "default" }}>
      <div style={{ color: "var(--color-text-muted)", fontSize: 12 }}>{kpi.label}</div>
      <div style={{ fontSize: 26, fontWeight: 700 }}>{kpi.value === null ? "n/a" : String(kpi.value)}</div>
      <div style={{ color: "var(--color-text-muted)", fontSize: 11 }}>{clickable ? "Click to see the records" : kpi.source}</div>
    </div>
  );
}

function DataTable({ table, onDrill, onPage }: { table: ReportTable; onDrill: (d: ReportDrill) => void; onPage: (page: number) => void }) {
  const paged = table.page !== null && table.page_size !== null;
  const pages = paged ? Math.max(1, Math.ceil(table.total_rows / (table.page_size as number))) : 1;
  return (
    <div className="card" style={{ overflowX: "auto" }}>
      <h2>{table.title} <span style={{ color: "var(--color-text-muted)", fontWeight: 400 }}>({table.total_rows})</span></h2>
      {table.rows.length === 0 ? (
        <p style={{ color: "var(--color-text-muted)" }}>No records match these filters.</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ color: "var(--color-text-muted)" }}>
              {table.columns.map((c) => <th key={c.key} style={{ padding: "6px 8px", textAlign: c.align, whiteSpace: "nowrap" }}>{c.label}</th>)}
            </tr>
          </thead>
          <tbody>
            {table.rows.map((row, i) => {
              const drill = row["_drill"] as ReportDrill | undefined;
              return (
                <tr key={i} onClick={() => drill && onDrill(drill)} style={{ cursor: drill ? "pointer" : "default" }}>
                  {table.columns.map((c) => (
                    <td key={c.key} style={{ padding: "6px 8px", textAlign: c.align }}><Cell value={row[c.key]} /></td>
                  ))}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {table.note && <p style={{ color: "var(--color-text-muted)" }}>{table.note}</p>}
      {paged && table.total_rows > 0 && (
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 8 }}>
          <button className="btn-secondary" disabled={(table.page as number) <= 1} onClick={() => onPage((table.page as number) - 1)}>Previous</button>
          <span>Page {table.page} of {pages}</span>
          <button className="btn-secondary" disabled={(table.page as number) >= pages} onClick={() => onPage((table.page as number) + 1)}>Next</button>
        </div>
      )}
    </div>
  );
}

export default function ReconciliationReportsPage() {
  const token = useAuthToken();
  const [catalog, setCatalog] = useState<ReportCatalogEntry[]>([]);
  const [report, setReport] = useState("overview");
  const [filters, setFilters] = useState<Filters>({});
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(25);
  const [result, setResult] = useState<ReportResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);
  const [entities, setEntities] = useState<{ id: string; name: string }[]>([]);
  const [accounts, setAccounts] = useState<{ id: string; label: string; entity: string }[]>([]);

  const applicable = APPLIES[report] ?? [];
  const query = useMemo(() => queryString(filters, applicable, { page: String(page), page_size: String(pageSize) }), [filters, applicable, page, pageSize]);

  useEffect(() => {
    if (!token) return;
    api.listReconciliationReports(token).then(setCatalog).catch(() => setCatalog([]));
    // Account choices come from the reconciliation bank-accounts report, so they need only reconciliation access
    // (not the separate banking permission) and are already limited to what the user may see.
    api.getReconciliationReport(token, "bank-accounts", "page_size=200")
      .then((r) => setAccounts((r.tables[0]?.rows ?? []).map((row) => ({
        id: String(row["id"]), entity: String(row["entity"]), label: `${row["bank"]} ${row["account"]} (${row["currency"]})`,
      }))))
      .catch(() => setAccounts([]));
    api.getReconciliationReport(token, "entity-dashboard", "")
      .then((r) => {
        const rows = r.tables.find((t) => t.key === "entities")?.rows ?? [];
        setEntities(rows.map((row) => ({ id: (row["_drill"] as ReportDrill).params.legal_entity_id, name: String(row["entity"]) })));
      })
      .catch(() => setEntities([]));
  }, [token]);

  const load = useCallback(() => {
    if (!token) return;
    setLoading(true);
    setError(null);
    api.getReconciliationReport(token, report, query)
      .then(setResult)
      .catch((err) => { setResult(null); setError(err instanceof ApiError ? err.message : "Unable to reach the API"); })
      .finally(() => setLoading(false));
  }, [token, report, query]);
  useEffect(load, [load]);

  function set(key: string, value: string) {
    setPage(1);
    setFilters((f) => ({ ...f, [key]: value }));
  }

  function drill(d: ReportDrill) {
    setPage(1);
    setFilters((f) => ({ ...f, ...d.params }));
    setReport(d.report);
  }

  async function download(format: "xlsx" | "pdf") {
    if (!token) return;
    setExporting(format);
    setError(null);
    try {
      const response = await fetch(api.reconciliationReportExportUrl(report, format, queryString(filters, applicable)), {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail ?? "Export failed");
      }
      const blob = await response.blob();
      // Same naming rule as the server (reconciliation_<report>_<from>_<to>.<ext>); Content-Disposition is not readable cross-origin.
      const name = `reconciliation_${report.replace(/-/g, "_")}_${result?.reporting_period.from ?? "period"}_${result?.reporting_period.to ?? ""}.${format}`;
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Export failed");
    } finally {
      setExporting(null);
    }
  }

  const exportable = catalog.find((c) => c.key === report)?.exportable ?? false;
  const field = { padding: "4px 6px" } as const;

  return (
    <div>
      <h1>Reconciliation reports</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Read-only reporting over the reconciliation data. A transaction, a match proposal, an accepted match and an exception are counted
        separately; a pending proposal is never counted as a completed reconciliation, and amounts are never added across currencies.{" "}
        <Link href="/reconciliation" style={{ color: "var(--color-accent)" }}>Back to runs</Link>
      </p>

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", margin: "12px 0" }}>
        {catalog.map((c) => (
          <button key={c.key} title={c.description} className={c.key === report ? "btn-primary" : "btn-secondary"}
                  onClick={() => { setReport(c.key); setPage(1); }}>{c.title}</button>
        ))}
      </div>

      <div className="card" style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end" }}>
        {applicable.includes("period") && (<>
          <label>From<br /><input type="date" value={filters.date_from ?? ""} onChange={(e) => set("date_from", e.target.value)} style={field} /></label>
          <label>To<br /><input type="date" value={filters.date_to ?? ""} onChange={(e) => set("date_to", e.target.value)} style={field} /></label>
        </>)}
        {applicable.includes("as_of") && <label>As of<br /><input type="date" value={filters.as_of ?? ""} onChange={(e) => set("as_of", e.target.value)} style={field} /></label>}
        {applicable.includes("entity") && (
          <label>Entity<br />
            <select value={filters.legal_entity_id ?? ""} onChange={(e) => set("legal_entity_id", e.target.value)} style={field}>
              <option value="">All authorized</option>
              {entities.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
            </select>
          </label>)}
        {applicable.includes("account") && (
          <label>Bank account<br />
            <select value={filters.bank_account_id ?? ""} onChange={(e) => set("bank_account_id", e.target.value)} style={field}>
              <option value="">All authorized</option>
              {accounts.filter((a) => !filters.legal_entity_id || a.entity === entities.find((e) => e.id === filters.legal_entity_id)?.name)
                .map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
            </select>
          </label>)}
        {applicable.includes("currency") && <label>Currency<br /><input maxLength={3} placeholder="All" value={filters.currency_code ?? ""} onChange={(e) => set("currency_code", e.target.value.toUpperCase())} style={{ ...field, width: 60 }} /></label>}
        {applicable.includes("run_status") && (
          <label>Run status<br /><select value={filters.run_status ?? ""} onChange={(e) => set("run_status", e.target.value)} style={field}>
            <option value="">Any</option>{RUN_STATUSES.map((s) => <option key={s}>{s}</option>)}</select></label>)}
        {applicable.includes("category") && (
          <label>Category<br /><select value={filters.category ?? ""} onChange={(e) => set("category", e.target.value)} style={field}>
            <option value="">All</option>{CATEGORIES.map((s) => <option key={s}>{s}</option>)}</select></label>)}
        {applicable.includes("status") && (
          <label>Status<br /><select value={filters.exception_status ?? ""} onChange={(e) => set("exception_status", e.target.value)} style={field}>
            <option value="">Outstanding (open + under review)</option>{STATUSES.map((s) => <option key={s}>{s}</option>)}</select></label>)}
        {applicable.includes("assigned") && <label><input type="checkbox" checked={filters.unassigned === "true"} onChange={(e) => set("unassigned", e.target.checked ? "true" : "")} /> Unassigned only</label>}
        {applicable.includes("txn") && (<>
          <label>Txn from<br /><input type="date" value={filters.txn_date_from ?? ""} onChange={(e) => set("txn_date_from", e.target.value)} style={field} /></label>
          <label>Txn to<br /><input type="date" value={filters.txn_date_to ?? ""} onChange={(e) => set("txn_date_to", e.target.value)} style={field} /></label>
        </>)}
        {applicable.includes("age") && (<>
          <label>Age min (days)<br /><input type="number" min={0} value={filters.min_age_days ?? ""} onChange={(e) => set("min_age_days", e.target.value)} style={{ ...field, width: 70 }} /></label>
          <label>Age max (days)<br /><input type="number" min={0} value={filters.max_age_days ?? ""} onChange={(e) => set("max_age_days", e.target.value)} style={{ ...field, width: 70 }} /></label>
        </>)}
        <label>Rows per page<br />
          <select value={pageSize} onChange={(e) => { setPageSize(Number(e.target.value)); setPage(1); }} style={field}>
            {[25, 50, 100, 200].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
        </label>
        <button className="btn-secondary" onClick={() => { setFilters({}); setPage(1); }}>Clear filters</button>
        {exportable && (<>
          <button className="btn-primary" disabled={exporting !== null} onClick={() => download("xlsx")}>{exporting === "xlsx" ? "Exporting…" : "Export Excel"}</button>
          <button className="btn-primary" disabled={exporting !== null} onClick={() => download("pdf")}>{exporting === "pdf" ? "Exporting…" : "Export PDF"}</button>
        </>)}
      </div>
      {!filters.date_from && !filters.date_to && (applicable.includes("period")) && (
        <p style={{ color: "var(--color-text-muted)", fontSize: 12 }}>
          No dates chosen: period-based reports use the last 30 days (UTC). For backlog reports (exceptions, ageing, assignment) the period is applied
          to transaction dates only when you choose dates.
        </p>)}

      {error && <p style={{ color: "var(--color-danger)" }}>{error}</p>}
      {loading && <p>Loading…</p>}

      {result && !loading && (
        <div>
          <p style={{ color: "var(--color-text-muted)", fontSize: 13 }}>
            <strong>{result.title}</strong> · {result.level} · {result.scope.entities.join("; ") || "no entities"} · {result.scope.accounts} · {result.scope.currency}
            <br />Period {result.reporting_period.from} to {result.reporting_period.to}{result.reporting_period.applies ? "" : " (not applied to this report)"}
            · as of {result.as_of} · {result.timezone} · generated {new Date(result.generated_at).toLocaleString()}
          </p>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            {result.kpis.map((k) => <KpiCard key={k.key} kpi={k} onDrill={drill} />)}
          </div>
          {result.tables.map((t, idx) => (
            <DataTable key={t.key} table={t} onDrill={drill} onPage={(p) => idx === 0 && setPage(p)} />
          ))}
          {result.notes.map((n, i) => <p key={i} style={{ color: "var(--color-text-muted)" }}>{n}</p>)}
          <details className="card">
            <summary><strong>Definitions and methodology</strong></summary>
            <ul>{result.definitions.map((d, i) => <li key={i}>{d}</li>)}</ul>
          </details>
        </div>
      )}
      {!result && !loading && !error && <p>No report loaded.</p>}
    </div>
  );
}
