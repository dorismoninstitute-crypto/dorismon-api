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

from app.routers import auth, catalog, student, teacher, admin, certificates, placement, events, progress, calendar, messages, payments, notifications
from app.routers import video  # V3.9.26: videollamadas propias
from app.routers import push  # V3.9.29: avisos al teléfono
from app.routers import ai  # V3.9.31: generar contenido
from app.core.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # v3.9.76 (P0): en producción, avisa (log CRÍTICO, sin bloquear) si quedan contraseñas de desarrollo
    from app.core.config import is_production
    if is_production():
        import asyncio
        from app.services.seguridad_arranque import auditar_cuentas_del_seed
        asyncio.create_task(auditar_cuentas_del_seed())
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
        "https://dorismon-web.vercel.app",
    ]
    # v3.9.76 (P0): localhost solo en desarrollo; en producción NO se permite por defecto
    from app.core.config import is_production as _es_produccion
    if not _es_produccion():
        _allowed_origins += ["http://localhost:3000", "http://localhost:3001"]

# V3.9.74: los previews de Vercel NO quedan abiertos por defecto.
# Si de verdad se necesitan para QA, se habilitan de forma explícita y solo
# para previews cuyo nombre pertenece al proyecto Dorismon.
_allow_vercel_previews = os.getenv("ALLOW_VERCEL_PREVIEWS", "false").lower() in {"1", "true", "yes"}
_vercel_preview_regex = r"https://dorismon-web-[a-zA-Z0-9-]+\.vercel\.app" if _allow_vercel_previews else None

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_origin_regex=_vercel_preview_regex,
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
app.include_router(notifications.router)
app.include_router(video.router)  # V3.9.26
app.include_router(push.router)  # V3.9.29
app.include_router(ai.router)  # V3.9.31  # V3.1 — centro de avisos universal


@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok", "service": "dorismon-api", "version": "1.0.0"}


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "Dorismon API V1.0", "docs": "/docs"}
