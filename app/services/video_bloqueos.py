"""GATE 6G-A — "Sacar de la clase" persistente durante esa sesión.

El backend es la autoridad: si hay un bloqueo activo para (clase, persona),
/join no emite token. Desconectar en LiveKit es un paso APARTE y posterior: si
LiveKit falla, el bloqueo sigue (fail-closed).

Cada cambio de estado deja constancia en audit_logs (quién, cuándo, a quién).
"""
from datetime import datetime, timezone as tz

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VideoAccessBlock
from app.services.audit import log_action

MENSAJE_RETIRADO = "El profesor te retiró de esta clase."


async def bloqueo_activo(db: AsyncSession, session_id: str, user_id: str) -> bool:
    return (await db.execute(
        select(VideoAccessBlock.id).where(
            VideoAccessBlock.session_id == session_id,
            VideoAccessBlock.user_id == user_id,
            VideoAccessBlock.active.is_(True),
        )
    )).first() is not None


async def _fila(db: AsyncSession, session_id: str, user_id: str):
    return (await db.execute(
        select(VideoAccessBlock).where(
            VideoAccessBlock.session_id == session_id,
            VideoAccessBlock.user_id == user_id,
        ).with_for_update()   # PostgreSQL: bloquea la fila; SQLite lo ignora
    )).scalar_one_or_none()


async def bloquear(db: AsyncSession, session_id: str, user_id: str, actor_id: str,
                   motivo: str | None = None) -> str:
    """Deja bloqueada a la persona en ESTA clase (y lo registra en auditoría).

    Devuelve "nuevo" | "reactivado" | "ya_estaba". Idempotente: dos clics
    seguidos o dos peticiones a la vez terminan en UNA fila activa.
    """
    ahora = datetime.now(tz.utc)
    for intento in range(2):
        fila = await _fila(db, session_id, user_id)
        if fila and fila.active:
            await db.commit()   # nada que cambiar; libera el bloqueo de fila
            return "ya_estaba"
        if fila:
            fila.active = True
            fila.blocked_by, fila.blocked_at = actor_id, ahora
            fila.unblocked_by = fila.unblocked_at = None
            fila.reason = motivo
            fila.times_blocked = (fila.times_blocked or 0) + 1
            resultado = "reactivado"
        else:
            db.add(VideoAccessBlock(session_id=session_id, user_id=user_id, active=True,
                                    blocked_by=actor_id, blocked_at=ahora, reason=motivo,
                                    times_blocked=1))
            resultado = "nuevo"
        await log_action(db, actor_id, "video_block", "video", target_id=session_id,
                         details=f"user_id={user_id}; {resultado}")
        try:
            await db.commit()
            return resultado
        except IntegrityError:
            # Otra petición creó la fila al mismo tiempo: se relee y se aplica encima
            await db.rollback()
            if intento:
                raise
    return "ya_estaba"


async def permitir_volver(db: AsyncSession, session_id: str, user_id: str, actor_id: str) -> bool:
    """Quita el bloqueo (no emite token ni conecta a nadie). False si no había."""
    fila = await _fila(db, session_id, user_id)
    if not fila or not fila.active:
        await db.commit()   # nada que cambiar; libera el bloqueo de fila
        return False
    fila.active = False
    fila.unblocked_by, fila.unblocked_at = actor_id, datetime.now(tz.utc)
    await log_action(db, actor_id, "video_unblock", "video", target_id=session_id,
                     details=f"user_id={user_id}")
    await db.commit()
    return True


async def bloqueados(db: AsyncSession, session_id: str) -> list[VideoAccessBlock]:
    return list((await db.execute(
        select(VideoAccessBlock).where(
            VideoAccessBlock.session_id == session_id,
            VideoAccessBlock.active.is_(True),
        ).order_by(VideoAccessBlock.blocked_at)
    )).scalars())
