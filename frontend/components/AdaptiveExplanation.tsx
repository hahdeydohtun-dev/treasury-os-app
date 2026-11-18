"use client";

import { AdaptiveExplanation as Explanation } from "@/lib/api-client";

// Stage 5G: why a suggestion deserves attention. The three scores are shown separately and never merged, because only the
// deterministic score decides what the suggestion IS (its tier and whether it can be decided). Adaptive evidence adjusts the
// recommendation only. Nothing here can accept, reject or change a suggestion.
const LEVEL_COLOUR: Record<string, string> = { NONE: "#64748b", LOW: "#b45309", MEDIUM: "#0369a1", HIGH: "#15803d" };
const signed = (v: string) => (Number(v) > 0 ? `+${v}` : v);

function Score({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div style={{ flex: "1 1 120px", padding: 8, border: "1px solid var(--color-border, #334)", borderRadius: 6 }}>
      <div style={{ color: "var(--color-text-muted)", fontSize: 12 }}>{label}</div>
      <div style={{ fontSize: 22, fontWeight: 700 }}>{value}</div>
      <div style={{ color: "var(--color-text-muted)", fontSize: 11 }}>{note}</div>
    </div>
  );
}

export default function AdaptiveExplanation({ explanation }: { explanation: Explanation | null }) {
  if (!explanation) {
    return (
      <div className="card" style={{ marginTop: 12 }}>
        <h3>Adaptive recommendation</h3>
        <p style={{ color: "var(--color-text-muted)" }}>
          No adaptive explanation was recorded for this suggestion: adaptive matching was disabled, or it was not applicable, when its run
          executed. Only the deterministic score applies.
        </p>
      </div>
    );
  }
  return (
    <div className="card" style={{ marginTop: 12 }}>
      <h3>
        Adaptive recommendation{" "}
        <span className="badge" style={{ background: LEVEL_COLOUR[explanation.support_level], color: "#fff" }}>Support: {explanation.support_level}</span>{" "}
        {explanation.fallback && <span className="badge" style={{ background: "#64748b", color: "#fff" }}>Fell back to deterministic</span>}
      </h3>
      <p style={{ color: "var(--color-warning)" }}>
        <strong>A suggestion requiring human review.</strong> Adaptive evidence never accepts or rejects a match and never changes this
        suggestion&apos;s status, deterministic confidence or whether it can be decided. There is no automatic acceptance.
      </p>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <Score label="Deterministic score" value={explanation.deterministic_score} note={`Stage 5C ${explanation.base_rule_version} · ${explanation.score_band === "AUTO" ? "auto-match tier" : "review tier"}`} />
        <Score label="Adaptive contribution" value={signed(explanation.adaptive_contribution)} note={explanation.fallback ? "none applied" : "capped; learned from your decisions"} />
        <Score label="Recommendation score" value={explanation.recommendation_score} note="stays within the deterministic tier" />
      </div>
      <p>{explanation.explanation}</p>
      <p style={{ color: "var(--color-text-muted)", fontSize: 13 }}>
        Evidence used: {explanation.accepted_examples} accepted and {explanation.rejected_examples} rejected earlier decisions · rule version{" "}
        {explanation.learning_rule_version} · configuration v{explanation.configuration_version ?? "–"} · evidence up to{" "}
        {new Date(explanation.evidence_cutoff).toLocaleString()} · scope: this bank account only ({explanation.scope.currency_code},{" "}
        {explanation.scope.direction.toLowerCase()}, {explanation.scope.match_type.toLowerCase()} matches)
      </p>
      {explanation.patterns.length > 0 && (
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ textAlign: "left", color: "var(--color-text-muted)" }}>
              <th style={{ padding: "4px 6px" }}>Learned pattern</th><th style={{ padding: "4px 6px" }}>Accepted</th>
              <th style={{ padding: "4px 6px" }}>Rejected</th><th style={{ padding: "4px 6px" }}>Effect</th>
            </tr>
          </thead>
          <tbody>
            {explanation.patterns.map((p) => (
              <tr key={p.family}>
                <td style={{ padding: "4px 6px" }}>{p.family_label}<br /><span style={{ color: "var(--color-text-muted)", fontSize: 12 }}>{p.label}</span></td>
                <td style={{ padding: "4px 6px" }}>{p.accepted}</td>
                <td style={{ padding: "4px 6px" }}>{p.rejected}</td>
                <td style={{ padding: "4px 6px" }}>{p.supported && !explanation.fallback ? signed(p.contribution) : `not used – ${p.note ?? "fell back to deterministic"}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
