"""
Seeds the default InvestmentType lookup (SECTION 3) - extensible via the
API afterward, never a fixed enum. Only FIXED_DEPOSIT is functionally
implemented in Stage 4 (is_implemented=True); the others are seeded so
the architecture is visibly extensible without faking functionality for
instruments that don't exist yet (SECTION 3/45).

Usage: python -m app.db.seed_investment_types
"""
import asyncio

from app.db.session import AsyncSessionLocal
from app.models.investment import InvestmentType

INVESTMENT_TYPES = [
    ("FIXED_DEPOSIT", "Fixed Deposit", True),
    ("CALL_DEPOSIT", "Call Deposit", False),
    ("MONEY_MARKET", "Money Market", False),
    ("TREASURY_BILL", "Treasury Bill", False),
    ("BOND", "Bond", False),
    ("COMMERCIAL_PAPER", "Commercial Paper", False),
    ("OTHER", "Other", False),
]


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        for code, name, is_implemented in INVESTMENT_TYPES:
            existing = await db.get(InvestmentType, code)
            if existing is None:
                db.add(InvestmentType(code=code, name=name, is_implemented=is_implemented))
        await db.commit()
    print("Investment types seeded.")


if __name__ == "__main__":
    asyncio.run(seed())
