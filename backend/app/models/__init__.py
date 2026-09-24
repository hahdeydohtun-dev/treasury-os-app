"""
Import every model module here so Alembic's autogenerate can see the
full metadata via app.db.base_class.Base.metadata.
"""
from app.models.audit import AuditEvent  # noqa: F401
from app.models.balance import BankBalance  # noqa: F401
from app.models.bank_charge import BankCharge  # noqa: F401
from app.models.banking import Bank, BankAccount, BankAccountStatus  # noqa: F401
from app.models.currency import Currency, FXRate, FXRateType  # noqa: F401
from app.models.entity import BusinessUnit, Group, LegalEntity  # noqa: F401
from app.models.excel_hub import (  # noqa: F401
    ExcelTemplate,
    ImportBatch,
    ImportBatchStatus,
    ImportIssue,
    IssueSeverity,
)
from app.models.expected_cash_flow import (  # noqa: F401
    ExpectedCollection,
    ExpectedPayment,
    ForecastItemStatus,
)
from app.models.facility import (  # noqa: F401
    CollateralStatus,
    CollateralType,
    CommitmentType,
    CovenantOperator,
    CovenantStatus,
    CovenantType,
    DayCountConvention,
    DrawdownStatus,
    Facility,
    FacilityCollateral,
    FacilityCovenant,
    FacilityDrawdown,
    FacilityEvent,
    FacilityEventType,
    FacilityFee,
    FacilityRepayment,
    FacilityStatus,
    FacilitySubLimit,
    FacilityType,
    FacilityVersion,
    FeeStatus,
    FeeType,
    FundingAction,
    FundingActionStatus,
    FundingActionType,
    InterestRateType,
    RepaymentMethod,
    RepaymentStatus,
    RepaymentType,
)
from app.models.forecast import (  # noqa: F401
    AdjustmentStatus,
    AlertSeverity,
    AlertStatus,
    Forecast,
    ForecastAdjustment,
    ForecastAlert,
    ForecastCategory,
    ForecastLine,
    ForecastScenarioAssumption,
    ForecastScenarioType,
    ForecastSourceType,
    ForecastStatus,
    ForecastValueBasis,
    ForecastWeek,
    LiquidityScopeType,
    LiquidityThreshold,
    RecurringCashFlow,
    RecurringFrequency,
    WeekStatus,
)
from app.models.bank_statement import (  # noqa: F401
    BankStatementEntryType,
    BankStatementTransaction,
    BankStatementTransactionStatus,
)
from app.models.investment import (  # noqa: F401
    INVESTMENT_STATUS_TRANSITIONS,
    InterestPaymentFrequency,
    InterestPaymentMethod,
    Investment,
    InvestmentConcentrationLimit,
    InvestmentEvent,
    InvestmentEventType,
    InvestmentRateType,
    InvestmentStatus,
    InvestmentTransaction,
    InvestmentTransactionStatus,
    InvestmentTransactionType,
    InvestmentType,
    InvestmentVersion,
    PenaltyType,
)
from app.models.lookup import AccountType, CashDirection, CashEventType  # noqa: F401
from app.models.rbac import (  # noqa: F401
    EntityScopeType,
    Permission,
    Role,
    TreasuryAction,
    TreasuryModule,
    User,
    UserRoleAssignment,
)
from app.models.treasury_transaction import (  # noqa: F401
    TransactionStatus,
    TreasuryTransaction,
)
