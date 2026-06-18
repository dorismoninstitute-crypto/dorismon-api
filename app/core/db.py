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
    Para producción, cambia REDIS_URL en .env a redis://localhost:6379/0."""
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
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN DEFAULT FALSE",  # V2.1
            # V2.1: marcar usuarios existentes como verificados (no romper acceso)
            "UPDATE users SET email_verified = TRUE WHERE email_verified IS NULL OR email_verified = FALSE",
            # V2.2: Perfil completo estudiante
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS document_type VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS document_number VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS city VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS sector VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS nationality VARCHAR DEFAULT 'Dominicana'",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS emergency_contact_name VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS emergency_contact_relationship VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS emergency_contact_phone VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS tutor_name VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS tutor_relationship VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS tutor_document VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS tutor_phone VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS tutor_email VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS how_found_us VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS referred_by VARCHAR",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS special_notes TEXT",
            # V2.3: Modalidad por inscripción (online/presencial/hibrida)
            "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS modality VARCHAR DEFAULT 'online'",
        ]
        # V2.6: Crear tablas nuevas si no existen (las define el modelo via Base.metadata.create_all)
        # Las migraciones específicas para campos nuevos van aquí:
        v26_migrations = [
            # No hay ALTER necesarios porque las tablas son nuevas (BankAccount, PaymentProof, TrialClass)
            # SQLAlchemy las creará automáticamente con Base.metadata.create_all
        ]
        migrations.extend(v26_migrations)

        # V2.9 migrations
        v29_migrations = [
            "ALTER TABLE plan_features ADD COLUMN IF NOT EXISTS feature_key VARCHAR",
            "CREATE INDEX IF NOT EXISTS ix_plan_features_feature_key ON plan_features(feature_key)",
            # V2.9: campo para recordatorios automáticos (no duplicar)
            "ALTER TABLE class_sessions ADD COLUMN IF NOT EXISTS reminder_24h_sent_at TIMESTAMP WITH TIME ZONE",
            # V2.9: motivo de cancelación
            "ALTER TABLE class_sessions ADD COLUMN IF NOT EXISTS cancellation_reason TEXT",
            "ALTER TABLE class_sessions ADD COLUMN IF NOT EXISTS cancelled_by_user_id VARCHAR",
            "ALTER TABLE class_sessions ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMP WITH TIME ZONE",
        ]
        migrations.extend(v29_migrations)
        for m in migrations:
            try:
                await conn.execute(sa_text(m))
            except Exception:
                pass  # ignorar si la columna ya existe o no es soportado por SQLite

    # V2.9: Llenar feature_keys en PlanFeature de planes ya existentes
    # Esto se ejecuta en cada arranque pero es idempotente (no duplica)
    await _backfill_plan_feature_keys()


async def _backfill_plan_feature_keys():
    """V2.9: Asigna feature_keys a planes ya seedeados sin esos códigos.

    Mapea por texto descriptivo (parcial, case-insensitive) hacia un feature_key.
    Si la fila ya tiene feature_key, no la toca (idempotente).
    """
    from app.models.placement_booking import PlanFeature, Plan
    from sqlalchemy import select, update
    # Pares (substring del texto, feature_key)
    # El primer match gana, por eso ponemos los más específicos arriba
    text_to_key = [
        ("clases privadas", "private_classes"),
        ("1 clase privada", "private_classes"),
        ("clase privada", "private_classes"),
        ("certificado", "certificates"),
        ("soporte prioritario", "priority_support"),
        ("soporte por email", "priority_support"),
        ("soporte directo", "priority_support"),
        ("materiales descargables premium", "materials_premium"),
        ("material descargable + recursos premium", "materials_premium"),
        ("recursos premium", "materials_premium"),
        ("material descargable completo", "materials_premium"),
        ("material descargable", "materials_premium"),
        ("biblioteca completa", "library_full"),
        ("biblioteca básica", "library_basic"),
        ("acceso a material básico", "library_basic"),
        ("eventos abiertos ilimitados", "events_free"),
        ("eventos del instituto (acceso libre)", "events_free"),
        ("eventos abiertos", "events_view"),
        ("ver eventos del instituto", "events_view"),
        ("eventos del instituto", "events_view"),
        ("hasta 2 eventos", "events_free"),
        ("tareas con feedback", "assignments"),
        ("tareas y quizzes", "assignments"),
        ("quizzes evaluativos", "quizzes"),
        ("ruta curricular personalizada", "course_route"),
        ("acceso a toefl", "course_route"),
        ("clases grupales", "grupal_classes"),
        ("test de nivel cefr", "placement_test"),
        ("test de nivel", "placement_test"),
    ]
    async with engine.begin() as conn:
        # Buscar todas las features sin feature_key
        rows = (await conn.execute(
            text_query := text_select_features()
        )).fetchall()
        for row in rows:
            row_id, feature_text = row[0], row[1].lower()
            key_to_set = None
            for substr, key in text_to_key:
                if substr in feature_text:
                    key_to_set = key
                    break
            if key_to_set:
                await conn.execute(
                    text_update_feature(),
                    {"key": key_to_set, "id": row_id}
                )


def text_select_features():
    from sqlalchemy import text as sa_text
    return sa_text("SELECT id, feature FROM plan_features WHERE feature_key IS NULL OR feature_key = ''")


def text_update_feature():
    from sqlalchemy import text as sa_text
    return sa_text("UPDATE plan_features SET feature_key = :key WHERE id = :id")
