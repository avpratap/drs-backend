from fastapi import APIRouter

from app.api.v1 import admin, auth, delivery, products, shops, subscriptions

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(shops.router)
api_router.include_router(subscriptions.router)
api_router.include_router(delivery.router)
api_router.include_router(admin.router)
api_router.include_router(products.router)
