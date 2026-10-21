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


def is_production() -> bool:
    """¿Está corriendo en producción? Falla CERRADO: Render siempre define RENDER=true y eso
    no se puede anular con ENVIRONMENT; fuera de Render, ENVIRONMENT=production/prod lo activa.
    Local y tests: no definen ninguna de las dos."""
    if os.getenv("RENDER", "").strip().lower() in {"1", "true", "yes"}:
        return True
    return (os.getenv("ENVIRONMENT") or os.getenv("APP_ENV") or "").strip().lower() in {"production", "prod"}


_SECRET_KEY_FALLBACK = "cambiar-en-produccion-usar-secreto-largo-y-aleatorio"
_SECRET_KEY_MIN_LEN = 32


def secret_key_valida(valor: str) -> bool:
    """Una SECRET_KEY de producción no puede ser el valor de ejemplo/fallback conocido ni corta."""
    v = (valor or "").strip()
    return len(v) >= _SECRET_KEY_MIN_LEN and "cambiar" not in v.lower() and v != _SECRET_KEY_FALLBACK


def exigir_configuracion_segura(secret_key: str, produccion: bool | None = None) -> None:
    """En producción NO se arranca con el fallback conocido: se corta ANTES de firmar un solo JWT.
    (No cambia ni lee el secreto real: solo valida el valor que ya está configurado.)"""
    if (is_production() if produccion is None else produccion) and not secret_key_valida(secret_key):
        raise RuntimeError(
            "SECRET_KEY ausente o insegura en producción: define una clave aleatoria de al menos "
            f"{_SECRET_KEY_MIN_LEN} caracteres (no el valor de ejemplo). El servicio no arranca."
        )


class Settings(BaseSettings):
    # Base de datos. En Render, esta variable la inyecta el servicio de Postgres.
    # Localmente, puedes usar SQLite: sqlite+aiosqlite:///./dorismon.db
    DATABASE_URL: str = "sqlite+aiosqlite:///./dorismon.db"
    REDIS_URL: str = "memory://"

    # Seguridad JWT
    SECRET_KEY: str = _SECRET_KEY_FALLBACK   # solo desarrollo/tests: en producción se rechaza (ver abajo)
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
exigir_configuracion_segura(settings.SECRET_KEY)   # producción: falla cerrado si falta una clave válida
# Normalización aplicada DESPUÉS de cargar las variables
settings.DATABASE_URL = _normalize_db_url(settings.DATABASE_URL)
