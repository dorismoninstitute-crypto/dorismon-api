"""V3.9.29 — Avisos al teléfono: registro de dispositivos y prueba."""
from typing import Annotated
from datetime import datetime, timezone as tz

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.security import get_current_user, CurrentUser
from app.core.db import get_db
from app.models import PushSubscription, User
from app.services.push_service import push_ready, public_key, send_push_sync

router = APIRouter(prefix="/push", tags=["push"])


@router.get("/config")
async def push_config():
    """Le dice al navegador si puede ofrecer los avisos y con qué clave.

    Es público a propósito: la clave pública no es secreta y el navegador la
    necesita antes de que la persona acepte.
    """
    return {"ready": push_ready(), "public_key": public_key()}


@router.post("/subscribe")
async def subscribe(
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Guarda el teléfono de esta persona para poder avisarle."""
    if not push_ready():
        raise HTTPException(503, "Los avisos al teléfono no están configurados todavía")

    sub = body.get("subscription") or body
    endpoint = (sub.get("endpoint") or "").strip()
    keys = sub.get("keys") or {}
    p256dh = (keys.get("p256dh") or "").strip()
    auth = (keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth:
        raise HTTPException(400, "Faltan datos del dispositivo")

    existente = (await db.execute(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    )).scalar_one_or_none()

    if existente:
        # El mismo dispositivo puede cambiar de dueño (equipo compartido)
        existente.user_id = user.user_id
        existente.p256dh = p256dh
        existente.auth = auth
        existente.last_used_at = datetime.now(tz.utc)
    else:
        db.add(PushSubscription(
            user_id=user.user_id, endpoint=endpoint,
            p256dh=p256dh, auth=auth,
            device=(body.get("device") or "")[:120] or None,
        ))
    await db.commit()
    return {"ok": True}


@router.post("/unsubscribe")
async def unsubscribe(
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """La persona ya no quiere avisos en este dispositivo."""
    endpoint = (body.get("endpoint") or "").strip()
    if not endpoint:
        raise HTTPException(400, "Falta el dispositivo")
    row = (await db.execute(
        select(PushSubscription).where(
            PushSubscription.endpoint == endpoint,
            PushSubscription.user_id == user.user_id,
        )
    )).scalar_one_or_none()
    if row:
        await db.delete(row)
        await db.commit()
    return {"ok": True}


@router.get("/status")
async def my_status(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """¿Cuántos dispositivos tiene registrados esta persona?"""
    rows = (await db.execute(
        select(PushSubscription).where(PushSubscription.user_id == user.user_id)
    )).scalars().all()
    return {"devices": len(rows), "ready": push_ready()}


@router.post("/test")
async def send_test(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Manda un aviso de prueba a los dispositivos de quien lo pide.

    Sirve para que la persona compruebe que le llega antes de confiar en el
    sistema para algo importante.
    """
    if not push_ready():
        raise HTTPException(503, "Los avisos al teléfono no están configurados")

    rows = (await db.execute(
        select(PushSubscription).where(PushSubscription.user_id == user.user_id)
    )).scalars().all()
    if not rows:
        raise HTTPException(400, "Todavía no has activado los avisos en este dispositivo")

    enviados = 0
    for r in rows:
        sub = {"endpoint": r.endpoint, "keys": {"p256dh": r.p256dh, "auth": r.auth}}
        ok, code = await run_in_threadpool(
            send_push_sync, sub, "✅ Avisos activados",
            "Así se verán los avisos de tus clases y tareas.", "/dashboard", "prueba",
        )
        if ok:
            enviados += 1
            r.last_used_at = datetime.now(tz.utc)
        elif code in (404, 410):
            await db.delete(r)  # ese dispositivo ya no existe
    await db.commit()
    return {"ok": enviados > 0, "sent": enviados}
