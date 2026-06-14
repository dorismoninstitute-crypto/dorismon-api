"""Configuración central — variables de entorno con Pydantic Settings.

CONVERSIÓN AUTOMÁTICA DE DATABASE_URL:
  Render (y Heroku) proveen DATABASE_URL=postgresql://...
  SQLAlchemy async necesita     postgresql+asyncpg://...
  Hacemos la conversión automática para evitar el bug típico del primer
  despliegue.
"""
import os
from pydantic_settings import BaseSettings


def _normalize_db_url(url: str) -> str:
    """Convierte postgres:// y postgresql:// al formato asyncpg si hace falta."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


class Settings(BaseSettings):
    # Base de datos. En Render, esta variable la inyecta el servicio de Postgres.
    # Localmente, puedes usar SQLite: sqlite+aiosqlite:///./dorismon.db
    DATABASE_URL: str = "sqlite+aiosqlite:///./dorismon.db"
    REDIS_URL: str = "memory://"

    # Seguridad JWT
    SECRET_KEY: str = "cambiar-en-produccion-usar-secreto-largo-y-aleatorio"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Integraciones
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_NUMBER: str = "+14155238886"
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""

    # Cloudflare R2
    R2_ACCOUNT_ID: str = ""
    R2_ACCESS_KEY: str = ""
    R2_SECRET_KEY: str = ""
    R2_BUCKET: str = "dorismon"

    # Reservas
    BOOKING_HOLD_MINUTES: int = 5

    # Zona horaria
    DEFAULT_TIMEZONE: str = "America/Santo_Domingo"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
# Normalización aplicada DESPUÉS de cargar las variables
settings.DATABASE_URL = _normalize_db_url(settings.DATABASE_URL)
