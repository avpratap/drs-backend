from fastapi import APIRouter

from app.api.v1 import (
    account,
    admin,
    ai,
    auth,
    billing,
    delivery,
    inventory,
    invoices,
    management,
    orders,
    payments,
    procurement,
    production,
    products,
    shipping,
    shops,
    subscriptions,
    transfers,
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
api_router.include_router(inventory.router)
api_router.include_router(procurement.router)
api_router.include_router(transfers.router)
api_router.include_router(production.router)
api_router.include_router(shipping.router)
api_router.include_router(ai.router)
