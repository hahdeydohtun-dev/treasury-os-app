const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000/api/v1";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(
  path: string,
  options: RequestInit = {},
  token?: string
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(options.headers as Record<string, string> | undefined),
  };
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
    cache: "no-store",
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(response.status, body.detail ?? response.statusText);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export interface CurrentUser {
  id: string;
  email: string;
  full_name: string;
  is_superuser: boolean;
  mfa_enabled: boolean;
}

export interface BankAccountOut {
  id: string;
  legal_entity_id: string;
  bank_id: string;
  account_name: string;
  account_number_masked: string;
  currency_code: string;
  account_type_code: string;
  status: string;
  is_active: boolean;
}

export interface CashPositionOut {
  as_of: string;
  account_count: number;
  total_cash: string;
  available_cash: string;
  overdraft: string;
  net_cash: string;
  by_currency: Record<string, string>;
}

export interface ExcelTemplateOut {
  id: string;
  code: string;
  name: string;
  version: number;
  is_active: boolean;
  required_columns: string[];
  optional_columns: string[];
}

export interface ImportIssueOut {
  row_number: number;
  column_name: string | null;
  value: string | null;
  severity: "ERROR" | "WARNING";
  error_code: string;
  message: string;
}

export interface ImportBatchOut {
  id: string;
  template_code: string;
  template_version: number;
  legal_entity_id: string | null;
  file_name: string;
  uploaded_by_user_id: string;
  uploaded_at: string;
  status: string;
  total_rows: number;
  valid_rows: number;
  invalid_rows: number;
  warning_rows: number;
  imported_rows: number;
  duplicate_rows: number;
}

export interface ImportBatchDetailOut extends ImportBatchOut {
  issues: ImportIssueOut[];
}

export interface FreshnessEntry {
  template_code: string;
  last_upload_at: string | null;
  last_status: string | null;
  last_imported_rows: number | null;
  age_hours: number | null;
  freshness_status: "CURRENT" | "STALE" | "NO_DATA";
}

