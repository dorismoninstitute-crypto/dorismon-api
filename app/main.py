"""Dorismon API V1.0 — academia de inglés (online/presencial/hibrida)."""
import os
import logging
import sys
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# V2.1.2: configurar logging visible en Render
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
    force=True,
)

from app.routers import auth, catalog, student, teacher, admin, certificates, placement, events, progress, calendar, messages, payments
from app.core.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Dorismon Language Institute API",
    version="1.0.0",
    description="Plataforma de gestión académica de inglés",
    lifespan=lifespan,
)

# V2.9.1: CORS robusto.
# Problema previo: allow_origins=["*"] + allow_credentials=True está PROHIBIDO por los navegadores.
# Solución: lista explícita de orígenes permitidos + regex para subdominios de Vercel.
_cors_env = os.getenv("CORS_ORIGINS", "").strip()
if _cors_env and _cors_env != "*":
    _allowed_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    # Default seguro: dominios de producción de Dorismon
    _allowed_origins = [
        "https://dorismon.com",
        "https://www.dorismon.com",
        "http://localhost:3000",
        "http://localhost:3001",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    # Permitir cualquier preview deploy de Vercel (dorismon-web-*.vercel.app)
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(student.router)
app.include_router(teacher.router)
app.include_router(admin.router)
app.include_router(certificates.router)
app.include_router(placement.router)
app.include_router(events.router)
app.include_router(progress.router)
app.include_router(calendar.router)
app.include_router(messages.router)  # V2.0
app.include_router(payments.router)  # V2.6


@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok", "service": "dorismon-api", "version": "1.0.0"}


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "Dorismon API V1.0", "docs": "/docs"}
