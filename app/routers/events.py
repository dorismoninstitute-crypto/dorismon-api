"""Eventos abiertos — clases a las que cualquier estudiante puede registrarse."""
from typing import Annotated
from datetime import datetime, timezone as tz, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, CurrentUser
from app.core.db import get_db
from app.models import (
    User, Student, Teacher, ClassSession, EventRegistration, SessionAttendance,
    Course, Level, Notification, NotificationType, SessionStatus,
)

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/")
async def list_open_events(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Lista eventos abiertos disponibles (futuros y no cancelados)."""
    now = datetime.now(tz.utc)
    stmt = (
        select(ClassSession)
        .where(
            ClassSession.is_open_event.is_(True),
            ClassSession.starts_at_utc > now,
            ClassSession.status == SessionStatus.scheduled,
        )
        .order_by(ClassSession.starts_at_utc)
    )
    sessions = (await db.execute(stmt)).scalars().all()
    out = []
    for s in sessions:
        teacher_user = await db.get(User, s.teacher_id)
        # cupos
        regs_count = (await db.execute(
            select(func.count()).select_from(EventRegistration).where(
                EventRegistration.session_id == s.id,
                EventRegistration.cancelled_at.is_(None),
            )
        )).scalar() or 0
        # ¿ya me anoté?
        already = None
        if user.role == "student":
            already = (await db.execute(
                select(EventRegistration).where(
                    EventRegistration.session_id == s.id,
                    EventRegistration.student_id == user.user_id,
                    EventRegistration.cancelled_at.is_(None),
                )
            )).scalar_one_or_none()
        out.append({
            "id": s.id, "title": s.title, "description": s.description,
            "modality": s.modality.value,
            "starts_at_utc": s.starts_at_utc.isoformat(),
            "ends_at_utc": s.ends_at_utc.isoformat(),
            "teacher_name": teacher_user.full_name if teacher_user else "—",
            "meeting_url": s.meeting_url,
            "capacity": s.capacity,
            "registered_count": regs_count,
            "spots_left": max(0, s.capacity - regs_count),
            "is_full": regs_count >= s.capacity,
            "i_am_registered": bool(already),
        })
    return out


@router.post("/{session_id}/register")
async def register_to_event(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Estudiante se registra a un evento abierto."""
    if user.role != "student":
        raise HTTPException(403, "Solo estudiantes pueden registrarse a eventos")
    session = await db.get(ClassSession, session_id)
    if not session:
        raise HTTPException(404, "Evento no encontrado")
    if not session.is_open_event:
        raise HTTPException(400, "Esta no es una clase abierta")
    if session.status != SessionStatus.scheduled:
        raise HTTPException(400, "El evento ya no está disponible")
    starts = session.starts_at_utc
    if starts.tzinfo is None:
        starts = starts.replace(tzinfo=tz.utc)
    if starts <= datetime.now(tz.utc):
        raise HTTPException(400, "El evento ya comenzó o finalizó")

    # ¿Ya está registrado?
    existing = (await db.execute(
        select(EventRegistration).where(
            EventRegistration.session_id == session_id,
            EventRegistration.student_id == user.user_id,
        )
    )).scalar_one_or_none()
    if existing and not existing.cancelled_at:
        raise HTTPException(409, "Ya estás registrado a este evento")

    # Verificar cupo
    regs_count = (await db.execute(
        select(func.count()).select_from(EventRegistration).where(
            EventRegistration.session_id == session_id,
            EventRegistration.cancelled_at.is_(None),
        )
    )).scalar() or 0
    if regs_count >= session.capacity:
        raise HTTPException(409, "El evento está lleno")

    # Crear o reactivar
    if existing:
        existing.cancelled_at = None
        existing.registered_at = datetime.now(tz.utc)
    else:
        db.add(EventRegistration(
            session_id=session_id, student_id=user.user_id,
        ))

    db.add(Notification(
        user_id=user.user_id, type=NotificationType.class_scheduled,
        title=f"✓ Registrado: {session.title}",
        body=f"Inicia {session.starts_at_utc.strftime('%d/%m a las %H:%M')}",
        link="/dashboard/student/events",
    ))
    await db.commit()
    return {"ok": True}


@router.post("/{session_id}/cancel")
async def cancel_event_registration(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Cancela el registro hasta 2 horas antes del evento."""
    if user.role != "student":
        raise HTTPException(403)
    session = await db.get(ClassSession, session_id)
    if not session:
        raise HTTPException(404)
    # No se puede cancelar si quedan menos de 2 horas
    starts = session.starts_at_utc
    if starts.tzinfo is None:
        starts = starts.replace(tzinfo=tz.utc)
    cancel_deadline = starts - timedelta(hours=2)
    if datetime.now(tz.utc) > cancel_deadline:
        raise HTTPException(400, "Ya no se puede cancelar (faltan menos de 2 horas)")
    reg = (await db.execute(
        select(EventRegistration).where(
            EventRegistration.session_id == session_id,
            EventRegistration.student_id == user.user_id,
            EventRegistration.cancelled_at.is_(None),
        )
    )).scalar_one_or_none()
    if not reg:
        raise HTTPException(404, "No estás registrado a este evento")
    reg.cancelled_at = datetime.now(tz.utc)
    await db.commit()
    return {"ok": True}


@router.get("/my-events")
async def my_events(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Eventos a los que el estudiante está registrado."""
    if user.role != "student":
        raise HTTPException(403)
    stmt = (
        select(EventRegistration, ClassSession)
        .join(ClassSession, EventRegistration.session_id == ClassSession.id)
        .where(
            EventRegistration.student_id == user.user_id,
            EventRegistration.cancelled_at.is_(None),
            ClassSession.status == SessionStatus.scheduled,
        )
        .order_by(ClassSession.starts_at_utc)
    )
    rows = (await db.execute(stmt)).all()
    out = []
    for reg, s in rows:
        teacher_user = await db.get(User, s.teacher_id)
        out.append({
            "id": s.id, "title": s.title,
            "modality": s.modality.value,
            "starts_at_utc": s.starts_at_utc.isoformat(),
            "teacher_name": teacher_user.full_name if teacher_user else "—",
            "meeting_url": s.meeting_url,
            "registration_id": reg.id,
            "can_cancel": (s.starts_at_utc if s.starts_at_utc.tzinfo else s.starts_at_utc.replace(tzinfo=tz.utc)) > datetime.now(tz.utc) + timedelta(hours=2),
        })
    return out
