"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { AdaptiveHealth, AdaptiveSettingsResponse, api, ApiError, ReportDrill } from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";
import ReconciliationTabs from "@/components/ReconciliationTabs";

// Stage 5G administration. Adaptive matching is OFF unless an authorised administrator turns it on for an account; a change creates
// a new, audited configuration version. There is deliberately no control that accepts or rejects anything: a person always decides.
type Form = {
  enabled: boolean; min_evidence: string; min_distinct_runs: string; min_signal_families: string; max_boost: string;
  max_penalty: string; half_life_days: string; max_evidence_age_days: string; min_support_level: string;
};
const FIELDS: { key: keyof Form; label: string; help: string }[] = [
  { key: "min_evidence", label: "Minimum decisions per pattern", help: "A learned pattern needs at least this many earlier accepted or rejected decisions." },
  { key: "min_distinct_runs", label: "Minimum runs", help: "...drawn from at least this many different reconciliation runs." },
  { key: "min_signal_families", label: "Independent signals required", help: "How many different kinds of pattern must agree before any adjustment is applied." },
  { key: "max_boost", label: "Maximum boost (points)", help: "The most a recommendation can rise. Never more than 15." },
  { key: "max_penalty", label: "Maximum penalty (points)", help: "The most a recommendation can fall. Never more than 15." },
  { key: "half_life_days", label: "Evidence half-life (days)", help: "Older decisions count for half as much for every full half-life." },
  { key: "max_evidence_age_days", label: "Ignore decisions older than (days)", help: "Decisions older than this are not used." },
];
const toForm = (s: AdaptiveSettingsResponse["settings"]): Form => ({
  enabled: s.enabled, min_evidence: String(s.min_evidence), min_distinct_runs: String(s.min_distinct_runs),
  min_signal_families: String(s.min_signal_families), max_boost: String(s.max_boost), max_penalty: String(s.max_penalty),
  half_life_days: String(s.half_life_days), max_evidence_age_days: String(s.max_evidence_age_days), min_support_level: s.min_support_level,
});

