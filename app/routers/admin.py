"""Admin — gestión completa del instituto."""
from typing import Annotated
from datetime import datetime, timedelta, timezone as tz
from secrets import token_urlsafe
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin, CurrentUser, hash_password
from app.core.db import get_db
from app.services.audit import log_action
from app.models import (
    User, Teacher, Student, Course, Level, Module, Lesson, LessonProgress,
    Enrollment, Branch, Classroom, ClassSession, ClassSeries, SessionAttendance,
    Assignment, AssignmentSubmission, Quiz, Material, Plan, Payment,
    Certificate, InstituteSetting, AuditLog, Notification, TeacherPayment,
    UserRole, Modality, SessionStatus, MaterialType, PaymentStatus, NotificationType,
    PlanFeature, ModuleProgress, EventRegistration, AttendanceState,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# === DASHBOARD ===
@router.get("/dashboard")
async def admin_dashboard(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(tz.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_ahead = now + timedelta(days=7)

    u = await db.get(User, admin.user_id)

    total_students = (await db.execute(
        select(func.count()).select_from(User).where(User.role == UserRole.student)
    )).scalar() or 0
    total_teachers = (await db.execute(
        select(func.count()).select_from(User).where(User.role == UserRole.teacher, User.is_active.is_(True))
    )).scalar() or 0
    total_courses = (await db.execute(
        select(func.count()).select_from(Course).where(Course.is_active.is_(True))
    )).scalar() or 0
    scheduled = (await db.execute(
        select(func.count()).select_from(ClassSession).where(
            ClassSession.ends_at_utc > now,  # V1.6.4
            ClassSession.starts_at_utc < week_ahead,
            ClassSession.status == SessionStatus.scheduled,
        )
    )).scalar() or 0
    new_month = (await db.execute(
        select(func.count()).select_from(User).where(
            User.role == UserRole.student, User.created_at >= month_start,
        )
    )).scalar() or 0
    # Ingresos del mes (pagos paid)
    income_q = await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == PaymentStatus.paid,
            Payment.paid_at >= month_start,
        )
    )
    income_month = float(income_q.scalar() or 0)
    pending_payments = (await db.execute(
        select(func.count()).select_from(Payment).where(Payment.status == PaymentStatus.pending)
    )).scalar() or 0
    certs_issued = (await db.execute(
        select(func.count()).select_from(Certificate).where(Certificate.revoked.is_(False))
    )).scalar() or 0

    # V1.5.1: Estudiantes sin profesor asignado
    unassigned_students = (await db.execute(
        select(func.count()).select_from(Enrollment).where(
            Enrollment.teacher_id.is_(None),
            Enrollment.is_active.is_(True),
        )
    )).scalar() or 0
    # V1.5.1: Profesores sin estudiantes asignados
    teachers_with_students_q = (await db.execute(
        select(Enrollment.teacher_id).where(
            Enrollment.teacher_id.is_not(None), Enrollment.is_active.is_(True),
        ).distinct()
    )).scalars().all()
    teachers_with_students = set(teachers_with_students_q)
    teachers_without_students = max(0, total_teachers - len(teachers_with_students))

    # V1.6.4: Total módulos cargados (para detectar sistema vacío)
    total_modules = (await db.execute(
        select(func.count()).select_from(Module)
    )).scalar() or 0

    return {
        "user": {"id": u.id, "full_name": u.full_name, "email": u.email,
                 "avatar_url": u.avatar_url, "role": "super_admin"},
        "stats": {
            "total_students": total_students,
            "total_teachers": total_teachers,
            "total_courses": total_courses,
            "scheduled_classes": scheduled,
            "new_students_month": new_month,
            "income_month": income_month,
            "pending_payments": pending_payments,
            "certificates_issued": certs_issued,
            "unassigned_students": unassigned_students,  # V1.5.1
            "teachers_without_students": teachers_without_students,  # V1.5.1
            "total_modules": total_modules,  # V1.6.4
        },
    }


# === USUARIOS ===
@router.get("/users")
async def list_users(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    role: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
):
    offset = (page - 1) * limit
    stmt = select(User)
    if role:
        try:
            stmt = stmt.where(User.role == UserRole(role))
        except ValueError:
            pass
    if q:
        stmt = stmt.where(or_(User.full_name.ilike(f"%{q}%"), User.email.ilike(f"%{q}%")))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    items = (await db.execute(stmt.order_by(User.created_at.desc()).offset(offset).limit(limit))).scalars().all()
    # V1.4: enriquecer estudiantes con su nivel y estado de pausa
    out_items = []
    for u in items:
        item = {
            "id": u.id, "email": u.email, "full_name": u.full_name,
            "phone": u.phone, "role": u.role.value, "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "level_code": None, "is_paused": False, "placement_done": False,
        }
        if u.role == UserRole.student:
            st = await db.get(Student, u.id)
            if st:
                item["is_paused"] = st.is_paused
                item["placement_done"] = st.placement_done
                if st.current_level_id:
                    lvl = await db.get(Level, st.current_level_id)
                    item["level_code"] = lvl.code if lvl else None
        out_items.append(item)
    return {
        "items": out_items,
        "total": total, "page": page, "limit": limit,
    }


@router.post("/users", status_code=201)
async def create_user(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    for f in ("email", "password", "full_name", "role"):
        if not body.get(f):
            raise HTTPException(400, f"{f} requerido")
    if (await db.execute(select(User).where(User.email == body["email"]))).scalar_one_or_none():
        raise HTTPException(409, "Email ya existe")
    try:
        role = UserRole(body["role"])
    except ValueError:
        raise HTTPException(400, "Rol inválido")
    user = User(
        email=body["email"], password_hash=hash_password(body["password"]),
        full_name=body["full_name"], phone=body.get("phone"), role=role,
    )
    db.add(user)
    await db.flush()
    if role == UserRole.student:
        db.add(Student(user_id=user.id))
    elif role == UserRole.teacher:
        db.add(Teacher(user_id=user.id, specialties=body.get("specialties", ""),
                       modalities=body.get("modalities", "online"), bio=body.get("bio")))
    await log_action(db, admin.user_id, "create_user", "admin", target_id=user.id)
    await db.commit()
    return {"id": user.id}


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404)
    for f in ("full_name", "phone", "avatar_url", "is_active"):
        if f in body:
            setattr(user, f, body[f])
    await log_action(db, admin.user_id, "update_user", "admin", target_id=user_id)
    await db.commit()
    return {"ok": True}


# === CURSOS / NIVELES / MÓDULOS / LECCIONES ===
@router.get("/courses")
async def admin_courses(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    items = (await db.execute(select(Course).order_by(Course.order_index))).scalars().all()
    out = []
    for c in items:
        level_count = (await db.execute(
            select(func.count()).select_from(Level).where(Level.course_id == c.id)
        )).scalar() or 0
        out.append({
            "id": c.id, "code": c.code, "name": c.name, "description": c.description,
            "color": c.color, "image_url": c.image_url, "is_active": c.is_active,
            "level_count": level_count,
        })
    return out


@router.post("/courses", status_code=201)
async def create_course(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    if not body.get("code") or not body.get("name"):
        raise HTTPException(400)
    if (await db.execute(select(Course).where(Course.code == body["code"]))).scalar_one_or_none():
        raise HTTPException(409, "Código ya existe")
    c = Course(
        code=body["code"], name=body["name"], description=body.get("description"),
        color=body.get("color", "#4361ee"), image_url=body.get("image_url"),
        is_active=body.get("is_active", True),
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return {"id": c.id}


@router.patch("/courses/{course_id}")
async def update_course(
    course_id: int, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    c = await db.get(Course, course_id)
    if not c:
        raise HTTPException(404)
    for f in ("name", "description", "color", "image_url", "is_active"):
        if f in body:
            setattr(c, f, body[f])
    await db.commit()
    return {"ok": True}


@router.post("/levels", status_code=201)
async def create_level(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    for f in ("course_id", "code", "name"):
        if not body.get(f):
            raise HTTPException(400)
    l = Level(
        course_id=body["course_id"], code=body["code"], name=body["name"],
        description=body.get("description"), hours_required=body.get("hours_required", 120),
    )
    db.add(l)
    await db.commit()
    await db.refresh(l)
    return {"id": l.id}


# V2.1: Endpoints POST /modules y POST /lessons obsoletos eliminados.
# Las versiones actualizadas están más abajo (con order_index).


@router.patch("/lessons/{lesson_id}")
async def update_lesson(
    lesson_id: int, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    lesson = await db.get(Lesson, lesson_id)
    if not lesson:
        raise HTTPException(404)
    for f in ("title", "description", "objectives", "can_do",
              "video_url", "pdf_url", "audio_url", "duration_min", "is_published"):
        if f in body:
            setattr(lesson, f, body[f])
    await db.commit()
    return {"ok": True}


@router.delete("/lessons/{lesson_id}")
async def delete_lesson(
    lesson_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.6.4: Eliminar una lección."""
    lesson = await db.get(Lesson, lesson_id)
    if not lesson:
        raise HTTPException(404, "Lección no encontrada")
    await log_action(db, admin.user_id, "delete_lesson", "catalog", target_id=str(lesson_id))
    await db.delete(lesson)
    await db.commit()
    return {"ok": True}


# === SEDES Y AULAS ===
@router.get("/branches")
async def list_branches(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    items = (await db.execute(select(Branch).order_by(Branch.id))).scalars().all()
    out = []
    for b in items:
        rooms_count = (await db.execute(
            select(func.count()).select_from(Classroom).where(Classroom.branch_id == b.id)
        )).scalar() or 0
        out.append({
            "id": b.id, "name": b.name, "address": b.address, "phone": b.phone,
            "is_active": b.is_active, "classrooms_count": rooms_count,
        })
    return out


@router.post("/branches", status_code=201)
async def create_branch(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    if not body.get("name"):
        raise HTTPException(400)
    b = Branch(name=body["name"], address=body.get("address"), phone=body.get("phone"))
    db.add(b)
    await db.commit()
    await db.refresh(b)
    return {"id": b.id}


@router.get("/classrooms")
async def list_classrooms(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    branch_id: int | None = None,
):
    stmt = select(Classroom, Branch).join(Branch, Classroom.branch_id == Branch.id)
    if branch_id:
        stmt = stmt.where(Classroom.branch_id == branch_id)
    rows = (await db.execute(stmt)).all()
    return [{
        "id": c.id, "name": c.name, "capacity": c.capacity,
        "branch_id": c.branch_id, "branch_name": b.name, "is_active": c.is_active,
    } for c, b in rows]


@router.post("/classrooms", status_code=201)
async def create_classroom(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    for f in ("branch_id", "name"):
        if not body.get(f):
            raise HTTPException(400)
    c = Classroom(branch_id=body["branch_id"], name=body["name"], capacity=body.get("capacity", 15))
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return {"id": c.id}


# === CLASES ===
@router.get("/sessions")
async def list_admin_sessions(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    offset = (page - 1) * limit
    stmt = select(ClassSession).order_by(ClassSession.starts_at_utc.desc()).offset(offset).limit(limit)
    sessions = (await db.execute(stmt)).scalars().all()
    out = []
    for s in sessions:
        teacher_user = await db.get(User, s.teacher_id)
        course = await db.get(Course, s.course_id)
        level = await db.get(Level, s.level_id)
        out.append({
            "id": s.id, "title": s.title, "modality": s.modality.value,
            "starts_at_utc": s.starts_at_utc.isoformat(),
            "ends_at_utc": s.ends_at_utc.isoformat(),
            "teacher_id": s.teacher_id, "teacher_name": teacher_user.full_name if teacher_user else None,
            "course_id": s.course_id, "course_name": course.name if course else None,
            "level_id": s.level_id, "level_code": level.code if level else None,
            "branch_id": s.branch_id, "classroom_id": s.classroom_id,
            "meeting_url": s.meeting_url, "capacity": s.capacity, "status": s.status.value,
        })
    return {"items": out, "page": page, "limit": limit}


@router.post("/sessions", status_code=201)
async def create_session(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    for f in ("teacher_id", "course_id", "level_id", "title", "modality", "starts_at_utc", "ends_at_utc"):
        if not body.get(f):
            raise HTTPException(400, f"{f} requerido")
    # V2.1: validar fechas
    try:
        starts_at = datetime.fromisoformat(body["starts_at_utc"].replace("Z", "+00:00"))
        ends_at = datetime.fromisoformat(body["ends_at_utc"].replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(400, "Formato de fecha inválido")
    if ends_at <= starts_at:
        raise HTTPException(400, "La hora de fin debe ser posterior a la hora de inicio")
    s = ClassSession(
        teacher_id=body["teacher_id"], course_id=body["course_id"], level_id=body["level_id"],
        title=body["title"], description=body.get("description"),
        modality=Modality(body["modality"]),
        starts_at_utc=starts_at,
        ends_at_utc=ends_at,
        meeting_url=body.get("meeting_url"),
        branch_id=body.get("branch_id"), classroom_id=body.get("classroom_id"),
        capacity=body.get("capacity", 15),
        module_id=body.get("module_id"),  # V1.5
        is_open_event=body.get("is_open_event", False),
    )
    db.add(s)
    await db.flush()

    # Notificar a los estudiantes del nivel
    students = (await db.execute(
        select(Enrollment.student_id).where(
            Enrollment.level_id == body["level_id"], Enrollment.is_active.is_(True),
        )
    )).scalars().all()
    for sid in students:
        db.add(Notification(
            user_id=sid, type=NotificationType.class_scheduled,
            title=f"Nueva clase: {s.title}",
            body=f"Inicia: {s.starts_at_utc.strftime('%d/%m %H:%M')}",
            link="/dashboard/student/calendar",
        ))

    await log_action(db, admin.user_id, "create_session", "admin", target_id=s.id)
    await db.commit()
    return {"id": s.id}


@router.delete("/sessions/{session_id}")
async def cancel_session(
    session_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404)
    s.status = SessionStatus.cancelled
    await log_action(db, admin.user_id, "cancel_session", "admin", target_id=session_id)
    await db.commit()
    return {"ok": True}


# === INSCRIPCIONES ===
@router.get("/enrollments")
async def list_enrollments(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    student_id: str | None = None,
):
    stmt = (
        select(Enrollment, User, Course, Level)
        .join(User, Enrollment.student_id == User.id)
        .join(Course, Enrollment.course_id == Course.id)
        .join(Level, Enrollment.level_id == Level.id)
    )
    if student_id:
        stmt = stmt.where(Enrollment.student_id == student_id)
    stmt = stmt.order_by(Enrollment.enrolled_at.desc()).limit(200)
    rows = (await db.execute(stmt)).all()
    out = []
    for e, u, c, l in rows:
        teacher_name = None
        plan_name = None
        if e.teacher_id:
            t_user = await db.get(User, e.teacher_id)
            teacher_name = t_user.full_name if t_user else None
        if e.plan_id:
            p = await db.get(Plan, e.plan_id)
            plan_name = p.name if p else None
        out.append({
            "id": e.id, "student_id": u.id, "student_name": u.full_name,
            "course_id": c.id, "course_name": c.name,
            "level_id": l.id, "level_code": l.code, "level_name": l.name,
            "teacher_id": e.teacher_id, "teacher_name": teacher_name,  # V1.5
            "plan_id": e.plan_id, "plan_name": plan_name,  # V1.5
            "enrolled_at": e.enrolled_at.isoformat() if e.enrolled_at else None,
            "is_active": e.is_active,
            "final_grade": float(e.final_grade) if e.final_grade else None,
        })
    return out


@router.post("/enrollments", status_code=201)
async def create_enrollment(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    for f in ("student_id", "course_id", "level_id"):
        if not body.get(f):
            raise HTTPException(400)

    teacher_id = body.get("teacher_id")
    auto_assigned = False

    # V1.5.1: Si no se especifica profe, auto-asignar al menos cargado del nivel
    if not teacher_id:
        # Buscar el nivel
        level = await db.get(Level, body["level_id"])
        if level:
            # Buscar profes que enseñan ese nivel
            all_teachers_rows = (await db.execute(
                select(Teacher, User).join(User, Teacher.user_id == User.id)
                .where(User.is_active.is_(True), User.role == UserRole.teacher)
            )).all()

            explicit_candidates = []
            inferred_candidates = []
            no_config_candidates = []

            for t, u in all_teachers_rows:
                explicit = set(s.strip().upper() for s in (t.levels_taught or "").split(",") if s.strip())
                load = (await db.execute(
                    select(func.count()).select_from(Enrollment).where(
                        Enrollment.teacher_id == u.id, Enrollment.is_active.is_(True),
                    )
                )).scalar() or 0
                if level.code in explicit:
                    explicit_candidates.append((u.id, load, u.full_name))
                elif not explicit:
                    no_config_candidates.append((u.id, load, u.full_name))
                else:
                    count_in_level = (await db.execute(
                        select(func.count()).select_from(Enrollment).where(
                            Enrollment.teacher_id == u.id,
                            Enrollment.level_id == level.id,
                            Enrollment.is_active.is_(True),
                        )
                    )).scalar() or 0
                    if count_in_level > 0:
                        inferred_candidates.append((u.id, load, u.full_name))

            candidates = explicit_candidates or inferred_candidates or no_config_candidates
            if candidates:
                candidates.sort(key=lambda x: x[1])
                teacher_id = candidates[0][0]
                auto_assigned = True

    e = Enrollment(
        student_id=body["student_id"], course_id=body["course_id"],
        level_id=body["level_id"], teacher_id=teacher_id,
        plan_id=body.get("plan_id"),
    )
    db.add(e)
    # V1.4.1: Actualizar nivel del estudiante + marcar placement_done
    st = await db.get(Student, body["student_id"])
    if st:
        st.current_level_id = body["level_id"]
        if not st.placement_done:
            st.placement_done = True
    # Notificación al estudiante
    db.add(Notification(
        user_id=body["student_id"],
        type=NotificationType.info,
        title="🎓 Inscripción confirmada",
        body="Has sido inscrito en un curso. Revisá tu dashboard.",
        link="/dashboard/student",
    ))
    # V1.5.1: Notificar al profe si fue auto-asignado
    if auto_assigned and teacher_id:
        st_user = await db.get(User, body["student_id"])
        level_obj = await db.get(Level, body["level_id"])
        db.add(Notification(
            user_id=teacher_id,
            type=NotificationType.info,
            title="👥 Nuevo estudiante asignado",
            body=f"{st_user.full_name if st_user else 'Estudiante'} fue asignado a tu grupo de {level_obj.code if level_obj else ''}.",
            link="/dashboard/teacher/students",
        ))
    await log_action(db, admin.user_id, "enroll", "admin", target_id=e.id,
                     details=f"auto_assigned={auto_assigned}")
    await db.commit()
    return {"id": e.id, "auto_assigned_teacher_id": teacher_id if auto_assigned else None}


# === PLANES Y PAGOS ===
@router.get("/plans")
async def list_plans(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    items = (await db.execute(select(Plan).order_by(Plan.id))).scalars().all()
    return [{
        "id": p.id, "code": p.code, "name": p.name, "description": p.description,
        "price": float(p.price), "currency": p.currency,
        "duration_months": p.duration_months, "features": p.features,
        "is_active": p.is_active,
    } for p in items]


@router.post("/plans", status_code=201)
async def create_plan(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    for f in ("code", "name", "price"):
        if body.get(f) is None:
            raise HTTPException(400)
    p = Plan(
        code=body["code"], name=body["name"],
        description=body.get("description"), price=body["price"],
        currency=body.get("currency", "USD"),
        duration_months=body.get("duration_months", 1),
        features=body.get("features"),
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return {"id": p.id}


@router.get("/payments")
async def list_payments(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Payment, User)
        .join(User, Payment.student_id == User.id)
        .order_by(Payment.created_at.desc()).limit(100)
    )
    rows = (await db.execute(stmt)).all()
    return [{
        "id": p.id, "student_name": u.full_name, "student_id": u.id,
        "amount": float(p.amount), "currency": p.currency,
        "status": p.status.value, "method": p.method,
        "paid_at": p.paid_at.isoformat() if p.paid_at else None,
        "created_at": p.created_at.isoformat(),
    } for p, u in rows]


@router.get("/finance/summary")
async def finance_summary(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(tz.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    income_month = float((await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == PaymentStatus.paid, Payment.paid_at >= month_start,
        )
    )).scalar() or 0)
    income_year = float((await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == PaymentStatus.paid, Payment.paid_at >= year_start,
        )
    )).scalar() or 0)
    pending_amount = float((await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == PaymentStatus.pending,
        )
    )).scalar() or 0)
    active_subscriptions = (await db.execute(
        select(func.count()).select_from(Payment).where(
            Payment.status == PaymentStatus.paid,
            Payment.paid_at > now - timedelta(days=31),
        )
    )).scalar() or 0
    return {
        "income_month": income_month,
        "income_year": income_year,
        "pending_amount": pending_amount,
        "active_subscriptions": active_subscriptions,
    }


# === CERTIFICADOS ===
def _generate_code() -> str:
    return "DRSM-" + token_urlsafe(6).upper().replace("_", "").replace("-", "")[:8]


@router.get("/certificates")
async def list_certs(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Certificate, User, Course, Level)
        .join(User, Certificate.student_id == User.id)
        .join(Course, Certificate.course_id == Course.id)
        .join(Level, Certificate.level_id == Level.id)
        .order_by(Certificate.issued_at.desc())
    )
    rows = (await db.execute(stmt)).all()
    return [{
        "id": c.id, "code": c.code,
        "student_name": u.full_name, "student_id": u.id,
        "course_name": course.name, "level_code": l.code,
        "hours": c.hours, "final_grade": float(c.final_grade) if c.final_grade else None,
        "issued_at": c.issued_at.isoformat(), "revoked": c.revoked,
    } for c, u, course, l in rows]


@router.post("/certificates", status_code=201)
async def issue_cert(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    for f in ("student_id", "course_id", "level_id"):
        if not body.get(f):
            raise HTTPException(400)
    c = Certificate(
        code=_generate_code(),
        student_id=body["student_id"], course_id=body["course_id"], level_id=body["level_id"],
        hours=body.get("hours", 120), final_grade=body.get("final_grade"),
    )
    db.add(c)
    db.add(Notification(
        user_id=body["student_id"], type=NotificationType.info,
        title="🎉 ¡Nuevo certificado emitido!",
        body=f"Tu código: {c.code}",
        link="/dashboard/student/certificates",
    ))
    await log_action(db, admin.user_id, "issue_certificate", "admin", target_id=c.id)
    await db.commit()
    await db.refresh(c)
    return {"id": c.id, "code": c.code}


# === SETTINGS ===
@router.get("/settings")
async def get_settings(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    s = await db.get(InstituteSetting, 1)
    if not s:
        s = InstituteSetting(id=1)
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return {
        "name": s.name, "logo_url": s.logo_url,
        "primary_color": s.primary_color, "accent_color": s.accent_color,
        "contact_email": s.contact_email, "contact_phone": s.contact_phone,
        "address": s.address, "timezone": s.timezone,
    }


@router.patch("/settings")
async def update_settings(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    s = await db.get(InstituteSetting, 1)
    if not s:
        s = InstituteSetting(id=1)
        db.add(s)
    for f in ("name", "logo_url", "primary_color", "accent_color",
              "contact_email", "contact_phone", "address", "timezone"):
        if f in body:
            setattr(s, f, body[f])
    await log_action(db, admin.user_id, "update_settings", "admin")
    await db.commit()
    return {"ok": True}


@router.get("/audit-logs")
async def audit_logs(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
):
    offset = (page - 1) * limit
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
    logs = (await db.execute(stmt)).scalars().all()
    return {
        "items": [{
            "id": l.id, "user_id": l.user_id, "action": l.action,
            "module": l.module, "target_id": l.target_id, "ip": l.ip,
            "created_at": l.created_at.isoformat(),
        } for l in logs],
        "page": page, "limit": limit,
    }


@router.get("/levels-by-course/{course_id}")
async def levels_by_course(
    course_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Lista de niveles de un curso específico — para selects."""
    levels = (await db.execute(
        select(Level).where(Level.course_id == course_id).order_by(Level.order_index)
    )).scalars().all()
    return [{"id": l.id, "code": l.code, "name": l.name} for l in levels]


@router.get("/teachers")
async def list_teachers_simple(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Lista simple de profesores — para selects de asignación."""
    teachers = (await db.execute(
        select(User).where(User.role == UserRole.teacher, User.is_active.is_(True))
    )).scalars().all()
    return [{"id": t.id, "full_name": t.full_name, "email": t.email} for t in teachers]


@router.get("/students-simple")
async def list_students_simple(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Lista simple de estudiantes — para selects de inscripción/certificado."""
    students = (await db.execute(
        select(User).where(User.role == UserRole.student, User.is_active.is_(True))
        .order_by(User.full_name)
    )).scalars().all()
    return [{"id": s.id, "full_name": s.full_name, "email": s.email} for s in students]


@router.get("/at-risk-students")
async def at_risk_students(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Estudiantes con 3+ ausencias en sus últimas 10 clases."""
    from app.models import AttendanceState as AS

    # Obtener todos los estudiantes
    students = (await db.execute(
        select(User).where(User.role == UserRole.student)
    )).scalars().all()

    at_risk = []
    for st in students:
        # Últimos 10 attendance records con state asignado
        attendances = (await db.execute(
            select(SessionAttendance, ClassSession)
            .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
            .where(
                SessionAttendance.student_id == st.id,
                SessionAttendance.state.is_not(None),
            )
            .order_by(ClassSession.starts_at_utc.desc())
            .limit(10)
        )).all()
        if not attendances:
            continue
        absent_count = sum(1 for a, _ in attendances if a.state == AS.absent)
        if absent_count >= 3:
            at_risk.append({
                "student_id": st.id,
                "full_name": st.full_name,
                "email": st.email,
                "absent_count": absent_count,
                "total_recorded": len(attendances),
                "absent_rate": round(absent_count * 100 / len(attendances), 1),
            })
    return at_risk


# ============= V1.3 — EDICIÓN UNIVERSAL =============

# --- Levels ---
@router.patch("/levels/{level_id}")
async def update_level(
    level_id: int, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    lvl = await db.get(Level, level_id)
    if not lvl: raise HTTPException(404)
    for field in ["code", "name", "order_index", "is_active"]:
        if field in body and body[field] is not None:
            setattr(lvl, field, body[field])
    await log_action(db, admin.user_id, "update_level", "levels", str(level_id))
    await db.commit()
    return {"ok": True}


# --- Modules ---
@router.patch("/modules/{module_id}")
async def update_module(
    module_id: int, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    m = await db.get(Module, module_id)
    if not m: raise HTTPException(404)
    for field in ["name", "description", "order_index"]:
        if field in body and body[field] is not None:
            setattr(m, field, body[field])
    await db.commit()
    return {"ok": True}


@router.delete("/modules/{module_id}")
async def delete_module(
    module_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    m = await db.get(Module, module_id)
    if not m: raise HTTPException(404)
    # Borrar solo si no tiene lecciones
    has_lessons = (await db.execute(select(func.count()).select_from(Lesson).where(Lesson.module_id == module_id))).scalar()
    if has_lessons:
        raise HTTPException(400, "El módulo tiene lecciones. Eliminá las lecciones primero.")
    await db.delete(m)
    await db.commit()
    return {"ok": True}


# --- Sessions PATCH (editar clase) ---
@router.patch("/sessions/{session_id}")
async def update_session(
    session_id: str, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    from datetime import timezone as tz
    s = await db.get(ClassSession, session_id)
    if not s: raise HTTPException(404)
    # ¿Es pasada? Si sí, solo permite editar título/descripción
    starts = s.starts_at_utc
    if starts.tzinfo is None:
        starts = starts.replace(tzinfo=tz.utc)
    is_past = starts <= datetime.now(tz.utc)
    allowed_past = {"title", "description", "recording_url", "teacher_notes"}
    for field, value in body.items():
        if is_past and field not in allowed_past:
            continue  # ignorar campos no permitidos para clases pasadas
        if field == "starts_at_utc" and value:
            s.starts_at_utc = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif field == "ends_at_utc" and value:
            s.ends_at_utc = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif field == "modality" and value:
            s.modality = Modality(value)
        elif hasattr(s, field):
            setattr(s, field, value)
    await log_action(db, admin.user_id, "update_session", "class_sessions", session_id)
    await db.commit()
    return {"ok": True, "is_past": is_past}


# --- Enrollments PATCH (cambiar teacher, plan, level del estudiante) ---
@router.patch("/enrollments/{enroll_id}")
async def update_enrollment(
    enroll_id: str, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    enr = await db.get(Enrollment, enroll_id)
    if not enr: raise HTTPException(404)
    old_teacher = enr.teacher_id
    old_plan = enr.plan_id
    old_level = enr.level_id
    if "teacher_id" in body: enr.teacher_id = body["teacher_id"]
    if "plan_id" in body: enr.plan_id = body["plan_id"]
    if "level_id" in body: enr.level_id = body["level_id"]
    if "is_active" in body: enr.is_active = body["is_active"]
    # Notificar al estudiante del cambio
    changes = []
    if old_teacher != enr.teacher_id: changes.append("profesor")
    if old_plan != enr.plan_id: changes.append("plan")
    if old_level != enr.level_id: changes.append("nivel")
    if changes:
        db.add(Notification(
            user_id=enr.student_id,
            type=NotificationType.info,
            title="📝 Cambios en tu inscripción",
            body=f"Se actualizó tu {', '.join(changes)}. Consultá los detalles con un coordinador.",
        ))
    await log_action(db, admin.user_id, "update_enrollment", "enrollments", enroll_id)
    await db.commit()
    return {"ok": True}


@router.delete("/enrollments/{enroll_id}")
async def delete_enrollment(
    enroll_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Desactiva la inscripción (soft delete)."""
    enr = await db.get(Enrollment, enroll_id)
    if not enr: raise HTTPException(404)
    enr.is_active = False
    await log_action(db, admin.user_id, "deactivate_enrollment", "enrollments", enroll_id)
    await db.commit()
    return {"ok": True}


# --- Branches y Classrooms PATCH ---
@router.patch("/branches/{branch_id}")
async def update_branch(
    branch_id: int, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    b = await db.get(Branch, branch_id)
    if not b: raise HTTPException(404)
    for field in ["name", "address", "phone", "is_active"]:
        if field in body and body[field] is not None:
            setattr(b, field, body[field])
    await db.commit()
    return {"ok": True}


@router.patch("/classrooms/{room_id}")
async def update_classroom(
    room_id: int, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    r = await db.get(Classroom, room_id)
    if not r: raise HTTPException(404)
    for field in ["name", "capacity", "is_active"]:
        if field in body and body[field] is not None:
            setattr(r, field, body[field])
    await db.commit()
    return {"ok": True}


# --- PLANS — CRUD completo con features ---
@router.patch("/plans/{plan_id}")
async def update_plan(
    plan_id: int, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(Plan, plan_id)
    if not p: raise HTTPException(404)
    for field in ["name", "description", "price", "billing_cycle", "is_active"]:
        if field in body and body[field] is not None:
            setattr(p, field, body[field])
    await log_action(db, admin.user_id, "update_plan", "plans", str(plan_id))
    await db.commit()
    return {"ok": True}


@router.delete("/plans/{plan_id}")
async def delete_plan(
    plan_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Desactiva el plan (soft) si tiene inscripciones."""
    p = await db.get(Plan, plan_id)
    if not p: raise HTTPException(404)
    # Si tiene enrollments activos, soft delete
    has_enrollments = (await db.execute(
        select(func.count()).select_from(Enrollment).where(
            Enrollment.plan_id == plan_id, Enrollment.is_active.is_(True)
        )
    )).scalar()
    if has_enrollments:
        p.is_active = False
        await log_action(db, admin.user_id, "deactivate_plan", "plans", str(plan_id))
    else:
        await db.delete(p)
        await log_action(db, admin.user_id, "delete_plan", "plans", str(plan_id))
    await db.commit()
    return {"ok": True, "deactivated": bool(has_enrollments)}


@router.get("/plans/{plan_id}/features")
async def list_plan_features(
    plan_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    feats = (await db.execute(
        select(PlanFeature).where(PlanFeature.plan_id == plan_id).order_by(PlanFeature.order_index)
    )).scalars().all()
    return [{"id": f.id, "feature": f.feature, "is_included": f.is_included, "order_index": f.order_index} for f in feats]


@router.post("/plans/{plan_id}/features", status_code=201)
async def add_plan_feature(
    plan_id: int, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    f = PlanFeature(
        plan_id=plan_id,
        feature=body.get("feature", ""),
        is_included=body.get("is_included", True),
        order_index=body.get("order_index", 0),
    )
    db.add(f)
    await db.commit()
    return {"id": f.id}


@router.patch("/plan-features/{feature_id}")
async def update_plan_feature(
    feature_id: int, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    f = await db.get(PlanFeature, feature_id)
    if not f: raise HTTPException(404)
    if "feature" in body: f.feature = body["feature"]
    if "is_included" in body: f.is_included = body["is_included"]
    await db.commit()
    return {"ok": True}


@router.delete("/plan-features/{feature_id}")
async def delete_plan_feature(
    feature_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    f = await db.get(PlanFeature, feature_id)
    if not f: raise HTTPException(404)
    await db.delete(f)
    await db.commit()
    return {"ok": True}


# --- Courses DELETE (soft) ---
@router.delete("/courses/{course_id}")
async def deactivate_course(
    course_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    c = await db.get(Course, course_id)
    if not c: raise HTTPException(404)
    # Si tiene enrollments activos → solo desactiva
    has_enr = (await db.execute(
        select(func.count()).select_from(Enrollment).where(
            Enrollment.course_id == course_id, Enrollment.is_active.is_(True)
        )
    )).scalar()
    c.is_active = False
    await log_action(db, admin.user_id, "deactivate_course", "courses", str(course_id))
    await db.commit()
    return {"ok": True, "had_enrollments": bool(has_enr)}


# --- PAUSE/RESUME estudiante ---
@router.post("/students/{student_id}/pause")
async def pause_student(
    student_id: str, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    from datetime import timezone as tz
    st = await db.get(Student, student_id)
    if not st: raise HTTPException(404, "Estudiante no encontrado")
    if st.is_paused:
        raise HTTPException(400, "El estudiante ya está pausado")
    st.is_paused = True
    st.paused_at = datetime.now(tz.utc)
    st.pause_reason = body.get("reason", "Sin especificar")
    # Desactivar enrollments temporalmente? NO — los dejamos activos para que se conserve progreso
    db.add(Notification(
        user_id=student_id,
        type=NotificationType.info,
        title="⏸ Tu cuenta fue pausada",
        body=f"Razón: {st.pause_reason}. Reactivá con un coordinador cuando quieras volver.",
    ))
    await log_action(db, admin.user_id, "pause_student", "students", student_id)
    await db.commit()
    return {"ok": True}


@router.post("/students/{student_id}/resume")
async def resume_student(
    student_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    st = await db.get(Student, student_id)
    if not st: raise HTTPException(404)
    if not st.is_paused:
        raise HTTPException(400, "El estudiante no está pausado")
    st.is_paused = False
    st.paused_at = None
    st.pause_reason = None
    db.add(Notification(
        user_id=student_id,
        type=NotificationType.info,
        title="▶ Tu cuenta fue reactivada",
        body="Bienvenido de vuelta. Continuá donde lo dejaste.",
    ))
    await log_action(db, admin.user_id, "resume_student", "students", student_id)
    await db.commit()
    return {"ok": True}


@router.get("/paused-students")
async def list_paused_students(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(Student, User).join(User, Student.user_id == User.id).where(Student.is_paused.is_(True))
    )).all()
    return [{
        "student_id": s.user_id, "full_name": u.full_name, "email": u.email,
        "paused_at": s.paused_at.isoformat() if s.paused_at else None,
        "reason": s.pause_reason,
    } for s, u in rows]


# ============= V1.4 — PLACEMENT RESULTS =============
@router.get("/placement-results")
async def list_placement_results(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    status: str = "all",  # all, pending, enrolled
):
    """Lista estudiantes que completaron placement test, con su nivel sugerido."""
    from app.models import PlacementTest
    q = select(PlacementTest, User, Student, Level).join(
        Student, PlacementTest.student_id == Student.user_id
    ).join(User, Student.user_id == User.id).outerjoin(
        Level, PlacementTest.suggested_level_id == Level.id
    ).where(PlacementTest.completed_at.is_not(None)).order_by(PlacementTest.completed_at.desc())
    rows = (await db.execute(q)).all()
    out = []
    for test, u, s, lvl in rows:
        # ¿Tiene inscripción activa?
        has_enrollment = (await db.execute(
            select(func.count()).select_from(Enrollment).where(
                Enrollment.student_id == u.id, Enrollment.is_active.is_(True)
            )
        )).scalar() > 0
        if status == "pending" and has_enrollment: continue
        if status == "enrolled" and not has_enrollment: continue
        out.append({
            "test_id": test.id,
            "student_id": u.id,
            "student_name": u.full_name,
            "student_email": u.email,
            "phone": u.phone,
            "completed_at": test.completed_at.isoformat() if test.completed_at else None,
            "suggested_level_id": test.suggested_level_id,
            "suggested_level_code": lvl.code if lvl else None,
            "suggested_level_name": lvl.name if lvl else None,
            "grammar_score": float(test.grammar_score) if test.grammar_score is not None else None,
            "reading_score": float(test.reading_score) if test.reading_score is not None else None,
            "is_enrolled": has_enrollment,
            "is_paused": s.is_paused,
        })
    return out


@router.get("/placement-results/{test_id}")
async def get_placement_detail(
    test_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Detalle completo del placement test con cada respuesta."""
    from app.models import PlacementTest, PlacementAnswer, PlacementQuestion
    test = await db.get(PlacementTest, test_id)
    if not test: raise HTTPException(404)
    u = await db.get(User, test.student_id)
    lvl = await db.get(Level, test.suggested_level_id) if test.suggested_level_id else None
    answers = (await db.execute(
        select(PlacementAnswer, PlacementQuestion).join(
            PlacementQuestion, PlacementAnswer.question_id == PlacementQuestion.id
        ).where(PlacementAnswer.placement_test_id == test_id)
    )).all()
    return {
        "test_id": test.id,
        "student_name": u.full_name if u else None,
        "student_email": u.email if u else None,
        "completed_at": test.completed_at.isoformat() if test.completed_at else None,
        "suggested_level_code": lvl.code if lvl else None,
        "suggested_level_name": lvl.name if lvl else None,
        "scores": {
            "grammar": float(test.grammar_score) if test.grammar_score is not None else None,
            "reading": float(test.reading_score) if test.reading_score is not None else None,
            "listening": None, "writing": None, "speaking": None,
        },
        "answers": [{
            "statement": q.statement,
            "skill": q.skill, "difficulty": q.difficulty_level,
            "selected": a.selected_option,
            "correct": q.correct_option,
            "is_correct": a.is_correct,
        } for a, q in answers],
    }


# ============= V1.4 — MÓDULOS Y LECCIONES (CRUD admin) =============
@router.get("/levels/{level_id}/modules")
async def list_level_modules(
    level_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    mods = (await db.execute(
        select(Module).where(Module.level_id == level_id).order_by(Module.order_index)
    )).scalars().all()
    out = []
    for m in mods:
        lessons_count = (await db.execute(
            select(func.count()).select_from(Lesson).where(Lesson.module_id == m.id)
        )).scalar() or 0
        out.append({
            "id": m.id, "name": m.name, "description": m.description,
            "order_index": m.order_index, "lessons_count": lessons_count,
        })
    return out


@router.post("/modules", status_code=201)
async def create_module(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    if not body.get("level_id") or not body.get("name"):
        raise HTTPException(400, "level_id y name requeridos")
    m = Module(
        level_id=body["level_id"], name=body["name"],
        description=body.get("description"),
        order_index=body.get("order_index", 0),
    )
    db.add(m)
    await db.commit()
    return {"id": m.id, "name": m.name}


@router.get("/modules/{module_id}/lessons")
async def list_module_lessons(
    module_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    lessons = (await db.execute(
        select(Lesson).where(Lesson.module_id == module_id).order_by(Lesson.order_index)
    )).scalars().all()
    return [{
        "id": l.id, "title": l.title, "description": l.description,
        "duration_min": l.duration_min, "order_index": l.order_index,
        "video_url": l.video_url, "pdf_url": l.pdf_url, "audio_url": l.audio_url,
        "is_published": l.is_published,
    } for l in lessons]


@router.post("/lessons", status_code=201)
async def create_lesson(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    if not body.get("module_id") or not body.get("title"):
        raise HTTPException(400, "module_id y title requeridos")
    l = Lesson(
        module_id=body["module_id"], title=body["title"],
        description=body.get("description"),
        objectives=body.get("objectives"),
        can_do=body.get("can_do"),
        video_url=body.get("video_url"),
        pdf_url=body.get("pdf_url"),
        audio_url=body.get("audio_url"),
        duration_min=body.get("duration_min", 15),
        order_index=body.get("order_index", 0),
        is_published=body.get("is_published", True),
    )
    db.add(l)
    await db.commit()
    return {"id": l.id, "title": l.title}


# ============= V1.4 — PAGOS MANUALES =============
@router.post("/payments", status_code=201)
async def register_payment(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Registra un pago manualmente (transferencia, efectivo, etc.)"""
    from app.models import Payment, PaymentStatus
    from datetime import timezone as tz
    if not body.get("student_id") or not body.get("amount"):
        raise HTTPException(400, "student_id y amount requeridos")
    pay = Payment(
        student_id=body["student_id"],
        plan_id=body.get("plan_id"),
        amount=float(body["amount"]),
        currency=body.get("currency", "USD"),
        status=PaymentStatus.paid,  # Si lo registra el admin manualmente, es porque ya cobró
        method=body.get("method", "cash"),  # cash, transfer, deposit
        reference=body.get("reference"),
        paid_at=datetime.now(tz.utc),
    )
    db.add(pay)
    # Notificación al estudiante
    db.add(Notification(
        user_id=body["student_id"],
        type=NotificationType.info,
        title="💰 Pago registrado",
        body=f"Se registró tu pago de ${float(body['amount']):.2f} {body.get('currency','USD')}. ¡Gracias!",
        link="/dashboard/student",
    ))
    await log_action(db, admin.user_id, "register_payment", "payments", pay.id)
    await db.commit()
    return {"id": pay.id, "ok": True}


# ============= V1.4 — VALIDADOR DE LINKS DE MEETING =============
@router.post("/validate-meeting-url")
async def validate_meeting_url(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
):
    """Valida y detecta el tipo de link de meeting (Zoom/Meet/Teams).

    V1.4.1: Mejor detección de subdominios de Zoom (us05web, us02web, etc.)
    """
    import re
    url = (body.get("url") or "").strip()
    if not url:
        return {"valid": False, "type": None, "reason": "URL vacía"}

    # Zoom: cualquier subdominio.zoom.us con /j/{id} o /my/{nombre} o /webinar/
    if re.match(r"^https?://[a-z0-9-]+(\.[a-z0-9-]+)*\.zoom\.us/(j|my|webinar|s)/[\w?=&.-]+", url, re.IGNORECASE):
        return {"valid": True, "type": "zoom", "label": "Zoom"}

    # Google Meet
    if re.match(r"^https?://meet\.google\.com/[a-z0-9-]+", url, re.IGNORECASE):
        return {"valid": True, "type": "google_meet", "label": "Google Meet"}

    # Microsoft Teams
    if re.match(r"^https?://teams\.microsoft\.com/l/meetup-join/", url, re.IGNORECASE):
        return {"valid": True, "type": "teams", "label": "Microsoft Teams"}

    # Otros HTTPS (advertencia)
    if re.match(r"^https?://[^\s]+", url, re.IGNORECASE):
        return {
            "valid": True, "type": "other", "label": "Link genérico",
            "warning": "El link no es de Zoom, Meet ni Teams. Verificá que sea correcto antes de guardar.",
        }

    return {
        "valid": False, "type": None,
        "reason": "Link no válido. Debe empezar con https:// y ser de Zoom, Google Meet o Microsoft Teams.",
    }


# ============= V1.5.1 — LEVELS TAUGHT + AUTO-ASSIGN =============
@router.get("/teachers-by-level/{level_code}")
async def teachers_by_level(
    level_code: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.5.1: Lista profes que enseñan un nivel específico, con carga actual.

    Combina:
    1. Profes que tienen ese nivel en su campo `levels_taught` (explícito)
    2. Profes que ya tienen al menos 1 estudiante de ese nivel (inferido)
    """
    # Buscar el nivel
    level = (await db.execute(
        select(Level).where(Level.code == level_code.upper()).limit(1)
    )).scalar_one_or_none()
    if not level:
        raise HTTPException(404, "Nivel no encontrado")

    # Todos los profes activos
    all_teachers_rows = (await db.execute(
        select(Teacher, User).join(User, Teacher.user_id == User.id)
        .where(User.is_active.is_(True), User.role == UserRole.teacher)
    )).all()

    out = []
    for t, u in all_teachers_rows:
        # ¿Enseña este nivel? (explícito o inferido)
        explicit_levels = [s.strip().upper() for s in (t.levels_taught or "").split(",") if s.strip()]
        teaches_explicit = level_code.upper() in explicit_levels

        # Conteo de estudiantes en este nivel asignados a él
        student_count = (await db.execute(
            select(func.count()).select_from(Enrollment).where(
                Enrollment.teacher_id == u.id,
                Enrollment.level_id == level.id,
                Enrollment.is_active.is_(True),
            )
        )).scalar() or 0

        # Total de estudiantes (todos los niveles)
        total_students = (await db.execute(
            select(func.count()).select_from(Enrollment).where(
                Enrollment.teacher_id == u.id,
                Enrollment.is_active.is_(True),
            )
        )).scalar() or 0

        # Si tiene marcado el nivel O ya tiene estudiantes ahí, incluirlo
        if teaches_explicit or student_count > 0 or not explicit_levels:
            # Si no tiene levels_taught configurado (None), lo incluimos todos como "potencial"
            out.append({
                "teacher_id": u.id,
                "full_name": u.full_name,
                "email": u.email,
                "teaches_explicit": teaches_explicit,
                "student_count_this_level": student_count,
                "total_students": total_students,
                "levels_taught": explicit_levels,
            })

    # Ordenar por carga (menos estudiantes primero)
    out.sort(key=lambda x: (x["student_count_this_level"], x["total_students"]))
    return out


@router.patch("/teachers/{teacher_id}/levels")
async def update_teacher_levels(
    teacher_id: str, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.5.1: Actualiza los niveles que enseña un profe."""
    t = await db.get(Teacher, teacher_id)
    if not t:
        raise HTTPException(404, "Profesor no encontrado")
    levels = body.get("levels", [])
    if not isinstance(levels, list):
        raise HTTPException(400, "levels debe ser array")
    # Validar códigos
    valid_codes = {"A1", "A2", "B1", "B2", "C1", "C2"}
    cleaned = [str(c).strip().upper() for c in levels if str(c).strip().upper() in valid_codes]
    t.levels_taught = ",".join(cleaned) if cleaned else None
    await log_action(db, admin.user_id, "update_teacher_levels", "teachers", teacher_id)
    await db.commit()
    return {"ok": True, "levels": cleaned}


@router.get("/teachers/{teacher_id}/levels")
async def get_teacher_levels(
    teacher_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.5.1: Niveles que enseña un profe."""
    t = await db.get(Teacher, teacher_id)
    if not t:
        raise HTTPException(404)
    explicit = [s.strip().upper() for s in (t.levels_taught or "").split(",") if s.strip()]
    return {"teacher_id": teacher_id, "levels": explicit}


@router.get("/unassigned-students")
async def list_unassigned_students(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.5.1: Inscripciones activas sin profesor asignado."""
    rows = (await db.execute(
        select(Enrollment, User, Level, Course)
        .join(User, Enrollment.student_id == User.id)
        .join(Level, Enrollment.level_id == Level.id)
        .join(Course, Enrollment.course_id == Course.id)
        .where(
            Enrollment.teacher_id.is_(None),
            Enrollment.is_active.is_(True),
        )
    )).all()
    return [{
        "enrollment_id": e.id,
        "student_id": u.id,
        "student_name": u.full_name,
        "student_email": u.email,
        "course_name": c.name,
        "level_id": l.id,
        "level_code": l.code,
        "level_name": l.name,
    } for e, u, l, c in rows]


@router.post("/auto-assign-teachers")
async def auto_assign_teachers(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.5.1: Distribuye automáticamente los estudiantes sin profe entre los disponibles.

    Lógica:
    1. Para cada inscripción sin profe (teacher_id IS NULL, is_active = true)
    2. Busca profes que enseñan ese nivel (explícito o inferido del histórico)
    3. Asigna al profe con menos carga total
    4. Si NO hay profe para ese nivel, lo deja sin asignar
    5. Notifica al estudiante y al profe
    """
    # Obtener inscripciones sin profe
    rows = (await db.execute(
        select(Enrollment, Level).join(Level, Enrollment.level_id == Level.id).where(
            Enrollment.teacher_id.is_(None),
            Enrollment.is_active.is_(True),
        )
    )).all()

    if not rows:
        return {"ok": True, "assigned": 0, "skipped": 0, "details": []}

    # Obtener todos los profes con su carga actual
    all_teachers_rows = (await db.execute(
        select(Teacher, User).join(User, Teacher.user_id == User.id)
        .where(User.is_active.is_(True), User.role == UserRole.teacher)
    )).all()

    # Map: teacher_id -> {explicit_levels: set, current_load: int}
    teacher_info = {}
    for t, u in all_teachers_rows:
        explicit = set(s.strip().upper() for s in (t.levels_taught or "").split(",") if s.strip())
        current_load = (await db.execute(
            select(func.count()).select_from(Enrollment).where(
                Enrollment.teacher_id == u.id, Enrollment.is_active.is_(True),
            )
        )).scalar() or 0
        teacher_info[u.id] = {
            "user": u,
            "explicit_levels": explicit,
            "current_load": current_load,
        }

    assigned_count = 0
    skipped = []
    details = []

    for e, level in rows:
        # Candidatos: profes que enseñan este nivel
        # Prioridad: 1) Explícito 2) Ya tiene estudiantes del nivel
        explicit_candidates = []
        inferred_candidates = []
        no_config_candidates = []

        for tid, info in teacher_info.items():
            if level.code in info["explicit_levels"]:
                explicit_candidates.append((tid, info))
            elif not info["explicit_levels"]:
                # Profe sin levels_taught configurado → puede enseñar cualquier nivel
                no_config_candidates.append((tid, info))
            else:
                # Verificar si ya tiene estudiantes del nivel
                count_in_level = (await db.execute(
                    select(func.count()).select_from(Enrollment).where(
                        Enrollment.teacher_id == tid,
                        Enrollment.level_id == level.id,
                        Enrollment.is_active.is_(True),
                    )
                )).scalar() or 0
                if count_in_level > 0:
                    inferred_candidates.append((tid, info))

        # Elegir el candidato con menos carga
        candidates = explicit_candidates or inferred_candidates or no_config_candidates
        if not candidates:
            skipped.append({
                "enrollment_id": e.id, "level_code": level.code,
                "reason": "No hay profesor configurado para este nivel",
            })
            continue

        # Ordenar por carga (menos primero)
        candidates.sort(key=lambda x: x[1]["current_load"])
        chosen_tid, chosen_info = candidates[0]

        # Asignar
        e.teacher_id = chosen_tid
        teacher_info[chosen_tid]["current_load"] += 1
        assigned_count += 1

        # Notificar al estudiante
        db.add(Notification(
            user_id=e.student_id,
            type=NotificationType.info,
            title="👨‍🏫 Profesor asignado",
            body=f"Tu profesor para {level.code} es {chosen_info['user'].full_name}.",
            link="/dashboard/student",
        ))
        # Notificar al profe
        student_u = await db.get(User, e.student_id)
        db.add(Notification(
            user_id=chosen_tid,
            type=NotificationType.info,
            title="👥 Nuevo estudiante asignado",
            body=f"Tenés un nuevo estudiante en {level.code}: {student_u.full_name if student_u else 'Estudiante'}.",
            link="/dashboard/teacher/students",
        ))

        details.append({
            "enrollment_id": e.id,
            "student_name": student_u.full_name if student_u else None,
            "level_code": level.code,
            "assigned_teacher": chosen_info["user"].full_name,
        })

    await log_action(db, admin.user_id, "auto_assign_teachers", "system",
                     details=f"assigned={assigned_count}, skipped={len(skipped)}")
    await db.commit()

    return {
        "ok": True,
        "assigned": assigned_count,
        "skipped": len(skipped),
        "details": details,
        "skipped_details": skipped,
    }


# ============= V1.6.3 — DETECTOR CANDIDATOS A CERTIFICACIÓN =============
@router.get("/certification-candidates")
async def list_certification_candidates(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.6.3: Detecta estudiantes que cumplen criterios para certificar.

    Criterios (combinación inteligente):
    - Todos los módulos del nivel completados (ModuleProgress.status = 'completed')
    - Asistencia promedio ≥ 70%
    - No tiene certificado activo para ese curso+nivel
    """
    from app.models import ModuleProgress, Module
    from sqlalchemy import and_

    # Obtener todas las inscripciones activas
    enrollments = (await db.execute(
        select(Enrollment, User, Course, Level)
        .join(User, Enrollment.student_id == User.id)
        .join(Course, Enrollment.course_id == Course.id)
        .join(Level, Enrollment.level_id == Level.id)
        .where(Enrollment.is_active.is_(True))
    )).all()

    candidates = []
    for e, u, c, l in enrollments:
        # ¿Ya tiene certificado activo para este curso+nivel?
        existing_cert = (await db.execute(
            select(func.count()).select_from(Certificate).where(
                Certificate.student_id == u.id,
                Certificate.course_id == c.id,
                Certificate.level_id == l.id,
                Certificate.revoked.is_(False),
            )
        )).scalar() or 0
        if existing_cert > 0:
            continue

        # Total de módulos del nivel
        total_modules = (await db.execute(
            select(func.count()).select_from(Module).where(Module.level_id == l.id)
        )).scalar() or 0
        if total_modules == 0:
            continue  # Sin módulos definidos, no podemos evaluar

        # Módulos completados por el estudiante
        completed_modules = (await db.execute(
            select(func.count()).select_from(ModuleProgress)
            .join(Module, ModuleProgress.module_id == Module.id)
            .where(
                ModuleProgress.student_id == u.id,
                Module.level_id == l.id,
                ModuleProgress.status == "completed",
            )
        )).scalar() or 0

        # ¿Todos los módulos completados?
        if completed_modules < total_modules:
            continue

        # Asistencia promedio del estudiante en clases de este nivel
        att_rows = (await db.execute(
            select(SessionAttendance.state)
            .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
            .where(
                SessionAttendance.student_id == u.id,
                ClassSession.level_id == l.id,
            )
        )).all()
        total_att = len(att_rows)
        if total_att > 0:
            present = sum(1 for (s,) in att_rows if s == AttendanceState.present)
            attendance_pct = round((present / total_att) * 100, 1)
        else:
            attendance_pct = None

        # Criterio: si tiene asistencia registrada, debe ser ≥ 70%
        meets_attendance = attendance_pct is None or attendance_pct >= 70

        if not meets_attendance:
            continue

        # Es candidato — incluirlo
        candidates.append({
            "enrollment_id": e.id,
            "student_id": u.id,
            "student_name": u.full_name,
            "student_email": u.email,
            "avatar_url": u.avatar_url,
            "gender": u.gender,
            "course_id": c.id,
            "course_name": c.name,
            "level_id": l.id,
            "level_code": l.code,
            "level_name": l.name,
            "teacher_id": e.teacher_id,
            "modules_completed": completed_modules,
            "total_modules": total_modules,
            "attendance_pct": attendance_pct,
            "enrolled_at": e.enrolled_at.isoformat() if e.enrolled_at else None,
        })

    return candidates


@router.post("/certification-candidates/{enrollment_id}/issue")
async def issue_certification_quick(
    enrollment_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.6.3: Emite certificado para un candidato con 1 click.

    Body opcional: { final_grade: float, hours_completed: int }
    """
    e = await db.get(Enrollment, enrollment_id)
    if not e:
        raise HTTPException(404, "Inscripción no encontrada")

    # Verificar que no exista certificado activo
    existing = (await db.execute(
        select(Certificate).where(
            Certificate.student_id == e.student_id,
            Certificate.course_id == e.course_id,
            Certificate.level_id == e.level_id,
            Certificate.revoked.is_(False),
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Este estudiante ya tiene certificado activo para este nivel")

    # Generar código único
    code = token_urlsafe(8).replace("_", "").replace("-", "").upper()[:12]
    while (await db.execute(select(Certificate).where(Certificate.code == code))).scalar_one_or_none():
        code = token_urlsafe(8).replace("_", "").replace("-", "").upper()[:12]

    final_grade = body.get("final_grade", 80.0)
    hours_completed = body.get("hours_completed", 60)

    cert = Certificate(
        code=code,
        student_id=e.student_id,
        course_id=e.course_id,
        level_id=e.level_id,
        final_grade=final_grade,
        hours_completed=hours_completed,
        issued_by=admin.user_id,
    )
    db.add(cert)

    # Notificar al estudiante
    level = await db.get(Level, e.level_id)
    course = await db.get(Course, e.course_id)
    db.add(Notification(
        user_id=e.student_id,
        type=NotificationType.success,
        title="🎓 ¡Tu certificado está listo!",
        body=f"¡Felicitaciones! Completaste {course.name if course else ''} nivel {level.code if level else ''}. Tu código de certificado es {code}.",
        link="/dashboard/student/certificates",
    ))

    # V2.1: email al estudiante con su certificado
    student_user = await db.get(User, e.student_id)
    if student_user and student_user.email:
        from app.services.email_service import send_email, tpl_certificate_issued
        try:
            await send_email(
                to=student_user.email,
                subject=f"🎓 ¡Tu certificado de {level.code if level else ''} está listo!",
                html=tpl_certificate_issued(student_user.full_name, level.code if level else "—", code),
            )
        except Exception:
            pass

    await log_action(db, admin.user_id, "issue_certificate", "certificates", target_id=cert.id)
    await db.commit()

    return {
        "ok": True,
        "certificate_id": cert.id,
        "code": code,
        "verify_url": f"/certificate/{code}",
    }


# ============= V1.6.4 — PLANTILLA DE MÓDULOS PRE-HECHOS =============
MODULE_TEMPLATES = {
    "A1": [
        ("Greetings & Introductions", "Saludos básicos, presentarse, decir nombre/edad/origen.", "Can introduce themself and others, ask and answer questions about personal details."),
        ("Numbers, Colors, Days", "Vocabulario básico: números 1-100, colores, días de la semana, meses.", "Can use basic vocabulary for everyday situations."),
        ("Daily Routine", "Presente simple, rutina diaria, vocabulario de hogar y trabajo.", "Can describe daily activities using simple present tense."),
        ("Family & Friends", "Vocabulario de relaciones, posesivos, descripción física básica.", "Can talk about family members and describe people using simple terms."),
        ("Food & Restaurant", "Comida, bebidas, ordenar en restaurante, gustos básicos.", "Can order food and drinks, express preferences with like/don't like."),
    ],
    "A2": [
        ("Past Simple", "Pasado simple regular e irregular, rutina del pasado.", "Can describe past events and past routines."),
        ("Travel & Transportation", "Vocabulario de viajes, transportes, direcciones, hotel.", "Can ask for and give directions, describe travel experiences."),
        ("Shopping & Money", "Precios, compras, ropa, comparativos básicos.", "Can shop for basic items and compare products."),
        ("Health & Body", "Partes del cuerpo, dolencias, consejos con should/shouldn't.", "Can describe health problems and give simple advice."),
        ("Weather & Hobbies", "Clima, tiempo libre, futuro con going to.", "Can talk about weather, hobbies and future plans."),
    ],
    "B1": [
        ("Present Perfect", "Present Perfect Simple y Continuous, experiencias de vida.", "Can describe life experiences and recent events using present perfect."),
        ("Conditionals 1st & 2nd", "Primera y segunda condicional, situaciones hipotéticas.", "Can talk about real and imaginary situations using conditionals."),
        ("Reported Speech", "Discurso indirecto, cambios de tiempo verbal.", "Can report what other people said using reported speech."),
        ("Phrasal Verbs Common", "Phrasal verbs frecuentes en conversación diaria.", "Can understand and use common phrasal verbs in conversation."),
        ("Work & Career", "Vocabulario profesional, entrevistas, CV, trabajo en equipo.", "Can discuss work-related topics and describe career experiences."),
    ],
    "B2": [
        ("Modal Verbs Advanced", "Modales avanzados: must/can't (deduction), should have, could have.", "Can express deduction, regret and criticism using modal verbs."),
        ("Passive Voice", "Voz pasiva en todos los tiempos verbales, causative have.", "Can use passive voice appropriately in formal and informal contexts."),
        ("Conditionals 3rd & Mixed", "Tercera condicional y condicionales mixtas, regret.", "Can talk about hypothetical past situations and their consequences."),
        ("Idioms & Expressions", "Idioms comunes, expresiones idiomáticas, vocabulario coloquial.", "Can understand and use common idioms in everyday speech."),
        ("Complex Discussions", "Debates, opiniones, argumentación, vocabulario formal.", "Can participate in extended discussions and defend opinions."),
    ],
    "C1": [
        ("Inversion Structures", "Inversión gramatical, estructuras enfáticas (never have I).", "Can use inverted structures for emphasis in formal contexts."),
        ("Advanced Relative Clauses", "Cláusulas relativas reducidas, defining vs non-defining.", "Can construct complex sentences with multiple relative clauses."),
        ("Academic Writing", "Redacción académica, ensayos, párrafos argumentativos.", "Can write structured academic essays with clear arguments."),
        ("Nuanced Vocabulary", "Sinónimos sutiles, collocations, registros formales/informales.", "Can choose vocabulary with nuance and appropriate register."),
        ("Professional Contexts", "Inglés de negocios avanzado, presentaciones, negociación.", "Can perform professionally in business meetings and presentations."),
    ],
    "C2": [
        ("Subtle Grammar Distinctions", "Distinciones gramaticales sutiles, native-like accuracy.", "Can use grammar with native-level precision and subtlety."),
        ("Native-like Idioms", "Idioms avanzados, slang, referencias culturales.", "Can understand and use idiomatic expressions like a native speaker."),
        ("Cross-cultural Communication", "Comunicación intercultural, sensibilidad cultural.", "Can navigate cross-cultural communication with sensitivity."),
        ("Argumentative Essays", "Ensayos argumentativos complejos, retórica.", "Can write sophisticated argumentative essays with rhetorical devices."),
        ("Specialized Vocabulary", "Vocabulario especializado por dominio (legal, médico, técnico).", "Can use specialized vocabulary in professional and academic domains."),
    ],
}

LESSON_TEMPLATES_PER_MODULE = [
    ("Introducción y teoría", "Presentación del tema, conceptos clave, ejemplos guiados.", 30),
    ("Práctica y aplicación", "Ejercicios prácticos, role-plays, situaciones reales.", 45),
]


@router.post("/load-module-templates")
async def load_module_templates(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.6.4: Carga plantilla de 30 módulos + 60 lecciones en niveles VACÍOS.

    No toca niveles que ya tengan módulos. Devuelve resumen.
    """
    # Obtener todos los niveles
    levels = (await db.execute(select(Level).order_by(Level.id))).scalars().all()
    if not levels:
        raise HTTPException(400, "No hay niveles configurados. Creá los cursos primero.")

    created_modules = 0
    created_lessons = 0
    skipped_levels = []
    processed_levels = []

    for level in levels:
        # ¿Este nivel ya tiene módulos?
        existing = (await db.execute(
            select(func.count()).select_from(Module).where(Module.level_id == level.id)
        )).scalar() or 0
        if existing > 0:
            skipped_levels.append({
                "level_code": level.code,
                "existing_modules": existing,
            })
            continue

        # Buscar plantilla
        template = MODULE_TEMPLATES.get(level.code.upper())
        if not template:
            continue

        # Crear módulos
        for idx, (name, description, can_do) in enumerate(template):
            module = Module(
                level_id=level.id,
                name=name,
                description=description,
                order_index=idx + 1,
            )
            db.add(module)
            await db.flush()  # para obtener el ID
            created_modules += 1

            # Crear 2 lecciones template por módulo
            for lidx, (l_title, l_desc, l_duration) in enumerate(LESSON_TEMPLATES_PER_MODULE):
                lesson = Lesson(
                    module_id=module.id,
                    title=f"{l_title}",
                    description=l_desc,
                    objectives=can_do,
                    can_do=can_do,
                    duration_min=l_duration,
                    is_published=True,
                    order_index=lidx + 1,
                )
                db.add(lesson)
                created_lessons += 1

        processed_levels.append({
            "level_code": level.code,
            "modules_created": len(template),
        })

    await log_action(db, admin.user_id, "load_module_templates", "catalog",
                     details=f"modules={created_modules}, lessons={created_lessons}")
    await db.commit()

    return {
        "ok": True,
        "modules_created": created_modules,
        "lessons_created": created_lessons,
        "processed_levels": processed_levels,
        "skipped_levels": skipped_levels,
    }


# ============= V1.7 — SERIES RECURRENTES + CLASES PRIVADAS =============

DAY_NAMES = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DAY_NAMES_REV = {v: k for k, v in DAY_NAMES.items()}


def _generate_session_dates(start_date, end_date, num_classes, days_of_week, start_time_hhmm):
    """Genera fechas+hora local para clases de una serie."""
    from datetime import datetime as dt, time, timedelta as td

    # Parse hora
    hh, mm = map(int, start_time_hhmm.split(":"))

    # Convertir días CSV → set de ints
    days = set()
    for d in days_of_week.split(","):
        d = d.strip().lower()
        if d in DAY_NAMES:
            days.add(DAY_NAMES[d])

    if not days:
        return []

    dates = []
    cur = start_date
    safety_limit = 365 * 2  # 2 años máximo

    while safety_limit > 0:
        if cur.weekday() in days:
            naive = dt.combine(cur, time(hh, mm))
            dates.append(naive)
            if num_classes and len(dates) >= num_classes:
                break
        cur = cur + td(days=1)
        if end_date and cur > end_date:
            break
        safety_limit -= 1

    return dates


@router.post("/class-series", status_code=201)
async def create_class_series(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.7: Crea una serie recurrente y genera N clases automáticamente.

    Body:
    {
      "name": "B1 Nocturno",
      "course_id": 1, "level_id": 4, "teacher_id": "uuid",
      "days_of_week": "mon,wed,fri",
      "start_time_hhmm": "19:00",
      "duration_min": 90,
      "start_date": "2026-06-15",  // YYYY-MM-DD
      "end_date": "2026-08-15",     // O num_classes
      "num_classes": 24,
      "modality": "online",
      "meeting_url": "https://...",
      "module_rotation": "1,2,3,4,5",  // opcional CSV de module_ids
      "capacity": 15,
      "plan_id": null
    }
    """
    from datetime import datetime as dt, time, date as ddate, timedelta as td

    # Validaciones básicas
    for f in ("name", "course_id", "level_id", "teacher_id", "days_of_week",
              "start_time_hhmm", "start_date", "modality"):
        if not body.get(f):
            raise HTTPException(400, f"Falta campo: {f}")

    if not body.get("end_date") and not body.get("num_classes"):
        raise HTTPException(400, "Especifica end_date O num_classes")

    # Parse fechas
    try:
        start_date = dt.strptime(body["start_date"], "%Y-%m-%d").date()
        end_date = dt.strptime(body["end_date"], "%Y-%m-%d").date() if body.get("end_date") else None
    except Exception:
        raise HTTPException(400, "Formato de fecha inválido (usá YYYY-MM-DD)")

    num_classes = body.get("num_classes")

    # Validar modalidad
    try:
        modality = Modality(body["modality"])
    except Exception:
        raise HTTPException(400, "Modalidad inválida (online/onsite/hybrid)")

    # Crear la serie
    series = ClassSeries(
        name=body["name"],
        course_id=body["course_id"],
        level_id=body["level_id"],
        teacher_id=body["teacher_id"],
        plan_id=body.get("plan_id"),
        days_of_week=body["days_of_week"],
        start_time_hhmm=body["start_time_hhmm"],
        duration_min=body.get("duration_min", 90),
        start_date=start_date,
        end_date=end_date,
        num_classes=num_classes,
        modality=modality,
        meeting_url=body.get("meeting_url"),
        branch_id=body.get("branch_id"),
        classroom_id=body.get("classroom_id"),
        module_rotation=body.get("module_rotation"),
        capacity=body.get("capacity", 15),
    )
    db.add(series)
    await db.flush()

    # Generar fechas
    dates = _generate_session_dates(start_date, end_date, num_classes, body["days_of_week"], body["start_time_hhmm"])
    if not dates:
        raise HTTPException(400, "No se pudieron generar fechas. Verificá los días y rango.")

    # Rotación de módulos
    module_ids = []
    if body.get("module_rotation"):
        module_ids = [int(m.strip()) for m in body["module_rotation"].split(",") if m.strip().isdigit()]

    # Distribuir módulos: si hay 5 módulos y 24 clases → cada módulo ~5 clases
    def assign_module(idx, total_classes, modules_list):
        if not modules_list:
            return None
        # Distribución balanceada
        per_module = max(1, total_classes // len(modules_list))
        mod_idx = min(idx // per_module, len(modules_list) - 1)
        return modules_list[mod_idx]

    # Crear las clases
    duration = body.get("duration_min", 90)
    created_classes = 0
    for i, naive_dt in enumerate(dates):
        starts_at = naive_dt.replace(tzinfo=tz.utc)  # Asumimos UTC; conversión TZ ya se hace en frontend
        ends_at = starts_at + timedelta(minutes=duration)
        mod_id = assign_module(i, len(dates), module_ids) if module_ids else None

        session = ClassSession(
            course_id=body["course_id"],
            level_id=body["level_id"],
            teacher_id=body["teacher_id"],
            title=f"{body['name']} — Clase {i+1}",
            modality=modality,
            starts_at_utc=starts_at,
            ends_at_utc=ends_at,
            meeting_url=body.get("meeting_url"),
            branch_id=body.get("branch_id"),
            classroom_id=body.get("classroom_id"),
            capacity=body.get("capacity", 15),
            module_id=mod_id,
            series_id=series.id,
        )
        db.add(session)
        created_classes += 1

    await log_action(db, admin.user_id, "create_class_series", "sessions",
                     target_id=series.id, details=f"classes={created_classes}")
    await db.commit()

    return {
        "ok": True,
        "series_id": series.id,
        "classes_created": created_classes,
        "first_date": dates[0].isoformat() if dates else None,
        "last_date": dates[-1].isoformat() if dates else None,
    }


@router.get("/class-series")
async def list_class_series(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.7: Lista todas las series con conteo de clases."""
    series_list = (await db.execute(
        select(ClassSeries).order_by(ClassSeries.created_at.desc())
    )).scalars().all()

    out = []
    now = datetime.now(tz.utc)
    for s in series_list:
        total = (await db.execute(
            select(func.count()).select_from(ClassSession).where(ClassSession.series_id == s.id)
        )).scalar() or 0
        future = (await db.execute(
            select(func.count()).select_from(ClassSession).where(
                ClassSession.series_id == s.id,
                ClassSession.ends_at_utc > now,
            )
        )).scalar() or 0
        # Teacher name
        t_user = await db.get(User, s.teacher_id)
        level = await db.get(Level, s.level_id)
        course = await db.get(Course, s.course_id)
        out.append({
            "id": s.id,
            "name": s.name,
            "course_id": s.course_id,
            "course_name": course.name if course else None,
            "level_id": s.level_id,
            "level_code": level.code if level else None,
            "teacher_id": s.teacher_id,
            "teacher_name": t_user.full_name if t_user else None,
            "days_of_week": s.days_of_week,
            "start_time_hhmm": s.start_time_hhmm,
            "duration_min": s.duration_min,
            "start_date": s.start_date.isoformat() if s.start_date else None,
            "end_date": s.end_date.isoformat() if s.end_date else None,
            "modality": s.modality.value,
            "is_active": s.is_active,
            "total_classes": total,
            "future_classes": future,
            "past_classes": total - future,
        })
    return out


@router.delete("/class-series/{series_id}")
async def delete_class_series(
    series_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    future_only: bool = True,
):
    """V1.7: Elimina una serie. Por default elimina solo clases futuras.

    ?future_only=false → elimina TODAS las clases de la serie + la serie misma
    ?future_only=true (default) → elimina solo clases futuras + desactiva serie
    """
    s = await db.get(ClassSeries, series_id)
    if not s:
        raise HTTPException(404, "Serie no encontrada")

    now = datetime.now(tz.utc)
    if future_only:
        # Eliminar solo clases futuras
        future_sessions = (await db.execute(
            select(ClassSession).where(
                ClassSession.series_id == series_id,
                ClassSession.starts_at_utc > now,
            )
        )).scalars().all()
        count = len(future_sessions)
        for sess in future_sessions:
            await db.delete(sess)
        s.is_active = False
    else:
        # Eliminar TODAS las clases de la serie
        all_sessions = (await db.execute(
            select(ClassSession).where(ClassSession.series_id == series_id)
        )).scalars().all()
        count = len(all_sessions)
        for sess in all_sessions:
            await db.delete(sess)
        await db.delete(s)

    await log_action(db, admin.user_id, "delete_class_series", "sessions",
                     target_id=series_id, details=f"deleted_classes={count}, future_only={future_only}")
    await db.commit()
    return {"ok": True, "deleted_classes": count}


# === CLASES PRIVADAS 1-a-1 ===
@router.post("/private-classes", status_code=201)
async def create_private_class(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.7: Crea una clase privada (1-a-1) asignada a un estudiante específico.

    Body:
    {
      "student_id": "uuid",
      "teacher_id": "uuid",
      "course_id": 1, "level_id": 4,
      "title": "Clase particular María - Refuerzo grammar",
      "starts_at_utc": "2026-06-20T19:00:00Z",
      "duration_min": 60,
      "modality": "online",
      "meeting_url": "https://...",
      "module_id": null,  // opcional
      "counts_for_progress": false  // admin elige
    }
    """
    for f in ("student_id", "teacher_id", "course_id", "level_id", "title",
              "starts_at_utc", "modality"):
        if not body.get(f):
            raise HTTPException(400, f"Falta campo: {f}")

    # Validar que el estudiante existe
    student = await db.get(Student, body["student_id"])
    if not student:
        raise HTTPException(404, "Estudiante no encontrado")

    try:
        modality = Modality(body["modality"])
    except Exception:
        raise HTTPException(400, "Modalidad inválida")

    # Parse fecha
    try:
        starts_at = datetime.fromisoformat(body["starts_at_utc"].replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(400, "Formato de fecha inválido")
    duration = body.get("duration_min", 60)
    if duration <= 0 or duration > 480:  # max 8h
        raise HTTPException(400, "Duración inválida (debe ser entre 1 y 480 minutos)")
    ends_at = starts_at + timedelta(minutes=duration)

    session = ClassSession(
        course_id=body["course_id"],
        level_id=body["level_id"],
        teacher_id=body["teacher_id"],
        title=body["title"],
        description=body.get("description"),
        modality=modality,
        starts_at_utc=starts_at,
        ends_at_utc=ends_at,
        meeting_url=body.get("meeting_url"),
        branch_id=body.get("branch_id"),
        classroom_id=body.get("classroom_id"),
        capacity=1,  # privada → siempre 1
        module_id=body.get("module_id"),
        student_id=body["student_id"],  # V1.7 marca como privada
        counts_for_progress=body.get("counts_for_progress", True),
    )
    db.add(session)
    await db.flush()

    # Notificar al estudiante
    teacher = await db.get(User, body["teacher_id"])
    db.add(Notification(
        user_id=body["student_id"],
        type=NotificationType.info,
        title="👤 Nueva clase privada agendada",
        body=f"Tu profesor {teacher.full_name if teacher else ''} agendó una clase privada: {body['title']}",
        link="/dashboard/student",
    ))
    # Notificar al profesor
    student_user = await db.get(User, body["student_id"])
    db.add(Notification(
        user_id=body["teacher_id"],
        type=NotificationType.info,
        title="📅 Clase privada agendada",
        body=f"Clase privada con {student_user.full_name if student_user else ''}: {body['title']}",
        link="/dashboard/teacher",
    ))

    await log_action(db, admin.user_id, "create_private_class", "sessions",
                     target_id=session.id)
    await db.commit()

    return {"ok": True, "session_id": session.id}


# ============= V1.9 — PAGOS A PROFESORES =============

def _classify_class_type(session: ClassSession) -> str:
    """Determina el tipo de clase para tarifa: group / private / event."""
    if session.is_open_event:
        return "event"
    if session.student_id is not None:
        return "private"
    return "group"


async def _calculate_teacher_period(db: AsyncSession, teacher_id: str, year: int, month: int):
    """V1.9: Calcula lo que el profe ganó en un período (año/mes).

    Solo cuenta clases con asistencia tomada (al menos 1 registro de asistencia).
    Si la clase está cancelada, NO cuenta.

    Retorna dict con conteo y totales.
    """
    from datetime import datetime as dt
    # Rango del mes
    period_start = dt(year, month, 1, tzinfo=tz.utc)
    if month == 12:
        period_end = dt(year + 1, 1, 1, tzinfo=tz.utc)
    else:
        period_end = dt(year, month + 1, 1, tzinfo=tz.utc)

    # Obtener tarifas del profe
    t = await db.get(Teacher, teacher_id)
    if not t:
        return None
    rates = {"group": t.rate_group, "private": t.rate_private, "event": t.rate_event}

    # Clases del profe en el período (no canceladas)
    sessions = (await db.execute(
        select(ClassSession).where(
            ClassSession.teacher_id == teacher_id,
            ClassSession.starts_at_utc >= period_start,
            ClassSession.starts_at_utc < period_end,
            ClassSession.status != SessionStatus.cancelled,
        ).order_by(ClassSession.starts_at_utc)
    )).scalars().all()

    classes_detail = []
    group_count = 0
    private_count = 0
    event_count = 0
    total = 0.0
    classes_paid_for = 0
    now_aware = datetime.now(tz.utc)

    for s in sessions:
        # ¿Tiene asistencia tomada?
        att_count = (await db.execute(
            select(func.count()).select_from(SessionAttendance).where(SessionAttendance.session_id == s.id)
        )).scalar() or 0
        has_attendance = att_count > 0

        ctype = _classify_class_type(s)
        rate = rates.get(ctype, 0)

        # Solo cobra si:
        # 1. Hay asistencia tomada (profe dio la clase)
        # 2. La clase ya pasó (ends_at_utc < ahora) o tiene asistencia
        # Fix V1.9: normalizar tzinfo si vino naive (SQLite a veces lo entrega así)
        already_ended = False
        if s.ends_at_utc:
            ends = s.ends_at_utc if s.ends_at_utc.tzinfo else s.ends_at_utc.replace(tzinfo=tz.utc)
            already_ended = ends < now_aware
        counts = has_attendance and already_ended

        if counts:
            total += rate
            classes_paid_for += 1
            if ctype == "group": group_count += 1
            elif ctype == "private": private_count += 1
            elif ctype == "event": event_count += 1

        classes_detail.append({
            "session_id": s.id,
            "title": s.title,
            "starts_at_utc": s.starts_at_utc.isoformat() if s.starts_at_utc else None,
            "type": ctype,
            "rate": rate,
            "has_attendance": has_attendance,
            "already_ended": already_ended,
            "counts_for_pay": counts,
        })

    # ¿Ya está pagado este período?
    existing_payment = (await db.execute(
        select(TeacherPayment).where(
            TeacherPayment.teacher_id == teacher_id,
            TeacherPayment.period_year == year,
            TeacherPayment.period_month == month,
        )
    )).scalar_one_or_none()

    return {
        "teacher_id": teacher_id,
        "year": year,
        "month": month,
        "total_amount": round(total, 2),
        "currency": "DOP",
        "classes_count": classes_paid_for,
        "group_count": group_count,
        "private_count": private_count,
        "event_count": event_count,
        "rates": rates,
        "classes_detail": classes_detail,
        "is_paid": existing_payment is not None,
        "paid_at": existing_payment.paid_at.isoformat() if existing_payment else None,
        "payment_id": existing_payment.id if existing_payment else None,
        "payment_method": existing_payment.payment_method if existing_payment else None,
        "reference": existing_payment.reference if existing_payment else None,
    }


@router.get("/teacher-payments")
async def list_teacher_payments(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    year: int | None = None,
    month: int | None = None,
):
    """V1.9: Lista lo que se debe pagar a CADA profe en el período (default mes actual)."""
    now = datetime.now(tz.utc)
    y = year or now.year
    m = month or now.month

    teachers = (await db.execute(select(Teacher))).scalars().all()
    out = []
    for t in teachers:
        u = await db.get(User, t.user_id)
        if not u or not u.is_active:
            continue
        period = await _calculate_teacher_period(db, t.user_id, y, m)
        if not period:
            continue
        out.append({
            "teacher_id": t.user_id,
            "teacher_name": u.full_name,
            "gender": u.gender,
            "rate_group": t.rate_group,
            "rate_private": t.rate_private,
            "rate_event": t.rate_event,
            "year": y,
            "month": m,
            "total_amount": period["total_amount"],
            "classes_count": period["classes_count"],
            "group_count": period["group_count"],
            "private_count": period["private_count"],
            "event_count": period["event_count"],
            "is_paid": period["is_paid"],
            "paid_at": period["paid_at"],
            "payment_id": period["payment_id"],
        })
    # Ordenar de mayor a menor
    out.sort(key=lambda x: -x["total_amount"])
    return {
        "year": y, "month": m,
        "items": out,
        "summary": {
            "total_to_pay": round(sum(o["total_amount"] for o in out if not o["is_paid"]), 2),
            "total_paid": round(sum(o["total_amount"] for o in out if o["is_paid"]), 2),
            "teachers_pending": sum(1 for o in out if not o["is_paid"] and o["total_amount"] > 0),
            "teachers_paid": sum(1 for o in out if o["is_paid"]),
        },
    }


@router.get("/teacher-payments/{teacher_id}/{year}/{month}")
async def get_teacher_payment_detail(
    teacher_id: str, year: int, month: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.9: Detalle clase x clase de lo que el profe va a cobrar."""
    period = await _calculate_teacher_period(db, teacher_id, year, month)
    if not period:
        raise HTTPException(404, "Profesor no encontrado")
    u = await db.get(User, teacher_id)
    period["teacher_name"] = u.full_name if u else "—"
    period["teacher_email"] = u.email if u else None
    return period


@router.post("/teacher-payments/mark-paid")
async def mark_teacher_payment_paid(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.9: Marca un pago como realizado.

    Body:
    {
      "teacher_id": "uuid",
      "year": 2026, "month": 6,
      "payment_method": "transferencia",  // opcional
      "reference": "TRX-12345",            // opcional
      "notes": "Pago de junio"             // opcional
    }
    """
    teacher_id = body.get("teacher_id")
    year = body.get("year")
    month = body.get("month")
    if not teacher_id or not year or not month:
        raise HTTPException(400, "Faltan campos: teacher_id, year, month")

    # ¿Ya existe pago para este período?
    existing = (await db.execute(
        select(TeacherPayment).where(
            TeacherPayment.teacher_id == teacher_id,
            TeacherPayment.period_year == year,
            TeacherPayment.period_month == month,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Este período ya está marcado como pagado")

    period = await _calculate_teacher_period(db, teacher_id, year, month)
    if not period:
        raise HTTPException(404, "Profesor no encontrado")

    if period["total_amount"] <= 0:
        raise HTTPException(400, "No hay clases pagables en este período")

    payment = TeacherPayment(
        teacher_id=teacher_id,
        period_year=year,
        period_month=month,
        classes_count=period["classes_count"],
        group_count=period["group_count"],
        private_count=period["private_count"],
        event_count=period["event_count"],
        total_amount=period["total_amount"],
        currency=period["currency"],
        payment_method=body.get("payment_method"),
        reference=body.get("reference"),
        notes=body.get("notes"),
        paid_by_admin_id=admin.user_id,
    )
    db.add(payment)

    # Notificar al profe (interna + email V2.1)
    db.add(Notification(
        user_id=teacher_id,
        type=NotificationType.info,
        title="💰 Pago recibido",
        body=f"Se registró el pago de tu período {month:02d}/{year} por RD$ {period['total_amount']:,.2f}",
        link="/dashboard/teacher/income",
    ))

    # V2.1: enviar email al profe
    teacher_user = await db.get(User, teacher_id)
    if teacher_user and teacher_user.email:
        from app.services.email_service import send_email, tpl_teacher_payment
        try:
            month_names = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
                          "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
            await send_email(
                to=teacher_user.email,
                subject=f"💰 Pago de {month_names[month]} {year} — Dorismon",
                html=tpl_teacher_payment(teacher_user.full_name, f"{month_names[month]} {year}",
                                         period["total_amount"], period["classes_count"]),
            )
        except Exception:
            pass  # No rompe el pago si email falla

    await log_action(db, admin.user_id, "mark_teacher_payment_paid", "payments",
                     target_id=payment.id,
                     details=f"teacher={teacher_id}, period={year}-{month:02d}, amount={period['total_amount']}")
    await db.commit()
    return {"ok": True, "payment_id": payment.id, "amount": period["total_amount"]}


@router.delete("/teacher-payments/{payment_id}")
async def delete_teacher_payment(
    payment_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.9: Revertir un pago marcado por error."""
    p = await db.get(TeacherPayment, payment_id)
    if not p:
        raise HTTPException(404, "Pago no encontrado")
    await log_action(db, admin.user_id, "delete_teacher_payment", "payments",
                     target_id=payment_id, details=f"amount={p.total_amount}")
    await db.delete(p)
    await db.commit()
    return {"ok": True}


@router.patch("/teachers/{teacher_id}/rates")
async def update_teacher_rates(
    teacher_id: str, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.9: Actualiza tarifas de pago de un profesor."""
    t = await db.get(Teacher, teacher_id)
    if not t:
        raise HTTPException(404, "Profesor no encontrado")
    for f in ("rate_group", "rate_private", "rate_event"):
        if f in body:
            val = float(body[f])
            if val < 0:
                raise HTTPException(400, f"{f} no puede ser negativo")
            setattr(t, f, val)
    await log_action(db, admin.user_id, "update_teacher_rates", "users",
                     target_id=teacher_id,
                     details=f"group={t.rate_group}, private={t.rate_private}, event={t.rate_event}")
    await db.commit()
    return {
        "ok": True,
        "rate_group": t.rate_group,
        "rate_private": t.rate_private,
        "rate_event": t.rate_event,
    }


@router.get("/teacher-payments-history/{teacher_id}")
async def teacher_payment_history(
    teacher_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V1.9: Historial de pagos a un profe."""
    payments = (await db.execute(
        select(TeacherPayment).where(TeacherPayment.teacher_id == teacher_id)
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
        "notes": p.notes,
        "paid_at": p.paid_at.isoformat() if p.paid_at else None,
    } for p in payments]
