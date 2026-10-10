from fastapi import APIRouter

from app.api.v1 import products

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(products.router)
