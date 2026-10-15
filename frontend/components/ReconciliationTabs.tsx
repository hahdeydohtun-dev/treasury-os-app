"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { api, MatchReviewSummary } from "@/lib/api-client";
import { useAuthToken } from "@/lib/use-auth-token";

// The two reconciliation queues are deliberately separate: EXCEPTIONS are unresolved items needing
// investigation; MATCH REVIEW holds engine PROPOSALS that are still awaiting an accept/reject decision.
// A proposal is not a reconciliation until it is accepted, so its count is shown prominently.
export default function ReconciliationTabs({ active, refreshKey }: { active: "exceptions" | "review"; refreshKey?: number }) {
  const token = useAuthToken();
  const [summary, setSummary] = useState<MatchReviewSummary | null>(null);

  useEffect(() => {
    if (!token) return;
    api.getMatchReviewSummary(token).then(setSummary).catch(() => setSummary(null));
  }, [token, refreshKey]);

  const tab = (isActive: boolean): React.CSSProperties => ({
    padding: "8px 14px", borderRadius: 6, textDecoration: "none", fontWeight: 600,
    border: "1px solid var(--color-border, #334)", color: "inherit",
    background: isActive ? "var(--color-accent, #3b82f6)" : "transparent",
  });
  const count = (n: number | undefined, colour: string) => (
    <span className="badge" style={{ marginLeft: 8, background: colour, color: "#fff" }}>{n ?? "–"}</span>
  );

  return (
    <div style={{ display: "flex", gap: 8, margin: "12px 0" }}>
      <Link href="/reconciliation/open-items" style={tab(active === "exceptions")}>
        Exceptions to investigate{count(summary?.open_exceptions, "#b45309")}
      </Link>
      <Link href="/reconciliation/match-review" style={tab(active === "review")}>
        Match review – decisions pending{count(summary?.pending_decisions, "#0369a1")}
      </Link>
    </div>
  );
}
