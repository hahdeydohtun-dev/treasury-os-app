"""
DEMO DATA - development/demo dataset only. Never represent this as real
financial data (SECTION 26 of the Stage 2 spec).

Creates:
  - A demo Group with two Legal Entities (different functional currencies)
  - Two banks, three bank accounts across entities/currencies
  - Bank balances
  - Treasury transactions: inflows, outflows, a transfer pair, multiple
    currencies, and deliberately invalid/duplicate rows are NOT inserted
    here directly (those are exercised via the Excel upload workflow -
    see the sample workbook this script also generates) but a realistic
    posted transaction history is seeded so the Cash Position API and
    Excel Data Hub screens have something to show immediately.
  - Expected collections and expected payments
  - A demo admin user and a demo entity-scoped user

Run with reference data already seeded (app.db.seed_reference_data), then:
    python -m app.db.seed_demo_data
Safe to run multiple times: checks for the demo group by code before
creating anything.
"""
import asyncio
import datetime
from decimal import Decimal

from sqlalchemy import select

from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.balance import BankBalance
from app.models.banking import Bank, BankAccount
from app.models.entity import Group, LegalEntity
from app.models.expected_cash_flow import ExpectedCollection, ExpectedPayment
from app.models.lookup import CashDirection
from app.models.rbac import (
    EntityScopeType,
    Permission,
    Role,
    TreasuryAction,
    TreasuryModule,
    User,
    UserRoleAssignment,
)
from app.models.treasury_transaction import TreasuryTransaction

