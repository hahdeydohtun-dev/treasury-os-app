"""
Seeds the default FacilityType lookup (SECTION 2) - extensible via the API
afterward, never a fixed enum.

Usage: python -m app.db.seed_facility_types
"""
import asyncio

from app.db.session import AsyncSessionLocal
from app.models.facility import FacilityType

FACILITY_TYPES = [
    ("BANK_OVERDRAFT", "Bank Overdraft"),
    ("REVOLVING_CREDIT", "Revolving Credit Facility"),
    ("TERM_LOAN", "Term Loan"),
    ("WORKING_CAPITAL", "Working Capital Facility"),
    ("CASH_CREDIT", "Cash Credit / Working Capital Line"),
    ("RECEIVABLES_FINANCING", "Invoice / Receivables Financing"),
    ("SHORT_TERM_LOAN", "Short-Term Loan"),
    ("ASSET_BACKED", "Asset-Backed Facility"),
    ("BILATERAL", "Bilateral Facility"),
    ("BRIDGE", "Bridge Facility"),
    ("OTHER", "Other"),
]


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        for code, name in FACILITY_TYPES:
            if await db.get(FacilityType, code) is None:
                db.add(FacilityType(code=code, name=name))
        await db.commit()
    print("Facility types seeded.")


if __name__ == "__main__":
    asyncio.run(seed())
