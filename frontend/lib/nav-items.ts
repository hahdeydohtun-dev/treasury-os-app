export interface NavItem {
  label: string;
  href: string;
  /** Modules not yet implemented beyond the foundation stage render as disabled. */
  enabled: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { label: "Dashboard", href: "/dashboard", enabled: true },
  { label: "Attention Center", href: "/attention-center", enabled: false },
  { label: "Cash & Liquidity", href: "/cash-liquidity", enabled: true },
  { label: "13-Week Forecast", href: "/forecast", enabled: true },
  { label: "Banks & Accounts", href: "/banks-accounts", enabled: true },
  { label: "Funding & Facilities", href: "/facilities", enabled: true },
  { label: "Payments", href: "/payments", enabled: false },
  { label: "Reconciliation", href: "/reconciliation", enabled: false },
  { label: "Intercompany", href: "/intercompany", enabled: false },
  { label: "Working Capital", href: "/working-capital", enabled: false },
  { label: "Investments", href: "/investments", enabled: true },
  { label: "Risk & Controls", href: "/risk-controls", enabled: false },
  { label: "KPIs", href: "/kpis", enabled: false },
  { label: "Reports", href: "/reports", enabled: false },
  { label: "Tasks", href: "/tasks", enabled: false },
  { label: "Excel Data Hub", href: "/excel-data-hub", enabled: true },
  { label: "AI Treasury Copilot", href: "/copilot", enabled: false },
  { label: "Administration", href: "/administration", enabled: true },
  { label: "Audit", href: "/audit", enabled: true },
];