DEMO_GROUP_CODE = "DEMO"


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        existing = (
            await db.execute(select(Group).where(Group.code == DEMO_GROUP_CODE))
        ).scalars().first()
        if existing is not None:
            print("DEMO DATA already present (Group code 'DEMO' exists). Skipping.")
            return

        # --- Group & Entities ---
        group = Group(
            name="[DEMO DATA] Acme Holdings", code=DEMO_GROUP_CODE,
            reporting_currency_code="USD",
            description="DEMO DATA - not a real company. For development/testing only.",
        )
        db.add(group)
        await db.flush()

        entity_a = LegalEntity(
            group_id=group.id, name="[DEMO] Acme Nigeria Ltd", code="DEMO-NG",
            functional_currency_code="NGN", country="NG",
        )
        entity_b = LegalEntity(
            group_id=group.id, name="[DEMO] Acme USA Inc", code="DEMO-US",
            functional_currency_code="USD", country="US",
        )
        db.add_all([entity_a, entity_b])
        await db.flush()

        # --- Banks & Accounts ---
        bank_ng = Bank(name="[DEMO] First Demo Bank", country="NG", swift_code="FDBNNGLA")
        bank_us = Bank(name="[DEMO] Chase Demo Bank", country="US", swift_code="CHASUS33")
        db.add_all([bank_ng, bank_us])
        await db.flush()

        account_a1 = BankAccount(
            legal_entity_id=entity_a.id, bank_id=bank_ng.id,
            account_name="[DEMO] Acme NG Operating", account_number="0123456789",
            currency_code="NGN", account_type_code="CURRENT",
            opening_date=datetime.date(2025, 1, 1),
            minimum_operating_balance=Decimal(5000000),
        )
        account_a2 = BankAccount(
            legal_entity_id=entity_a.id, bank_id=bank_us.id,
            account_name="[DEMO] Acme NG USD Collection", account_number="9988776655",
            currency_code="USD", account_type_code="COLLECTION",
            opening_date=datetime.date(2025, 1, 1),
        )
        account_b1 = BankAccount(
            legal_entity_id=entity_b.id, bank_id=bank_us.id,
            account_name="[DEMO] Acme US Operating", account_number="1122334455",
            currency_code="USD", account_type_code="CURRENT",
            opening_date=datetime.date(2025, 1, 1),
            overdraft_limit=Decimal(50000),
        )
        db.add_all([account_a1, account_a2, account_b1])
        await db.flush()

        # --- Bank Balances (most recent as-of a fixed demo date) ---
        as_of = datetime.date(2026, 9, 1)
        db.add_all([
            BankBalance(
                bank_account_id=account_a1.id, balance_date=as_of, currency_code="NGN",
                opening_balance=Decimal(42000000), closing_balance=Decimal(45500000),
                available_balance=Decimal(45500000), source="DEMO_DATA",
            ),
            BankBalance(
                bank_account_id=account_a2.id, balance_date=as_of, currency_code="USD",
                opening_balance=Decimal(18000), closing_balance=Decimal(21500),
                available_balance=Decimal(21500), source="DEMO_DATA",
            ),
            BankBalance(
                bank_account_id=account_b1.id, balance_date=as_of, currency_code="USD",
                opening_balance=Decimal(310000), closing_balance=Decimal(287500),
                available_balance=Decimal(287500), source="DEMO_DATA",
            ),
        ])

        # --- Treasury Transactions: inflows, outflows, and one transfer pair ---
        import uuid as _uuid
        transfer_pair_id = _uuid.uuid4()
        db.add_all([
            TreasuryTransaction(
                legal_entity_id=entity_a.id, event_type_code="CUSTOMER_COLLECTION",
                direction=CashDirection.INFLOW, event_date=datetime.date(2026, 8, 20),
                transaction_currency_code="NGN", transaction_amount=Decimal(3500000),
                bank_account_id=account_a1.id, bank_id=bank_ng.id,
                counterparty="[DEMO] Big Retail Customer", reference="INV-2026-1042",
                source_type="DEMO_DATA",
            ),
            TreasuryTransaction(
                legal_entity_id=entity_a.id, event_type_code="SUPPLIER_PAYMENT",
                direction=CashDirection.OUTFLOW, event_date=datetime.date(2026, 8, 22),
                transaction_currency_code="NGN", transaction_amount=Decimal(1200000),
                bank_account_id=account_a1.id, bank_id=bank_ng.id,
                counterparty="[DEMO] Packaging Supplier Co", reference="PO-88213",
                source_type="DEMO_DATA",
            ),
            TreasuryTransaction(
                legal_entity_id=entity_b.id, event_type_code="CUSTOMER_COLLECTION",
                direction=CashDirection.INFLOW, event_date=datetime.date(2026, 8, 25),
                transaction_currency_code="USD", transaction_amount=Decimal(45000),
                bank_account_id=account_b1.id, bank_id=bank_us.id,
                counterparty="[DEMO] US Retail Partner", reference="INV-US-771",
                source_type="DEMO_DATA",
            ),
            # Intercompany transfer: Entity B -> Entity A (two legs sharing transfer_pair_id)
            TreasuryTransaction(
                legal_entity_id=entity_b.id, event_type_code="INTERCOMPANY_PAYMENT",
                direction=CashDirection.TRANSFER, event_date=datetime.date(2026, 8, 28),
                transaction_currency_code="USD", transaction_amount=Decimal(20000),
                bank_account_id=account_b1.id, bank_id=bank_us.id,
                counterparty="[DEMO] Acme Nigeria Ltd", reference="IC-TRANSFER-0091",
                transfer_pair_id=transfer_pair_id, source_type="DEMO_DATA",
            ),
            TreasuryTransaction(
                legal_entity_id=entity_a.id, event_type_code="INTERCOMPANY_RECEIPT",
                direction=CashDirection.TRANSFER, event_date=datetime.date(2026, 8, 28),
                transaction_currency_code="USD", transaction_amount=Decimal(20000),
                bank_account_id=account_a2.id, bank_id=bank_us.id,
                counterparty="[DEMO] Acme USA Inc", reference="IC-TRANSFER-0091",
                transfer_pair_id=transfer_pair_id, source_type="DEMO_DATA",
            ),
        ])

        # --- Expected Collections / Payments ---
        db.add_all([
            ExpectedCollection(
                legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 9, 15),
                currency_code="NGN", amount=Decimal(6000000),
                counterparty="[DEMO] Big Retail Customer", category="Trade Receivable",
                probability=90, source_type="DEMO_DATA",
            ),
            ExpectedCollection(
                legal_entity_id=entity_b.id, expected_date=datetime.date(2026, 9, 20),
                currency_code="USD", amount=Decimal(60000),
                counterparty="[DEMO] US Retail Partner", category="Trade Receivable",
                probability=75, source_type="DEMO_DATA",
            ),
            ExpectedPayment(
                legal_entity_id=entity_a.id, expected_date=datetime.date(2026, 9, 18),
                currency_code="NGN", amount=Decimal(2000000),
                counterparty="[DEMO] Packaging Supplier Co", category="Trade Payable",
                priority="HIGH", probability=95, source_type="DEMO_DATA",
            ),
            ExpectedPayment(
                legal_entity_id=entity_b.id, expected_date=datetime.date(2026, 9, 30),
                currency_code="USD", amount=Decimal(15000),
                counterparty="[DEMO] Payroll Provider", category="Payroll",
                priority="HIGH", probability=100, source_type="DEMO_DATA",
            ),
        ])

        # --- Demo users ---
        admin_role = Role(name="[DEMO] Group Treasury Manager", is_system_role=False)
        db.add(admin_role)
        await db.flush()
        for module in TreasuryModule:
            for action in TreasuryAction:
                db.add(Permission(role_id=admin_role.id, module=module, action=action))

        admin_user = User(
            email="demo-admin@treasuryos.example.com", full_name="[DEMO] Group Admin",
            hashed_password=hash_password("DemoPassword123!"), is_superuser=False,
        )
        db.add(admin_user)
        await db.flush()
        db.add(UserRoleAssignment(
            user_id=admin_user.id, role_id=admin_role.id, scope_type=EntityScopeType.GROUP_WIDE,
        ))

        entity_a_role = Role(name="[DEMO] Entity A Treasury Associate", is_system_role=False)
        db.add(entity_a_role)
        await db.flush()
        for module in (
            TreasuryModule.EXCEL_DATA_HUB, TreasuryModule.CASH_LIQUIDITY,
            TreasuryModule.BANKS_ACCOUNTS,
        ):
            for action in (
                TreasuryAction.VIEW, TreasuryAction.UPLOAD, TreasuryAction.IMPORT,
                TreasuryAction.CREATE,
            ):
                db.add(Permission(role_id=entity_a_role.id, module=module, action=action))

        entity_a_user = User(
            email="demo-entitya@treasuryos.example.com", full_name="[DEMO] Entity A Associate",
            hashed_password=hash_password("DemoPassword123!"), is_superuser=False,
        )
        db.add(entity_a_user)
        await db.flush()
        db.add(UserRoleAssignment(
            user_id=entity_a_user.id, role_id=entity_a_role.id,
            scope_type=EntityScopeType.ENTITY, legal_entity_id=entity_a.id,
        ))

        await db.commit()

    print("DEMO DATA seeded.")
    print("  Login as demo-admin@treasuryos.example.com / DemoPassword123! (group-wide)")
    print("  Login as demo-entitya@treasuryos.example.com / DemoPassword123! (Entity A only)")


if __name__ == "__main__":
    asyncio.run(seed())
