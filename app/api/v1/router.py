from fastapi import APIRouter

from app.api.v1 import (
    account,
    admin,
    auth,
    billing,
    delivery,
    invoices,
    management,
    orders,
    payments,
    products,
    shops,
    subscriptions,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(shops.router)
api_router.include_router(subscriptions.router)
api_router.include_router(orders.router)
api_router.include_router(delivery.router)
api_router.include_router(billing.router)
api_router.include_router(payments.router)
api_router.include_router(admin.router)
api_router.include_router(products.router)
api_router.include_router(account.router)
api_router.include_router(invoices.router)
api_router.include_router(management.router)
