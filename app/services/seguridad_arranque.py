"""v3.9.76 (hotfix de auditoría) — Aviso de cuentas del seed de desarrollo en PRODUCCIÓN.

El seed histórico publicó contraseñas de desarrollo. Este módulo YA NO conserva ninguna contraseña conocida
(no puede verificarlas): solo avisa de que existen cuentas con los correos del seed, para que se verifique y
se rote/desactive MANUALMENTE (ver PENDIENTES_DEPLOY.md). No cambia nada, no bloquea cuentas y nunca lanza.
"""
import logging

log = logging.getLogger(__name__)

# Correos que sembraba el seed de desarrollo (sin contraseñas).
CORREOS_DEL_SEED = (
    "admin@dorismon.do", "ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do",
    "maria.estudiante@dorismon.do", "carlos.estudiante@dorismon.do", "juana.estudiante@dorismon.do",
)


async def cuentas_del_seed_existentes(db) -> list[str]:
    """Correos del seed que existen y están activos en esta base (solo lectura)."""
    from sqlalchemy import select
    from app.models import User
    filas = (await db.execute(select(User.email).where(User.email.in_(CORREOS_DEL_SEED), User.is_active.is_(True)))).scalars().all()
    return sorted(filas)


async def auditar_cuentas_del_seed():
    try:
        from app.core.db import SessionLocal
        async with SessionLocal() as db:
            presentes = await cuentas_del_seed_existentes(db)
        if presentes:
            log.critical("SEGURIDAD: existen %d cuenta(s) activas con correo del seed de desarrollo: %s. Verifica que "
                         "no conserven una contraseña histórica; rota la contraseña o desactiva la cuenta "
                         "(ver PENDIENTES_DEPLOY.md).", len(presentes), ", ".join(presentes))
        else:
            log.info("Seguridad: no hay cuentas activas con correos del seed de desarrollo.")
    except Exception as e:   # nunca impide arrancar
        log.warning("No se pudo auditar las cuentas del seed: %s", type(e).__name__)
