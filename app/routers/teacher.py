"""Teacher — agenda, asistencia, quizzes, tareas, materiales, observaciones."""
from typing import Annotated
from datetime import datetime, timedelta, timezone as tz
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_teacher_or_admin, CurrentUser, get_current_user
from app.core.db import get_db
from app.services.audit import log_action
from app.models import (
    User, Teacher, ClassSession, SessionAttendance, Enrollment,
    Assignment, AssignmentSubmission, Quiz, QuizQuestion, QuizAttempt,
    Material, Observation, Notification, Student,
    AttendanceState, QuestionType, MaterialType, NotificationType, SessionStatus,
    Course, Level, UserRole, Branch, Classroom, EventRegistration, ClassConfirmation, TrialClass,
    VideoPresence, AssignmentKind,
)

router = APIRouter(prefix="/teacher", tags=["teacher"])




def _tipo_tarea(valor) -> AssignmentKind:
    """Convierte lo que llega del panel al tipo de tarea. Si no se reconoce,
    queda como escrita (que es como funcionaba antes)."""
    try:
        return AssignmentKind(str(valor or "written"))
    except ValueError:
        return AssignmentKind.written


def _blanks_json(blanks) -> str | None:
    """Guarda los ejercicios de completar espacios.

    Formato esperado: [{"text": "I ___ to school", "answer": "went"}, ...]
    Se valida aquí para no guardar algo roto que después falle al calificar.
    """
    if not blanks or not isinstance(blanks, list):
        return None
    import json as _json
    limpios = []
    for b in blanks[:20]:
        if not isinstance(b, dict):
            continue
        texto = str(b.get("text") or "").strip()
        resp = str(b.get("answer") or "").strip()
        if texto and resp:
            limpios.append({"text": texto[:300], "answer": resp[:100]})
    return _json.dumps(limpios, ensure_ascii=False) if limpios else None


def _leer_blanks(txt):
    """Lee los ejercicios de completar espacios guardados."""
    if not txt:
        return None
    import json as _json
    try:
        return _json.loads(txt)
    except Exception:
        return None



async def _avisar_quiz_publicado(db, q, preguntas: int) -> int:
    """V3.9.34 — Avisa a los estudiantes que hay un quiz nuevo.

    Antes el quiz aparecía en silencio: nadie se enteraba salvo que entrara
    por casualidad. Ahora llega a la campana, al teléfono y por correo.
    """
    from app.services.push_service import notify_user
    from app.services.email_service import send_email

    if not q.level_id:
        return 0

    # V3.9.46 P1 — Misma audiencia que el quiz. Antes se avisaba a todo el
    # nivel: si el quiz era para el grupo de la mañana, el de la noche
    # también recibía correo y notificación de un quiz que ni siquiera podía
    # abrir.
    from app.services.audience import destinatarios_de_actividad
    _ids = await destinatarios_de_actividad(db, q)
    filas = (await db.execute(
        select(User.id, User.email, User.full_name).where(User.id.in_(_ids))
    )).all() if _ids else []

    titulo = "📝 Nuevo quiz disponible"
    cuerpo = f"'{q.title}' — {preguntas} preguntas. ¡Ponte a prueba!"
    avisados = 0

    for uid, email, nombre in filas:
        db.add(Notification(
            user_id=uid, type=NotificationType.info,
            title=titulo, body=cuerpo, link="/dashboard/student/quizzes",
        ))
        try:
            await notify_user(db, uid, titulo, cuerpo,
                              "/dashboard/student/quizzes", f"quiz:{q.id}")
        except Exception:
            pass
        try:
            if email:
                await send_email(
                    to=email,
                    subject=f"Nuevo quiz: {q.title}",
                    html=(
                        f"<p>Hola {nombre or ''},</p>"
                        f"<p>Tu profesor publicó un nuevo quiz: "
                        f"<strong>{q.title}</strong> ({preguntas} preguntas).</p>"
                        f"<p>Entra a la plataforma para responderlo.</p>"
                        f"<p>— Dorismon Language Institute</p>"
                    ),
                )
        except Exception:
            pass
        avisados += 1
    return avisados

