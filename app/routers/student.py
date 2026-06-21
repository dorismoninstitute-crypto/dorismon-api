"""Student — dashboard, cursos inscritos, tareas, quizzes, expediente, calendario."""
from typing import Annotated
from datetime import datetime, date, timezone as tz, timedelta
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, CurrentUser
from app.core.db import get_db
from app.services.audit import log_action  # V2.2
from app.models import (
    Student, User, Enrollment, Course, Level, Module, Lesson, LessonProgress,
    Assignment, AssignmentSubmission, Quiz, QuizAttempt, QuizQuestion, QuizAnswer,
    ClassSession, SessionAttendance, Certificate, Notification, Material,
    AttendanceState, QuestionType, AbsenceNotice, NotificationType, SessionStatus, UserRole,
)
from datetime import timezone as _tz, datetime as _dt, timedelta as _td

router = APIRouter(prefix="/student", tags=["student"])


@router.get("/dashboard")
async def student_dashboard(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    if user.role != "student":
        raise HTTPException(403, "Solo estudiantes")

    u = await db.get(User, user.user_id)
    student = await db.get(Student, user.user_id)
    if not student:
        raise HTTPException(404, "Perfil de estudiante no encontrado")

    now = datetime.now(tz.utc)
    week_ahead = now + timedelta(days=7)

    # Cursos inscritos activos
    enrollments_stmt = (
        select(Enrollment, Course, Level)
        .join(Course, Enrollment.course_id == Course.id)
        .join(Level, Enrollment.level_id == Level.id)
        .where(Enrollment.student_id == user.user_id, Enrollment.is_active.is_(True))
    )
    enrollments_rows = (await db.execute(enrollments_stmt)).all()
    enrollments = []
    for e, c, l in enrollments_rows:
        teacher_name = None
        teacher_id = None
        if e.teacher_id:
            t_user = await db.get(User, e.teacher_id)
            # V2.9.1: solo mostrar el profe si sigue ACTIVO
            if t_user and t_user.is_active:
                teacher_name = t_user.full_name
                teacher_id = e.teacher_id
        enrollments.append({
            "id": e.id, "course_id": c.id, "course_name": c.name,
            "level_id": l.id, "level_code": l.code, "level_name": l.name,
            "color": c.color, "enrolled_at": e.enrolled_at.isoformat() if e.enrolled_at else None,
            "final_grade": float(e.final_grade) if e.final_grade else None,
            "teacher_id": teacher_id, "teacher_name": teacher_name,  # V1.5, V2.9.1 solo si activo
        })

    # Próximas clases
    # V1.7: filtrar correctamente:
    #   - Grupales: student_id IS NULL + level_id en enrollments
    #   - Privadas: student_id == este estudiante
    next_sessions_stmt = (
        select(ClassSession)
        .where(
            ClassSession.ends_at_utc > now,  # V1.6.4: hasta fin de clase
            ClassSession.starts_at_utc < week_ahead,
            ClassSession.status == "scheduled",
        )
        .order_by(ClassSession.starts_at_utc)
        .limit(5)
    )
    # V1.7: condición compleja: (grupal de su nivel) OR (privada para él)
    if enrollments_rows:
        level_ids = [l.id for e, c, l in enrollments_rows]
        next_sessions_stmt = next_sessions_stmt.where(
            or_(
                # Grupales del nivel correcto (no privadas)
                (ClassSession.level_id.in_(level_ids)) & (ClassSession.student_id.is_(None)),
                # Privadas para este estudiante
                ClassSession.student_id == user.user_id,
            )
        )
    else:
        # Sin enrollments solo ve sus privadas
        next_sessions_stmt = next_sessions_stmt.where(ClassSession.student_id == user.user_id)

    sessions = (await db.execute(next_sessions_stmt)).scalars().all()
    next_classes = []
    for s in sessions:
        teacher_user = await db.get(User, s.teacher_id) if s.teacher_id else None
        # V2.9.1: mostrar profe solo si está activo
        t_name = teacher_user.full_name if (teacher_user and teacher_user.is_active) else "—"
        next_classes.append({
            "id": s.id, "title": s.title, "modality": s.modality.value,
            "starts_at_utc": s.starts_at_utc.isoformat(),
            "ends_at_utc": s.ends_at_utc.isoformat() if s.ends_at_utc else None,
            "meeting_url": s.meeting_url,
            "teacher_name": t_name,
            "is_private": s.student_id is not None,  # V1.7
        })

    # Tareas pendientes
    pending_assignments_q = (
        select(Assignment).where(
            (Assignment.level_id.in_([l.id for _, _, l in enrollments_rows])
             if enrollments_rows else False)
        )
    ) if enrollments_rows else None
    pending_assignments = 0
    next_assignment = None
    if pending_assignments_q is not None:
        items = (await db.execute(pending_assignments_q.limit(30))).scalars().all()
        for a in items:
            sub = (await db.execute(
                select(AssignmentSubmission).where(
                    AssignmentSubmission.assignment_id == a.id,
                    AssignmentSubmission.student_id == user.user_id,
                )
            )).scalar_one_or_none()
            if not sub or sub.submitted_at is None:
                pending_assignments += 1
                if not next_assignment:
                    next_assignment = {
                        "id": a.id, "title": a.title,
                        "due_at": a.due_at.isoformat() if a.due_at else None,
                    }

    # Quizzes disponibles
    pending_quizzes = 0
    if enrollments_rows:
        level_ids = [l.id for _, _, l in enrollments_rows]
        quizzes = (await db.execute(
            select(Quiz).where(Quiz.level_id.in_(level_ids), Quiz.is_published.is_(True)).limit(20)
        )).scalars().all()
        for q in quizzes:
            attempts = (await db.execute(
                select(func.count()).select_from(QuizAttempt).where(
                    QuizAttempt.quiz_id == q.id,
                    QuizAttempt.student_id == user.user_id,
                    QuizAttempt.submitted_at.is_not(None),
                )
            )).scalar() or 0
            if attempts == 0:
                pending_quizzes += 1

    # Certificados
    certs_count = (await db.execute(
        select(func.count()).select_from(Certificate).where(
            Certificate.student_id == user.user_id, Certificate.revoked.is_(False),
        )
    )).scalar() or 0

    # Asistencia
    attendances = (await db.execute(
        select(SessionAttendance).where(
            SessionAttendance.student_id == user.user_id,
            SessionAttendance.state.is_not(None),
        )
    )).scalars().all()
    total_att = len(attendances)
    present_count = sum(1 for a in attendances if a.state in (AttendanceState.present, AttendanceState.late))
    attendance_rate = int(present_count * 100 / total_att) if total_att else 0

    # Última calificación
    last_grade = (await db.execute(
        select(AssignmentSubmission).where(
            AssignmentSubmission.student_id == user.user_id,
            AssignmentSubmission.score.is_not(None),
        ).order_by(AssignmentSubmission.graded_at.desc()).limit(1)
    )).scalar_one_or_none()
    last_grade_data = None
    if last_grade:
        a = await db.get(Assignment, last_grade.assignment_id)
        last_grade_data = {
            "assignment_title": a.title if a else "—",
            "score": float(last_grade.score) if last_grade.score else None,
            "max_score": float(a.max_score) if a else 100.0,
        }

    # V3.0.1: Estado de la clase de prueba (para mostrar en el dashboard)
    trial_info = None
    from app.models import TrialClass
    tc = (await db.execute(
        select(TrialClass).where(TrialClass.student_id == user.user_id)
    )).scalar_one_or_none()
    if tc:
        # V3.0.2: Detectar si la clase de prueba ya pasó y actualizar estado
        # Si estaba "scheduled" y la sesión ya terminó → ver si asistió o no
        trial_passed = False
        attended = None
        if tc.status == "scheduled" and tc.scheduled_at:
            sched = tc.scheduled_at if tc.scheduled_at.tzinfo else tc.scheduled_at.replace(tzinfo=tz.utc)
            # La clase dura ~1h; consideramos "pasó" 1h después del inicio
            if now > sched + timedelta(hours=1):
                trial_passed = True
                # ¿Asistió? Revisar asistencia en la sesión vinculada
                if tc.session_id:
                    att = (await db.execute(
                        select(SessionAttendance).where(
                            SessionAttendance.session_id == tc.session_id,
                            SessionAttendance.student_id == user.user_id,
                        )
                    )).scalar_one_or_none()
                    if att and att.state and att.state.value in ("present", "late"):
                        attended = True
                    else:
                        attended = False
                # Actualizar estado del trial
                tc.status = "completed" if attended else "no_show"
                tc.completed_at = now
                await db.commit()

        t_teacher_name = None
        if tc.teacher_id:
            t_user = await db.get(User, tc.teacher_id)
            t_teacher_name = t_user.full_name if t_user else None
        trial_info = {
            "status": tc.status,  # requested / scheduled / completed / no_show / cancelled
            "teacher_name": t_teacher_name,
            "scheduled_at": tc.scheduled_at.isoformat() if tc.scheduled_at else None,
            "modality": tc.modality.value if tc.modality else None,
            # V3.0.2: control de reagenda
            "can_reschedule": (tc.status == "no_show" and tc.reschedule_count < 1 and not tc.reschedule_requested),
            "reschedule_requested": tc.reschedule_requested,
        }

    # V3.0: Clases canceladas recientemente que afectan al estudiante (últimos 7 días)
    # Para mostrar un aviso visible en el dashboard
    recent_cancelled = []
    cancelled_since = now - _td(days=7)
    level_ids_for_cancel = [l.id for e, c, l in enrollments_rows] if enrollments_rows else []
    cancel_stmt = (
        select(ClassSession)
        .where(
            ClassSession.status == SessionStatus.cancelled,
            ClassSession.cancelled_at >= cancelled_since,
        )
        .order_by(ClassSession.cancelled_at.desc())
        .limit(5)
    )
    if level_ids_for_cancel:
        cancel_stmt = cancel_stmt.where(
            or_(
                (ClassSession.level_id.in_(level_ids_for_cancel)) & (ClassSession.student_id.is_(None)),
                ClassSession.student_id == user.user_id,
            )
        )
    else:
        cancel_stmt = cancel_stmt.where(ClassSession.student_id == user.user_id)
    for cs in (await db.execute(cancel_stmt)).scalars().all():
        cs_starts = cs.starts_at_utc if cs.starts_at_utc.tzinfo else cs.starts_at_utc.replace(tzinfo=_tz.utc)
        recent_cancelled.append({
            "id": cs.id, "title": cs.title,
            "starts_at_utc": cs.starts_at_utc.isoformat(),
            "reason": cs.cancellation_reason,
            "cancelled_at": cs.cancelled_at.isoformat() if cs.cancelled_at else None,
        })

    # V3.0: IDs de clases donde el estudiante ya avisó que faltará
    my_absence_ids = []
    next_ids = [c["id"] for c in next_classes]
    if next_ids:
        notices = (await db.execute(
            select(AbsenceNotice.session_id).where(
                AbsenceNotice.student_id == user.user_id,
                AbsenceNotice.session_id.in_(next_ids),
            )
        )).scalars().all()
        my_absence_ids = list(notices)

    return {
        "user": {"id": u.id, "full_name": u.full_name, "email": u.email,
                 "avatar_url": u.avatar_url, "role": "student"},
        "stats": {
            "enrolled_courses": len(enrollments),
            "next_classes": len(next_classes),
            "pending_assignments": pending_assignments,
            "pending_quizzes": pending_quizzes,
            "certificates": certs_count,
            "attendance_rate": attendance_rate,
        },
        "enrollments": enrollments,
        "next_classes": next_classes,
        "next_assignment": next_assignment,
        "last_grade": last_grade_data,
        "recent_cancelled": recent_cancelled,  # V3.0
        "my_absence_session_ids": my_absence_ids,  # V3.0
        "trial_info": trial_info,  # V3.0.1
    }


@router.get("/courses")
async def my_courses(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    if user.role != "student":
        raise HTTPException(403)
    stmt = (
        select(Enrollment, Course, Level, User)
        .join(Course, Enrollment.course_id == Course.id)
        .join(Level, Enrollment.level_id == Level.id)
        .outerjoin(User, Enrollment.teacher_id == User.id)
        .where(Enrollment.student_id == user.user_id, Enrollment.is_active.is_(True))
    )
    rows = (await db.execute(stmt)).all()
    out = []
    for e, c, l, t in rows:
        # Calcular progreso del nivel
        modules = (await db.execute(
            select(Module).where(Module.level_id == l.id)
        )).scalars().all()
        mod_ids = [m.id for m in modules]
        total_lessons = (await db.execute(
            select(func.count()).select_from(Lesson).where(
                Lesson.module_id.in_(mod_ids) if mod_ids else False,
                Lesson.is_published.is_(True),
            )
        )).scalar() or 0
        completed = (await db.execute(
            select(func.count()).select_from(LessonProgress).where(
                LessonProgress.student_id == user.user_id,
                LessonProgress.is_completed.is_(True),
            )
        )).scalar() or 0
        out.append({
            "enrollment_id": e.id, "course_id": c.id, "course_name": c.name,
            "course_color": c.color, "course_description": c.description,
            "level_id": l.id, "level_code": l.code, "level_name": l.name,
            "teacher_name": (t.full_name if (t and t.is_active) else None),  # V2.9.1 solo activo
            "total_lessons": total_lessons,
            "completed_lessons": min(completed, total_lessons),
            "progress_pct": int(min(completed, total_lessons) * 100 / total_lessons) if total_lessons else 0,
        })
    return out


@router.get("/assignments")
async def my_assignments(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    if user.role != "student":
        raise HTTPException(403)
    # V2.9: Feature gate — si el plan no incluye assignments, devolver vacío + flag
    from app.services.feature_gates import student_has_feature
    has_access = await student_has_feature(db, user.user_id, "assignments")
    if not has_access:
        return {"items": [], "blocked_by_plan": True, "feature_key": "assignments"}
    enrollments = (await db.execute(
        select(Enrollment.level_id).where(
            Enrollment.student_id == user.user_id, Enrollment.is_active.is_(True),
        )
    )).scalars().all()
    if not enrollments:
        return {"items": [], "blocked_by_plan": False}
    items = (await db.execute(
        select(Assignment).where(Assignment.level_id.in_(enrollments))
        .order_by(Assignment.due_at.asc().nullsfirst()).limit(40)
    )).scalars().all()
    out = []
    for a in items:
        sub = (await db.execute(
            select(AssignmentSubmission).where(
                AssignmentSubmission.assignment_id == a.id,
                AssignmentSubmission.student_id == user.user_id,
            )
        )).scalar_one_or_none()
        out.append({
            "id": a.id, "title": a.title, "description": a.description,
            "instructions": a.instructions,
            "max_score": float(a.max_score),
            "due_at": a.due_at.isoformat() if a.due_at else None,
            "submitted": bool(sub and sub.submitted_at),
            "graded": bool(sub and sub.graded_at),
            "score": float(sub.score) if sub and sub.score else None,
            "feedback": sub.feedback if sub else None,
            "submission_id": sub.id if sub else None,
            "submitted_at": sub.submitted_at.isoformat() if sub and sub.submitted_at else None,
        })
    return {"items": out, "blocked_by_plan": False}


@router.post("/assignments/{assignment_id}/submit")
async def submit_assignment(
    assignment_id: int, body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    if user.role != "student":
        raise HTTPException(403)
    a = await db.get(Assignment, assignment_id)
    if not a:
        raise HTTPException(404, "Tarea no encontrada")
    sub = (await db.execute(
        select(AssignmentSubmission).where(
            AssignmentSubmission.assignment_id == assignment_id,
            AssignmentSubmission.student_id == user.user_id,
        )
    )).scalar_one_or_none()
    if not sub:
        sub = AssignmentSubmission(assignment_id=assignment_id, student_id=user.user_id)
        db.add(sub)
    sub.content = body.get("content")
    sub.file_url = body.get("file_url")
    sub.file_name = body.get("file_name")
    sub.submitted_at = datetime.now(tz.utc)
    await db.commit()
    await db.refresh(sub)
    return {"submission_id": sub.id, "submitted_at": sub.submitted_at.isoformat()}


@router.get("/quizzes")
async def my_quizzes(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    if user.role != "student":
        raise HTTPException(403)
    # V2.9: Feature gate quizzes
    from app.services.feature_gates import student_has_feature
    has_access = await student_has_feature(db, user.user_id, "quizzes")
    if not has_access:
        return {"items": [], "blocked_by_plan": True, "feature_key": "quizzes"}
    enrollments = (await db.execute(
        select(Enrollment.level_id).where(
            Enrollment.student_id == user.user_id, Enrollment.is_active.is_(True),
        )
    )).scalars().all()
    if not enrollments:
        return {"items": [], "blocked_by_plan": False}
    items = (await db.execute(
        select(Quiz).where(Quiz.level_id.in_(enrollments), Quiz.is_published.is_(True))
        .order_by(Quiz.created_at.desc()).limit(40)
    )).scalars().all()
    out = []
    for q in items:
        attempts_q = (await db.execute(
            select(QuizAttempt).where(
                QuizAttempt.quiz_id == q.id,
                QuizAttempt.student_id == user.user_id,
            ).order_by(QuizAttempt.started_at.desc())
        )).scalars().all()
        last_attempt = attempts_q[0] if attempts_q else None
        question_count = (await db.execute(
            select(func.count()).select_from(QuizQuestion).where(QuizQuestion.quiz_id == q.id)
        )).scalar() or 0
        out.append({
            "id": q.id, "title": q.title, "description": q.description,
            "passing_score": float(q.passing_score), "max_attempts": q.max_attempts,
            "question_count": question_count,
            "attempts_used": len(attempts_q),
            "last_score": float(last_attempt.score) if last_attempt and last_attempt.score else None,
            "passed": last_attempt.passed if last_attempt else None,
        })
    return {"items": out, "blocked_by_plan": False}


@router.get("/quizzes/{quiz_id}")
async def get_quiz(
    quiz_id: int,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    if user.role != "student":
        raise HTTPException(403)
    quiz = await db.get(Quiz, quiz_id)
    if not quiz or not quiz.is_published:
        raise HTTPException(404, "Quiz no encontrado")
    questions = (await db.execute(
        select(QuizQuestion).where(QuizQuestion.quiz_id == quiz_id).order_by(QuizQuestion.order_index)
    )).scalars().all()
    return {
        "id": quiz.id, "title": quiz.title, "description": quiz.description,
        "passing_score": float(quiz.passing_score),
        "questions": [{
            "id": q.id, "type": q.type.value, "statement": q.statement,
            "options": q.options, "points": float(q.points),
        } for q in questions],
    }


@router.post("/quizzes/{quiz_id}/submit")
async def submit_quiz(
    quiz_id: int, body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """body = {answers: [{question_id, answer}]}"""
    if user.role != "student":
        raise HTTPException(403)
    quiz = await db.get(Quiz, quiz_id)
    if not quiz:
        raise HTTPException(404, "Quiz no encontrado")
    answers = body.get("answers", [])
    if not isinstance(answers, list):
        raise HTTPException(400, "answers debe ser una lista")

    # Crear intento
    attempt = QuizAttempt(quiz_id=quiz_id, student_id=user.user_id)
    db.add(attempt)
    await db.flush()

    total_points = 0.0
    earned_points = 0.0
    for ans in answers:
        qid = ans.get("question_id")
        student_ans = (ans.get("answer") or "").strip()
        question = await db.get(QuizQuestion, qid)
        if not question or question.quiz_id != quiz_id:
            continue
        total_points += float(question.points)
        # Corrección automática
        is_correct = None
        if question.type in (QuestionType.multiple_choice, QuestionType.true_false, QuestionType.fill_blank):
            is_correct = student_ans.lower() == question.correct_answer.strip().lower()
        elif question.type == QuestionType.short_answer:
            # exacta o similar simple
            is_correct = student_ans.lower() == question.correct_answer.strip().lower()
        pts = float(question.points) if is_correct else 0.0
        earned_points += pts
        db.add(QuizAnswer(
            attempt_id=attempt.id, question_id=qid, answer=student_ans,
            is_correct=is_correct, points_earned=pts,
        ))

    score_pct = (earned_points / total_points * 100) if total_points else 0
    attempt.score = round(score_pct, 2)
    attempt.passed = score_pct >= float(quiz.passing_score)
    attempt.submitted_at = datetime.now(tz.utc)
    await db.commit()

    return {
        "attempt_id": attempt.id,
        "score": float(attempt.score),
        "passed": attempt.passed,
        "earned_points": earned_points,
        "total_points": total_points,
    }


@router.get("/calendar")
async def my_calendar(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Calendario de las próximas 4 semanas: clases + tareas + quizzes."""
    if user.role != "student":
        raise HTTPException(403)
    now = datetime.now(tz.utc)
    horizon = now + timedelta(days=28)

    enrollments = (await db.execute(
        select(Enrollment.level_id).where(
            Enrollment.student_id == user.user_id, Enrollment.is_active.is_(True),
        )
    )).scalars().all()

    events = []
    # V3.0.2: clases del estudiante = grupales de su nivel + privadas suyas (incluye clase de prueba)
    # Antes solo mostraba grupales si había inscripción; ahora también las privadas con student_id.
    session_filter = ClassSession.student_id == user.user_id  # privadas (incluye trial)
    if enrollments:
        session_filter = or_(
            ClassSession.student_id == user.user_id,
            and_(
                ClassSession.level_id.in_(enrollments),
                ClassSession.student_id.is_(None),
            ),
        )
    sessions = (await db.execute(
        select(ClassSession).where(
            session_filter,
            ClassSession.ends_at_utc >= now,
            ClassSession.starts_at_utc < horizon,
            ClassSession.status == "scheduled",
        ).order_by(ClassSession.starts_at_utc)
    )).scalars().all()
    for s in sessions:
        events.append({
            "type": "class", "id": s.id, "title": s.title,
            "starts_at": s.starts_at_utc.isoformat(),
            "modality": s.modality.value,
            "meeting_url": s.meeting_url,
        })
    if enrollments:
        assignments = (await db.execute(
            select(Assignment).where(
                Assignment.level_id.in_(enrollments),
                Assignment.due_at >= now,
                Assignment.due_at < horizon,
            )
        )).scalars().all()
        for a in assignments:
            events.append({
                "type": "assignment", "id": a.id, "title": a.title,
                "starts_at": a.due_at.isoformat() if a.due_at else None,
            })
    events.sort(key=lambda e: e["starts_at"] or "")
    return events


@router.get("/attendance")
async def my_attendance(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    if user.role != "student":
        raise HTTPException(403)
    stmt = (
        select(SessionAttendance, ClassSession)
        .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
        .where(SessionAttendance.student_id == user.user_id)
        .order_by(ClassSession.starts_at_utc.desc()).limit(50)
    )
    rows = (await db.execute(stmt)).all()
    return [{
        "session_id": s.id, "title": s.title,
        "date": s.starts_at_utc.isoformat(),
        "state": a.state.value if a.state else None,
        "notes": a.notes,
    } for a, s in rows]


@router.get("/certificates")
async def my_certificates(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    certs = (await db.execute(
        select(Certificate, Course, Level)
        .join(Course, Certificate.course_id == Course.id)
        .join(Level, Certificate.level_id == Level.id)
        .where(Certificate.student_id == user.user_id, Certificate.revoked.is_(False))
        .order_by(Certificate.issued_at.desc())
    )).all()
    return [{
        "id": c.id, "code": c.code, "course_name": course.name,
        "level_code": l.code, "level_name": l.name,
        "hours": c.hours, "final_grade": float(c.final_grade) if c.final_grade else None,
        "issued_at": c.issued_at.isoformat(), "color": course.color,
    } for c, course, l in certs]


@router.get("/notifications")
async def my_notifications(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    unread_only: bool = False,
):
    stmt = select(Notification).where(Notification.user_id == user.user_id)
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    items = (await db.execute(stmt.order_by(Notification.created_at.desc()).limit(50))).scalars().all()
    return [{
        "id": n.id, "type": n.type.value, "title": n.title, "body": n.body,
        "link": n.link, "is_read": n.is_read,
        "created_at": n.created_at.isoformat(),
    } for n in items]


@router.post("/notifications/{notif_id}/read")
async def mark_notification_read(
    notif_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    n = await db.get(Notification, notif_id)
    if not n or n.user_id != user.user_id:
        raise HTTPException(404)
    n.is_read = True
    await db.commit()
    return {"ok": True}


@router.get("/library")
async def my_library(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    course_id: int | None = None,
    level_id: int | None = None,
    type: str | None = None,
):
    """Biblioteca filtrable."""
    stmt = select(Material).where(Material.is_public.is_(True))
    if course_id:
        stmt = stmt.where(Material.course_id == course_id)
    if level_id:
        stmt = stmt.where(Material.level_id == level_id)
    if type:
        stmt = stmt.where(Material.type == type)
    materials = (await db.execute(stmt.order_by(Material.created_at.desc()).limit(100))).scalars().all()
    return [{
        "id": m.id, "title": m.title, "description": m.description,
        "type": m.type.value, "url": m.url,
        "course_id": m.course_id, "level_id": m.level_id,
    } for m in materials]


@router.get("/transcript")
async def academic_transcript(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Expediente académico completo."""
    if user.role != "student":
        raise HTTPException(403)
    u = await db.get(User, user.user_id)
    st = await db.get(Student, user.user_id)

    # Asistencia agregada
    attendances = (await db.execute(
        select(SessionAttendance).where(
            SessionAttendance.student_id == user.user_id,
            SessionAttendance.state.is_not(None),
        )
    )).scalars().all()
    total_sessions = len(attendances)
    present = sum(1 for a in attendances if a.state in (AttendanceState.present, AttendanceState.late))
    attendance_rate = round(present * 100 / total_sessions, 1) if total_sessions else 0

    # Notas
    grades = (await db.execute(
        select(AssignmentSubmission, Assignment)
        .join(Assignment, AssignmentSubmission.assignment_id == Assignment.id)
        .where(AssignmentSubmission.student_id == user.user_id,
               AssignmentSubmission.score.is_not(None))
    )).all()
    avg_grade = None
    if grades:
        total_pct = sum(float(s.score) * 100 / float(a.max_score) for s, a in grades if a.max_score)
        avg_grade = round(total_pct / len(grades), 1)

    # Inscripciones (cursos cursados)
    enrollments = (await db.execute(
        select(Enrollment, Course, Level)
        .join(Course, Enrollment.course_id == Course.id)
        .join(Level, Enrollment.level_id == Level.id)
        .where(Enrollment.student_id == user.user_id)
    )).all()

    # Certificados
    certs = (await db.execute(
        select(Certificate).where(Certificate.student_id == user.user_id, Certificate.revoked.is_(False))
    )).scalars().all()

    return {
        "student": {
            "id": u.id, "full_name": u.full_name, "email": u.email,
            "phone": u.phone, "enrolled_at": st.enrolled_at.isoformat() if st else None,
            "current_level_id": st.current_level_id if st else None,
            "placement_done": st.placement_done if st else False,
            "speaking_score": float(st.speaking_score) if st and st.speaking_score else None,
            "listening_score": float(st.listening_score) if st and st.listening_score else None,
            "reading_score": float(st.reading_score) if st and st.reading_score else None,
            "writing_score": float(st.writing_score) if st and st.writing_score else None,
        },
        "stats": {
            "total_sessions": total_sessions,
            "attendance_rate": attendance_rate,
            "avg_grade": avg_grade,
            "total_assignments": len(grades),
            "total_certificates": len(certs),
        },
        "enrollments": [{
            "course_name": c.name, "level_code": l.code, "level_name": l.name,
            "enrolled_at": e.enrolled_at.isoformat() if e.enrolled_at else None,
            "completed_at": e.completed_at.isoformat() if e.completed_at else None,
            "is_active": e.is_active,
            "final_grade": float(e.final_grade) if e.final_grade else None,
        } for e, c, l in enrollments],
        "recent_grades": [{
            "title": a.title, "score": float(s.score) if s.score else 0,
            "max_score": float(a.max_score),
            "graded_at": s.graded_at.isoformat() if s.graded_at else None,
        } for s, a in grades[:10]],
        "certificates": [{
            "code": c.code, "issued_at": c.issued_at.isoformat(),
        } for c in certs],
    }


# ============= V2.2 — PERFIL COMPLETO ESTUDIANTE =============

@router.get("/profile")
async def get_student_profile(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.2: Devuelve el perfil completo del estudiante (datos personales + tutor + emergencia)."""
    u = await db.get(User, user.user_id)
    if not u:
        raise HTTPException(404)
    s = await db.get(Student, user.user_id)

    # Calcular edad si tiene fecha de nacimiento
    age = None
    is_minor = False
    if s and s.birth_date:
        today = date.today()
        age = today.year - s.birth_date.year - ((today.month, today.day) < (s.birth_date.month, s.birth_date.day))
        is_minor = age < 18

    return {
        # Datos del User
        "user_id": u.id,
        "email": u.email,
        "full_name": u.full_name,
        "phone": u.phone,
        "gender": u.gender,
        "avatar_url": u.avatar_url,
        "email_verified": u.email_verified,
        # Datos personales
        "birth_date": s.birth_date.isoformat() if s and s.birth_date else None,
        "age": age,
        "is_minor": is_minor,
        "document_type": s.document_type if s else None,
        "document_number": s.document_number if s else None,
        "address": s.address if s else None,
        "city": s.city if s else None,
        "sector": s.sector if s else None,
        "nationality": s.nationality if s else None,
        # Contacto emergencia
        "emergency_contact_name": s.emergency_contact_name if s else None,
        "emergency_contact_relationship": s.emergency_contact_relationship if s else None,
        "emergency_contact_phone": s.emergency_contact_phone if s else None,
        # Tutor (si es menor)
        "tutor_name": s.tutor_name if s else None,
        "tutor_relationship": s.tutor_relationship if s else None,
        "tutor_document": s.tutor_document if s else None,
        "tutor_phone": s.tutor_phone if s else None,
        "tutor_email": s.tutor_email if s else None,
        # Info adicional
        "how_found_us": s.how_found_us if s else None,
        "referred_by": s.referred_by if s else None,
        "special_notes": s.special_notes if s else None,
        # Estado de completitud
        "profile_complete": _is_profile_complete(s) if s else False,
    }


def _is_profile_complete(s) -> bool:
    """V2.2: Verifica si el perfil está completo."""
    if not s:
        return False
    required = [s.birth_date, s.document_type, s.document_number, s.address,
                s.emergency_contact_name, s.emergency_contact_phone]
    if not all(required):
        return False
    # Si es menor, validar también datos de tutor
    if s.birth_date:
        today = date.today()
        age = today.year - s.birth_date.year - ((today.month, today.day) < (s.birth_date.month, s.birth_date.day))
        if age < 18:
            tutor_required = [s.tutor_name, s.tutor_phone, s.tutor_document]
            if not all(tutor_required):
                return False
    return True


@router.patch("/profile")
async def update_student_profile(
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.2: Actualiza el perfil completo del estudiante.

    Body puede tener cualquier campo del perfil. Solo los enviados se actualizan.
    """
    s = await db.get(Student, user.user_id)
    if not s:
        raise HTTPException(404, "Perfil de estudiante no encontrado")

    # Campos editables
    str_fields = [
        "document_type", "document_number", "address", "city", "sector", "nationality",
        "emergency_contact_name", "emergency_contact_relationship", "emergency_contact_phone",
        "tutor_name", "tutor_relationship", "tutor_document", "tutor_phone", "tutor_email",
        "how_found_us", "referred_by", "special_notes",
    ]
    for f in str_fields:
        if f in body:
            val = body[f]
            if val == "":
                val = None
            setattr(s, f, val)

    # Birth date
    if "birth_date" in body:
        val = body["birth_date"]
        if val:
            try:
                s.birth_date = date.fromisoformat(val)
            except Exception:
                raise HTTPException(400, "Fecha de nacimiento inválida (formato: YYYY-MM-DD)")
        else:
            s.birth_date = None

    # Validar: si es menor, datos de tutor son obligatorios
    if s.birth_date:
        today = date.today()
        age = today.year - s.birth_date.year - ((today.month, today.day) < (s.birth_date.month, s.birth_date.day))
        if age < 18:
            # Si el usuario está intentando completar el perfil (mandó al menos un campo de tutor)
            # validamos que estén todos
            tutor_fields_sent = any(f in body for f in ["tutor_name", "tutor_phone", "tutor_document"])
            if tutor_fields_sent and not all([s.tutor_name, s.tutor_phone, s.tutor_document]):
                raise HTTPException(400,
                    "Como eres menor de edad, los datos del tutor son obligatorios: nombre, teléfono y documento")

    await log_action(db, user.user_id, "update_profile", "students", target_id=user.user_id)
    await db.commit()
    return {"ok": True, "profile_complete": _is_profile_complete(s)}


# ============= V3.0 — AVISAR AUSENCIA =============

from pydantic import BaseModel as _BaseModel, Field as _Field


class AbsenceNoticeRequest(_BaseModel):
    reason: str = _Field(min_length=5, max_length=500,
                         description="Motivo de la ausencia (mínimo 5 caracteres)")


@router.post("/sessions/{session_id}/notify-absence")
async def notify_absence(
    session_id: str,
    body: AbsenceNoticeRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V3.0: El estudiante avisa que faltará a una clase.

    - Solo para clases futuras y programadas
    - Registra si avisó con tiempo (>=2h antes) o a último momento
    - Notifica al profesor de la clase
    - No se puede avisar dos veces la misma clase (puede actualizar el motivo)
    """
    if user.role != "student":
        raise HTTPException(403, "Solo estudiantes pueden avisar ausencias")

    session = await db.get(ClassSession, session_id)
    if not session:
        raise HTTPException(404, "Clase no encontrada")
    if session.status == SessionStatus.cancelled:
        raise HTTPException(400, "Esta clase fue cancelada")

    now = _dt.now(_tz.utc)
    starts = session.starts_at_utc if session.starts_at_utc.tzinfo else session.starts_at_utc.replace(tzinfo=_tz.utc)
    if starts < now:
        raise HTTPException(400, "Esta clase ya pasó, no puedes avisar ausencia")

    # Verificar que el estudiante pertenece a esta clase
    # (grupal de su nivel o privada para él)
    belongs = False
    if session.student_id == user.user_id:
        belongs = True
    else:
        enr = (await db.execute(
            select(Enrollment).where(
                Enrollment.student_id == user.user_id,
                Enrollment.level_id == session.level_id,
                Enrollment.is_active.is_(True),
            )
        )).scalar_one_or_none()
        if enr:
            belongs = True
    if not belongs:
        raise HTTPException(403, "Esta clase no está en tu plan")

    # ¿Avisó con tiempo? (>= 2h antes)
    in_advance = (starts - now).total_seconds() >= 7200

    # ¿Ya existe un aviso? → actualizar
    existing = (await db.execute(
        select(AbsenceNotice).where(
            AbsenceNotice.session_id == session_id,
            AbsenceNotice.student_id == user.user_id,
        )
    )).scalar_one_or_none()

    if existing:
        existing.reason = body.reason.strip()
        existing.notified_in_advance = in_advance
    else:
        db.add(AbsenceNotice(
            session_id=session_id,
            student_id=user.user_id,
            reason=body.reason.strip(),
            notified_in_advance=in_advance,
        ))

    # Notificar al profe
    if session.teacher_id:
        student_user = await db.get(User, user.user_id)
        student_name = student_user.full_name if student_user else "Un estudiante"
        when = starts.strftime("%d/%m a las %H:%M")
        db.add(Notification(
            user_id=session.teacher_id,
            type=NotificationType.info,
            title="Un estudiante avisó que faltará",
            body=f"{student_name} no asistirá a '{session.title}' ({when}). Motivo: {body.reason[:120]}",
            link=f"/dashboard/teacher/sessions/{session_id}",
        ))

    await log_action(db, user.user_id, "notify_absence", "student", target_id=session_id)
    await db.commit()
    return {"ok": True, "notified_in_advance": in_advance}


@router.delete("/sessions/{session_id}/notify-absence")
async def cancel_absence_notice(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V3.0: El estudiante retira su aviso de ausencia (al final sí puede ir)."""
    if user.role != "student":
        raise HTTPException(403)
    notice = (await db.execute(
        select(AbsenceNotice).where(
            AbsenceNotice.session_id == session_id,
            AbsenceNotice.student_id == user.user_id,
        )
    )).scalar_one_or_none()
    if not notice:
        raise HTTPException(404, "No tienes aviso de ausencia para esta clase")
    await db.delete(notice)
    await db.commit()
    return {"ok": True}


# ============= V3.0.2 — REAGENDAR CLASE DE PRUEBA =============

@router.post("/trial-class/reschedule")
async def request_trial_reschedule(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V3.0.2: El estudiante pide reagendar su clase de prueba (solo si no asistió, 1 vez).

    Marca la solicitud para que el admin la vuelva a agendar. Notifica a los admins.
    """
    if user.role != "student":
        raise HTTPException(403, "Solo estudiantes")

    from app.models import TrialClass
    tc = (await db.execute(
        select(TrialClass).where(TrialClass.student_id == user.user_id)
    )).scalar_one_or_none()
    if not tc:
        raise HTTPException(404, "No tienes clase de prueba")
    if tc.status != "no_show":
        raise HTTPException(400, "Solo puedes reagendar si no asististe a tu clase de prueba")
    if tc.reschedule_count >= 1:
        raise HTTPException(400, "Ya reagendaste una vez. Contacta al instituto para más opciones.")
    if tc.reschedule_requested:
        raise HTTPException(400, "Ya pediste reagendar. Te contactaremos pronto.")

    tc.reschedule_requested = True

    # Notificar admins
    student_user = await db.get(User, user.user_id)
    admins = (await db.execute(
        select(User).where(User.role == UserRole.super_admin, User.is_active.is_(True))
    )).scalars().all()
    for adm in admins:
        db.add(Notification(
            user_id=adm.id,
            type=NotificationType.info,
            title="🔄 Solicitud de reagenda de clase de prueba",
            body=f"{student_user.full_name if student_user else 'Un estudiante'} pidió reagendar su clase de prueba (no asistió a la anterior).",
            link="/dashboard/admin/trial-classes",
        ))

    await log_action(db, user.user_id, "request_trial_reschedule", "student")
    await db.commit()
    return {"ok": True, "message": "Solicitud enviada. Te contactaremos para coordinar la nueva fecha."}
