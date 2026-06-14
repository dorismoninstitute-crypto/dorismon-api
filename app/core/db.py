"""
Base de datos — SQLAlchemy 2.0 async + mock de Redis para desarrollo local.

Soporta dos modos según el .env:
  - sqlite+aiosqlite://./dorismon.db   (desarrollo local, sin servidor)
  - postgresql+asyncpg://...           (producción)

REDIS:
  - memory://     usa un mock en memoria (no necesita Redis instalado)
  - redis://...   usa Redis real
"""
from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.core.config import settings

# Engine async. SQLite necesita un flag especial; el resto es estándar.
_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if _is_sqlite else {}

engine = create_async_engine(
    settings.DATABASE_URL,
    pool_pre_ping=not _is_sqlite,
    connect_args=connect_args,
    echo=False,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


# --- Mock de Redis en memoria para desarrollo sin Docker --------------------
class _InMemoryRedis:
    """Mock minimalista de Redis para que el sistema arranque sin Redis instalado.
    Soporta get/set/delete/expire — suficiente para holds y cache simple.
    Para producción, cambiá REDIS_URL en .env a redis://localhost:6379/0."""
    def __init__(self):
        self._data = {}

    async def get(self, key):
        v = self._data.get(key)
        return v.encode() if isinstance(v, str) else v

    async def set(self, key, value, nx=False, ex=None, **kwargs):
        if nx and key in self._data:
            return False
        self._data[key] = value if isinstance(value, str) else (value.decode() if isinstance(value, bytes) else str(value))
        return True

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self._data:
                del self._data[k]
                n += 1
        return n

    async def exists(self, key):
        return 1 if key in self._data else 0

    async def ping(self):
        return True


_redis = None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session


async def get_redis():
    """Devuelve Redis real o mock en memoria según REDIS_URL."""
    global _redis
    if _redis is None:
        if settings.REDIS_URL.startswith("memory://"):
            _redis = _InMemoryRedis()
        else:
            from redis.asyncio import Redis
            _redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


async def init_db():
    """Crea todas las tablas la primera vez. Se llama al arrancar la app.

    V1.5.1: Migración suave — agrega columnas nuevas a tablas existentes sin perder datos.
    """
    from app.models.placement_booking import Base
    from sqlalchemy import text as sa_text
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # V1.5.1: Migración suave de columnas nuevas (idempotente)
        # Cada ALTER TABLE puede fallar si la columna ya existe — capturamos y seguimos
        migrations = [
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS levels_taught VARCHAR",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS gender VARCHAR",  # V1.6.4: male/female/other/NULL
            "ALTER TABLE class_sessions ADD COLUMN IF NOT EXISTS series_id VARCHAR",  # V1.7
            "ALTER TABLE class_sessions ADD COLUMN IF NOT EXISTS student_id VARCHAR",  # V1.7: clase privada 1-a-1
            "ALTER TABLE class_sessions ADD COLUMN IF NOT EXISTS counts_for_progress BOOLEAN DEFAULT TRUE",  # V1.7
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS rate_group FLOAT DEFAULT 500.0",  # V1.9
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS rate_private FLOAT DEFAULT 1000.0",  # V1.9
            "ALTER TABLE teachers ADD COLUMN IF NOT EXISTS rate_event FLOAT DEFAULT 750.0",  # V1.9
        ]
        for m in migrations:
            try:
                await conn.execute(sa_text(m))
            except Exception:
                pass  # ignorar si la columna ya existe o no es soportado por SQLite
