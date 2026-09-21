"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, ApiError, FacilityOut, FundingDashboardOut } from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

function fmt(value: string): string {
  const n = Number(value);
  return n.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

export default function FacilitiesPage() {
  const token = useAuthToken();
  const [dashboard, setDashboard] = useState<FundingDashboardOut | null>(null);
  const [facilities, setFacilities] = useState<FacilityOut[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!token) return;
    setLoading(true);
    setError(null);
    Promise.all([api.getFundingDashboard(token), api.listFacilities(token)])
      .then(([d, f]) => {
        setDashboard(d);
        setFacilities(f);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Unable to reach the API"))
      .finally(() => setLoading(false));
  }, [token]);

  if (token === undefined) return null;
  if (token === null) {
    return (
      <div>
        <h1>Funding &amp; Credit Facilities</h1>
        <p>
          Please <Link href="/login">sign in</Link> to view facilities.
        </p>
      </div>
    );
  }

  return (
    <div>
      <h1>Funding &amp; Credit Facilities</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Committed and uncommitted facility inventory, utilization, and funding capacity.
        Committed available capacity is never combined with uncommitted/potential capacity
        into a single misleading number.
      </p>

      {error && <p className="error-text">{error}</p>}
      {loading && <p style={{ color: "var(--color-text-muted)" }}>Loading...</p>}

      {dashboard && (
        <div style={{ display: "flex", gap: 16, marginTop: 20, flexWrap: "wrap" }}>
          {[
            ["Total Committed Limit", fmt(dashboard.total_committed_limit)],
            ["Total Drawn", fmt(dashboard.total_drawn)],
            ["Total Available", fmt(dashboard.total_available)],
            ["Utilization", `${dashboard.utilization_pct}%`],
            ["Facilities", String(dashboard.facility_count)],
            ["Maturing (30d)", String(dashboard.facilities_maturing_30_days)],
            ["Covenant Warnings", String(dashboard.covenant_warnings)],
            ["Covenant Breaches", String(dashboard.covenant_breaches)],
          ].map(([label, value]) => (
            <div key={label} className="card" style={{ flex: "1 1 160px" }}>
              <div style={{ fontSize: 12, color: "var(--color-text-muted)" }}>{label}</div>
              <div
                style={{
                  fontSize: 20, marginTop: 4,
                  color:
                    label === "Covenant Breaches" && dashboard.covenant_breaches > 0
                      ? "var(--color-danger)"
                      : label === "Covenant Warnings" && dashboard.covenant_warnings > 0
                      ? "var(--color-warning)"
                      : undefined,
                }}
              >
                {value}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="card" style={{ marginTop: 20 }}>
        <h3 style={{ marginTop: 0 }}>Facility Register</h3>
        <table style={{ width: "100%", fontSize: 14, borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--color-text-muted)" }}>
              <th style={{ padding: "6px 8px" }}>Reference</th>
              <th style={{ padding: "6px 8px" }}>Name</th>
              <th style={{ padding: "6px 8px" }}>Type</th>
              <th style={{ padding: "6px 8px" }}>Commitment</th>
              <th style={{ padding: "6px 8px" }}>Currency</th>
              <th style={{ padding: "6px 8px" }}>Committed Limit</th>
              <th style={{ padding: "6px 8px" }}>Drawn</th>
              <th style={{ padding: "6px 8px" }}>Status</th>
              <th style={{ padding: "6px 8px" }}>Maturity</th>
            </tr>
          </thead>
          <tbody>
            {facilities.map((f) => (
              <tr key={f.id} style={{ borderTop: "1px solid var(--color-border)" }}>
                <td style={{ padding: "6px 8px" }}>
                  <Link href={`/facilities/${f.id}`} style={{ color: "var(--color-accent)" }}>
                    {f.facility_reference}
                  </Link>
                </td>
                <td style={{ padding: "6px 8px" }}>{f.facility_name}</td>
                <td style={{ padding: "6px 8px" }}>{f.facility_type_code}</td>
                <td style={{ padding: "6px 8px" }}>
                  <span className="badge">{f.commitment_type}</span>
                </td>
                <td style={{ padding: "6px 8px" }}>{f.currency_code}</td>
                <td style={{ padding: "6px 8px" }}>{fmt(f.committed_limit)}</td>
                <td style={{ padding: "6px 8px" }}>{fmt(f.current_drawn_amount)}</td>
                <td style={{ padding: "6px 8px" }}>{f.status}</td>
                <td style={{ padding: "6px 8px" }}>{f.maturity_date}</td>
              </tr>
            ))}
            {facilities.length === 0 && !loading && (
              <tr>
                <td colSpan={9} style={{ padding: "12px 8px", color: "var(--color-text-muted)" }}>
                  No facilities yet. Create one via the API or the Excel Data Hub&apos;s
                  Facility Master template.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
