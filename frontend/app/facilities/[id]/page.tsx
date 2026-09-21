"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  FacilityCollateralOut,
  FacilityCovenantOut,
  FacilityDrawdownOut,
  FacilityEventOut,
  FacilityFeeOut,
  FacilityOut,
  FacilityRepaymentOut,
  FacilitySubLimitOut,
  FacilityUtilizationOut,
  FacilityVersionOut,
  FundingForecastImpactOut,
} from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

function fmt(value: string): string {
  const n = Number(value);
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

const TABS = [
  "Overview", "Utilization", "Versions", "Drawdowns", "Repayments", "Fees",
  "Covenants", "Collateral", "Sub-Limits", "Events", "Forecast Impact",
] as const;
type Tab = (typeof TABS)[number];

function StatusBadge({ status }: { status: string }) {
  const danger = ["BREACH", "OVERDUE", "REJECTED", "CANCELLED"].includes(status);
  const warning = ["WARNING", "DUE", "PARTIALLY_PAID", "SUBMITTED", "UNDER_REVIEW"].includes(status);
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

export default function FacilityDetailPage() {
  const token = useAuthToken();
  const params = useParams();
  const facilityId = params.id as string;
  const [tab, setTab] = useState<Tab>("Overview");

  const [facility, setFacility] = useState<FacilityOut | null>(null);
  const [utilization, setUtilization] = useState<FacilityUtilizationOut | null>(null);
  const [events, setEvents] = useState<FacilityEventOut[]>([]);
  const [drawdowns, setDrawdowns] = useState<FacilityDrawdownOut[]>([]);
  const [repayments, setRepayments] = useState<FacilityRepaymentOut[]>([]);
  const [fees, setFees] = useState<FacilityFeeOut[]>([]);
  const [covenants, setCovenants] = useState<FacilityCovenantOut[]>([]);
  const [collateral, setCollateral] = useState<FacilityCollateralOut[]>([]);
  const [subLimits, setSubLimits] = useState<FacilitySubLimitOut[]>([]);
  const [versions, setVersions] = useState<FacilityVersionOut[]>([]);
  const [forecastImpact, setForecastImpact] = useState<FundingForecastImpactOut | null>(null);
  const [forecastImpactError, setForecastImpactError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token || !facilityId) return;
    Promise.all([
      api.getFacility(token, facilityId),
      api.getFacilityUtilization(token, facilityId),
      api.getFacilityEvents(token, facilityId),
      api.getFacilityDrawdowns(token, facilityId),
      api.getFacilityRepayments(token, facilityId),
      api.getFacilityFees(token, facilityId),
      api.getFacilityCovenants(token, facilityId),
      api.getFacilityCollateral(token, facilityId),
      api.getFacilitySubLimits(token, facilityId),
      api.getFacilityVersions(token, facilityId),
    ])
      .then(([f, u, e, d, r, fe, c, col, sl, v]) => {
        setFacility(f);
        setUtilization(u);
        setEvents(e);
        setDrawdowns(d);
        setRepayments(r);
        setFees(fe);
        setCovenants(c);
        setCollateral(col);
        setSubLimits(sl);
        setVersions(v);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Unable to reach the API"));
  }, [token, facilityId]);

  useEffect(() => {
    if (!token) return;
    // Forecast Impact is queried per-forecast, not per-facility - use the
    // most recently created forecast as a reasonable default so this tab
    // shows something meaningful without requiring the user to pick one.
    api
      .listForecasts(token)
      .then((forecasts) => {
        if (forecasts.length === 0) {
          setForecastImpactError("No forecasts exist yet - calculate one from the Forecast page first.");
          return;
        }
        const mostRecent = forecasts[0];
        return api.getFundingForecastImpact(token, mostRecent.id).then(setForecastImpact);
      })
      .catch((err) => setForecastImpactError(err instanceof ApiError ? err.message : "Unable to reach the API"));
  }, [token]);

  if (token === undefined) return null;
  if (token === null) {
    return (
      <div>
        <h1>Facility Detail</h1>
        <p>
          Please <Link href="/login">sign in</Link> to view this facility.
        </p>
      </div>
    );
  }

  if (error) {
    return (
      <div>
        <h1>Facility Detail</h1>
        <p className="error-text">{error}</p>
      </div>
    );
  }

  if (!facility || !utilization) {
    return <p style={{ color: "var(--color-text-muted)" }}>Loading...</p>;
  }

  const th: React.CSSProperties = { padding: "6px 8px", textAlign: "left" };
  const td: React.CSSProperties = { padding: "6px 8px" };

  return (
    <div>
      <Link href="/facilities" style={{ color: "var(--color-text-muted)", fontSize: 13 }}>
        &larr; Back to Facilities
      </Link>
      <h1 style={{ marginTop: 8 }}>{facility.facility_name}</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        {facility.facility_reference} &middot; {facility.facility_type_code} &middot;{" "}
        <span className="badge">{facility.commitment_type}</span>{" "}
        <StatusBadge status={facility.status} />
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
            {t === "Covenants" && covenants.some((c) => c.status === "BREACH" || c.status === "WARNING") && (
              <span style={{ marginLeft: 4, color: "var(--color-danger)" }}>&bull;</span>
            )}
          </button>
        ))}
      </div>

      <div style={{ marginTop: 20 }}>
        {tab === "Overview" && (
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Commercial Terms</h3>
            <table style={{ fontSize: 14 }}>
              <tbody>
                <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Currency</td><td>{facility.currency_code}</td></tr>
                <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Approved Limit</td><td>{fmt(facility.approved_limit)}</td></tr>
                <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Committed Limit</td><td>{fmt(facility.committed_limit)}</td></tr>
                <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Maturity Date</td><td>{facility.maturity_date}</td></tr>
                <tr><td style={{ paddingRight: 16, color: "var(--color-text-muted)" }}>Version</td><td>{facility.version}</td></tr>
              </tbody>
            </table>
          </div>
        )}

        {tab === "Utilization" && (
          <>
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
              {[
                ["Committed Limit", fmt(utilization.committed_limit)],
                ["Drawn", fmt(utilization.drawn_amount)],
                ["Undrawn", fmt(utilization.undrawn_amount)],
                ["Available", fmt(utilization.available_amount)],
                ["Utilization", `${utilization.utilization_pct}%`],
                ["Effective Rate", utilization.effective_interest_rate ? `${utilization.effective_interest_rate}%` : "N/A"],
              ].map(([label, value]) => (
                <div key={label} className="card" style={{ flex: "1 1 150px" }}>
                  <div style={{ fontSize: 12, color: "var(--color-text-muted)" }}>{label}</div>
                  <div style={{ fontSize: 18, marginTop: 4 }}>{value}</div>
                </div>
              ))}
            </div>
            {utilization.covenant_restricted_amount !== "0.00" && (
              <p style={{ fontSize: 12, color: "var(--color-warning)", marginTop: 8 }}>
                {fmt(utilization.covenant_restricted_amount)} of undrawn capacity is covenant-restricted
                and excluded from Available.
              </p>
            )}
          </>
        )}

        {tab === "Versions" && (
          <div className="card">
            <p style={{ fontSize: 12, color: "var(--color-text-muted)", marginTop: 0 }}>
              Every commercial-term change creates a new version; earlier versions are never
              overwritten and remain available for historical reporting.
            </p>
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead><tr style={{ color: "var(--color-text-muted)" }}>
                <th style={th}>Version</th><th style={th}>Effective Date</th>
                <th style={th}>Committed Limit</th><th style={th}>Rate</th><th style={th}>Maturity</th>
                <th style={th}>Reason</th>
              </tr></thead>
              <tbody>
                {versions.map((v) => (
                  <tr key={v.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                    <td style={td}>v{v.version}</td><td style={td}>{v.effective_date}</td>
                    <td style={td}>{v.terms.committed_limit ? fmt(v.terms.committed_limit) : "-"}</td>
                    <td style={td}>{v.terms.fixed_rate ? `${v.terms.fixed_rate}%` : "-"}</td>
                    <td style={td}>{v.terms.maturity_date ?? "-"}</td>
                    <td style={td}>{v.change_reason ?? "-"}</td>
                  </tr>
                ))}
                {versions.length === 0 && <tr><td colSpan={6} style={{ ...td, color: "var(--color-text-muted)" }}>No versions yet.</td></tr>}
              </tbody>
            </table>
          </div>
        )}

        {tab === "Drawdowns" && (
          <div className="card">
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead><tr style={{ color: "var(--color-text-muted)" }}>
                <th style={th}>Date</th><th style={th}>Amount</th><th style={th}>Currency</th>
                <th style={th}>Status</th><th style={th}>Reference</th>
              </tr></thead>
              <tbody>
                {drawdowns.map((d) => (
                  <tr key={d.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                    <td style={td}>{d.drawdown_date}</td><td style={td}>{fmt(d.drawdown_amount)}</td>
                    <td style={td}>{d.currency_code}</td><td style={td}><StatusBadge status={d.status} /></td>
                    <td style={td}>{d.reference ?? "-"}</td>
                  </tr>
                ))}
                {drawdowns.length === 0 && <tr><td colSpan={5} style={{ ...td, color: "var(--color-text-muted)" }}>No drawdowns yet.</td></tr>}
              </tbody>
            </table>
          </div>
        )}

        {tab === "Repayments" && (
          <div className="card">
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead><tr style={{ color: "var(--color-text-muted)" }}>
                <th style={th}>Due Date</th><th style={th}>Type</th><th style={th}>Original</th>
                <th style={th}>Paid</th><th style={th}>Status</th>
              </tr></thead>
              <tbody>
                {repayments.map((r) => (
                  <tr key={r.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                    <td style={td}>{r.due_date}</td><td style={td}>{r.repayment_type}</td>
                    <td style={td}>{fmt(r.original_amount)}</td><td style={td}>{fmt(r.paid_amount)}</td>
                    <td style={td}><StatusBadge status={r.status} /></td>
                  </tr>
                ))}
                {repayments.length === 0 && <tr><td colSpan={5} style={{ ...td, color: "var(--color-text-muted)" }}>No repayments scheduled yet.</td></tr>}
              </tbody>
            </table>
          </div>
        )}

        {tab === "Fees" && (
          <div className="card">
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead><tr style={{ color: "var(--color-text-muted)" }}>
                <th style={th}>Due Date</th><th style={th}>Fee Type</th><th style={th}>Amount</th><th style={th}>Status</th>
              </tr></thead>
              <tbody>
                {fees.map((f) => (
                  <tr key={f.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                    <td style={td}>{f.due_date}</td><td style={td}>{f.fee_type}</td>
                    <td style={td}>{fmt(f.amount)} {f.currency_code}</td><td style={td}><StatusBadge status={f.status} /></td>
                  </tr>
                ))}
                {fees.length === 0 && <tr><td colSpan={4} style={{ ...td, color: "var(--color-text-muted)" }}>No fees recorded yet.</td></tr>}
              </tbody>
            </table>
          </div>
        )}

        {tab === "Covenants" && (
          <div className="card">
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead><tr style={{ color: "var(--color-text-muted)" }}>
                <th style={th}>Name</th><th style={th}>Type</th><th style={th}>Operator</th>
                <th style={th}>Threshold</th><th style={th}>Current Value</th><th style={th}>Status</th>
              </tr></thead>
              <tbody>
                {covenants.map((c) => (
                  <tr key={c.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                    <td style={td}>{c.name}</td><td style={td}>{c.covenant_type}</td><td style={td}>{c.operator}</td>
                    <td style={td}>{c.threshold ?? "-"}</td><td style={td}>{c.current_value ?? "Not yet measured"}</td>
                    <td style={td}><StatusBadge status={c.status} /></td>
                  </tr>
                ))}
                {covenants.length === 0 && <tr><td colSpan={6} style={{ ...td, color: "var(--color-text-muted)" }}>No covenants configured.</td></tr>}
              </tbody>
            </table>
          </div>
        )}

        {tab === "Collateral" && (
          <div className="card">
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead><tr style={{ color: "var(--color-text-muted)" }}>
                <th style={th}>Type</th><th style={th}>Value</th><th style={th}>Haircut</th>
                <th style={th}>Eligible Value</th><th style={th}>Status</th>
              </tr></thead>
              <tbody>
                {collateral.map((c) => (
                  <tr key={c.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                    <td style={td}>{c.collateral_type}</td><td style={td}>{fmt(c.value)} {c.currency_code}</td>
                    <td style={td}>{c.haircut_pct}%</td><td style={td}>{fmt(c.eligible_value)}</td>
                    <td style={td}><StatusBadge status={c.status} /></td>
                  </tr>
                ))}
                {collateral.length === 0 && <tr><td colSpan={5} style={{ ...td, color: "var(--color-text-muted)" }}>No collateral recorded.</td></tr>}
              </tbody>
            </table>
          </div>
        )}

        {tab === "Sub-Limits" && (
          <div className="card">
            <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
              <thead><tr style={{ color: "var(--color-text-muted)" }}>
                <th style={th}>Name</th><th style={th}>Purpose</th><th style={th}>Limit</th>
                <th style={th}>Drawn</th><th style={th}>Remaining</th>
              </tr></thead>
              <tbody>
                {subLimits.map((s) => (
                  <tr key={s.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                    <td style={td}>{s.name}</td><td style={td}>{s.purpose_code ?? "-"}</td>
                    <td style={td}>{fmt(s.limit_amount)}</td><td style={td}>{fmt(s.drawn_amount)}</td>
                    <td style={td}>{fmt(String(Number(s.limit_amount) - Number(s.drawn_amount)))}</td>
                  </tr>
                ))}
                {subLimits.length === 0 && <tr><td colSpan={5} style={{ ...td, color: "var(--color-text-muted)" }}>No sub-limits configured - the full committed limit applies.</td></tr>}
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

        {tab === "Forecast Impact" && (
          <div className="card">
            <p style={{ fontSize: 12, color: "var(--color-text-muted)", marginTop: 0 }}>
              Shows facility-driven lines (repayments, interest, fees, approved drawdowns) in the
              most recently created forecast - not filtered to this specific facility. Visit the
              Forecast page for full drill-down.
            </p>
            {forecastImpactError && <p className="error-text">{forecastImpactError}</p>}
            {forecastImpact && (
              <>
                <div style={{ display: "flex", gap: 16, marginBottom: 12 }}>
                  <span className="badge">Lines: {forecastImpact.facility_driven_line_count}</span>
                  <span className="badge">Inflows: {fmt(forecastImpact.total_facility_inflows)}</span>
                  <span className="badge">Outflows: {fmt(forecastImpact.total_facility_outflows)}</span>
                </div>
                <table style={{ width: "100%", fontSize: 13, borderCollapse: "collapse" }}>
                  <thead><tr style={{ color: "var(--color-text-muted)" }}>
                    <th style={th}>Category</th><th style={th}>Direction</th><th style={th}>Amount</th>
                    <th style={th}>Source</th><th style={th}>Description</th>
                  </tr></thead>
                  <tbody>
                    {forecastImpact.lines.map((l, i) => (
                      <tr key={i} style={{ borderTop: "1px solid var(--color-border)" }}>
                        <td style={td}>{l.category_code}</td><td style={td}>{l.direction}</td>
                        <td style={td}>{fmt(l.amount)}</td><td style={td}>{l.source_type}</td>
                        <td style={td}>{l.description ?? "-"}</td>
                      </tr>
                    ))}
                    {forecastImpact.lines.length === 0 && (
                      <tr><td colSpan={5} style={{ ...td, color: "var(--color-text-muted)" }}>No facility-driven lines in this forecast.</td></tr>
                    )}
                  </tbody>
                </table>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
