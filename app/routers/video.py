"""V3.9.26 — Entrar a la sala de video de una clase (dentro de Dorismon)."""
from typing import Annotated
from datetime import datetime, timedelta, timezone as tz

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, CurrentUser
from app.core.db import get_db
from app.models import (
    ClassSession, Enrollment, User, UserRole, SessionStatus,
    EventRegistration,
)
from app.services.livekit_service import (
    livekit_ready, livekit_url, build_token, room_name,
    MINUTOS_ANTES, MINUTOS_DESPUES,
)

router = APIRouter(prefix="/video", tags=["video"])


@router.get("/status")
async def video_status(user: Annotated[CurrentUser, Depends(get_current_user)]):
    """¿Está disponible el video propio? Lo usa el admin para saber si puede
    ofrecer la opción 'Video de Dorismon' al crear una clase."""
    return {"ready": livekit_ready(), "url_set": bool(livekit_url())}


@router.post("/sessions/{session_id}/join")
async def join_class_video(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Da permiso de entrada a la sala de video de una clase.

    CONTROL DE ACCESO (lo que un enlace de Meet no puede hacer):
    - El profesor de la clase entra como moderador.
    - El admin puede entrar (para supervisar o resolver un problema).
    - El estudiante entra solo si esa clase es suya: es su clase privada o de
      prueba, está inscrito en el nivel, o está registrado si es un evento.
    - Cualquier otro recibe 403, aunque tenga el enlace.

    VENTANA DE TIEMPO: se puede entrar desde 15 minutos antes y hasta 20
    minutos después de terminar. Fuera de eso, no.
    """
    if not livekit_ready():
        raise HTTPException(
            503,
            "El video de Dorismon no está configurado todavía. Usa el enlace de la clase.",
        )

    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    if s.status == SessionStatus.cancelled:
        raise HTTPException(400, "Esta clase fue cancelada")

    me = await db.get(User, user.user_id)
    if not me:
        raise HTTPException(404, "Usuario no encontrado")

    is_teacher = s.teacher_id == user.user_id
    is_admin = me.role == UserRole.super_admin
    is_moderator = is_teacher or is_admin

    # ¿Puede entrar este estudiante?
    if not is_moderator:
        allowed = False
        if s.student_id and s.student_id == user.user_id:
            allowed = True  # su clase privada o de prueba
        elif s.is_open_event:
            reg = (await db.execute(
                select(EventRegistration).where(
                    EventRegistration.session_id == session_id,
                    EventRegistration.student_id == user.user_id,
                    EventRegistration.cancelled_at.is_(None),
                )
            )).scalar_one_or_none()
            allowed = reg is not None
        else:
            enr = (await db.execute(
                select(Enrollment).where(
                    Enrollment.student_id == user.user_id,
                    Enrollment.course_id == s.course_id,
                    Enrollment.level_id == s.level_id,
                    Enrollment.is_active.is_(True),
                )
            )).scalar_one_or_none()
            allowed = enr is not None
        if not allowed:
            raise HTTPException(403, "Esta clase no es tuya")

    # Ventana de entrada
    now = datetime.now(tz.utc)
    starts = s.starts_at_utc
    ends = s.ends_at_utc
    if starts and starts.tzinfo is None:
        starts = starts.replace(tzinfo=tz.utc)
    if ends and ends.tzinfo is None:
        ends = ends.replace(tzinfo=tz.utc)

    if starts and now < starts - timedelta(minutes=MINUTOS_ANTES):
        raise HTTPException(
            400,
            f"La sala abre {MINUTOS_ANTES} minutos antes de la clase.",
        )
    if ends and now > ends + timedelta(minutes=MINUTOS_DESPUES):
        raise HTTPException(400, "Esta clase ya terminó.")

    token = build_token(
        session_id=session_id,
        user_id=user.user_id,
        display_name=me.full_name or "Participante",
        is_moderator=is_moderator,
    )
    return {
        "token": token,
        "url": livekit_url(),
        "room": room_name(session_id),
        "is_moderator": is_moderator,
        "title": s.title,
        "display_name": me.full_name,
        # Plan B: si algo falla en vivo, el enlace de siempre sigue disponible
        "fallback_url": s.meeting_url,
    }
