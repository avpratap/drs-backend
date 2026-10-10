from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.health import router as health_router
from app.api.v1.router import api_router
from app.core.config import settings

app = FastAPI(title="DRS API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)  # /healthz, /readyz
app.include_router(api_router)  # /api/v1/...


@app.get("/")
async def root():
    return {"app": "DRS API", "env": settings.ENV, "docs": "/docs"}