export const api = {
  login: (email: string, password: string) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  me: (token: string) => request<CurrentUser>("/auth/me", {}, token),
  listCurrencies: (token?: string) =>
    request<{ code: string; name: string }[]>("/currencies", {}, token),
  listGroups: (token: string) =>
    request<{ id: string; name: string; code: string }[]>("/groups", {}, token),

  // --- Banking ---
  listBankAccounts: (token: string) =>
    request<BankAccountOut[]>("/bank-accounts", {}, token),

  // --- Cash & Liquidity ---
  getCashPosition: (
    token: string,
    params?: { legal_entity_id?: string; currency_code?: string }
  ) => {
    const qs = new URLSearchParams(params as Record<string, string>).toString();
    return request<CashPositionOut>(`/cash-position${qs ? `?${qs}` : ""}`, {}, token);
  },

  // --- Excel Data Hub ---
  listTemplates: (token: string) =>
    request<ExcelTemplateOut[]>("/excel/templates", {}, token),
  listImportHistory: (token: string) =>
    request<ImportBatchOut[]>("/excel/imports", {}, token),
  getFreshness: (token: string) =>
    request<FreshnessEntry[]>("/excel/freshness", {}, token),
  getValidation: (token: string, batchId: string) =>
    request<ImportBatchDetailOut>(`/excel/validation/${batchId}`, {}, token),
  uploadExcel: async (
    token: string,
    file: File,
    templateCode: string,
    templateVersion: number
  ): Promise<ImportBatchDetailOut> => {
    const form = new FormData();
    form.append("template_code", templateCode);
    form.append("template_version", String(templateVersion));
    form.append("file", file);
    const response = await fetch(`${API_BASE_URL}/excel/uploads`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new ApiError(response.status, body.detail ?? response.statusText);
    }
    return response.json();
  },
  confirmImport: (token: string, batchId: string) =>
    request<{ batch: ImportBatchOut; imported_rows: number; skipped_rows: number }>(
      `/excel/imports/${batchId}/confirm`,
      { method: "POST" },
      token
    ),

  // --- 13-Week Forecast ---
  listForecasts: (token: string) => request<ForecastOut[]>("/forecast", {}, token),
  getForecast: (token: string, id: string) => request<ForecastOut>(`/forecast/${id}`, {}, token),
  createForecast: (token: string, payload: Record<string, unknown>) =>
    request<ForecastOut>("/forecast", { method: "POST", body: JSON.stringify(payload) }, token),
  calculateForecast: (token: string, id: string) =>
    request<ForecastOut>(`/forecast/${id}/calculate`, { method: "POST" }, token),
  publishForecast: (token: string, id: string) =>
    request<ForecastOut>(`/forecast/${id}/publish`, { method: "POST" }, token),
  rollForecast: (token: string, id: string) =>
    request<ForecastOut>(`/forecast/${id}/roll`, { method: "POST" }, token),
  getForecastSummary: (token: string, id: string) =>
    request<ForecastSummaryOut>(`/forecast/${id}/summary`, {}, token),
  getForecastWeeks: (token: string, id: string) =>
    request<ForecastWeekOut[]>(`/forecast/${id}/weeks`, {}, token),
  getForecastAlerts: (token: string, id: string) =>
    request<ForecastAlertOut[]>(`/forecast/${id}/alerts`, {}, token),
  getForecastLines: (token: string, id: string, weekNumber: number) =>
    request<ForecastLineOut[]>(`/forecast/${id}/lines?week_number=${weekNumber}`, {}, token),
  getForecastAccuracy: (token: string, id: string) =>
    request<AccuracyOut>(`/forecast/${id}/accuracy`, {}, token),
  exportForecastUrl: (id: string) => `${API_BASE_URL}/forecast/${id}/export`,

  // --- Funding & Credit Facilities ---
  listFacilities: (token: string) => request<FacilityOut[]>("/facilities", {}, token),
  getFacility: (token: string, id: string) => request<FacilityOut>(`/facilities/${id}`, {}, token),
  getFacilityUtilization: (token: string, id: string) =>
    request<FacilityUtilizationOut>(`/facilities/${id}/utilization`, {}, token),
  getFacilityEvents: (token: string, id: string) =>
    request<FacilityEventOut[]>(`/facilities/${id}/events`, {}, token),
  getFacilityVersions: (token: string, id: string) =>
    request<FacilityVersionOut[]>(`/facilities/${id}/versions`, {}, token),
  getFacilityDrawdowns: (token: string, id: string) =>
    request<FacilityDrawdownOut[]>(`/facilities/${id}/drawdowns`, {}, token),
  getFacilityRepayments: (token: string, id: string) =>
    request<FacilityRepaymentOut[]>(`/facilities/${id}/repayments`, {}, token),
  getFacilityFees: (token: string, id: string) =>
    request<FacilityFeeOut[]>(`/facilities/${id}/fees`, {}, token),
  getFacilityCovenants: (token: string, id: string) =>
    request<FacilityCovenantOut[]>(`/facilities/${id}/covenants`, {}, token),
  getFacilityCollateral: (token: string, id: string) =>
    request<FacilityCollateralOut[]>(`/facilities/${id}/collateral`, {}, token),
  getFacilitySubLimits: (token: string, id: string) =>
    request<FacilitySubLimitOut[]>(`/facilities/${id}/sub-limits`, {}, token),
  getFundingForecastImpact: (token: string, forecastId: string) =>
    request<FundingForecastImpactOut>(
      `/funding/forecast-impact?forecast_id=${forecastId}`, {}, token
    ),
  getFundingDashboard: (token: string) => request<FundingDashboardOut>("/funding/dashboard", {}, token),
  getFundingCalendar: (token: string) => request<FundingCalendarEntry[]>("/funding/calendar", {}, token),
  listFacilityTypes: (token: string) =>
    request<{ code: string; name: string }[]>("/facility-types", {}, token),

  // --- Bank Statement Ingestion (Stage 5A) ---
  listBankStatementTransactions: (token: string, params?: Record<string, string>) =>
    request<BankStatementTransactionOut[]>(
      `/bank-statements/transactions${params ? "?" + new URLSearchParams(params) : ""}`, {}, token
    ),
  getBankStatementTransaction: (token: string, id: string) =>
    request<BankStatementTransactionOut>(`/bank-statements/transactions/${id}`, {}, token),

  // --- Reconciliation Data Model (Stage 5B) ---
  listReconciliationRuns: (token: string, params?: Record<string, string>) =>
    request<ReconciliationRunOut[]>(
      `/reconciliation/runs${params ? "?" + new URLSearchParams(params) : ""}`, {}, token
    ),
  getReconciliationRun: (token: string, id: string) =>
    request<ReconciliationRunOut>(`/reconciliation/runs/${id}`, {}, token),
  createReconciliationRun: (token: string, payload: Record<string, unknown>) =>
    request<ReconciliationRunOut>("/reconciliation/runs", {
      method: "POST", body: JSON.stringify(payload),
    }, token),
  executeReconciliationRun: (token: string, id: string) =>
    request<ReconciliationRunOut>(`/reconciliation/runs/${id}/execute`, { method: "POST" }, token),
  cancelReconciliationRun: (token: string, id: string) =>
    request<ReconciliationRunOut>(`/reconciliation/runs/${id}/cancel`, { method: "POST" }, token),
  listReconciliationConfigurations: (token: string, params?: Record<string, string>) =>
    request<ReconciliationConfigurationOut[]>(
      `/reconciliation/configurations${params ? "?" + new URLSearchParams(params) : ""}`, {}, token
    ),
  createReconciliationConfiguration: (token: string, payload: Record<string, unknown>) =>
    request<ReconciliationConfigurationOut>("/reconciliation/configurations", {
      method: "POST", body: JSON.stringify(payload),
    }, token),

  // --- Investments & Fixed Deposit Management ---
  listInvestments: (token: string, params?: Record<string, string>) =>
    request<InvestmentOut[]>(`/investments${params ? "?" + new URLSearchParams(params) : ""}`, {}, token),
  getInvestment: (token: string, id: string) => request<InvestmentOut>(`/investments/${id}`, {}, token),
  getInvestmentVersions: (token: string, id: string) =>
    request<InvestmentVersionOut[]>(`/investments/${id}/versions`, {}, token),
  getInvestmentEvents: (token: string, id: string) =>
    request<InvestmentEventOut[]>(`/investments/${id}/events`, {}, token),
  getInvestmentTransactions: (token: string, id: string) =>
    request<InvestmentTransactionOut[]>(`/investments/${id}/transactions`, {}, token),
  listInvestmentTypes: (token: string) =>
    request<{ code: string; name: string; is_implemented: boolean }[]>("/investment-types", {}, token),
  getInvestmentLiquidity: (token: string, legalEntityId?: string) =>
    request<InvestmentLiquidityOut>(
      `/investments-reports/liquidity${legalEntityId ? `?legal_entity_id=${legalEntityId}` : ""}`, {}, token
    ),
  getInvestmentMaturityCalendar: (token: string, params?: Record<string, string>) =>
    request<InvestmentOut[]>(
      `/investments-reports/maturity-calendar${params ? "?" + new URLSearchParams(params) : ""}`, {}, token
    ),
  getInvestmentConcentration: (token: string, legalEntityId?: string) =>
    request<ConcentrationRow[]>(
      `/investments-reports/concentration${legalEntityId ? `?legal_entity_id=${legalEntityId}` : ""}`, {}, token
    ),
  getInvestmentAlerts: (token: string, legalEntityId?: string) =>
    request<InvestmentAlertOut[]>(
      `/investments-reports/alerts${legalEntityId ? `?legal_entity_id=${legalEntityId}` : ""}`, {}, token
    ),
};

