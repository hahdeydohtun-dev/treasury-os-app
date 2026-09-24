import uuid
from collections.abc import AsyncGenerator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_password
from app.db.session import AsyncSessionLocal, engine
from app.main import app
from app.models.currency import Currency
from app.models.rbac import (
    EntityScopeType,
    Permission,
    Role,
    TreasuryAction,
    TreasuryModule,
    User,
    UserRoleAssignment,
)


@pytest_asyncio.fixture(autouse=True)
async def _reset_engine_pool() -> AsyncGenerator[None, None]:
    """
    Each test gets its own asyncio event loop (function-scoped, the
    pytest-asyncio default). asyncpg connections are bound to the loop
    they were opened on, so the shared module-level engine's pool must be
    disposed between tests - otherwise a connection opened under a
    previous test's loop gets reused under a new one and asyncpg raises
    'Task ... attached to a different loop'.
    """
    await engine.dispose()
    yield


@pytest_asyncio.fixture(autouse=True)
async def _clean_database(_reset_engine_pool: None) -> AsyncGenerator[None, None]:
    """
    The foundation test DB is a real Postgres instance (no per-test
    transaction rollback across the app's own session - see
    `_reset_engine_pool`), so tables are truncated before every test to
    keep fixtures like Currency (natural PK, e.g. 'USD') idempotent.
    """
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "TRUNCATE TABLE audit_events, import_issues, import_batches, "
                "bank_statement_transactions, "
                "reconciliation_open_items, reconciliation_match_suggestions, "
                "reconciliation_runs, reconciliation_configurations, "
                "forecast_alerts, forecast_lines, forecast_weeks, forecast_adjustments, "
                "forecast_scenario_assumptions, forecasts, recurring_cash_flows, "
                "liquidity_thresholds, "
                "investment_events, investment_transactions, investment_versions, "
                "investment_concentration_limits, investments, "
                "funding_actions, facility_events, facility_collateral, facility_covenants, "
                "facility_fees, facility_repayments, facility_drawdowns, facility_sub_limits, "
                "facility_versions, facilities, "
                "treasury_transactions, expected_collections, expected_payments, "
                "bank_charges, bank_balances, bank_accounts, banks, "
                "fx_rates, user_role_assignments, "
                "permissions, roles, business_units, legal_entities, groups, "
                "currencies, users RESTART IDENTITY CASCADE"
            )
        )
        await session.commit()
    yield


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """
    Talks to the real app dependency (its own DB session per request against
    the same test database) rather than sharing a session object with test
    setup code - asyncpg does not support concurrent use of one AsyncSession.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def base_currency(db_session: AsyncSession) -> Currency:
    currency = Currency(code="USD", name="US Dollar", symbol="$", is_base_currency=True)
    db_session.add(currency)
    await db_session.flush()
    return currency


@pytest_asyncio.fixture
async def superuser(db_session: AsyncSession) -> User:
    user = User(
        email=f"admin-{uuid.uuid4().hex[:8]}@treasuryos.example.com",
        full_name="Test Admin",
        hashed_password=hash_password("Password123!"),
        is_superuser=True,
    )
    db_session.add(user)
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def demo_currencies(db_session: AsyncSession) -> list:
    codes = [("NGN", "Nigerian Naira"), ("USD", "US Dollar"),
             ("GBP", "British Pound"), ("EUR", "Euro")]
    created = []
    for code, name in codes:
        c = Currency(code=code, name=name, is_base_currency=True)
        db_session.add(c)
        created.append(c)
    await db_session.flush()
    return created


@pytest_asyncio.fixture
async def cash_event_types(db_session: AsyncSession):
    """
    Seeds the CashEventType rows Stage 4's cash-integration hardening
    pass needs (app/services/investment_service.py writes real
    TreasuryTransaction rows referencing these codes for placement,
    termination, maturity settlement, and rollover). Idempotent (checks
    existence first) so it composes safely with any other fixture that
    might also seed reference data.
    """
    from app.models.lookup import CashDirection, CashEventType

    codes = [
        ("INVESTMENT_PLACEMENT", "Investment Placement", CashDirection.OUTFLOW),
        ("INVESTMENT_MATURITY", "Investment Maturity", CashDirection.INFLOW),
        ("INVESTMENT_TERMINATION", "Investment Early Termination Proceeds", CashDirection.INFLOW),
        ("INVESTMENT_INTEREST_RECEIPT", "Investment Interest Receipt", CashDirection.INFLOW),
        ("INVESTMENT_ROLLOVER", "Investment Rollover (internal, non-cash)", CashDirection.NON_CASH),
    ]
    for code, name, direction in codes:
        if await db_session.get(CashEventType, code) is None:
            db_session.add(CashEventType(code=code, name=name, default_direction=direction))
    await db_session.flush()


@pytest_asyncio.fixture
async def demo_group_and_entities(db_session: AsyncSession, demo_currencies):
    from app.models.entity import Group, LegalEntity

    group = Group(name="Demo Group", code="DEMOGRP", reporting_currency_code="USD")
    db_session.add(group)
    await db_session.flush()

    entity_a = LegalEntity(
        group_id=group.id, name="Entity A", code="ENTA",
        functional_currency_code="NGN", country="NG",
    )
    entity_b = LegalEntity(
        group_id=group.id, name="Entity B", code="ENTB",
        functional_currency_code="USD", country="US",
    )
    db_session.add_all([entity_a, entity_b])
    await db_session.flush()
    return group, entity_a, entity_b


@pytest_asyncio.fixture
async def demo_bank_and_account(db_session: AsyncSession, demo_group_and_entities):
    from app.models.banking import Bank, BankAccount
    from app.models.lookup import AccountType

    _, entity_a, _ = demo_group_and_entities

    account_type = await db_session.get(AccountType, "CURRENT")
    if account_type is None:
        account_type = AccountType(code="CURRENT", name="Current Account")
        db_session.add(account_type)
        await db_session.flush()

    bank = Bank(name="Demo Bank A", country="NG")
    db_session.add(bank)
    await db_session.flush()

    account = BankAccount(
        legal_entity_id=entity_a.id,
        bank_id=bank.id,
        account_name="Entity A Operating Account",
        account_number="1000200030",
        currency_code="NGN",
        account_type_code="CURRENT",
    )
    db_session.add(account)
    await db_session.flush()
    return bank, account


def make_xlsx_bytes(headers: list, rows: list) -> bytes:
    """Builds an in-memory .xlsx file for upload-flow tests."""
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@pytest_asyncio.fixture
async def entity_a_scoped_user(db_session: AsyncSession, demo_group_and_entities) -> User:
    """A non-superuser granted Excel Data Hub + Cash & Liquidity + Banks perms, Entity A only."""
    _, entity_a, _ = demo_group_and_entities

    role = Role(name=f"entity-a-user-{uuid.uuid4().hex[:6]}", is_system_role=True)
    db_session.add(role)
    await db_session.flush()

    for module in (
        TreasuryModule.EXCEL_DATA_HUB, TreasuryModule.CASH_LIQUIDITY, TreasuryModule.BANKS_ACCOUNTS,
    ):
        for action in (
            TreasuryAction.VIEW, TreasuryAction.UPLOAD, TreasuryAction.IMPORT,
            TreasuryAction.CREATE,
        ):
            db_session.add(Permission(role_id=role.id, module=module, action=action))

    user = User(
        email=f"entitya-{uuid.uuid4().hex[:8]}@treasuryos.example.com",
        full_name="Entity A User",
        hashed_password=hash_password("Password123!"),
        is_superuser=False,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(UserRoleAssignment(
        user_id=user.id, role_id=role.id, scope_type=EntityScopeType.ENTITY,
        legal_entity_id=entity_a.id,
    ))
    await db_session.flush()
    return user


@pytest_asyncio.fixture
async def forecast_categories(db_session: AsyncSession):
    from app.models.forecast import ForecastCategory
    from app.models.lookup import CashDirection

    codes = [
        ("CUSTOMER_COLLECTIONS", CashDirection.INFLOW),
        ("SUPPLIER_PAYMENTS", CashDirection.OUTFLOW),
        ("OTHER_INFLOW", CashDirection.INFLOW),
        ("OTHER_OUTFLOW", CashDirection.OUTFLOW),
        ("INTERCOMPANY_TRANSFER", CashDirection.TRANSFER),
        ("PAYROLL", CashDirection.OUTFLOW),
    ]
    for code, direction in codes:
        if await db_session.get(ForecastCategory, code) is None:
            db_session.add(ForecastCategory(code=code, name=code.title(), type=direction))
    await db_session.flush()


@pytest_asyncio.fixture
async def facility_types(db_session: AsyncSession):
    from app.models.facility import FacilityType

    for code, name in [("TERM_LOAN", "Term Loan"), ("BANK_OVERDRAFT", "Bank Overdraft"),
                        ("REVOLVING_CREDIT", "Revolving Credit Facility")]:
        if await db_session.get(FacilityType, code) is None:
            db_session.add(FacilityType(code=code, name=name))
    await db_session.flush()


@pytest_asyncio.fixture
async def investment_types(db_session: AsyncSession, cash_event_types):
    from app.models.investment import InvestmentType

    for code, name, is_implemented in [
        ("FIXED_DEPOSIT", "Fixed Deposit", True), ("CALL_DEPOSIT", "Call Deposit", False),
        ("MONEY_MARKET", "Money Market", False),
    ]:
        if await db_session.get(InvestmentType, code) is None:
            db_session.add(InvestmentType(code=code, name=name, is_implemented=is_implemented))
    await db_session.flush()


@pytest_asyncio.fixture
async def group_wide_manager(db_session: AsyncSession) -> User:
    """A non-superuser, group-wide Treasury Manager granted all module/action perms."""
    role = Role(name=f"treasury-manager-{uuid.uuid4().hex[:6]}", is_system_role=True)
    db_session.add(role)
    await db_session.flush()

    for module in TreasuryModule:
        for action in TreasuryAction:
            db_session.add(Permission(role_id=role.id, module=module, action=action))

    user = User(
        email=f"manager-{uuid.uuid4().hex[:8]}@treasuryos.example.com",
        full_name="Test Manager",
        hashed_password=hash_password("Password123!"),
        is_superuser=False,
    )
    db_session.add(user)
    await db_session.flush()

    db_session.add(
        UserRoleAssignment(
            user_id=user.id, role_id=role.id, scope_type=EntityScopeType.GROUP_WIDE
        )
    )
    await db_session.flush()
    return user
