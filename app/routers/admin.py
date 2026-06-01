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
    Enrollment, Branch, Classroom, ClassSession, SessionAttendance,
    Assignment, AssignmentSubmission, Quiz, Material, Plan, Payment,
    Certificate, InstituteSetting, AuditLog, Notification,
    UserRole, Modality, SessionStatus, MaterialType, PaymentStatus, NotificationType,
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
            ClassSession.starts_at_utc > now,
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
    return {
        "items": [{
            "id": u.id, "email": u.email, "full_name": u.full_name,
            "phone": u.phone, "role": u.role.value, "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        } for u in items],
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


@router.post("/modules", status_code=201)
async def create_module(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    for f in ("level_id", "name"):
        if not body.get(f):
            raise HTTPException(400)
    m = Module(level_id=body["level_id"], name=body["name"], description=body.get("description"))
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return {"id": m.id}


@router.post("/lessons", status_code=201)
async def create_lesson(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    if not body.get("module_id") or not body.get("title"):
        raise HTTPException(400)
    lesson = Lesson(
        module_id=body["module_id"], title=body["title"],
        description=body.get("description"), objectives=body.get("objectives"),
        can_do=body.get("can_do"),
        video_url=body.get("video_url"), pdf_url=body.get("pdf_url"),
        audio_url=body.get("audio_url"),
        duration_min=body.get("duration_min", 15),
    )
    db.add(lesson)
    await db.commit()
    await db.refresh(lesson)
    return {"id": lesson.id}


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
    s = ClassSession(
        teacher_id=body["teacher_id"], course_id=body["course_id"], level_id=body["level_id"],
        title=body["title"], description=body.get("description"),
        modality=Modality(body["modality"]),
        starts_at_utc=datetime.fromisoformat(body["starts_at_utc"].replace("Z", "+00:00")),
        ends_at_utc=datetime.fromisoformat(body["ends_at_utc"].replace("Z", "+00:00")),
        meeting_url=body.get("meeting_url"),
        branch_id=body.get("branch_id"), classroom_id=body.get("classroom_id"),
        capacity=body.get("capacity", 15),
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
    return [{
        "id": e.id, "student_id": u.id, "student_name": u.full_name,
        "course_id": c.id, "course_name": c.name,
        "level_id": l.id, "level_code": l.code,
        "teacher_id": e.teacher_id,
        "enrolled_at": e.enrolled_at.isoformat() if e.enrolled_at else None,
        "is_active": e.is_active,
        "final_grade": float(e.final_grade) if e.final_grade else None,
    } for e, u, c, l in rows]


@router.post("/enrollments", status_code=201)
async def create_enrollment(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    for f in ("student_id", "course_id", "level_id"):
        if not body.get(f):
            raise HTTPException(400)
    e = Enrollment(
        student_id=body["student_id"], course_id=body["course_id"],
        level_id=body["level_id"], teacher_id=body.get("teacher_id"),
    )
    db.add(e)
    # Actualizar nivel del estudiante
    st = await db.get(Student, body["student_id"])
    if st:
        st.current_level_id = body["level_id"]
    await log_action(db, admin.user_id, "enroll", "admin", target_id=e.id)
    await db.commit()
    return {"id": e.id}


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
