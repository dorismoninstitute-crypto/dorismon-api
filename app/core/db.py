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

        # V2.9.1 — constraint único en teacher_payments (evita doble pago)
        # Primero elimina duplicados existentes (deja el más reciente por período),
        # luego crea el índice único. Idempotente.
        v291_migrations = [
            # Borrar duplicados: mantener el de paid_at más reciente por (teacher, año, mes)
            """
            DELETE FROM teacher_payments tp
            USING teacher_payments tp2
            WHERE tp.teacher_id = tp2.teacher_id
              AND tp.period_year = tp2.period_year
              AND tp.period_month = tp2.period_month
              AND tp.paid_at < tp2.paid_at
            """,
            # Crear índice único (idempotente con IF NOT EXISTS)
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_teacher_payment_period
            ON teacher_payments(teacher_id, period_year, period_month)
            """,
        ]
        migrations.extend(v291_migrations)

        # V3.0.2 — campos de reagenda en trial_classes
        v302_migrations = [
            "ALTER TABLE trial_classes ADD COLUMN IF NOT EXISTS reschedule_count INTEGER DEFAULT 0",
            "ALTER TABLE trial_classes ADD COLUMN IF NOT EXISTS reschedule_requested BOOLEAN DEFAULT FALSE",
            # V3.9: Archivado de perfil de estudiante (al convertir a profesor)
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS archived BOOLEAN DEFAULT FALSE",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP WITH TIME ZONE",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS archived_reason VARCHAR",
        ]
        migrations.extend(v302_migrations)

        # V3.9.26 — dónde ocurre el video de cada clase.
        # Las clases que ya existen quedan en "meet" (su enlace de siempre),
        # así nada cambia para las clases ya agendadas.
        # V3.9.27: la tabla video_presence la crea sola Base.metadata.create_all
        # V3.9.45 — Audiencia por GRUPO en tareas y quizzes.
        # ADITIVA: las filas existentes quedan con series_id NULL y se
        # comportan exactamente igual que antes (todos los del profesor en
        # ese nivel). Ninguna tarea ni quiz existente cambia de audiencia.
        # V3.9.46 P1 — Audiencia de materiales.
        # ADITIVA Y CONSERVADORA: todos los materiales existentes quedan como
        # "institutional", que es como se comportaban. No se les inventa una
        # audiencia que nadie definió.
        # V3.9.49 P2 — Seguimiento de tareas.
        # ADITIVA: las entregas existentes quedan con NULL, que significa
        # "no consta que la viera". No se inventa historial que no ocurrió.
        # ── V3.9.53 P3 — Progresión académica ──
        # TODO ADITIVO. Las matrículas existentes quedan con
        # academic_status='active', que es exactamente lo que son hoy.
        # Los campos viejos Student.speaking_score etc. NO se tocan.
        # ── V3.9.55 — Progreso de lecciones por matrícula ──
        # ADITIVA. Los registros existentes quedan con enrollment_id NULL y
        # se siguen contando como progreso del estudiante, igual que antes.
        # No se les asigna matrícula a la fuerza: no se puede saber de cuál
        # eran si el estudiante repitió el nivel.
        # ══ V3.9.56 — PROGRESO POR MATRÍCULA ══
        #
        # Estas migraciones son CRÍTICAS: sin ellas un estudiante que repite
        # un nivel heredaría el progreso anterior. Por eso van con control de
        # errores explícito más abajo (`criticas`), no en silencio.
        v3956_migrations = [
            "ALTER TABLE module_progress ADD COLUMN IF NOT EXISTS enrollment_id VARCHAR",
            # La unicidad pasa a ser por matrícula. Se elimina la vieja solo
            # si existe; los registros legacy (enrollment_id NULL) quedan
            # protegidos por el índice parcial siguiente.
            "ALTER TABLE lesson_progress DROP CONSTRAINT IF EXISTS lesson_progress_student_id_lesson_id_key",
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_lesson_progress_enrollment
               ON lesson_progress(enrollment_id, lesson_id)
               WHERE enrollment_id IS NOT NULL""",
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_lesson_progress_legacy
               ON lesson_progress(student_id, lesson_id)
               WHERE enrollment_id IS NULL""",
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_module_progress_enrollment
               ON module_progress(enrollment_id, module_id)
               WHERE enrollment_id IS NOT NULL""",
        ]
        # Se añade después de v3955: sus índices dependen de lesson_progress.enrollment_id.

        v3955_migrations = [
            "ALTER TABLE lesson_progress ADD COLUMN IF NOT EXISTS enrollment_id VARCHAR",
        ]
        migrations.extend(v3955_migrations)
        # IMPORTANTE: v3956 usa lesson_progress.enrollment_id; por eso va después de v3955.
        migrations.extend(v3956_migrations)

        v3953_migrations = [
            "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS academic_status VARCHAR DEFAULT 'active'",
            "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
            "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS approved_by VARCHAR",
            "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS final_result VARCHAR",
            "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS final_score NUMERIC(5,2)",
            "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS completion_snapshot TEXT",
            "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS withdrawn_at TIMESTAMPTZ",
            "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS withdrawn_reason VARCHAR",
            "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS withdrawn_by VARCHAR",
            "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS previous_enrollment_id VARCHAR",
            "ALTER TABLE certificates ADD COLUMN IF NOT EXISTS enrollment_id VARCHAR",
            "UPDATE enrollments SET academic_status = 'active' WHERE academic_status IS NULL",
        ]
        migrations.extend(v3953_migrations)

        v3949_migrations = [
            "ALTER TABLE assignment_submissions ADD COLUMN IF NOT EXISTS viewed_at TIMESTAMPTZ",
            "ALTER TABLE assignment_submissions ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
        ]
        migrations.extend(v3949_migrations)

        v3946_migrations = [
            "ALTER TABLE materials ADD COLUMN IF NOT EXISTS audience_kind VARCHAR DEFAULT 'institutional'",
            "ALTER TABLE materials ADD COLUMN IF NOT EXISTS series_id VARCHAR",
            "ALTER TABLE materials ADD COLUMN IF NOT EXISTS student_id VARCHAR",
            "UPDATE materials SET audience_kind = 'institutional' WHERE audience_kind IS NULL",
        ]
        migrations.extend(v3946_migrations)

        v3945_migrations = [
            "ALTER TABLE assignments ADD COLUMN IF NOT EXISTS series_id VARCHAR",
            "ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS series_id VARCHAR",
        ]
        migrations.extend(v3945_migrations)

        v3943_migrations = [
            "ALTER TABLE class_sessions ADD COLUMN IF NOT EXISTS scheduled_teacher_id VARCHAR",
            "ALTER TABLE class_sessions ADD COLUMN IF NOT EXISTS cancel_reason VARCHAR",
            "ALTER TABLE class_sessions ADD COLUMN IF NOT EXISTS teacher_absent_alert_at TIMESTAMPTZ",
            # Las clases que ya existen conservan su profesor como el programado
            "UPDATE class_sessions SET scheduled_teacher_id = teacher_id WHERE scheduled_teacher_id IS NULL",
        ]
        migrations.extend(v3943_migrations)

        v3937_migrations = [
            "ALTER TABLE makeup_requests ADD COLUMN IF NOT EXISTS created_by VARCHAR DEFAULT 'student'",
            "ALTER TABLE makeup_requests ADD COLUMN IF NOT EXISTS counts_for_progress BOOLEAN DEFAULT FALSE",
            "ALTER TABLE makeup_requests ALTER COLUMN original_session_id DROP NOT NULL",
        ]
        migrations.extend(v3937_migrations)

        v3933_migrations = [
            "ALTER TABLE assignments ADD COLUMN IF NOT EXISTS kind VARCHAR DEFAULT 'written'",
            "ALTER TABLE assignments ADD COLUMN IF NOT EXISTS media_url VARCHAR",
            "ALTER TABLE assignments ADD COLUMN IF NOT EXISTS blanks_json TEXT",
            "UPDATE assignments SET kind = 'written' WHERE kind IS NULL",
            "ALTER TABLE enrollments ADD COLUMN IF NOT EXISTS series_id VARCHAR",
        ]
        migrations.extend(v3933_migrations)

        v3932_migrations = [
            "ALTER TABLE class_sessions ADD COLUMN IF NOT EXISTS reminder_30m_sent_at TIMESTAMPTZ",
        ]
        migrations.extend(v3932_migrations)

        v3928_migrations = [
            "ALTER TABLE certificates ADD COLUMN IF NOT EXISTS revoked_reason VARCHAR",
            "ALTER TABLE certificates ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ",
        ]
        migrations.extend(v3928_migrations)

        v3926_migrations = [
            "ALTER TABLE class_sessions ADD COLUMN IF NOT EXISTS video_provider VARCHAR DEFAULT 'meet'",
            "UPDATE class_sessions SET video_provider = 'meet' WHERE video_provider IS NULL",
        ]
        migrations.extend(v3926_migrations)

        # ⚠️ V3.9.57 — ESTE BLOQUE VA AL FINAL A PROPÓSITO.
        #
        # Las migraciones se aplican en el orden de esta lista, y los bloques
        # se van añadiendo del más nuevo al más viejo. Los índices sobre
        # `enrollment_id` necesitan que la columna YA exista, y esa la añade
        # v3.9.56 más abajo en el archivo. Estando arriba, se ejecutaban
        # primero y fallaban con "no such column".

        # ══ V3.9.57 — Eliminar de VERDAD las constraints viejas ══
        #
        # En v3.9.56 se creaban los índices modernos, pero la constraint
        # antigua seguía existiendo y bloqueaba igual. Y su nombre se daba por
        # supuesto: si PostgreSQL lo generó distinto, el DROP no hacía nada.
        #
        # Este bloque BUSCA las constraints reales que impiden dos filas del
        # mismo módulo/lección para dos matrículas, y las elimina por su
        # nombre real. Sin adivinar.
        v3957_migrations = [
            """
            DO $$
            DECLARE r RECORD;
            BEGIN
              -- LessonProgress: cualquier unicidad sobre (student_id, lesson_id)
              FOR r IN
                SELECT c.conname
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = 'lesson_progress'
                  AND c.contype = 'u'
                  AND (SELECT array_agg(a.attname ORDER BY a.attname)
                       FROM unnest(c.conkey) k
                       JOIN pg_attribute a
                         ON a.attrelid = c.conrelid AND a.attnum = k)
                      = ARRAY['lesson_id','student_id']
              LOOP
                EXECUTE format('ALTER TABLE lesson_progress DROP CONSTRAINT %I',
                               r.conname);
              END LOOP;

              -- ModuleProgress: cualquier unicidad sobre (student_id, module_id)
              FOR r IN
                SELECT c.conname
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                WHERE t.relname = 'module_progress'
                  AND c.contype = 'u'
                  AND (SELECT array_agg(a.attname ORDER BY a.attname)
                       FROM unnest(c.conkey) k
                       JOIN pg_attribute a
                         ON a.attrelid = c.conrelid AND a.attnum = k)
                      = ARRAY['module_id','student_id']
              LOOP
                EXECUTE format('ALTER TABLE module_progress DROP CONSTRAINT %I',
                               r.conname);
              END LOOP;

              -- Y los índices únicos equivalentes que no sean constraint
              FOR r IN
                SELECT i.relname AS iname
                FROM pg_index x
                JOIN pg_class i ON i.oid = x.indexrelid
                JOIN pg_class t ON t.oid = x.indrelid
                WHERE t.relname IN ('lesson_progress','module_progress')
                  AND x.indisunique
                  AND x.indpred IS NULL
                  AND i.relname NOT LIKE 'uq_%_enrollment'
                  AND i.relname NOT LIKE '%_pkey'
              LOOP
                EXECUTE format('DROP INDEX IF EXISTS %I', r.iname);
              END LOOP;
            END $$;
            """,
            # Unicidad moderna: por matrícula
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_module_progress_enrollment
               ON module_progress(enrollment_id, module_id)
               WHERE enrollment_id IS NOT NULL""",
            # Y los legacy siguen únicos entre sí
            """CREATE UNIQUE INDEX IF NOT EXISTS uq_module_progress_legacy
               ON module_progress(student_id, module_id)
               WHERE enrollment_id IS NULL""",
        ]
        migrations.extend(v3957_migrations)

        # ══ V3.9.56 — LAS MIGRACIONES YA NO FALLAN EN SILENCIO ══
        #
        # Antes cualquier error se tragaba con `except: pass`. Eso está bien
        # para un `ADD COLUMN IF NOT EXISTS` que ya se aplicó, pero significaba
        # que producción podía arrancar con el esquema incompleto y nadie se
        # enteraba hasta que algo fallaba en uso real.
        #
        # Ahora:
        #   · todo error se registra en el log, con la sentencia
        #   · las migraciones CRÍTICAS (las que sostienen el aislamiento por
        #     matrícula) detienen el arranque si no se aplican
        #
        # SQLite no soporta parte de esta sintaxis (índices parciales sí,
        # DROP CONSTRAINT no), así que en desarrollo se avisa sin romper.
        import logging as _logging
        import re as _re
        _log = _logging.getLogger("dorismon.migrations")

        # ── V3.9.57 — Compatibilidad con SQLite ──
        #
        # SQLite no entiende `ADD COLUMN IF NOT EXISTS`. Antes esas
        # migraciones se omitían con un aviso, así que en desarrollo el
        # esquema quedaba incompleto y el escenario de UPGRADE no se podía
        # verificar: solo se probaba con bases nuevas.
        #
        # Ahora, en SQLite se comprueba si la columna existe y se ejecuta el
        # ALTER sin el IF NOT EXISTS. En PostgreSQL no cambia nada.
        _es_postgres = conn.engine.dialect.name == "postgresql"

        async def _columna_existe(tabla: str, columna: str) -> bool:
            try:
                filas = await conn.exec_driver_sql(f"PRAGMA table_info({tabla})")
                return any(r[1] == columna for r in filas.fetchall())
            except Exception:
                return True  # ante la duda, no se toca

        _re_add = _re.compile(
            r"ALTER TABLE (\w+) ADD COLUMN IF NOT EXISTS (\w+)(.*)",
            _re.IGNORECASE | _re.DOTALL)

        # Sin estas, un estudiante que repite nivel heredaría su progreso
        # V3.9.57 — Las SEIS operaciones que sostienen la semántica por
        # matrícula. Si falta cualquiera, repetir un nivel se rompe: o se
        # hereda el progreso anterior, o el insert falla. Mejor no arrancar.
        _criticas = (
            "module_progress ADD COLUMN IF NOT EXISTS enrollment_id",
            "lesson_progress ADD COLUMN IF NOT EXISTS enrollment_id",
            "enrollments ADD COLUMN IF NOT EXISTS academic_status",
            # Quitar las unicidades viejas (bloque DO $$)
            "DROP CONSTRAINT %I",
            # Crear las modernas
            "uq_lesson_progress_enrollment",
            "uq_module_progress_enrollment",
        )
        _fallos_criticos = []

        for m in migrations:
            _sql = m
            # En SQLite: traducir el ADD COLUMN IF NOT EXISTS
            if not _es_postgres:
                _mm = _re_add.match(" ".join(m.split()))
                if _mm:
                    _tabla, _col, _resto = _mm.groups()
                    if await _columna_existe(_tabla, _col):
                        continue  # ya está: nada que hacer
                    _sql = f"ALTER TABLE {_tabla} ADD COLUMN {_col}{_resto}"

            _normalizada = " ".join(m.split())
            _resumen = _normalizada[:120]
            _critica = any(c in _normalizada for c in _criticas)

            try:
                # PostgreSQL deja TODA la transacción en estado abortado tras
                # una sentencia fallida. Un SAVEPOINT por migración permite
                # revertir solo esa sentencia y continuar de forma segura con
                # las no críticas. DDL de PostgreSQL es transaccional.
                if _es_postgres:
                    async with conn.begin_nested():
                        await conn.execute(sa_text(_sql))
                else:
                    await conn.execute(sa_text(_sql))
            except Exception as exc:
                _texto = str(exc).lower()
                # Estos son esperables e inofensivos: el SAVEPOINT ya dejó la
                # transacción principal utilizable.
                _ya_estaba = any(x in _texto for x in (
                    "already exists", "duplicate", "ya existe"))
                if _ya_estaba:
                    continue

                if _critica and _es_postgres:
                    # Una migración crítica no se acumula para seguir probando:
                    # detenemos el arranque con la causa original.
                    _log.exception("MIGRACIÓN CRÍTICA FALLÓ: %s", _resumen)
                    raise RuntimeError(
                        f"Migración crítica falló: {_resumen} → {exc}"
                    ) from exc
                else:
                    # En SQLite (desarrollo) algunas sentencias PostgreSQL no
                    # aplican; en PostgreSQL las no críticas quedan aisladas por
                    # SAVEPOINT y no envenenan las siguientes migraciones.
                    _log.warning("Migración omitida: %s → %s", _resumen, exc)

        # ── V3.9.57 — SQLite: recrear las tablas sin la constraint vieja ──
        #
        # SQLite no soporta DROP CONSTRAINT: la única forma de quitar un
        # UNIQUE de tabla es recrearla. Sin esto, una base de desarrollo
        # migrada mantiene el bloqueo y no se puede probar el escenario de
        # upgrade — que es justo donde vive el riesgo real.
        #
        # Se copia todo el contenido: no se pierde ni un registro.
        if not _es_postgres:
            for _tabla, _cols, _uniq in (
                ("lesson_progress",
                 "id, student_id, lesson_id, enrollment_id, is_completed, "
                 "progress_pct, last_viewed_at, completed_at",
                 "student_id, lesson_id"),
                ("module_progress",
                 "id, student_id, module_id, enrollment_id, status, "
                 "attended_count, quiz_passed, completed_at",
                 "student_id, module_id"),
            ):
                try:
                    _r = await conn.exec_driver_sql(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                        (_tabla,))
                    _ddl = (_r.fetchone() or [""])[0] or ""
                    # ¿Conserva la unicidad vieja?
                    _norm = " ".join(_ddl.split()).upper()
                    if f"UNIQUE ({_uniq.upper()})" not in _norm:
                        continue

                    _log.warning(
                        "Recreando %s para quitar la unicidad antigua (%s)",
                        _tabla, _uniq)
                    _nuevo = _ddl.replace(
                        f"UNIQUE ({_uniq})", "").replace(
                        f"UNIQUE({_uniq})", "")
                    _nuevo = _nuevo.replace(f"CREATE TABLE {_tabla}",
                                            f"CREATE TABLE {_tabla}__nuevo")
                    # Limpieza: al quitar el UNIQUE queda una coma colgante
                    # antes del paréntesis de cierre.
                    _nuevo = " ".join(_nuevo.split())
                    _nuevo = _re.sub(r",\s*,", ",", _nuevo)
                    _nuevo = _re.sub(r"\(\s*,", "(", _nuevo)
                    _nuevo = _re.sub(r",\s*\)", ")", _nuevo)

                    await conn.exec_driver_sql(_nuevo)
                    await conn.exec_driver_sql(
                        f"INSERT INTO {_tabla}__nuevo ({_cols}) "
                        f"SELECT {_cols} FROM {_tabla}")
                    await conn.exec_driver_sql(f"DROP TABLE {_tabla}")
                    await conn.exec_driver_sql(
                        f"ALTER TABLE {_tabla}__nuevo RENAME TO {_tabla}")
                    # Los índices modernos se recrean tras el rename
                    await conn.exec_driver_sql(
                        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{_tabla}_enrollment "
                        f"ON {_tabla}(enrollment_id, "
                        f"{'lesson_id' if 'lesson' in _tabla else 'module_id'}) "
                        f"WHERE enrollment_id IS NOT NULL")
                    await conn.exec_driver_sql(
                        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{_tabla}_legacy "
                        f"ON {_tabla}({_uniq}) WHERE enrollment_id IS NULL")
                except Exception as exc:
                    _log.error("No se pudo recrear %s: %s", _tabla, exc)
                    _fallos_criticos.append(f"recrear {_tabla}: {exc}")

        if _fallos_criticos:
            # Mejor no arrancar que arrancar con el esquema a medias: con el
            # aislamiento por matrícula roto, los datos académicos se mezclan.
            raise RuntimeError(
                "No se pudieron aplicar migraciones críticas: "
                + " | ".join(_fallos_criticos)
            )

    # V2.9: Llenar feature_keys en PlanFeature de planes ya existentes
    # Esto se ejecuta en cada arranque pero es idempotente (no duplica)
    await _backfill_plan_feature_keys()
    # V3.0.5: limpiar clases de prueba duplicadas (por doble click previo)
    await _cleanup_duplicate_trial_sessions()


async def _cleanup_duplicate_trial_sessions():
    """V3.0.5: Elimina ClassSessions de prueba duplicadas creadas por doble click.

    Una clase de prueba es duplicada si hay 2+ sesiones para el mismo estudiante,
    misma hora, con counts_for_progress=False. Deja solo una (la vinculada al
    TrialClass si existe, o la más antigua).
    """
    from sqlalchemy import select, func as _func
    from app.models.placement_booking import ClassSession, TrialClass
    try:
        async with engine.begin() as conn:
            # Buscar grupos de sesiones de prueba duplicadas
            rows = (await conn.execute(
                select(
                    ClassSession.student_id,
                    ClassSession.starts_at_utc,
                    _func.count(ClassSession.id).label("cnt"),
                ).where(
                    ClassSession.counts_for_progress.is_(False),
                    ClassSession.student_id.isnot(None),
                ).group_by(ClassSession.student_id, ClassSession.starts_at_utc)
                .having(_func.count(ClassSession.id) > 1)
            )).all()

            for student_id, starts_at, cnt in rows:
                # Sesiones de este grupo
                sessions = (await conn.execute(
                    select(ClassSession.id).where(
                        ClassSession.student_id == student_id,
                        ClassSession.starts_at_utc == starts_at,
                        ClassSession.counts_for_progress.is_(False),
                    ).order_by(ClassSession.id)
                )).scalars().all()
                # ¿Cuál está vinculada a un TrialClass? esa se conserva
                linked = (await conn.execute(
                    select(TrialClass.session_id).where(TrialClass.session_id.in_(sessions))
                )).scalars().all()
                keep = linked[0] if linked else sessions[0]
                to_delete = [s for s in sessions if s != keep]
                for sid in to_delete:
                    await conn.execute(
                        ClassSession.__table__.delete().where(ClassSession.id == sid)
                    )
    except Exception:
        pass  # no bloquear el arranque si algo falla


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
