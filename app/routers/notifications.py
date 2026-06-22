"""V3.1 — Centro de avisos UNIVERSAL (campanita).

Funciona para los 3 roles (estudiante, profe, admin). Antes las notificaciones
solo las podía ver el estudiante; ahora cualquier usuario ve las suyas.

El endpoint /student/notifications viejo se mantiene para no romper nada, pero
este es el oficial de aquí en adelante.
"""
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import get_current_user, CurrentUser
from app.models import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
async def list_notifications(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    unread_only: bool = False,
    limit: int = 30,
):
    """Lista las notificaciones del usuario actual (cualquier rol)."""
    stmt = select(Notification).where(Notification.user_id == user.user_id)
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    stmt = stmt.order_by(Notification.created_at.desc()).limit(min(limit, 100))
    items = (await db.execute(stmt)).scalars().all()
    return [{
        "id": n.id,
        "type": n.type.value if n.type else "info",
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "is_read": n.is_read,
        "created_at": n.created_at.isoformat() if n.created_at else None,
    } for n in items]


@router.get("/unread-count")
async def unread_count(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Cuántas notificaciones sin leer tiene el usuario (para el badge de la campanita)."""
    n = (await db.execute(
        select(func.count()).select_from(Notification).where(
            Notification.user_id == user.user_id,
            Notification.is_read.is_(False),
        )
    )).scalar() or 0
    return {"unread": n}


@router.post("/{notif_id}/read")
async def mark_read(
    notif_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Marca una notificación como leída."""
    n = await db.get(Notification, notif_id)
    if not n or n.user_id != user.user_id:
        raise HTTPException(404, "Notificación no encontrada")
    n.is_read = True
    await db.commit()
    return {"ok": True}


@router.post("/read-all")
async def mark_all_read(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Marca TODAS las notificaciones del usuario como leídas."""
    await db.execute(
        update(Notification)
        .where(Notification.user_id == user.user_id, Notification.is_read.is_(False))
        .values(is_read=True)
    )
    await db.commit()
    return {"ok": True}