export default function AdaptiveMatchingPage() {
  const token = useAuthToken();
  const [entities, setEntities] = useState<{ id: string; name: string }[]>([]);
  const [accounts, setAccounts] = useState<{ id: string; label: string; entity: string }[]>([]);
  const [entityId, setEntityId] = useState("");
  const [accountId, setAccountId] = useState("");
  const [settings, setSettings] = useState<AdaptiveSettingsResponse | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [health, setHealth] = useState<AdaptiveHealth | null>(null);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Entity and account choices come from the reconciliation reports (reconciliation access only; already limited to what the user may see).
  useEffect(() => {
    if (!token) return;
    api.getReconciliationReport(token, "entity-dashboard", "")
      .then((r) => setEntities((r.tables.find((t) => t.key === "entities")?.rows ?? [])
        .map((row) => ({ id: (row["_drill"] as ReportDrill).params.legal_entity_id, name: String(row["entity"]) }))))
      .catch(() => setEntities([]));
    api.getReconciliationReport(token, "bank-accounts", "page_size=200")
      .then((r) => setAccounts((r.tables[0]?.rows ?? []).map((row) => ({ id: String(row["id"]), entity: String(row["entity"]), label: `${row["bank"]} ${row["account"]} (${row["currency"]})` }))))
      .catch(() => setAccounts([]));
  }, [token]);

  const entityName = entities.find((e) => e.id === entityId)?.name;
  const accountChoices = accounts.filter((a) => !entityName || a.entity === entityName);

  const load = useCallback(() => {
    setSettings(null); setForm(null); setHealth(null); setError(null); setNotice(null);
    if (!token || !entityId) return;
    api.getAdaptiveSettings(token, entityId, accountId || undefined)
      .then((s) => { setSettings(s); setForm(toForm(s.settings)); })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Unable to load the settings"));
    if (accountId) api.getAdaptiveHealth(token, entityId, accountId).then(setHealth).catch(() => setHealth(null));
  }, [token, entityId, accountId]);
  useEffect(load, [load]);

  async function save() {
    if (!token || !form || !entityId) return;
    setSaving(true); setError(null); setNotice(null);
    try {
      const saved = await api.updateAdaptiveSettings(token, {
        legal_entity_id: entityId, bank_account_id: accountId || null, reason: reason || undefined,
        settings: {
          enabled: form.enabled, scope: "account", min_support_level: form.min_support_level,
          min_evidence: Number(form.min_evidence), min_distinct_runs: Number(form.min_distinct_runs), min_signal_families: Number(form.min_signal_families),
          max_boost: Number(form.max_boost), max_penalty: Number(form.max_penalty), half_life_days: Number(form.half_life_days),
          max_evidence_age_days: Number(form.max_evidence_age_days),
        },
      });
      setSettings(saved); setForm(toForm(saved.settings)); setReason("");
      setNotice(`Saved as configuration version ${saved.configuration_version}. It applies to reconciliation runs executed from now on; earlier runs and explanations are unchanged.`);
      if (accountId) api.getAdaptiveHealth(token, entityId, accountId).then(setHealth).catch(() => setHealth(null));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not save the settings");
    } finally {
      setSaving(false);
    }
  }

  const b = settings?.bounds;
  return (
    <div>
      <h1>Adaptive matching</h1>
      <p style={{ color: "var(--color-text-muted)" }}>
        Adaptive matching learns recurring patterns from the matches your team has explicitly <strong>accepted or rejected</strong> on a bank
        account, and uses them to help you prioritise review. It is <strong>off by default</strong>, it only produces recommendations, and a
        person must still accept or reject every match. It cannot change a suggestion&apos;s confidence, tier or whether it can be decided.{" "}
        <Link href="/reconciliation" style={{ color: "var(--color-accent)" }}>Back to runs</Link>
      </p>
      <ReconciliationTabs active="adaptive" />
      {settings && <p className="card" style={{ color: "var(--color-text-muted)" }}>{settings.scope_note}</p>}

      <div className="card" style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "end" }}>
        <label>Entity<br />
          <select value={entityId} onChange={(e) => { setEntityId(e.target.value); setAccountId(""); }}>
            <option value="">Choose an entity</option>
            {entities.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
          </select>
        </label>
        <label>Bank account<br />
          <select value={accountId} disabled={!entityId} onChange={(e) => setAccountId(e.target.value)}>
            <option value="">Choose a bank account</option>
            {accountChoices.map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}
          </select>
        </label>
      </div>
      {error && <p style={{ color: "var(--color-danger)" }}>{error}</p>}
      {notice && <p style={{ color: "var(--color-accent)" }}>{notice}</p>}
      {!entityId && <p>Choose an entity and a bank account to see its adaptive matching settings and learning health.</p>}

      {settings && form && (
        <div className="card">
          <h2>
            Settings {settings.settings.enabled
              ? <span className="badge" style={{ background: "#15803d", color: "#fff" }}>Enabled</span>
              : <span className="badge" style={{ background: "#64748b", color: "#fff" }}>Disabled</span>}
          </h2>
          <p style={{ color: "var(--color-text-muted)", fontSize: 13 }}>
            {settings.configuration_version === null ? "No configuration exists for this scope yet; the defaults apply." : `Current configuration version ${settings.configuration_version}.`}{" "}
            Learning is at bank-account level only: evidence is never shared across accounts, entities, currencies or transaction directions.
            Rule version {settings.learning_rule_version} on top of matching rule {settings.base_rule_version}.
          </p>
          {settings.settings_unusable && <p style={{ color: "var(--color-danger)" }}>The stored settings are unusable, so adaptive matching is disabled and runs use deterministic matching only.</p>}
          {settings.warnings.map((w) => <p key={w} style={{ color: "var(--color-warning)" }}>{w}</p>)}
          <label style={{ display: "block", margin: "8px 0" }}>
            <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /> Enable adaptive matching for{" "}
            {accountId ? "this bank account" : "every account this entity-level configuration covers"}
          </label>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))", gap: 12 }}>
            {FIELDS.map((f) => (
              <label key={f.key} title={f.help}>{f.label}<br />
                <input type="number" step={f.key === "max_boost" || f.key === "max_penalty" ? "0.5" : "1"} value={form[f.key] as string}
                       min={b?.[f.key]?.[0]} max={b?.[f.key]?.[1]} onChange={(e) => setForm({ ...form, [f.key]: e.target.value })} style={{ width: "100%" }} />
                <span style={{ color: "var(--color-text-muted)", fontSize: 11 }}>{f.help} Allowed {b?.[f.key]?.[0]}–{b?.[f.key]?.[1]}; default {String(settings.defaults[f.key as keyof typeof settings.defaults])}.</span>
              </label>
            ))}
            <label title="Adjustments are applied only when the evidence reaches this support level.">Minimum support level<br />
              <select value={form.min_support_level} onChange={(e) => setForm({ ...form, min_support_level: e.target.value })} style={{ width: "100%" }}>
                {["LOW", "MEDIUM", "HIGH"].map((l) => <option key={l}>{l}</option>)}
              </select>
            </label>
          </div>
          <textarea rows={2} style={{ width: "100%", marginTop: 12 }} value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Reason for this change (recorded in the audit trail)" />
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button className="btn-primary" disabled={saving} onClick={save}>{saving ? "Saving…" : "Save as a new configuration version"}</button>
            <button className="btn-secondary" disabled={saving} onClick={() => setForm(toForm(settings.settings))}>Reset</button>
          </div>
          <p style={{ color: "var(--color-text-muted)", fontSize: 12 }}>Changing settings needs the Reconcile Configure permission. Each save creates a new version and is audited; disabling keeps every earlier explanation.</p>
        </div>
      )}

      {health && (
        <div className="card">
          <h2>Learning health</h2>
          <p>
            <strong>{health.evidence.eligible}</strong> eligible decisions ({health.evidence.eligible_accepted} accepted, {health.evidence.eligible_rejected} rejected) out of{" "}
            {health.evidence.decisions_scanned} on this account{health.evidence.latest_eligible_decision_at ? `, latest ${new Date(health.evidence.latest_eligible_decision_at).toLocaleDateString()}` : ""}.
            {health.evidence.scan_truncated && " Only the most recent decisions were scanned."}
          </p>
          {Object.keys(health.evidence.excluded_by_reason).length > 0 && (
            <>
              <h3>Decisions not used as evidence</h3>
              <ul>{Object.entries(health.evidence.excluded_by_reason).map(([k, v]) => <li key={k}>{k.replace(/_/g, " ").toLowerCase()}: {v}</li>)}</ul>
            </>
          )}
          <h3>Learned patterns</h3>
          {Object.keys(health.patterns_by_family).length === 0 ? <p>None yet.</p> : (
            <ul>{Object.entries(health.patterns_by_family).map(([k, v]) => <li key={k}>{k.replace(/_/g, " ")}: {v.supported} of {v.patterns} patterns have enough evidence to be used</li>)}</ul>
          )}
          <h3>Recent use</h3>
          <p>
            {health.scored_suggestions.total} suggestion{health.scored_suggestions.total === 1 ? "" : "s"} scored: {health.scored_suggestions.with_adaptive_contribution} received an adaptive adjustment and{" "}
            {health.scored_suggestions.fell_back_to_deterministic} fell back to the deterministic score
            {health.scored_suggestions.last_annotated_at ? `; last scored ${new Date(health.scored_suggestions.last_annotated_at).toLocaleString()}` : ""}.
          </p>
        </div>
      )}
    </div>
  );
}
