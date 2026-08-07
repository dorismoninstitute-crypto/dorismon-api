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
    EventRegistration, VideoPresence,
)
from app.services.livekit_service import (
    livekit_ready, livekit_url, build_token, room_name,
    mute_participant, remove_participant, list_participants,
    MINUTOS_ANTES, MINUTOS_DESPUES,
)

# Mínimo de permanencia para sugerir "presente" en la asistencia
MINUTOS_PARA_PRESENTE = 10

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

    # V3.9.27: dejar constancia de que esta persona entró a la clase.
    # Sirve para sugerir la asistencia después (el profesor confirma).
    try:
        prev = (await db.execute(
            select(VideoPresence).where(
                VideoPresence.session_id == session_id,
                VideoPresence.user_id == user.user_id,
            )
        )).scalar_one_or_none()
        if prev:
            prev.last_seen_at = now
        else:
            db.add(VideoPresence(session_id=session_id, user_id=user.user_id))
        await db.commit()
    except Exception:
        pass  # nunca impedir entrar a clase por un problema de registro

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


@router.post("/sessions/{session_id}/heartbeat")
async def heartbeat(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.27 — La página avisa cada minuto que la persona sigue en la clase.

    Con esto sabemos cuánto tiempo estuvo, no solo que entró. Es la diferencia
    entre 'se asomó 2 minutos' y 'estuvo toda la clase'.
    """
    row = (await db.execute(
        select(VideoPresence).where(
            VideoPresence.session_id == session_id,
            VideoPresence.user_id == user.user_id,
        )
    )).scalar_one_or_none()
    if not row:
        row = VideoPresence(session_id=session_id, user_id=user.user_id)
        db.add(row)
        await db.commit()
        return {"ok": True, "minutes": 0}

    now = datetime.now(tz.utc)
    last = row.last_seen_at
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=tz.utc)
    # Solo se cuenta el tiempo REAL transcurrido. El aviso llega cada minuto:
    # - menos de 30 segundos → no suma (evita inflar con avisos repetidos)
    # - más de 3 minutos → no suma (la persona se fue y volvió, o quedó una
    #   pestaña abierta; no queremos regalar tiempo que no estuvo)
    if last:
        delta = (now - last).total_seconds()
        if 30 <= delta <= 180:
            row.minutes = (row.minutes or 0) + int(round(delta / 60))
    row.last_seen_at = now
    await db.commit()
    return {"ok": True, "minutes": row.minutes}


@router.get("/sessions/{session_id}/participants")
async def who_is_connected(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Quiénes están conectados ahora. Solo para el profesor o el admin."""
    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    me = await db.get(User, user.user_id)
    if not (s.teacher_id == user.user_id or (me and me.role == UserRole.super_admin)):
        raise HTTPException(403, "Solo el profesor de la clase puede ver esto")
    if not livekit_ready():
        return {"items": []}

    parts = await list_participants(session_id)
    items = []
    for p in parts:
        uid = p.get("identity")
        u = await db.get(User, uid) if uid else None
        items.append({
            "identity": uid,
            "name": (u.full_name if u else None) or p.get("name") or "Participante",
            "is_teacher": uid == s.teacher_id,
        })
    return {"items": items}


@router.post("/sessions/{session_id}/moderate")
async def moderate(
    session_id: str,
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.27 — El profesor silencia o saca a un participante.

    Acciones: "mute" (silenciar), "unmute" (dejar hablar), "remove" (sacar).
    Solo el profesor de la clase o el admin. Nadie puede sacar al profesor.
    """
    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    me = await db.get(User, user.user_id)
    if not (s.teacher_id == user.user_id or (me and me.role == UserRole.super_admin)):
        raise HTTPException(403, "Solo el profesor de la clase puede moderar")
    if not livekit_ready():
        raise HTTPException(503, "El video de Dorismon no está configurado")

    action = (body.get("action") or "").strip()
    identity = (body.get("identity") or "").strip()
    if not identity:
        raise HTTPException(400, "Falta indicar a quién")
    if identity == s.teacher_id:
        raise HTTPException(400, "No se puede silenciar ni sacar al profesor de la clase")

    if action == "mute":
        ok = await mute_participant(session_id, identity, True)
    elif action == "unmute":
        ok = await mute_participant(session_id, identity, False)
    elif action == "remove":
        ok = await remove_participant(session_id, identity)
    else:
        raise HTTPException(400, "Acción no válida")

    if not ok:
        raise HTTPException(400, "No se pudo aplicar. ¿Sigue esa persona conectada?")
    return {"ok": True, "action": action}