export interface ForecastOut {
  id: string;
  group_id: string | null;
  legal_entity_id: string | null;
  forecast_start_date: string;
  forecast_end_date: string;
  scenario: "BASE" | "CONSERVATIVE" | "STRESS";
  value_basis: "GROSS" | "PROBABILITY_ADJUSTED";
  reporting_currency_code: string;
  version: number;
  parent_forecast_id: string | null;
  status: "DRAFT" | "PUBLISHED" | "ARCHIVED";
  data_quality_warnings: string[];
  published_at: string | null;
}

export interface ForecastWeekOut {
  week_number: number;
  start_date: string;
  end_date: string;
  status: "FUTURE" | "CURRENT" | "COMPLETED";
  opening_cash: string;
  total_inflows: string;
  total_outflows: string;
  net_transfers: string;
  net_cash_flow: string;
  closing_cash: string;
  minimum_required_liquidity: string;
  surplus_or_gap: string;
  liquidity_coverage_ratio: string | null;
}

export interface ForecastSummaryOut {
  forecast: ForecastOut;
  weeks: ForecastWeekOut[];
  opening_cash: string;
  thirteen_week_net_cash_flow: string;
  lowest_projected_cash: string;
  lowest_projected_cash_week: number | null;
  largest_funding_gap: string;
  largest_funding_gap_week: number | null;
  total_surplus: string;
  average_liquidity_coverage: string | null;
}

