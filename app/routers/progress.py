"""Progreso académico del estudiante: módulos, ruta visual."""
from typing import Annotated
from datetime import datetime, timezone as tz
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, CurrentUser
from app.core.db import get_db
from app.models import (
    User, Student, Module, Lesson, Level, Course, Enrollment,
    SessionAttendance, ClassSession, ModuleProgress, Quiz, QuizAttempt,
    AttendanceState, Branch, Classroom,
)

router = APIRouter(prefix="/progress", tags=["progress"])


@router.get("/my-course")
async def my_course_progress(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Ruta visual del curso del estudiante (módulos del nivel actual)."""
    if user.role != "student":
        raise HTTPException(403)
    # Obtener enrollment activo
    enr = (await db.execute(
        select(Enrollment).where(
            Enrollment.student_id == user.user_id,
            Enrollment.is_active.is_(True),
        ).order_by(Enrollment.enrolled_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not enr:
        return {"enrolled": False}

    level = await db.get(Level, enr.level_id)
    course = await db.get(Course, enr.course_id)

    # Obtener módulos del nivel
    modules = (await db.execute(
        select(Module).where(Module.level_id == enr.level_id).order_by(Module.order_index)
    )).scalars().all()

    # Obtener progreso del estudiante
    progress_map = {}
    progress = (await db.execute(
        select(ModuleProgress).where(ModuleProgress.student_id == user.user_id)
    )).scalars().all()
    for p in progress:
        progress_map[p.module_id] = p

    # Determinar estado de cada módulo (locked/in_progress/completed)
    modules_out = []
    last_completed_idx = -1
    for i, m in enumerate(modules):
        p = progress_map.get(m.id)
        if p:
            status = p.status
            if status == "completed":
                last_completed_idx = i
        else:
            status = "locked"
        modules_out.append({
            "id": m.id, "name": m.name, "description": m.description,
            "order_index": m.order_index, "status": status,
            "attended_count": p.attended_count if p else 0,
            "quiz_passed": p.quiz_passed if p else False,
        })

    # Desbloquear el siguiente al último completado
    next_idx = last_completed_idx + 1
    if 0 <= next_idx < len(modules_out) and modules_out[next_idx]["status"] == "locked":
        modules_out[next_idx]["status"] = "in_progress"

    # Próxima clase del estudiante
    # V1.7: filtrar grupales de su nivel + privadas asignadas a él
    next_session = (await db.execute(
        select(ClassSession).where(
            or_(
                # Grupal del nivel correcto (no privada)
                (ClassSession.level_id == enr.level_id) & (ClassSession.student_id.is_(None)),
                # Privada para este estudiante
                ClassSession.student_id == user.user_id,
            ),
            ClassSession.ends_at_utc > datetime.now(tz.utc),  # V1.6.4
            ClassSession.is_open_event.is_(False),
        ).order_by(ClassSession.starts_at_utc).limit(1)
    )).scalar_one_or_none()
    next_session_data = None
    if next_session:
        teacher = await db.get(User, next_session.teacher_id) if next_session.teacher_id else None
        # V3.0.3: ubicación para presencial/híbrida
        location = None
        if next_session.branch_id or next_session.classroom_id:
            branch = await db.get(Branch, next_session.branch_id) if next_session.branch_id else None
            classroom = await db.get(Classroom, next_session.classroom_id) if next_session.classroom_id else None
            if classroom and not branch and classroom.branch_id:
                branch = await db.get(Branch, classroom.branch_id)
            if branch or classroom:
                maps_url = None
                if branch and branch.address:
                    from urllib.parse import quote
                    maps_url = f"https://www.google.com/maps/search/?api=1&query={quote(branch.name + ' ' + branch.address)}"
                location = {
                    "branch_name": branch.name if branch else None,
                    "address": branch.address if branch else None,
                    "phone": branch.phone if branch else None,
                    "classroom_name": classroom.name if classroom else None,
                    "maps_url": maps_url,
                }
        next_session_data = {
            "id": next_session.id, "title": next_session.title,
            "starts_at_utc": next_session.starts_at_utc.isoformat() if next_session.starts_at_utc else None,
            "ends_at_utc": next_session.ends_at_utc.isoformat() if next_session.ends_at_utc else None,
            "modality": next_session.modality.value,
            "meeting_url": next_session.meeting_url,
            "teacher_name": teacher.full_name if teacher else None,
            "teacher_notes": next_session.teacher_notes,
            "module_id": next_session.module_id,
            "is_private": next_session.student_id is not None,  # V1.7
            "location": location,  # V3.0.3
        }

    # Última clase asistida con notas del profe
    last_attended = (await db.execute(
        select(ClassSession, SessionAttendance)
        .join(SessionAttendance, ClassSession.id == SessionAttendance.session_id)
        .where(
            SessionAttendance.student_id == user.user_id,
            SessionAttendance.state == AttendanceState.present,
            ClassSession.starts_at_utc < datetime.now(tz.utc),
        )
        .order_by(ClassSession.starts_at_utc.desc()).limit(1)
    )).first()
    last_class_data = None
    if last_attended:
        last_session, _ = last_attended
        last_class_data = {
            "title": last_session.title,
            "starts_at_utc": last_session.starts_at_utc.isoformat() if last_session.starts_at_utc else None,
            "teacher_notes": last_session.teacher_notes,
        }

    completed_count = sum(1 for m in modules_out if m["status"] == "completed")
    progress_pct = round(completed_count * 100 / len(modules_out), 1) if modules_out else 0

    return {
        "enrolled": True,
        "course_name": course.name if course else None,
        "level_code": level.code if level else None,
        "level_name": level.name if level else None,
        "total_modules": len(modules_out),
        "completed_modules": completed_count,
        "progress_pct": progress_pct,
        "modules": modules_out,
        "next_session": next_session_data,
        "last_class": last_class_data,
    }


@router.post("/recompute")
async def recompute_progress(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Recalcula el progreso del estudiante (útil después de quiz o asistencia)."""
    if user.role != "student":
        raise HTTPException(403)
    # Obtener enrollment activo
    enr = (await db.execute(
        select(Enrollment).where(
            Enrollment.student_id == user.user_id,
            Enrollment.is_active.is_(True),
        ).limit(1)
    )).scalar_one_or_none()
    if not enr:
        return {"ok": True, "no_enrollment": True}

    modules = (await db.execute(
        select(Module).where(Module.level_id == enr.level_id).order_by(Module.order_index)
    )).scalars().all()

    for m in modules:
        # ¿Cuántas asistencias presentes a clases de este módulo?
        attended = (await db.execute(
            select(func.count())
            .select_from(SessionAttendance)
            .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
            .where(
                SessionAttendance.student_id == user.user_id,
                SessionAttendance.state == AttendanceState.present,
                ClassSession.module_id == m.id,
            )
        )).scalar() or 0

        # Buscar progreso existente
        mp = (await db.execute(
            select(ModuleProgress).where(
                ModuleProgress.student_id == user.user_id,
                ModuleProgress.module_id == m.id,
            )
        )).scalar_one_or_none()

        if not mp:
            mp = ModuleProgress(
                student_id=user.user_id, module_id=m.id,
                status="locked", attended_count=0, quiz_passed=False,
            )
            db.add(mp)

        mp.attended_count = attended

        # Lógica: completado si asistió al menos 1 vez al módulo (puede mejorarse)
        if attended >= 1:
            if mp.status == "locked":
                mp.status = "in_progress"

        # Si hay quiz del módulo y aprobó, marcar quiz_passed
        # (módulos no tienen quizzes directamente, los quizzes son por level)
        # Lógica simplificada: completado = asistió + (no hay quiz pendiente o aprobó)

        if attended >= 1:
            mp.status = "completed"
            if not mp.completed_at:
                mp.completed_at = datetime.now(tz.utc)

    await db.commit()
    return {"ok": True}
