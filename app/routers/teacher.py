"""Teacher — agenda, asistencia, quizzes, tareas, materiales, observaciones."""
from typing import Annotated
from datetime import datetime, timedelta, timezone as tz
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_teacher_or_admin, CurrentUser, get_current_user
from app.core.db import get_db
from app.services.audit import log_action
from app.models import (
    User, Teacher, ClassSession, SessionAttendance, Enrollment,
    Assignment, AssignmentSubmission, Quiz, QuizQuestion, QuizAttempt,
    Material, Observation, Notification, Student,
    AttendanceState, QuestionType, MaterialType, NotificationType, SessionStatus,
    Course, Level,
)

router = APIRouter(prefix="/teacher", tags=["teacher"])


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
        for u, e, l in my_students_q:
            # Asistencia del estudiante en mis clases
            att_rows = (await db.execute(
                select(SessionAttendance.state)
                .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
                .where(
                    SessionAttendance.student_id == u.id,
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
                    "student_id": u.id,
                    "student_name": u.full_name,
                    "gender": u.gender,
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
):
    stmt = select(ClassSession)
    if teacher.role == "teacher":
        stmt = stmt.where(ClassSession.teacher_id == teacher.user_id)
    stmt = stmt.order_by(ClassSession.starts_at_utc.desc()).limit(50)
    sessions = (await db.execute(stmt)).scalars().all()
    out = []
    for s in sessions:
        course = await db.get(Course, s.course_id)
        level = await db.get(Level, s.level_id)
        out.append({
            "id": s.id, "title": s.title, "modality": s.modality.value,
            "starts_at_utc": s.starts_at_utc.isoformat(),
            "course_name": course.name if course else None,
            "level_code": level.code if level else None,
            "meeting_url": s.meeting_url,
            "status": s.status.value,
            "capacity": s.capacity,
        })
    return out


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
    students_q = (
        select(Enrollment, User)
        .join(User, Enrollment.student_id == User.id)
        .where(
            Enrollment.course_id == session.course_id,
            Enrollment.level_id == session.level_id,
            Enrollment.is_active.is_(True),
        )
    )
    rows = (await db.execute(students_q)).all()
    out_students = []
    for e, u in rows:
        att = (await db.execute(
            select(SessionAttendance).where(
                SessionAttendance.session_id == session_id,
                SessionAttendance.student_id == u.id,
            )
        )).scalar_one_or_none()
        out_students.append({
            "student_id": u.id, "full_name": u.full_name, "email": u.email,
            "attendance_id": att.id if att else None,
            "state": att.state.value if att and att.state else None,
            "notes": att.notes if att else None,
        })

    return {
        "session": {
            "id": session.id, "title": session.title,
            "starts_at_utc": session.starts_at_utc.isoformat(),
            "modality": session.modality.value,
            "teacher_notes": session.teacher_notes,
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
        if r.get("state"):
            try:
                att.state = AttendanceState(r["state"])
            except ValueError:
                continue
        if "notes" in r:
            att.notes = r["notes"]
        att.recorded_at = now
        updated += 1
    await log_action(db, teacher.user_id, "save_attendance", "teacher", target_id=session_id)
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
            mp = (await db.execute(
                select(ModuleProgress).where(
                    ModuleProgress.student_id == sid,
                    ModuleProgress.module_id == session.module_id,
                )
            )).scalar_one_or_none()
            if not mp:
                mp = ModuleProgress(student_id=sid, module_id=session.module_id, status="in_progress")
                db.add(mp)
            mp.attended_count = (mp.attended_count or 0) + 1
            mp.status = "completed" if mp.attended_count >= 1 else "in_progress"
            if mp.status == "completed" and not mp.completed_at:
                mp.completed_at = now
        await db.commit()

    return {"ok": True, "updated": updated}


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
    a = Assignment(
        title=body["title"], description=body.get("description"),
        instructions=body.get("instructions"),
        teacher_id=teacher.user_id,
        level_id=body.get("level_id"),
        lesson_id=body.get("lesson_id"),
        max_score=body.get("max_score", 100.0),
        due_at=datetime.fromisoformat(body["due_at"].replace("Z", "+00:00")) if body.get("due_at") else None,
    )
    db.add(a)
    await db.flush()

    # Crear notificaciones a estudiantes del nivel
    if a.level_id:
        students = (await db.execute(
            select(Enrollment.student_id).where(
                Enrollment.level_id == a.level_id, Enrollment.is_active.is_(True),
            )
        )).scalars().all()
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
    if teacher.role == "teacher" and a.teacher_id != teacher.user_id:
        raise HTTPException(403)
    stmt = (
        select(AssignmentSubmission, User)
        .join(User, AssignmentSubmission.student_id == User.id)
        .where(AssignmentSubmission.assignment_id == assignment_id)
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
    if teacher.role == "teacher" and a.teacher_id != teacher.user_id:
        raise HTTPException(403)
    sub.score = body.get("score")
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
    q = Quiz(
        title=body["title"], description=body.get("description"),
        teacher_id=teacher.user_id, level_id=body.get("level_id"),
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
    return {"id": q.id, "title": q.title}


# === MATERIALES ===
@router.get("/materials")
async def list_materials(
    teacher: Annotated[CurrentUser, Depends(require_teacher_or_admin)],
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Material).order_by(Material.created_at.desc()).limit(200)
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
    m = Material(
        title=body["title"], description=body.get("description"),
        type=MaterialType(body["type"]), url=body["url"],
        course_id=body.get("course_id"), level_id=body.get("level_id"),
        module_id=body.get("module_id"), lesson_id=body.get("lesson_id"),
        uploaded_by=teacher.user_id,
        is_public=body.get("is_public", True),
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