export interface ForecastAlertOut {
  id: string;
  severity: "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
  currency_code: string | null;
  week_number: number | null;
  metric: string;
  message: string;
  status: string;
}

export interface ForecastLineOut {
  id: string;
  category_code: string;
  direction: string;
  transaction_currency_code: string;
  original_amount: string;
  reporting_amount: string;
  source_type: string;
  description: string | null;
}

export interface AccuracyOut {
  overall_accuracy: string | null;
  target_variance_pct: string;
  actual_variance_pct: string | null;
  within_target: boolean | null;
  weeks_measured: number;
}

// --- Funding & Credit Facilities ---
export interface FacilityOut {
  id: string;
  facility_reference: string;
  facility_name: string;
  facility_type_code: string;
  commitment_type: "COMMITTED" | "UNCOMMITTED";
  lender_id: string;
  legal_entity_id: string;
  currency_code: string;
  approved_limit: string;
  committed_limit: string;
  current_drawn_amount: string;
  status: string;
  maturity_date: string;
  version: number;
}

export interface FacilityUtilizationOut {
  committed_limit: string;
  drawn_amount: string;
  undrawn_amount: string;
  covenant_restricted_amount: string;
  available_amount: string;
  utilization_pct: string;
  effective_interest_rate: string | null;
}

export interface FundingDashboardOut {
  total_committed_limit: string;
  total_drawn: string;
  total_undrawn: string;
  total_available: string;
  utilization_pct: string;
  facility_count: number;
  by_currency: Record<string, { committed_limit: string; drawn: string }>;
  facilities_maturing_30_days: number;
  facilities_maturing_90_days: number;
  covenant_warnings: number;
  covenant_breaches: number;
}

export interface FundingCalendarEntry {
  date: string;
  facility_id: string;
  facility_name: string;
  currency_code: string;
  event_type: string;
  amount: string;
  status: string;
}

export interface FacilityEventOut {
  id: string;
  event_type: string;
  event_date: string;
  description: string;
}

export interface FacilityVersionOut {
  id: string;
  facility_id: string;
  version: number;
  effective_date: string;
  terms: Record<string, string | null>;
  change_reason: string | null;
}

export interface FacilityDrawdownOut {
  id: string;
  facility_id: string;
  legal_entity_id: string;
  sub_limit_id: string | null;
  currency_code: string;
  drawdown_amount: string;
  drawdown_date: string;
  status: string;
  reference: string | null;
  purpose: string | null;
}

export interface FacilityRepaymentOut {
  id: string;
  facility_id: string;
  currency_code: string;
  repayment_type: string;
  original_amount: string;
  paid_amount: string;
  due_date: string;
  actual_payment_date: string | null;
  status: string;
  is_early_repayment: boolean;
}

export interface FacilityFeeOut {
  id: string;
  facility_id: string;
  fee_type: string;
  currency_code: string;
  amount: string;
  due_date: string;
  paid_date: string | null;
  status: string;
}

export interface FacilityCovenantOut {
  id: string;
  facility_id: string;
  name: string;
  covenant_type: string;
  threshold: string | null;
  operator: string;
  current_value: string | null;
  headroom: string | null;
  status: string;
  warning_threshold: string | null;
  breach_threshold: string | null;
}

export interface FacilityCollateralOut {
  id: string;
  facility_id: string;
  collateral_type: string;
  value: string;
  currency_code: string;
  haircut_pct: string;
  eligible_value: string;
  status: string;
}

export interface FacilitySubLimitOut {
  id: string;
  facility_id: string;
  name: string;
  purpose_code: string | null;
  limit_amount: string;
  drawn_amount: string;
  is_active: boolean;
}

