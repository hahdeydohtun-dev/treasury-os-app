from fastapi import APIRouter

from app.api.v1 import (
    audit,
    auth,
    bank_balances,
    banking,
    cash_position,
    currencies,
    entities,
    excel,
    facilities,
    forecast,
    forecast_inputs,
    funding,
    investment_reports,
    investments,
    transactions,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(entities.router)
api_router.include_router(currencies.router)
api_router.include_router(audit.router)
api_router.include_router(banking.router)
api_router.include_router(bank_balances.router)
api_router.include_router(transactions.router)
api_router.include_router(forecast_inputs.router)
api_router.include_router(cash_position.router)
api_router.include_router(excel.router)
api_router.include_router(forecast.router)
api_router.include_router(facilities.router)
api_router.include_router(funding.router)
api_router.include_router(investments.router)
api_router.include_router(investment_reports.router)
