"""Dorismon API V1.0 — academia de inglés (online/presencial/hibrida)."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import auth, catalog, student, teacher, admin, certificates, placement, events, progress, calendar, messages
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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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


@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok", "service": "dorismon-api", "version": "1.0.0"}


@app.get("/", include_in_schema=False)
async def root():
    return {"service": "Dorismon API V1.0", "docs": "/docs"}