export interface FundingForecastImpactOut {
  facility_driven_line_count: number;
  total_facility_inflows: string;
  total_facility_outflows: string;
  lines: {
    week_id: string;
    category_code: string;
    direction: string;
    amount: string;
    source_type: string;
    source_id: string | null;
    description: string | null;
  }[];
}

// --- Investments & Fixed Deposit Management types ---
export interface InvestmentOut {
  id: string;
  investment_reference: string;
  investment_type_code: string;
  legal_entity_id: string;
  institution_id: string;
  currency_code: string;
  principal_amount: string;
  original_principal_amount: string;
  placement_date: string | null;
  start_date: string;
  maturity_date: string;
  tenor_days: number;
  interest_rate: string;
  rate_type: string;
  day_count_convention: string;
  interest_payment_method: string;
  expected_interest: string;
  accrued_interest: string;
  received_interest: string;
  early_termination_allowed: boolean;
  partial_termination_allowed: boolean;
  rollover_allowed: boolean;
  status: string;
  version: number;
  previous_investment_id: string | null;
  rolled_to_investment_id: string | null;
  is_active: boolean;
}

export interface InvestmentVersionOut {
  id: string;
  investment_id: string;
  version: number;
  effective_date: string;
  terms: Record<string, string | number | boolean | null>;
  change_reason: string | null;
}

export interface InvestmentEventOut {
  id: string;
  investment_id: string;
  event_type: string;
  event_date: string;
  description: string;
  amount: string | null;
  currency_code: string | null;
}

export interface InvestmentTransactionOut {
  id: string;
  investment_id: string;
  transaction_type: string;
  currency_code: string;
  amount: string;
  transaction_date: string;
  status: string;
  reference: string | null;
  description: string | null;
  related_investment_id: string | null;
}

export interface InvestmentLiquidityOut {
  total_invested: string;
  maturing_7_days: string;
  maturing_30_days: string;
  maturing_90_days: string;
  total_expected_interest: string;
  weighted_average_rate: string | null;
  investment_count: number;
  by_currency: Record<string, string>;
  by_entity: Record<string, string>;
  by_institution: Record<string, string>;
}

export interface ConcentrationRow {
  dimension: string;
  key: string;
  current_amount: string;
  limit_amount: string | null;
  available_capacity: string | null;
  utilization_pct: string | null;
  status: string;
}

export interface InvestmentAlertOut {
  alert_type: string;
  investment_id: string;
  investment_reference: string;
  message: string;
  severity: string;
  due_date: string | null;
}

// --- Bank Statement Ingestion (Stage 5A) types ---
export interface BankStatementTransactionOut {
  id: string;
  legal_entity_id: string;
  bank_id: string;
  bank_account_id: string;
  statement_period_start: string;
  statement_period_end: string;
  transaction_date: string;
  value_date: string | null;
  posting_date: string | null;
  entry_type: "DEBIT" | "CREDIT";
  amount: string;
  currency_code: string;
  balance_after_transaction: string | null;
  bank_reference: string | null;
  external_transaction_id: string | null;
  narration: string | null;
  has_strong_identity: boolean;
  status: string;
  import_batch_id: string;
  source_row_number: number;
}

// --- Reconciliation Data Model (Stage 5B) types ---
export interface ReconciliationRunOut {
  id: string;
  legal_entity_id: string;
  bank_account_id: string;
  period_start: string;
  period_end: string;
  status: "DRAFT" | "READY" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
  configuration_id: string | null;
  statement_transaction_count: number;
  eligible_transaction_count: number;
  candidate_count: number;
  suggestion_count: number;
  matched_count: number;
  ambiguous_count: number;
  unmatched_count: number;
  matching_rule_version: string | null;
  notes: string | null;
  failure_reason: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface ReconciliationConfigurationOut {
  id: string;
  legal_entity_id: string | null;
  bank_account_id: string | null;
  currency_code: string | null;
  amount_tolerance_pct: string;
  date_tolerance_days: number;
  high_value_threshold: string | null;
  duplicate_policy: string | null;
  matching_rule_config: Record<string, unknown>;
  is_active: boolean;
  version: number;
  is_current: boolean;
  superseded_by_id: string | null;
  effective_from: string;
}
