export default function DashboardPage() {
  return (
    <div>
      <h1>Dashboard</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Foundation stage. The Treasury Attention Center and full dashboard
        widgets (SECTION 19) are built module-by-module after this
        foundation is reviewed.
      </p>

      <div className="card" style={{ marginTop: 24 }}>
        <h3 style={{ marginTop: 0 }}>What&apos;s live right now</h3>
        <ul style={{ color: "var(--color-text-muted)", lineHeight: 1.8 }}>
          <li>Group &amp; Legal Entity administration (API-backed)</li>
          <li>Currency &amp; FX Rate administration (API-backed)</li>
          <li>Authentication (JWT) and entity-scoped RBAC</li>
          <li>Central audit trail</li>
        </ul>
        <span className="badge">DEMO DATA where applicable</span>
      </div>
    </div>
  );
}