@router.get("/dashboard")
async def teacher_dashboard(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    u = await db.get(User, teacher.user_id)
    now = datetime.now(tz.utc)
    week_ahead = now + timedelta(days=7)

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    # Filtro: si es teacher solo lo suyo; admin ve agregado
    base_filter = (ClassSession.teacher_id == teacher.user_id) if teacher.role == "teacher" else True

    today_classes = (await db.execute(
        select(ClassSession).where(
            base_filter,
            ClassSession.starts_at_utc >= today_start,
            ClassSession.starts_at_utc < today_end,
            ClassSession.status == SessionStatus.scheduled,
        ).order_by(ClassSession.starts_at_utc)
    )).scalars().all()

    next_week = (await db.execute(
        select(func.count()).select_from(ClassSession).where(
            base_filter,
            ClassSession.ends_at_utc > now,  # V1.6.4
            ClassSession.starts_at_utc < week_ahead,
            ClassSession.status == SessionStatus.scheduled,
        )
    )).scalar() or 0

    # Tareas por corregir
    pending_grading = (await db.execute(
        select(func.count()).select_from(AssignmentSubmission)
        .join(Assignment, AssignmentSubmission.assignment_id == Assignment.id)
        .where(
            Assignment.teacher_id == teacher.user_id if teacher.role == "teacher" else True,
            AssignmentSubmission.submitted_at.is_not(None),
            AssignmentSubmission.graded_at.is_(None),
        )
    )).scalar() or 0

    # Estudiantes (los inscritos a mis cursos)
    if teacher.role == "teacher":
        student_count = (await db.execute(
            select(func.count(func.distinct(Enrollment.student_id))).where(
                Enrollment.teacher_id == teacher.user_id, Enrollment.is_active.is_(True),
            )
        )).scalar() or 0
    else:
        student_count = (await db.execute(
            select(func.count(func.distinct(Enrollment.student_id))).where(Enrollment.is_active.is_(True))
        )).scalar() or 0

    # V3.9.21: identificar cuáles clases de hoy son CLASES DE PRUEBA (para etiqueta 🎯)
    today_ids = [s.id for s in today_classes]
    trial_session_ids = set()
    if today_ids:
        trial_rows = (await db.execute(
            select(TrialClass.session_id).where(TrialClass.session_id.in_(today_ids))
        )).all()
        trial_session_ids = {x for (x,) in trial_rows if x}

    today_data = []
    for s in today_classes:
        teacher_user = await db.get(User, s.teacher_id)
        # V1.8: agregar más info útil
        level = await db.get(Level, s.level_id) if s.level_id else None
        today_data.append({
            "id": s.id, "title": s.title, "modality": s.modality.value,
            "starts_at_utc": s.starts_at_utc.isoformat(),
            "ends_at_utc": s.ends_at_utc.isoformat() if s.ends_at_utc else None,
            "meeting_url": s.meeting_url, "teacher_name": teacher_user.full_name if teacher_user else "—",
            "level_code": level.code if level else None,
            "is_private": s.student_id is not None,  # V1.7
            "module_id": s.module_id,
            "status": s.status.value if s.status else "scheduled",  # V3.9.19
            "is_open_event": bool(s.is_open_event),  # V3.9.19: para etiqueta 🎉
            "is_trial": s.id in trial_session_ids,  # V3.9.21: para etiqueta 🎯 Prueba
            "video_provider": getattr(s, "video_provider", "meet") or "meet",  # V3.9.26
        })

    # V1.8: Próximas clases de la semana (no solo hoy)
    week_classes_q = (await db.execute(
        select(ClassSession).where(
            base_filter,
            ClassSession.starts_at_utc >= today_end,
            ClassSession.starts_at_utc < week_ahead,
            ClassSession.status == SessionStatus.scheduled,
        ).order_by(ClassSession.starts_at_utc).limit(10)
    )).scalars().all()
    week_schedule = []
    for s in week_classes_q:
        level = await db.get(Level, s.level_id) if s.level_id else None
        week_schedule.append({
            "id": s.id, "title": s.title, "modality": s.modality.value,
            "starts_at_utc": s.starts_at_utc.isoformat(),
            "ends_at_utc": s.ends_at_utc.isoformat() if s.ends_at_utc else None,
            "level_code": level.code if level else None,
            "is_private": s.student_id is not None,
        })

    # V1.8: Distribución de estudiantes por nivel
    levels_distribution = []
    if teacher.role == "teacher":
        level_counts = (await db.execute(
            select(Level.code, Level.name, func.count(func.distinct(Enrollment.student_id)))
            .join(Enrollment, Enrollment.level_id == Level.id)
            .where(
                Enrollment.teacher_id == teacher.user_id,
                Enrollment.is_active.is_(True),
            )
            .group_by(Level.code, Level.name)
        )).all()
        for code, name, count in level_counts:
            levels_distribution.append({
                "level_code": code, "level_name": name,
                "student_count": count,
            })

    # V1.8: Estudiantes con asistencia baja (<70%)
    students_at_risk = []
    if teacher.role == "teacher":
        # Mis estudiantes
        my_students_q = (await db.execute(
            select(User, Enrollment, Level)
            .join(Enrollment, Enrollment.student_id == User.id)
            .join(Level, Enrollment.level_id == Level.id)
            .where(
                Enrollment.teacher_id == teacher.user_id,
                Enrollment.is_active.is_(True),
            )
        )).all()
        for stu, e, l in my_students_q:
            # Asistencia del estudiante en mis clases
            att_rows = (await db.execute(
                select(SessionAttendance.state)
                .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
                .where(
                    SessionAttendance.student_id == stu.id,
                    ClassSession.teacher_id == teacher.user_id,
                )
            )).all()
            total = len(att_rows)
            if total < 3:
                continue  # ignorar si tiene menos de 3 clases tomadas (poca data)
            present = sum(1 for (s,) in att_rows if s == AttendanceState.present)
            pct = round((present / total) * 100, 1)
            if pct < 70:
                students_at_risk.append({
                    "student_id": stu.id,
                    "student_name": stu.full_name,
                    "gender": stu.gender,
                    "level_code": l.code,
                    "attendance_pct": pct,
                    "total_classes": total,
                })

    return {
        "user": {"id": u.id, "full_name": u.full_name, "email": u.email,
                 "avatar_url": u.avatar_url, "gender": u.gender, "role": teacher.role},
        "stats": {
            "today_classes": len(today_classes),
            "next_week_classes": next_week,
            "pending_grading": pending_grading,
            "total_students": student_count,
        },
        "today_schedule": today_data,
        "week_schedule": week_schedule,  # V1.8
        "levels_distribution": levels_distribution,  # V1.8
        "students_at_risk": students_at_risk,  # V1.8
    }


@router.get("/sessions")
async def my_sessions(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
    filter_period: str = "upcoming",  # upcoming/this_week/this_month/past/all
):
    """V2.9.1: Clases del profe con filtro de período.

    - upcoming (default): clases de hoy en adelante, orden ASC (próximas primero)
    - this_week: clases de la semana actual
    - this_month: clases del mes actual
    - past: clases pasadas, orden DESC
    - all: todas
    """
    from datetime import timedelta as td
    from calendar import monthrange
    now = datetime.now(tz.utc)

    stmt = select(ClassSession)
    if teacher.role == "teacher":
        stmt = stmt.where(ClassSession.teacher_id == teacher.user_id)

    if filter_period == "upcoming":
        stmt = stmt.where(ClassSession.starts_at_utc >= now - td(hours=3))
        stmt = stmt.order_by(ClassSession.starts_at_utc.asc())
    elif filter_period == "this_week":
        start = (now - td(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + td(days=7)
        stmt = stmt.where(ClassSession.starts_at_utc >= start, ClassSession.starts_at_utc < end)
        stmt = stmt.order_by(ClassSession.starts_at_utc.asc())
    elif filter_period == "this_month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        last_day = monthrange(start.year, start.month)[1]
        end = start.replace(day=last_day, hour=23, minute=59, second=59)
        stmt = stmt.where(ClassSession.starts_at_utc >= start, ClassSession.starts_at_utc <= end)
        stmt = stmt.order_by(ClassSession.starts_at_utc.asc())
    elif filter_period == "past":
        stmt = stmt.where(ClassSession.starts_at_utc < now)
        stmt = stmt.order_by(ClassSession.starts_at_utc.desc())
    else:  # all
        stmt = stmt.order_by(ClassSession.starts_at_utc.asc())

    stmt = stmt.limit(100)
    sessions = (await db.execute(stmt)).scalars().all()
    out = []
    for s in sessions:
        # V2.9.2: NO mostrar clases canceladas en "Mis clases"
        # (evita que el profe vea/pase asistencia en clases que ya no existen)
        if s.status == SessionStatus.cancelled:
            continue
        course = await db.get(Course, s.course_id)
        level = await db.get(Level, s.level_id)
        starts = s.starts_at_utc if s.starts_at_utc.tzinfo else s.starts_at_utc.replace(tzinfo=tz.utc)
        out.append({
            "id": s.id, "title": s.title, "modality": s.modality.value,
            "starts_at_utc": s.starts_at_utc.isoformat(),
            "course_name": course.name if course else None,
            "level_code": level.code if level else None,
            "meeting_url": s.meeting_url,
            "status": s.status.value,
            "capacity": s.capacity,
        })
    return {"items": out, "filter_period": filter_period}


@router.get("/sessions/{session_id}/attendance")
async def get_attendance(
    session_id: str,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(ClassSession, session_id)
    if not session:
        raise HTTPException(404, "Sesión no encontrada")
    if teacher.role == "teacher" and session.teacher_id != teacher.user_id:
        raise HTTPException(403, "No es tu sesión")

    # Inscritos a este level del curso
    # V3.9.19: si es un EVENTO ABIERTO, la lista de asistencia son los
    # REGISTRADOS al evento (no el curso/nivel, que en eventos es relleno técnico)
    # V3.9.22: si la clase tiene un ESTUDIANTE ASIGNADO (clase de prueba o
    # privada), la lista es ESE estudiante — esté inscrito o no. Antes, la
    # clase de prueba mostraba "Estudiantes (0)" porque el estudiante de
    # prueba aún no tiene inscripción, y el profe no podía pasar asistencia
    # (ni cobrar la clase).
    if session.is_open_event:
        students_q = (
            select(EventRegistration, User)
            .join(User, EventRegistration.student_id == User.id)
            .where(
                EventRegistration.session_id == session_id,
                EventRegistration.cancelled_at.is_(None),
            )
        )
        rows = (await db.execute(students_q)).all()
    elif session.student_id:
        assigned_user = await db.get(User, session.student_id)
        rows = [(None, assigned_user)] if assigned_user else []
    else:
        # V3.9.46 P1 — El roster para pasar lista usa la MISMA audiencia que
        # la clase. Antes traía a TODOS los del nivel: en una clase del grupo
        # de la mañana le aparecían también los de la noche, y el profesor
        # podía marcar asistencia a quien no estuvo invitado.
        from app.services.audience import destinatarios_de_clase
        _ids = await destinatarios_de_clase(db, session)
        if _ids:
            students_q = (
                select(Enrollment, User)
                .join(User, Enrollment.student_id == User.id)
                .where(
                    Enrollment.student_id.in_(_ids),
                    Enrollment.is_active.is_(True),
                )
            )
            rows = (await db.execute(students_q)).all()
        else:
            rows = []
    # V3.0: avisos de ausencia para esta clase
    from app.models import AbsenceNotice
    absence_map = {}
    for an in (await db.execute(
        select(AbsenceNotice).where(AbsenceNotice.session_id == session_id)
    )).scalars().all():
        absence_map[an.student_id] = an
    # V3.9.21: quiénes confirmaron asistencia a esta clase
    conf_rows = (await db.execute(
        select(ClassConfirmation.student_id).where(ClassConfirmation.session_id == session_id)
    )).all()
    confirmed_ids = {x for (x,) in conf_rows}

    # V3.9.27: quiénes estuvieron en la videollamada y cuánto tiempo.
    # Se usa para SUGERIR la asistencia; el profesor confirma o corrige.
    MIN_PRESENTE = 10
    pres_rows = (await db.execute(
        select(VideoPresence).where(VideoPresence.session_id == session_id)
    )).scalars().all()
    presencia = {p.user_id: (p.minutes or 0) for p in pres_rows}

    out_students = []
    for e, u in rows:
        att = (await db.execute(
            select(SessionAttendance).where(
                SessionAttendance.session_id == session_id,
                SessionAttendance.student_id == u.id,
            )
        )).scalar_one_or_none()
        notice = absence_map.get(u.id)
        out_students.append({
            "student_id": u.id, "full_name": u.full_name, "email": u.email,
            "attendance_id": att.id if att else None,
            "state": att.state.value if att and att.state else None,
            "notes": att.notes if att else None,
            "confirmed": u.id in confirmed_ids,  # V3.9.21: confirmó que asistirá
            # V3.9.27: presencia detectada en la videollamada
            "video_minutes": presencia.get(u.id),
            "video_suggests_present": (presencia.get(u.id, 0) >= MIN_PRESENTE) if u.id in presencia else None,
            # V3.0: aviso de ausencia del estudiante
            "absence_notice": ({
                "reason": notice.reason,
                "in_advance": notice.notified_in_advance,
            } if notice else None),
        })

    # V3.0.3: ubicación (aula/sede) para clases presenciales
    teacher_location = None
    if session.branch_id or session.classroom_id:
        br = await db.get(Branch, session.branch_id) if session.branch_id else None
        cr = await db.get(Classroom, session.classroom_id) if session.classroom_id else None
        if cr and not br and cr.branch_id:
            br = await db.get(Branch, cr.branch_id)
        teacher_location = {
            "branch_name": br.name if br else None,
            "address": br.address if br else None,
            "classroom_name": cr.name if cr else None,
        }

    return {
        "session": {
            "id": session.id, "title": session.title,
            "starts_at_utc": session.starts_at_utc.isoformat(),
            "modality": session.modality.value,
            "teacher_notes": session.teacher_notes,
            # V2.9: status + datos de cancelación
            "status": session.status.value if session.status else "scheduled",
            "cancellation_reason": session.cancellation_reason,
            "cancelled_at": session.cancelled_at.isoformat() if session.cancelled_at else None,
            "meeting_url": session.meeting_url,
            "location": teacher_location,  # V3.0.3
        },
        "students": out_students,
    }


@router.post("/sessions/{session_id}/attendance")
async def save_attendance(
    session_id: str, body: dict,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(ClassSession, session_id)
    if not session:
        raise HTTPException(404)
    if teacher.role == "teacher" and session.teacher_id != teacher.user_id:
        raise HTTPException(403)
    # V2.9.2: no permitir pasar asistencia en clase cancelada
    if session.status == SessionStatus.cancelled:
        raise HTTPException(400, "Esta clase fue cancelada. No se puede registrar asistencia.")
    records = body.get("records", [])
    updated = 0
    now = datetime.now(tz.utc)
    for r in records:
        sid = r.get("student_id")
        if not sid:
            continue
        att = (await db.execute(
            select(SessionAttendance).where(
                SessionAttendance.session_id == session_id,
                SessionAttendance.student_id == sid,
            )
        )).scalar_one_or_none()
        if not att:
            att = SessionAttendance(session_id=session_id, student_id=sid)
            db.add(att)
        # V3.9.12: detectar la transición a "ausente" para avisar al estudiante
        was_absent = (att.state == AttendanceState.absent)
        if r.get("state"):
            try:
                att.state = AttendanceState(r["state"])
            except ValueError:
                continue
        if "notes" in r:
            att.notes = r["notes"]
        att.recorded_at = now
        # V3.9.12: si quedó marcado como ausente y ANTES no lo estaba, avisar (una sola vez)
        if att.state == AttendanceState.absent and not was_absent:
            # Evitar duplicar el aviso para esta misma clase
            existing = (await db.execute(
                select(Notification).where(
                    Notification.user_id == sid,
                    Notification.link == f"absence:{session_id}",
                )
            )).scalar_one_or_none()
            if not existing:
                fecha_txt = ""
                if session.starts_at_utc:
                    sa = session.starts_at_utc if session.starts_at_utc.tzinfo else session.starts_at_utc.replace(tzinfo=tz.utc)
                    fecha_txt = sa.strftime("%d/%m/%Y")
                db.add(Notification(
                    user_id=sid,
                    type=NotificationType.info,
                    title="Faltaste a tu clase",
                    body=f"No registramos tu asistencia a la clase de {session.title or 'tu curso'}"
                         + (f" del {fecha_txt}" if fecha_txt else "")
                         + ". Si crees que es un error, contáctanos.",
                    link=f"absence:{session_id}",
                ))
        updated += 1
    await log_action(db, teacher.user_id, "save_attendance", "teacher", target_id=session_id)

    # V2.1: auto-marcar sesión como completed si ya pasó
    if session.ends_at_utc:
        ends_aware = session.ends_at_utc if session.ends_at_utc.tzinfo else session.ends_at_utc.replace(tzinfo=tz.utc)
        if ends_aware < now and session.status == SessionStatus.scheduled:
            session.status = SessionStatus.completed

    await db.commit()

    # V1.3: recomputar progreso de módulo para todos los estudiantes presentes
    # V1.7: solo si la clase counts_for_progress (default True)
    counts = getattr(session, "counts_for_progress", True)
    if session.module_id and counts:
        from app.models import ModuleProgress
        for r in records:
            sid = r.get("student_id")
            state = r.get("state")
            if not sid or state != "present":
                continue
            # V3.9.56 — El progreso pertenece a la MATRÍCULA del estudiante en
            # ese nivel. Si repite, la nueva no hereda la anterior.
            _enr_att = (await db.execute(
                select(Enrollment).where(
                    Enrollment.student_id == sid,
                    Enrollment.level_id == session.level_id,
                    Enrollment.is_active.is_(True),
                ).order_by(Enrollment.enrolled_at.desc()).limit(1)
            )).scalar_one_or_none()

            mp = (await db.execute(
                select(ModuleProgress).where(
                    ModuleProgress.student_id == sid,
                    ModuleProgress.module_id == session.module_id,
                    # V3.9.57 — Solo de esta matrícula
                    ModuleProgress.enrollment_id == (_enr_att.id if _enr_att else None),
                ).limit(1)
            )).scalar_one_or_none()
            if not mp:
                mp = ModuleProgress(
                    student_id=sid, module_id=session.module_id,
                    status="in_progress",
                    enrollment_id=_enr_att.id if _enr_att else None,
                )
                db.add(mp)
            # V3.9.57 — El legacy NO se adopta: se conserva como estaba.
            mp.attended_count = (mp.attended_count or 0) + 1
            # V3.9.54 — ELIMINADA la regla `attended >= 1 → completed`.
            #
            # Asistir a una clase del módulo NO lo completa: solo demuestra
            # que empezó. El estado real lo calcula `estado_de_modulo()` en
            # progression.py mirando asistencia, tareas y quizzes del módulo.
            #
            # Aquí solo se registra el hecho: vino a una clase.
            if mp.status in (None, "locked"):
                mp.status = "in_progress"
        await db.commit()

    return {"ok": True, "updated": updated}


@router.get("/pending-attendance")
async def pending_attendance(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.22: Clases del profe YA TERMINADAS (últimos 7 días) que aún NO
    tienen asistencia registrada. Para el bloque '⏳ Clases sin asistencia' —
    el profe termina su clase y la encuentra ahí arriba, con acceso directo,
    sin buscar entre las pasadas. Al pasar la lista, desaparece del bloque."""
    now = datetime.now(tz.utc)
    week_ago = now - timedelta(days=7)
    q = select(ClassSession).where(
        ClassSession.teacher_id == teacher.user_id,
        ClassSession.ends_at_utc <= now,
        ClassSession.ends_at_utc >= week_ago,
        ClassSession.status != SessionStatus.cancelled,
    ).order_by(ClassSession.ends_at_utc.desc())
    sessions = (await db.execute(q)).scalars().all()

    # Cuáles ya tienen asistencia registrada
    ids = [s.id for s in sessions]
    att_ids = set()
    if ids:
        att_rows = (await db.execute(
            select(SessionAttendance.session_id).where(
                SessionAttendance.session_id.in_(ids),
                SessionAttendance.state.is_not(None),
            ).distinct()
        )).all()
        att_ids = {x for (x,) in att_rows}

    # Cuáles son clases de prueba (para la etiqueta)
    trial_ids = set()
    if ids:
        t_rows = (await db.execute(
            select(TrialClass.session_id).where(TrialClass.session_id.in_(ids))
        )).all()
        trial_ids = {x for (x,) in t_rows if x}

    out = []
    for s in sessions:
        if s.id in att_ids:
            continue  # ya tiene lista pasada
        out.append({
            "id": s.id, "title": s.title,
            "starts_at_utc": s.starts_at_utc.isoformat() if s.starts_at_utc else None,
            "is_trial": s.id in trial_ids,
            "is_open_event": bool(s.is_open_event),
            "is_private": s.student_id is not None,
        })
    return {"items": out, "count": len(out)}


@router.post("/sessions/{session_id}/finalize")
async def finalize_session(
    session_id: str,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.19: Finalizar una clase manualmente. Disponible para CUALQUIER clase
    del profe (normal, evento, prueba) que esté en curso o ya pasada. Marca la
    sesión como completada — deja de mostrarse "EN CURSO" de inmediato, sin
    esperar la hora de fin programada.
    Se puede finalizar sin haber pasado lista: la respuesta indica si falta
    asistencia para que el frontend recuerde pasarla."""
    session = await db.get(ClassSession, session_id)
    if not session:
        raise HTTPException(404, "Sesión no encontrada")
    if teacher.role == "teacher" and session.teacher_id != teacher.user_id:
        raise HTTPException(403, "No es tu sesión")
    if session.status == SessionStatus.cancelled:
        raise HTTPException(400, "La clase está cancelada, no se puede finalizar")
    now = datetime.now(tz.utc)
    starts = session.starts_at_utc
    if starts and starts.tzinfo is None:
        starts = starts.replace(tzinfo=tz.utc)
    if starts and starts > now:
        raise HTTPException(400, "La clase aún no ha empezado")

    session.status = SessionStatus.completed

    # ¿Se pasó asistencia? (para el recordatorio del frontend)
    att_count = (await db.execute(
        select(func.count()).select_from(SessionAttendance).where(
            SessionAttendance.session_id == session_id,
            SessionAttendance.state.is_not(None),
        )
    )).scalar() or 0

    await db.commit()
    return {"ok": True, "attendance_taken": att_count > 0, "attendance_count": att_count}


@router.get("/assignments")
async def list_assignments(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Assignment)
    if teacher.role == "teacher":
        stmt = stmt.where(Assignment.teacher_id == teacher.user_id)
    items = (await db.execute(stmt.order_by(Assignment.created_at.desc()))).scalars().all()
    out = []
    for a in items:
        submitted = (await db.execute(
            select(func.count()).select_from(AssignmentSubmission).where(
                AssignmentSubmission.assignment_id == a.id,
                AssignmentSubmission.submitted_at.is_not(None),
            )
        )).scalar() or 0
        graded = (await db.execute(
            select(func.count()).select_from(AssignmentSubmission).where(
                AssignmentSubmission.assignment_id == a.id,
                AssignmentSubmission.graded_at.is_not(None),
            )
        )).scalar() or 0
        out.append({
            "id": a.id, "title": a.title, "description": a.description,
            "max_score": float(a.max_score),
            "due_at": a.due_at.isoformat() if a.due_at else None,
            "kind": (a.kind.value if a.kind else "written"),  # V3.9.33
            "series_id": getattr(a, "series_id", None),  # V3.9.45
            "media_url": a.media_url,
            "blanks": _leer_blanks(a.blanks_json),
            "level_id": a.level_id,
            "submitted": submitted, "graded": graded,
        })
    return out


@router.post("/assignments", status_code=201)
async def create_assignment(
    body: dict,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    if not body.get("title"):
        raise HTTPException(400, "title requerido")
    # V3.9.47 SEGURIDAD — El series_id que llega NO se acepta a ciegas: se
    # comprueba que ese grupo sea de este profesor. Antes bastaba con conocer
    # el ID de un grupo ajeno para dirigirle contenido.
    from app.services.teacher_permissions import exigir_grupo_propio
    await exigir_grupo_propio(db, teacher.user_id, body.get("series_id"))

    a = Assignment(
        title=body["title"], description=body.get("description"),
        instructions=body.get("instructions"),
        teacher_id=teacher.user_id,
        level_id=body.get("level_id"),
        lesson_id=body.get("lesson_id"),
        max_score=body.get("max_score", 100.0),
        due_at=datetime.fromisoformat(body["due_at"].replace("Z", "+00:00")) if body.get("due_at") else None,
        # V3.9.45 — A qué grupo va. Si no se indica, va a todos los del
        # profesor en ese nivel (como se comportaba antes).
        series_id=(body.get("series_id") or None),
        # V3.9.33 — Tipo de tarea (escrita, audio, escuchar, completar...)
        kind=_tipo_tarea(body.get("kind")),
        media_url=(body.get("media_url") or "").strip() or None,
        blanks_json=_blanks_json(body.get("blanks")),
    )
    db.add(a)
    await db.flush()

    # V3.9.46 P1 — Los avisos usan la MISMA audiencia que el recurso. Antes
    # se avisaba a todos los inscritos del nivel: si la tarea era para el
    # grupo de la mañana, el de la noche también recibía el aviso.
    if a.level_id:
        from app.services.audience import destinatarios_de_actividad
        students = await destinatarios_de_actividad(db, a)
        for sid in students:
            db.add(Notification(
                user_id=sid, type=NotificationType.new_assignment,
                title=f"Nueva tarea: {a.title}",
                body=a.description or "Revisa el detalle en la sección Tareas.",
                link=f"/dashboard/student/assignments",
            ))

    await log_action(db, teacher.user_id, "create_assignment", "teacher", target_id=str(a.id))
    await db.commit()
    await db.refresh(a)
    return {"id": a.id, "title": a.title}


@router.get("/assignments/{assignment_id}/submissions")
async def list_submissions(
    assignment_id: int,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    a = await db.get(Assignment, assignment_id)
    if not a:
        raise HTTPException(404)

    # V3.9.50 — Autorización CENTRAL. Antes comparaba `a.teacher_id` a mano,
    # así que tras una transferencia permanente de grupo el nuevo profesor
    # responsable NO podía ver las entregas de las tareas que creó el
    # anterior. La función central sí distingue creador de responsable actual.
    from app.services.teacher_permissions import exigir_actividad_propia
    await exigir_actividad_propia(db, teacher.user_id, a, "tarea")

    # V3.9.50 — SOLO ENTREGAS REALES.
    #
    # Desde P2 existen filas de AssignmentSubmission creadas solo para el
    # seguimiento (marcar que vio o empezó la tarea), sin `submitted_at`.
    # "Existe fila" NO significa "entregó": esta pantalla es de entregas, así
    # que exige `submitted_at`.
    stmt = (
        select(AssignmentSubmission, User)
        .join(User, AssignmentSubmission.student_id == User.id)
        .where(
            AssignmentSubmission.assignment_id == assignment_id,
            AssignmentSubmission.submitted_at.is_not(None),
        )
    )
    rows = (await db.execute(stmt)).all()
    return [{
        "id": s.id, "student_id": u.id, "student_name": u.full_name,
        "submitted_at": s.submitted_at.isoformat() if s.submitted_at else None,
        "content": s.content, "file_url": s.file_url, "file_name": s.file_name,
        "score": float(s.score) if s.score else None,
        "feedback": s.feedback,
        "graded_at": s.graded_at.isoformat() if s.graded_at else None,
    } for s, u in rows]


@router.post("/submissions/{submission_id}/grade")
async def grade_submission(
    submission_id: str, body: dict,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    sub = await db.get(AssignmentSubmission, submission_id)
    if not sub:
        raise HTTPException(404)
    a = await db.get(Assignment, sub.assignment_id)
    if not a:
        raise HTTPException(404)

    # V3.9.50 — Autorización central: el profesor que RECIBE un grupo puede
    # calificar las tareas que dejó pendientes el anterior, sin que eso
    # borre a quien la creó. Un sustituto de sesión NO obtiene este permiso.
    from app.services.teacher_permissions import exigir_actividad_propia
    await exigir_actividad_propia(db, teacher.user_id, a, "tarea")

    # V3.9.50 — No se puede calificar lo que no se entregó (las filas de
    # seguimiento no son entregas)
    if not sub.submitted_at:
        raise HTTPException(400, "Ese estudiante todavía no ha entregado")
    # V3.9.20 FIX: validar la nota — antes aceptaba cualquier body y podía marcar
    # "calificado" sin nota (y notificar "Tu calificación es None")
    if body.get("score") is None:
        raise HTTPException(400, "Falta la nota (score)")
    try:
        score_val = float(body["score"])
    except (TypeError, ValueError):
        raise HTTPException(400, "La nota debe ser un número")
    if score_val < 0 or (a.max_score and score_val > float(a.max_score)):
        raise HTTPException(400, f"La nota debe estar entre 0 y {a.max_score}")
    sub.score = score_val
    sub.feedback = body.get("feedback")
    sub.graded_at = datetime.now(tz.utc)

    # Notificar al estudiante
    db.add(Notification(
        user_id=sub.student_id, type=NotificationType.grade_published,
        title=f"Calificación publicada: {a.title}",
        body=f"Tu calificación es {sub.score}/{a.max_score}",
        link="/dashboard/student/assignments",
    ))
    await log_action(db, teacher.user_id, "grade_submission", "teacher", target_id=submission_id)
    await db.commit()
    return {"ok": True}


# === QUIZZES ===
@router.get("/quizzes")
async def list_quizzes(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Quiz)
    if teacher.role == "teacher":
        stmt = stmt.where(Quiz.teacher_id == teacher.user_id)
    items = (await db.execute(stmt.order_by(Quiz.created_at.desc()))).scalars().all()
    out = []
    for q in items:
        question_count = (await db.execute(
            select(func.count()).select_from(QuizQuestion).where(QuizQuestion.quiz_id == q.id)
        )).scalar() or 0
        attempts = (await db.execute(
            select(func.count()).select_from(QuizAttempt).where(
                QuizAttempt.quiz_id == q.id, QuizAttempt.submitted_at.is_not(None),
            )
        )).scalar() or 0
        out.append({
            "id": q.id, "title": q.title, "description": q.description,
            "passing_score": float(q.passing_score),
            "level_id": q.level_id, "is_published": q.is_published,
            "question_count": question_count, "attempts": attempts,
        })
    return out


@router.post("/quizzes", status_code=201)
async def create_quiz(
    body: dict,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """body = {title, description, level_id, passing_score, questions: [{type, statement, options, correct_answer, points}]}"""
    if not body.get("title"):
        raise HTTPException(400, "title requerido")
    # V3.9.47 SEGURIDAD — mismo control para los quizzes
    from app.services.teacher_permissions import exigir_grupo_propio
    await exigir_grupo_propio(db, teacher.user_id, body.get("series_id"))

    q = Quiz(
        title=body["title"], description=body.get("description"),
        teacher_id=teacher.user_id, level_id=body.get("level_id"),
        # V3.9.45 — A qué grupo va este quiz
        series_id=(body.get("series_id") or None),
        passing_score=body.get("passing_score", 60.0),
        max_attempts=body.get("max_attempts", 3),
    )
    db.add(q)
    await db.flush()
    questions = body.get("questions", [])
    for i, qq in enumerate(questions):
        db.add(QuizQuestion(
            quiz_id=q.id,
            type=QuestionType(qq.get("type", "multiple_choice")),
            statement=qq.get("statement", ""),
            options=qq.get("options"),
            correct_answer=str(qq.get("correct_answer", "")),
            points=qq.get("points", 10.0),
            order_index=i,
        ))
    await log_action(db, teacher.user_id, "create_quiz", "teacher", target_id=str(q.id))
    await db.commit()

    # V3.9.34 — Avisar a los estudiantes del nivel (el quiz nace publicado)
    avisados = 0
    if q.is_published and len(questions) > 0:
        try:
            avisados = await _avisar_quiz_publicado(db, q, len(questions))
            await db.commit()
        except Exception:
            pass
    return {"id": q.id, "notified": avisados}


# === MATERIALES ===
@router.get("/materials")
async def list_materials(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    # V3.9.47 SEGURIDAD — Antes el listado traía TODOS los materiales: un
    # profesor veía el material privado que otro había subido para un
    # estudiante suyo, con su URL incluida.
    from app.services.teacher_permissions import (
        es_admin, grupos_del_profesor, filtro_materiales_del_profesor,
    )
    if await es_admin(db, teacher.user_id):
        stmt = select(Material)
    else:
        _grupos = await grupos_del_profesor(db, teacher.user_id)
        stmt = select(Material).where(
            filtro_materiales_del_profesor(Material, teacher.user_id, _grupos)
        )
    items = (await db.execute(stmt)).scalars().all()
    return [{
        "id": m.id, "title": m.title, "description": m.description,
        "type": m.type.value, "url": m.url,
        "course_id": m.course_id, "level_id": m.level_id,
        "module_id": m.module_id, "lesson_id": m.lesson_id,
        "is_public": m.is_public,
    } for m in items]


@router.post("/materials", status_code=201)
async def upload_material(
    body: dict,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    if not body.get("title") or not body.get("url") or not body.get("type"):
        raise HTTPException(400, "title, url y type son requeridos")
    # V3.9.47 SEGURIDAD — tres controles antes de guardar:
    #   · el grupo debe ser suyo
    #   · el estudiante debe ser suyo (material individual)
    #   · "institucional" es contenido oficial: solo el admin
    from app.services.teacher_permissions import (
        exigir_grupo_propio, exigir_estudiante_propio, resolver_audiencia_material,
    )
    await exigir_grupo_propio(db, teacher.user_id, body.get("series_id"))
    await exigir_estudiante_propio(db, teacher.user_id, body.get("student_id"))
    _aud = await resolver_audiencia_material(db, teacher.user_id, body)

    m = Material(
        title=body["title"], description=body.get("description"),
        type=MaterialType(body["type"]), url=body["url"],
        course_id=body.get("course_id"), level_id=body.get("level_id"),
        module_id=body.get("module_id"), lesson_id=body.get("lesson_id"),
        uploaded_by=teacher.user_id,
        is_public=body.get("is_public", True),
        # V3.9.46 P1 — A quién va este material.
        #   "teacher" (por defecto del profesor): a sus estudiantes, o solo a
        #             un grupo si se indica series_id
        #   "student": a un estudiante concreto (feedback, refuerzo)
        #   "institutional": material de Dorismon para todo el nivel/curso
        audience_kind=_aud["audience_kind"],
        series_id=_aud["series_id"],
        student_id=_aud["student_id"],
    )
    db.add(m)
    await log_action(db, teacher.user_id, "upload_material", "teacher", target_id=str(m.id))
    await db.commit()
    return {"id": m.id}


# === OBSERVACIONES ===
@router.get("/observations/{student_id}")
async def list_observations(
    student_id: str,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    # V3.9.50 SEGURIDAD — Antes se podían LEER las observaciones de un
    # estudiante ajeno con solo conocer su ID. El POST ya estaba protegido;
    # el GET no. Leer información privada es tan grave como escribirla.
    from app.services.teacher_permissions import exigir_estudiante_propio
    await exigir_estudiante_propio(db, teacher.user_id, student_id)

    items = (await db.execute(
        select(Observation, User)
        .join(User, Observation.teacher_id == User.id)
        .where(Observation.student_id == student_id)
        .order_by(Observation.created_at.desc())
    )).all()
    return [{
        "id": o.id, "content": o.content, "is_private": o.is_private,
        "teacher_name": u.full_name,
        "created_at": o.created_at.isoformat(),
    } for o, u in items]


@router.post("/observations/{student_id}", status_code=201)
async def add_observation(
    student_id: str, body: dict,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    if not body.get("content"):
        raise HTTPException(400, "content requerido")
    # V3.9.47 SEGURIDAD — Antes cualquier profesor podía dejar una
    # observación sobre un estudiante que no era suyo.
    from app.services.teacher_permissions import exigir_estudiante_propio
    await exigir_estudiante_propio(db, teacher.user_id, student_id)

    o = Observation(
        student_id=student_id, teacher_id=teacher.user_id,
        content=body["content"], is_private=body.get("is_private", True),
    )
    db.add(o)
    await log_action(db, teacher.user_id, "add_observation", "teacher", target_id=student_id)
    await db.commit()
    return {"id": o.id}


@router.post("/sessions/{session_id}/notes")
async def save_session_notes(
    session_id: str, body: dict,
    teacher: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Profe guarda notas para los estudiantes después de una clase."""
    if teacher.role != "teacher":
        raise HTTPException(403)
    s = await db.get(ClassSession, session_id)
    if not s: raise HTTPException(404)
    if s.teacher_id != teacher.user_id:
        raise HTTPException(403, "No sos el profe de esta clase")
    s.teacher_notes = body.get("notes", "")
    # Notificar a estudiantes que asistieron
    if s.teacher_notes:
        attendees = (await db.execute(
            select(SessionAttendance).where(
                SessionAttendance.session_id == session_id,
                SessionAttendance.state == AttendanceState.present,
            )
        )).scalars().all()
        for a in attendees:
            db.add(Notification(
                user_id=a.student_id,
                type=NotificationType.info,
                title=f"📝 Nota del profesor: {s.title}",
                body=s.teacher_notes[:140] + ("..." if len(s.teacher_notes) > 140 else ""),
                link="/dashboard/student",
            ))
    await db.commit()
    return {"ok": True}


# ============= V1.5 — MIS ESTUDIANTES =============
@router.get("/my-students")
async def teacher_my_students(
    teacher: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V1.5: Estudiantes asignados a este profesor (vía Enrollment.teacher_id).

    Si el profe no tiene enrollments con teacher_id=él, devolvemos lista vacía.
    Para que un estudiante aparezca, el admin debe asignar el profe en la inscripción.
    """
    if teacher.role != "teacher":
        raise HTTPException(403)

    rows = (await db.execute(
        select(Enrollment, User, Student, Course, Level)
        .join(User, Enrollment.student_id == User.id)
        .join(Student, Enrollment.student_id == Student.user_id)
        .join(Course, Enrollment.course_id == Course.id)
        .join(Level, Enrollment.level_id == Level.id)
        .where(
            Enrollment.teacher_id == teacher.user_id,
            Enrollment.is_active.is_(True),
        )
    )).all()

    out = []
    for enr, u, st, course, level in rows:
        # Asistencia % del estudiante en clases pasadas del profe
        from datetime import timezone as tz
        att_rows = (await db.execute(
            select(SessionAttendance.state)
            .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
            .where(
                SessionAttendance.student_id == u.id,
                ClassSession.teacher_id == teacher.user_id,
                ClassSession.starts_at_utc < datetime.now(tz.utc),
            )
        )).all()
        total_att = len(att_rows)
        present = sum(1 for (st_state,) in att_rows if st_state == AttendanceState.present)
        attendance_pct = round((present / total_att) * 100, 1) if total_att > 0 else None

        out.append({
            "student_id": u.id,
            "full_name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "course_name": course.name,
            "level_code": level.code,
            "level_name": level.name,
            "enrolled_at": enr.enrolled_at.isoformat() if enr.enrolled_at else None,
            "is_paused": st.is_paused,
            "attendance_pct": attendance_pct,
            "total_classes_with_me": total_att,
        })
    return out


@router.get("/my-students-by-level")
async def teacher_students_by_level(
    teacher: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V1.5: Estudiantes del profe agrupados por nivel."""
    if teacher.role != "teacher":
        raise HTTPException(403)

    rows = (await db.execute(
        select(Enrollment, User, Level)
        .join(User, Enrollment.student_id == User.id)
        .join(Level, Enrollment.level_id == Level.id)
        .where(
            Enrollment.teacher_id == teacher.user_id,
            Enrollment.is_active.is_(True),
        )
    )).all()

    by_level: dict = {}
    for enr, u, level in rows:
        key = (level.id, level.code, level.name)
        by_level.setdefault(key, []).append({
            "id": u.id, "full_name": u.full_name, "email": u.email,
        })
    return [
        {"level_id": k[0], "level_code": k[1], "level_name": k[2], "students": v, "count": len(v)}
        for k, v in by_level.items()
    ]


# ============= V1.9 — INGRESOS DEL PROFESOR =============

@router.get("/income")
async def teacher_income(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
    year: int | None = None,
    month: int | None = None,
):
    """V1.9: Lo que el profe ganó/va a ganar en un período."""
    # Reusamos el helper de admin
    from app.routers.admin import _calculate_teacher_period
    now = datetime.now(tz.utc)
    y = year or now.year
    m = month or now.month

    period = await _calculate_teacher_period(db, teacher.user_id, y, m)
    if not period:
        raise HTTPException(404, "Datos no disponibles")

    u = await db.get(User, teacher.user_id)
    period["teacher_name"] = u.full_name if u else "—"

    # No revelamos al profe el classes_detail con detalles de pago de OTROS profes, pero acá es del propio profe → OK
    return period


@router.get("/income-history")
async def teacher_income_history(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.9: Historial de pagos recibidos."""
    from app.models import TeacherPayment
    payments = (await db.execute(
        select(TeacherPayment).where(TeacherPayment.teacher_id == teacher.user_id)
        .order_by(TeacherPayment.period_year.desc(), TeacherPayment.period_month.desc())
    )).scalars().all()
    return [{
        "id": p.id,
        "period_year": p.period_year,
        "period_month": p.period_month,
        "classes_count": p.classes_count,
        "group_count": p.group_count,
        "private_count": p.private_count,
        "event_count": p.event_count,
        "total_amount": p.total_amount,
        "currency": p.currency,
        "payment_method": p.payment_method,
        "reference": p.reference,
        "paid_at": p.paid_at.isoformat() if p.paid_at else None,
    } for p in payments]


# ============= V2.9 — PROFE CANCELA SU CLASE =============

from pydantic import BaseModel, Field


class CancelSessionRequest(BaseModel):
    reason: str = Field(min_length=20, max_length=500,
                        description="Motivo de la cancelación (mínimo 20 chars)")


@router.post("/sessions/{session_id}/cancel")
async def cancel_my_session(
    session_id: str,
    body: CancelSessionRequest,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.9: Profe cancela su propia clase.

    Reglas:
    - Solo el profe asignado a la clase puede cancelarla (admin también)
    - Mínimo 2 horas de anticipación al starts_at_utc
    - Solo sesiones con status=scheduled
    - Marca la sesión como cancelled + guarda motivo + cancelled_by + cancelled_at
    - Notifica a TODOS los estudiantes inscritos (in-app + email)
    - Notifica al admin (in-app)
    """
    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")

    # Verificar permisos: el profe debe ser el asignado, o admin
    requester = await db.get(User, teacher.user_id)
    if not requester:
        raise HTTPException(401, "No autenticado")
    is_admin = requester.role.value == "super_admin"
    if not is_admin and s.teacher_id != teacher.user_id:
        raise HTTPException(403, "Solo el profe asignado puede cancelar esta clase")

    # Verificar status
    if s.status != SessionStatus.scheduled:
        raise HTTPException(400, f"Esta clase ya está en estado '{s.status.value}', no se puede cancelar")

    # Verificar anticipación mínima (admin puede saltar esta regla)
    now = datetime.now(tz.utc)
    if not is_admin:
        # SQLite devuelve datetime naive — normalizar a UTC para comparar
        starts = s.starts_at_utc
        if starts.tzinfo is None:
            starts = starts.replace(tzinfo=tz.utc)
        diff = (starts - now).total_seconds() / 3600.0
        if diff < 2:
            raise HTTPException(
                400,
                f"Debes cancelar con al menos 2 horas de anticipación. "
                f"Esta clase es en {diff:.1f} horas. Contacta al admin si es urgente.",
            )

    # Marcar como cancelada
    s.status = SessionStatus.cancelled
    s.cancellation_reason = body.reason.strip()
    s.cancelled_by_user_id = teacher.user_id
    s.cancelled_at = now

    # === Notificar a estudiantes inscritos ===
    starts_for_msg = s.starts_at_utc if s.starts_at_utc.tzinfo else s.starts_at_utc.replace(tzinfo=tz.utc)
    when_local = starts_for_msg.strftime("%d/%m/%Y a las %H:%M UTC")
    # Encontrar estudiantes con asistencia o inscripción en esa clase
    # 1. Por asistencias ya registradas (estudiantes que ya marcaron presente)
    attended = (await db.execute(
        select(SessionAttendance.student_id).where(SessionAttendance.session_id == session_id)
    )).scalars().all()
    student_ids: set[str] = set(attended)

    # 2. Si la clase tiene student_id (privada), agregarlo
    if s.student_id:
        student_ids.add(s.student_id)

    # 3. Si la clase es grupal: los de SU audiencia.
    # V3.9.46 P1 — Antes se avisaba a todos los del curso+nivel: al cancelar
    # una clase del grupo de la mañana, el de la noche también recibía el
    # aviso de una cancelación que no le afectaba.
    if not s.student_id:  # clase grupal
        from app.services.audience import destinatarios_de_clase
        for sid in await destinatarios_de_clase(db, s):
            student_ids.add(sid)

    # Crear notificaciones in-app
    when_local = s.starts_at_utc.strftime("%d/%m/%Y a las %H:%M UTC")
    teacher_name = requester.full_name
    title = "Clase cancelada"
    msg_short = f"Tu clase '{s.title}' del {when_local} fue cancelada por {teacher_name}. Motivo: {body.reason[:120]}"

    for sid in student_ids:
        db.add(Notification(
            user_id=sid,
            type=NotificationType.class_cancelled if hasattr(NotificationType, "class_cancelled") else NotificationType.general,
            title=title,
            body=msg_short,
            link=f"/dashboard/student/sessions/{session_id}",
        ))

    # Notificar a admins
    admins = (await db.execute(
        select(User.id).where(User.role == UserRole.super_admin, User.is_active.is_(True))
    )).scalars().all()
    for aid in admins:
        db.add(Notification(
            user_id=aid,
            type=NotificationType.general,
            title="Profesor canceló una clase",
            body=f"{teacher_name} canceló la clase '{s.title}' del {when_local}. Motivo: {body.reason[:100]}",
            link=f"/dashboard/admin/sessions/{session_id}",
        ))

    # Email a estudiantes (solo a los que tienen email verificado)
    try:
        from app.services.email_service import send_class_cancelled_email
        for sid in student_ids:
            stu = await db.get(User, sid)
            if stu and stu.email_verified and stu.is_active:
                await send_class_cancelled_email(
                    to_email=stu.email,
                    student_name=stu.full_name,
                    class_title=s.title,
                    when_local=when_local,
                    teacher_name=teacher_name,
                    reason=body.reason,
                )
    except Exception:
        # No bloquear el endpoint si el email falla
        pass

    await log_action(
        db, teacher.user_id, "cancel_session", "teacher",
        target_id=session_id,
        details=f"reason={body.reason[:80]} students_notified={len(student_ids)}",
    )
    await db.commit()
    return {
        "ok": True,
        "session_id": session_id,
        "students_notified": len(student_ids),
        "status": "cancelled",
    }


@router.get("/my-levels")
async def my_levels(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.33 — Los niveles donde este profesor da clase.

    Sirve para saber dónde puede publicar un quiz o una tarea sin tener que
    buscar entre todos los niveles del instituto.
    """
    filas = (await db.execute(
        select(Level, Course)
        .join(Course, Level.course_id == Course.id)
        .join(ClassSession, ClassSession.level_id == Level.id)
        .where(ClassSession.teacher_id == teacher.user_id)
        .distinct()
        .order_by(Level.order_index)
    )).all()

    # Si aún no tiene clases asignadas, mostrar todos (para no bloquearlo)
    if not filas:
        filas = (await db.execute(
            select(Level, Course)
            .join(Course, Level.course_id == Course.id)
            .order_by(Level.order_index)
        )).all()

    return {"items": [{
        "id": l.id, "code": l.code, "name": l.name,
        "course_id": c.id, "course_name": c.name,
    } for l, c in filas]}


@router.post("/quizzes/{quiz_id}/publish")
async def publish_quiz(
    quiz_id: int,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.34 — Publicar un quiz y avisarle a los estudiantes.

    Útil cuando el quiz se guardó sin publicar (por ejemplo, los que genera
    la IA, que quedan esperando revisión del profesor).
    """
    q = await db.get(Quiz, quiz_id)
    if not q:
        raise HTTPException(404, "Quiz no encontrado")
    # V3.9.47 SEGURIDAD — Antes cualquier profesor podía publicar o
    # despublicar el quiz de otro con solo conocer el ID.
    from app.services.teacher_permissions import exigir_actividad_propia
    await exigir_actividad_propia(db, teacher.user_id, q, "quiz")

    preguntas = (await db.execute(
        select(func.count()).select_from(QuizQuestion).where(QuizQuestion.quiz_id == quiz_id)
    )).scalar() or 0
    if preguntas == 0:
        raise HTTPException(400, "El quiz no tiene preguntas todavía")

    ya_estaba = q.is_published
    q.is_published = True

    # Si ya estaba publicado, se avisa igual (sirve como recordatorio)
    avisados = await _avisar_quiz_publicado(db, q, preguntas)
    await db.commit()
    return {
        "ok": True, "published": True, "notified": avisados,
        "questions": preguntas, "was_published": ya_estaba,
    }


@router.post("/quizzes/{quiz_id}/unpublish")
async def unpublish_quiz(
    quiz_id: int,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Despublicar un quiz (por si se publicó por error). No borra nada."""
    q = await db.get(Quiz, quiz_id)
    if not q:
        raise HTTPException(404, "Quiz no encontrado")
    # V3.9.47 SEGURIDAD — igual que publicar
    from app.services.teacher_permissions import exigir_actividad_propia
    await exigir_actividad_propia(db, teacher.user_id, q, "quiz")
    q.is_published = False
    await db.commit()
    return {"ok": True, "published": False}


@router.get("/my-groups")
async def my_groups(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.48 — Los grupos de los que este profesor es RESPONSABLE.

    POR QUÉ EXISTE: el selector de audiencia del panel llamaba a
    `/admin/groups`, que exige rol de admin. El profesor recibía 403, el
    componente lo convertía en lista vacía, y le decía "No hay grupos" aunque
    sí los tuviera.

    Devuelve SOLO sus grupos. Un profesor nunca necesita ver los de otros.

    ⚠️ Sustituir una sesión NO hace aparecer aquí el grupo del titular: eso
    se autoriza por sesión, no por grupo.
    """
    from app.services.teacher_permissions import es_admin, grupos_del_profesor
    from app.models import ClassSeries

    ahora = datetime.now(tz.utc)

    if await es_admin(db, teacher.user_id):
        series = (await db.execute(
            select(ClassSeries).where(ClassSeries.is_active.is_(True))
            .order_by(ClassSeries.created_at.desc())
        )).scalars().all()
    else:
        mios = await grupos_del_profesor(db, teacher.user_id)
        if not mios:
            return {"items": []}
        series = (await db.execute(
            select(ClassSeries).where(
                ClassSeries.id.in_(mios),
                ClassSeries.is_active.is_(True),
            ).order_by(ClassSeries.created_at.desc())
        )).scalars().all()

    out = []
    for s in series:
        inscritos = (await db.execute(
            select(func.count()).select_from(Enrollment).where(
                Enrollment.series_id == s.id,
                Enrollment.is_active.is_(True),
            )
        )).scalar() or 0
        futuras = (await db.execute(
            select(func.count()).select_from(ClassSession).where(
                ClassSession.series_id == s.id,
                ClassSession.starts_at_utc >= ahora,
                ClassSession.status != SessionStatus.cancelled,
            )
        )).scalar() or 0
        nivel = await db.get(Level, s.level_id) if s.level_id else None

        out.append({
            "id": s.id,
            "name": s.name,
            "level_id": s.level_id,
            "level_code": nivel.code if nivel else None,
            "course_id": s.course_id,
            "days_of_week": s.days_of_week,
            "start_time_hhmm": s.start_time_hhmm,
            "modality": s.modality.value if s.modality else None,
            "students": inscritos,
            "capacity": s.capacity or 6,
            "upcoming_classes": futuras,
        })
    return {"items": out}


# ============================================================================
# V3.9.49 P2 — SEGUIMIENTO ACADÉMICO
# ============================================================================

@router.get("/assignments/{assignment_id}/tracking")
async def assignment_tracking(
    assignment_id: int,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Quién entregó y quién NO, sobre el roster real de la tarea.

    ANTES solo se listaban las entregas existentes: quien no entregaba
    simplemente no aparecía, y no había forma de saber a quién le faltaba.
    """
    from app.services.tracking import seguimiento_de_tarea
    from app.services.teacher_permissions import exigir_actividad_propia

    a = await db.get(Assignment, assignment_id)
    if not a:
        raise HTTPException(404, "Tarea no encontrada")
    await exigir_actividad_propia(db, teacher.user_id, a, "tarea")

    datos = await seguimiento_de_tarea(db, a)
    return {
        "assignment": {
            "id": a.id, "title": a.title,
            "due_at": a.due_at.isoformat() if a.due_at else None,
            "max_score": float(a.max_score) if getattr(a, "max_score", None) else 100.0,
            "series_id": getattr(a, "series_id", None),
        },
        **datos,
    }


@router.get("/quizzes/{quiz_id}/tracking")
async def quiz_tracking(
    quiz_id: int,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Quién hizo el quiz, con qué nota, y quién no lo ha intentado."""
    from app.services.tracking import seguimiento_de_quiz
    from app.services.teacher_permissions import exigir_actividad_propia

    q = await db.get(Quiz, quiz_id)
    if not q:
        raise HTTPException(404, "Quiz no encontrado")
    await exigir_actividad_propia(db, teacher.user_id, q, "quiz")

    datos = await seguimiento_de_quiz(db, q)
    return {
        "quiz": {
            "id": q.id, "title": q.title,
            "max_attempts": q.max_attempts,
            "passing_score": float(q.passing_score or 60),
            "is_published": q.is_published,
        },
        **datos,
    }


@router.post("/assignments/{assignment_id}/remind")
async def remind_pending(
    assignment_id: int,
    body: dict,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Recordarle la tarea a quienes no la han entregado.

    Como pediste: por notificación DENTRO de la plataforma y al teléfono,
    no por WhatsApp. Se puede avisar a todos los pendientes o a uno solo.
    """
    from app.services.tracking import seguimiento_de_tarea
    from app.services.teacher_permissions import exigir_actividad_propia
    from app.services.push_service import notify_user

    a = await db.get(Assignment, assignment_id)
    if not a:
        raise HTTPException(404, "Tarea no encontrada")
    await exigir_actividad_propia(db, teacher.user_id, a, "tarea")

    datos = await seguimiento_de_tarea(db, a)
    solo = (body.get("student_id") or "").strip() or None

    pendientes = [
        x for x in datos["items"]
        if x["estado"] in ("assigned", "viewed", "in_progress", "overdue")
        and (not solo or x["student_id"] == solo)
    ]
    if not pendientes:
        return {"ok": True, "notified": 0,
                "mensaje": "No hay entregas pendientes."}

    cuando = ""
    if a.due_at:
        from zoneinfo import ZoneInfo as _ZI
        _d = a.due_at if a.due_at.tzinfo else a.due_at.replace(tzinfo=tz.utc)
        cuando = _d.astimezone(_ZI("America/Santo_Domingo")).strftime(
            " (vence el %d/%m a las %I:%M %p)").replace(" 0", " ")

    avisados = 0
    for p in pendientes:
        vencida = p["estado"] == "overdue"
        titulo = "⏰ Tarea atrasada" if vencida else "📝 Recordatorio de tarea"
        cuerpo = (
            f"'{a.title}' sigue sin entregar{cuando}."
            if vencida else
            f"No olvides entregar '{a.title}'{cuando}."
        )
        db.add(Notification(
            user_id=p["student_id"], type=NotificationType.reminder,
            title=titulo, body=cuerpo, link="/dashboard/student/assignments",
        ))
        try:
            await notify_user(db, p["student_id"], titulo, cuerpo,
                              "/dashboard/student/assignments", f"tarea:{a.id}")
        except Exception:
            pass
        avisados += 1

    await db.commit()
    return {
        "ok": True, "notified": avisados,
        "mensaje": f"Se le recordó a {avisados} estudiante{'s' if avisados != 1 else ''}.",
    }


@router.get("/pending-grading")
async def pending_grading(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Entregas esperando calificación, para el panel del profesor.

    ANTES el profesor tenía que entrar tarea por tarea para enterarse de que
    le habían entregado. No le llegaba ningún aviso.
    """
    from app.services.teacher_permissions import es_admin, grupos_del_profesor

    admin = await es_admin(db, teacher.user_id)
    cond = [
        AssignmentSubmission.submitted_at.is_not(None),
        AssignmentSubmission.score.is_(None),
    ]
    if not admin:
        grupos = await grupos_del_profesor(db, teacher.user_id)
        propias = [Assignment.teacher_id == teacher.user_id]
        if grupos:
            propias.append(Assignment.series_id.in_(grupos))
        cond.append(or_(*propias))

    filas = (await db.execute(
        select(AssignmentSubmission, Assignment, User)
        .join(Assignment, AssignmentSubmission.assignment_id == Assignment.id)
        .join(User, AssignmentSubmission.student_id == User.id)
        .where(*cond)
        .order_by(AssignmentSubmission.submitted_at)
    )).all()

    ahora = datetime.now(tz.utc)
    items = []
    for sub, a, u in filas:
        entregada = sub.submitted_at
        if entregada and entregada.tzinfo is None:
            entregada = entregada.replace(tzinfo=tz.utc)
        items.append({
            "submission_id": sub.id,
            "assignment_id": a.id, "assignment_title": a.title,
            "student_id": u.id, "student_name": u.full_name,
            "submitted_at": entregada.isoformat() if entregada else None,
            "days_waiting": (ahora - entregada).days if entregada else 0,
            "has_file": bool(sub.file_url),
            "file_url": sub.file_url, "file_name": sub.file_name,
        })

    return {
        "items": items,
        "count": len(items),
        "oldest_days": max((x["days_waiting"] for x in items), default=0),
    }


@router.get("/at-risk")
async def teacher_at_risk(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Los estudiantes del profesor que necesitan atención, con el motivo."""
    from app.services.tracking import estudiantes_en_riesgo
    from app.services.teacher_permissions import es_admin

    solo = None if await es_admin(db, teacher.user_id) else teacher.user_id
    return await estudiantes_en_riesgo(db, solo)


# ============================================================================
# V3.9.53 P3 — PROGRESIÓN ACADÉMICA (lado del profesor)
# ============================================================================

async def _puede_gestionar_enrollment(db, user_id: str, enr) -> bool:
    """¿Este profesor es el RESPONSABLE ACTUAL de esta matrícula?

    Vale el profesor asignado a la matrícula o el titular de su grupo. Un
    sustituto de una sesión NO: cubrir una clase no da derecho a decidir si
    alguien termina un nivel.
    """
    from app.services.teacher_permissions import es_admin, grupos_del_profesor

    if await es_admin(db, user_id):
        return True
    if getattr(enr, "teacher_id", None) == user_id:
        return True
    grupo = getattr(enr, "series_id", None)
    if grupo:
        return grupo in await grupos_del_profesor(db, user_id)
    return False


@router.get("/enrollments/{enrollment_id}/eligibility")
async def enrollment_eligibility(
    enrollment_id: str,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Si este estudiante está listo para terminar su nivel, y qué le falta."""
    from app.services.progression import elegibilidad_de_enrollment
    from app.models import SkillAssessment, CompletionReview

    enr = await db.get(Enrollment, enrollment_id)
    if not enr:
        raise HTTPException(404, "Matrícula no encontrada")
    if not await _puede_gestionar_enrollment(db, teacher.user_id, enr):
        raise HTTPException(404, "Matrícula no encontrada")

    datos = await elegibilidad_de_enrollment(db, enr)

    u = await db.get(User, enr.student_id)
    nivel = await db.get(Level, enr.level_id)
    curso = await db.get(Course, enr.course_id)

    # Historial de evaluaciones: no se pisan, se acumulan
    historial = [{
        "skill": s.skill, "score": float(s.score), "source": s.source,
        "notes": s.notes,
        "evaluated_at": s.evaluated_at.isoformat() if s.evaluated_at else None,
    } for s in (await db.execute(
        select(SkillAssessment)
        .where(SkillAssessment.enrollment_id == enrollment_id)
        .order_by(SkillAssessment.evaluated_at.desc())
    )).scalars().all()]

    revisiones = [{
        "recommendation": r.recommendation, "comment": r.comment,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in (await db.execute(
        select(CompletionReview)
        .where(CompletionReview.enrollment_id == enrollment_id)
        .order_by(CompletionReview.created_at.desc())
    )).scalars().all()]

    return {
        "student": {"id": u.id, "name": u.full_name} if u else None,
        "course_name": curso.name if curso else None,
        "level_code": nivel.code if nivel else None,
        "level_name": nivel.name if nivel else None,
        **datos,
        "skill_history": historial,
        "reviews": revisiones,
    }


@router.post("/enrollments/{enrollment_id}/skills", status_code=201)
async def assess_skill(
    enrollment_id: str,
    body: dict,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Evaluar una habilidad. Escala 0–100.

    Cada evaluación se AÑADE: no se sobrescribe la anterior. Así queda el
    historial de cómo fue mejorando, y la nota de B1 no se pierde cuando se
    le evalúe en B2 (son matrículas distintas).
    """
    from app.models import SkillAssessment
    from app.services.academic_config import (
        HABILIDADES_REQUERIDAS, HABILIDADES_OPCIONALES, ESCALA_MAXIMA,
    )

    enr = await db.get(Enrollment, enrollment_id)
    if not enr:
        raise HTTPException(404, "Matrícula no encontrada")
    if not await _puede_gestionar_enrollment(db, teacher.user_id, enr):
        raise HTTPException(404, "Matrícula no encontrada")

    skill = (body.get("skill") or "").strip().lower()
    validas = HABILIDADES_REQUERIDAS + HABILIDADES_OPCIONALES
    if skill not in validas:
        raise HTTPException(400, f"Habilidad no válida. Opciones: {', '.join(validas)}")

    try:
        score = float(body.get("score"))
    except (TypeError, ValueError):
        raise HTTPException(400, "La nota debe ser un número")
    if not (0 <= score <= ESCALA_MAXIMA):
        raise HTTPException(400, f"La nota va de 0 a {ESCALA_MAXIMA:.0f}")

    fuente = body.get("source") or "teacher_assessment"
    if fuente not in ("continuous", "final_exam", "teacher_assessment"):
        fuente = "teacher_assessment"

    a = SkillAssessment(
        enrollment_id=enrollment_id, student_id=enr.student_id,
        skill=skill, score=score, source=fuente,
        notes=(body.get("notes") or "").strip()[:500] or None,
        evaluated_by=teacher.user_id,
    )
    db.add(a)
    await log_action(db, teacher.user_id, "assess_skill", "progression",
                     target_id=enrollment_id, details=f"{skill}={score}")
    await db.commit()
    return {"ok": True, "id": a.id, "skill": skill, "score": score}


@router.post("/enrollments/{enrollment_id}/recommend")
async def recommend_completion(
    enrollment_id: str,
    body: dict,
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """La recomendación del profesor sobre terminar el nivel.

    ⚠️ NO completa nada. Solo pasa el caso a Dirección, que es quien aprueba
    oficialmente. El profesor conoce al estudiante; Dirección responde por el
    certificado.
    """
    from app.models import CompletionReview
    from app.services.progression import elegibilidad_de_enrollment, construir_snapshot
    from app.services.academic_config import RECOMENDACIONES, puede_pasar_a

    enr = await db.get(Enrollment, enrollment_id)
    if not enr:
        raise HTTPException(404, "Matrícula no encontrada")
    if not await _puede_gestionar_enrollment(db, teacher.user_id, enr):
        raise HTTPException(404, "Matrícula no encontrada")

    if (getattr(enr, "academic_status", "active") or "active") == "completed":
        raise HTTPException(400, "Ese nivel ya está completado")

    rec = (body.get("recommendation") or "").strip()
    if rec not in RECOMENDACIONES:
        raise HTTPException(
            400, f"Recomendación no válida. Opciones: {', '.join(RECOMENDACIONES)}")

    comentario = (body.get("comment") or "").strip()
    if not comentario:
        raise HTTPException(400, "Explica brevemente tu recomendación")

    elegib = await elegibilidad_de_enrollment(db, enr)

    r = CompletionReview(
        enrollment_id=enrollment_id, teacher_id=teacher.user_id,
        recommendation=rec, comment=comentario[:1000],
        reinforcement_area=(body.get("reinforcement_area") or "").strip() or None,
        metrics_snapshot=construir_snapshot(elegib, recomendacion=rec),
    )
    db.add(r)

    # El estado avanza según lo que recomiende
    destino = {
        "recommend_promotion": "completion_review",
        "requires_reinforcement": "requires_reinforcement",
        "requires_reevaluation": "requires_reevaluation",
    }[rec]
    actual = getattr(enr, "academic_status", "active") or "active"
    if puede_pasar_a(actual, destino):
        enr.academic_status = destino

    # Avisar a Dirección cuando hay que aprobar
    try:
        u = await db.get(User, enr.student_id)
        nivel = await db.get(Level, enr.level_id)
        if rec == "recommend_promotion":
            for a in (await db.execute(
                select(User).where(User.role == UserRole.super_admin)
            )).scalars().all():
                db.add(Notification(
                    user_id=a.id, type=NotificationType.info,
                    title="🎓 Listo para aprobar nivel",
                    body=(f"{u.full_name if u else 'Un estudiante'} está listo para "
                          f"terminar {nivel.code if nivel else 'su nivel'}."),
                    link="/dashboard/admin/completions",
                ))
        elif rec == "requires_reinforcement":
            db.add(Notification(
                user_id=enr.student_id, type=NotificationType.info,
                title="📚 Tu profesor te recomendó refuerzo",
                body=comentario[:200], link="/dashboard/student",
            ))
    except Exception:
        pass

    await log_action(db, teacher.user_id, "recommend_completion", "progression",
                     target_id=enrollment_id, details=rec)
    await db.commit()
    return {
        "ok": True, "recommendation": rec,
        "academic_status": enr.academic_status,
        "mensaje": ("Enviado a Dirección para aprobación."
                    if rec == "recommend_promotion"
                    else "Registrado. El estudiante fue notificado."),
    }


@router.get("/completion-queue")
async def teacher_completion_queue(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Los estudiantes del profesor listos para revisión final, o con refuerzo
    pendiente. Solo sus matrículas."""
    from app.services.progression import elegibilidad_de_enrollment
    from app.services.teacher_permissions import es_admin, grupos_del_profesor

    cond = [Enrollment.is_active.is_(True)]
    if not await es_admin(db, teacher.user_id):
        grupos = await grupos_del_profesor(db, teacher.user_id)
        propias = [Enrollment.teacher_id == teacher.user_id]
        if grupos:
            propias.append(Enrollment.series_id.in_(grupos))
        cond.append(or_(*propias))

    filas = (await db.execute(
        select(Enrollment, User, Level)
        .join(User, Enrollment.student_id == User.id)
        .join(Level, Enrollment.level_id == Level.id)
        .where(*cond)
    )).all()

    listos, refuerzo, en_curso = [], [], 0
    for e, u, nivel in filas:
        estado = getattr(e, "academic_status", "active") or "active"
        if estado == "completed":
            continue
        elegib = await elegibilidad_de_enrollment(db, e)
        fila = {
            "enrollment_id": e.id,
            "student_id": u.id, "student_name": u.full_name,
            "level_code": nivel.code,
            "academic_status": estado,
            "eligible": elegib["eligible"],
            "pending": elegib["pending"],
            "met_count": elegib["met_count"],
            "total_count": elegib["total_count"],
        }
        if estado in ("requires_reinforcement", "requires_reevaluation"):
            refuerzo.append(fila)
        elif elegib["eligible"] or estado == "completion_review":
            listos.append(fila)
        else:
            en_curso += 1

    return {
        "ready_for_review": listos,
        "needs_reinforcement": refuerzo,
        "in_progress": en_curso,
        "counts": {"ready": len(listos), "reinforcement": len(refuerzo),
                   "in_progress": en_curso},
    }
