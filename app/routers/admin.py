"""Admin — gestión completa del instituto."""
from typing import Annotated
from datetime import datetime, date, timedelta, timezone as tz
from secrets import token_urlsafe
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from sqlalchemy import select, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import require_admin, CurrentUser, hash_password
from app.core.db import get_db
from app.services.audit import log_action
from app.models import (
    User, Teacher, Student, Course, Level, Module, Lesson, LessonProgress,
    Enrollment, Branch, Classroom, ClassSession, ClassSeries, SessionAttendance,
    Assignment, AssignmentSubmission, Quiz, Material, Plan, Payment,
    Certificate, InstituteSetting, AuditLog, Notification, TeacherPayment,
    SiteImage, Testimonial, AlertAction, AbsenceNotice,
    UserRole, Modality, SessionStatus, MaterialType, PaymentStatus, NotificationType,
    PlanFeature, ModuleProgress, EventRegistration, AttendanceState,
    # V2.6: Pagos por transferencia + clase de prueba
    BankAccount, BankAccountType, PaymentProof, PaymentProofStatus, PaymentMethod, TrialClass,
)

router = APIRouter(prefix="/admin", tags=["admin"])


# === DASHBOARD ===


async def _destinatarios_de_clase(db, s) -> set[str]:
    """V3.9.43 — Delega en el servicio central de audiencia.

    Antes esta función tenía su propia copia de la regla. Ahora hay UNA sola
    fuente de verdad (app/services/audience.py) que usan los avisos, los
    listados y el acceso al video. Así no vuelven a divergir.
    """
    from app.services.audience import destinatarios_de_clase
    return await destinatarios_de_clase(db, s)


@router.get("/dashboard")
async def admin_dashboard(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(tz.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_ahead = now + timedelta(days=7)

    # La identidad del dashboard debe ser SIEMPRE la del usuario autenticado.
    # Usamos un nombre explícito para evitar que loops posteriores sobrescriban
    # accidentalmente esta referencia (regresión detectada en v3.9.60).
    admin_user = await db.get(User, admin.user_id)
    if not admin_user:
        raise HTTPException(404, "Usuario administrador no encontrado")

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
    # V1.5.1 + V2.3: Profesores sin estudiantes asignados (con LISTA detallada)
    teachers_with_students_q = (await db.execute(
        select(Enrollment.teacher_id).where(
            Enrollment.teacher_id.is_not(None), Enrollment.is_active.is_(True),
        ).distinct()
    )).scalars().all()
    teachers_with_students = set(teachers_with_students_q)

    # V2.3: Lista de profes SIN estudiantes (con nombre y datos)
    all_teachers = (await db.execute(
        select(Teacher, User).join(User, Teacher.user_id == User.id)
        .where(User.is_active.is_(True), User.role == UserRole.teacher)
    )).all()
    teachers_without_students_list = []
    for t, teacher_user in all_teachers:
        if t.user_id not in teachers_with_students:
            teachers_without_students_list.append({
                "user_id": teacher_user.id,
                "full_name": teacher_user.full_name,
                "email": teacher_user.email,
                "gender": teacher_user.gender,
                "specialties": t.specialties or "",
                "modalities": t.modalities or "",
                "levels_taught": t.levels_taught or "",
            })
    teachers_without_students = len(teachers_without_students_list)

    # V1.6.4: Total módulos cargados (para detectar sistema vacío)
    total_modules = (await db.execute(
        select(func.count()).select_from(Module)
    )).scalar() or 0

    return {
        "user": {
            "id": admin_user.id,
            "full_name": admin_user.full_name,
            "email": admin_user.email,
            "avatar_url": admin_user.avatar_url,
            "role": admin_user.role.value,
        },
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
            "teachers_without_students_list": teachers_without_students_list,  # V2.3: lista detallada
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
            # V3.9.2: ¿tiene inscripciones? (para saber si se puede convertir a profesor)
            enr = (await db.execute(
                select(func.count()).select_from(Enrollment).where(Enrollment.student_id == u.id)
            )).scalar() or 0
            item["has_enrollments"] = enr > 0
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

    # V2.3: Validar email real (dominio MX) — para profes/admins/estudiantes
    from app.services.email_service import validate_email_domain
    valid, err = await validate_email_domain(body["email"])
    if not valid:
        raise HTTPException(400, err)

    # V2.3: Validar gender si se envía
    gender = body.get("gender")
    if gender and gender not in ("male", "female", "other"):
        raise HTTPException(400, "gender debe ser 'male', 'female' u 'other'")

    user = User(
        email=body["email"], password_hash=hash_password(body["password"]),
        full_name=body["full_name"], phone=body.get("phone"), role=role,
        gender=gender,
        # V2.3: Si lo crea el admin, asumir email_verified=True (admin ya validó)
        email_verified=True,
    )
    db.add(user)
    await db.flush()
    if role == UserRole.student:
        db.add(Student(user_id=user.id))
    elif role == UserRole.teacher:
        # V2.3: Permitir más campos al crear profe
        db.add(Teacher(
            user_id=user.id,
            specialties=body.get("specialties", ""),
            modalities=body.get("modalities", "online"),
            bio=body.get("bio"),
            levels_taught=body.get("levels_taught"),
            rate_group=body.get("rate_group", 500.0),
            rate_private=body.get("rate_private", 1000.0),
            rate_event=body.get("rate_event", 750.0),
        ))
    await log_action(db, admin.user_id, "create_user", "admin", target_id=user.id,
                     details=f"role={role.value}, email={body['email']}")
    await db.commit()
    return {"id": user.id}


@router.post("/users/{user_id}/change-role", status_code=200)
async def change_user_role(
    user_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.8: Cambiar el rol de un usuario existente (ej: un estudiante que en
    realidad es profesor y se registró por el formulario público).

    Reutiliza la MISMA cuenta y correo — no crea una nueva. Conserva los datos
    del perfil anterior (no borra nada), solo crea el perfil del nuevo rol si
    no existe y actualiza el rol de la cuenta.
    """
    new_role_str = body.get("new_role")
    if not new_role_str:
        raise HTTPException(400, "new_role requerido")
    try:
        new_role = UserRole(new_role_str)
    except ValueError:
        raise HTTPException(400, "Rol inválido")

    # No permitir cambiar a/desde admin por esta vía (seguridad)
    if new_role == UserRole.super_admin:
        raise HTTPException(403, "No se puede asignar rol de administrador por esta vía")

    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404, "Usuario no encontrado")
    if user.role == UserRole.super_admin:
        raise HTTPException(403, "No se puede cambiar el rol de un administrador")
    if user.role == new_role:
        raise HTTPException(400, f"El usuario ya tiene el rol {new_role.value}")

    # V3.9.2: Bloquear conversión estudiante → profesor si ya tiene historial de
    # clases. Solo se permite convertir cuentas "limpias" (ej: un maestro que se
    # registró por el formulario público y aún no tomó clases). Esto evita dejar
    # inscripciones "colgando" de una persona que ya no es estudiante.
    if new_role == UserRole.teacher and user.role == UserRole.student:
        enroll_count = (await db.execute(
            select(func.count()).select_from(Enrollment).where(
                Enrollment.student_id == user.id
            )
        )).scalar() or 0
        if enroll_count > 0:
            raise HTTPException(
                409,
                "No se puede convertir en profesor: este estudiante ya tiene clases o "
                "inscripciones registradas. Solo se pueden convertir cuentas sin historial "
                "de clases (por ejemplo, un maestro que se registró por error y aún no tomó clases)."
            )

    old_role = user.role.value
    user.role = new_role

    # Crear el perfil del nuevo rol si no existe (conservamos el perfil anterior)
    if new_role == UserRole.teacher:
        existing_teacher = await db.get(Teacher, user.id)
        if not existing_teacher:
            db.add(Teacher(
                user_id=user.id,
                specialties=body.get("specialties", ""),
                modalities=body.get("modalities", "online"),
                bio=body.get("bio"),
                levels_taught=body.get("levels_taught"),
                rate_group=body.get("rate_group", 500.0),
                rate_private=body.get("rate_private", 1000.0),
                rate_event=body.get("rate_event", 750.0),
            ))
        # V3.9: Archivar el perfil de estudiante (no se borra, solo se marca inactivo
        # para que no aparezca en listas/reportes de estudiantes). Reversible.
        student_profile = await db.get(Student, user.id)
        if student_profile and not student_profile.archived:
            student_profile.archived = True
            student_profile.archived_at = datetime.now(tz.utc)
            student_profile.archived_reason = "Convertido a profesor"
    elif new_role == UserRole.student:
        existing_student = await db.get(Student, user.id)
        if not existing_student:
            db.add(Student(user_id=user.id))
        else:
            # V3.9: Si vuelve a ser estudiante, des-archivar su perfil
            existing_student.archived = False
            existing_student.archived_at = None
            existing_student.archived_reason = None

    await log_action(db, admin.user_id, "change_role", "admin", target_id=user.id,
                     details=f"{old_role} → {new_role.value}, email={user.email}")
    await db.commit()
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "old_role": old_role,
        "new_role": new_role.value,
    }


@router.patch("/users/{user_id}")
async def update_user(
    user_id: str, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(404)
    # V2.3: Permitir editar email también (con validación)
    if "email" in body and body["email"] != user.email:
        # Verificar que no exista otro con ese email
        existing = (await db.execute(select(User).where(User.email == body["email"]))).scalar_one_or_none()
        if existing and existing.id != user.id:
            raise HTTPException(409, "Ya existe otro usuario con ese email")
        # Validar dominio
        from app.services.email_service import validate_email_domain
        valid, err = await validate_email_domain(body["email"])
        if not valid:
            raise HTTPException(400, err)
        user.email = body["email"]

    for f in ("full_name", "phone", "avatar_url", "is_active", "email_verified"):
        if f in body:
            setattr(user, f, body[f])

    # V2.3: Permitir cambiar gender
    if "gender" in body:
        gender = body["gender"]
        if gender and gender not in ("male", "female", "other"):
            raise HTTPException(400, "gender debe ser 'male', 'female' u 'other'")
        user.gender = gender if gender else None

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
    filter_period: str = Query("upcoming", description="upcoming/this_week/this_month/past/all"),
    teacher_id: str | None = None,
    course_id: int | None = None,
    level_id: int | None = None,
):
    """V2.8: Lista sesiones con filtros y orden ascendente (próximas primero).

    filter_period:
    - upcoming: clases futuras (>= hoy), orden ASC (default)
    - this_week: clases de esta semana
    - this_month: clases de este mes
    - past: clases pasadas, orden DESC (más reciente primero)
    - all: todas, orden ASC
    """
    from datetime import timedelta as td
    now = datetime.now(tz.utc)

    stmt = select(ClassSession)

    # Filtros de fecha
    if filter_period == "upcoming":
        stmt = stmt.where(ClassSession.starts_at_utc >= now - td(hours=2))  # incluye clases en curso
        stmt = stmt.order_by(ClassSession.starts_at_utc.asc())
    elif filter_period == "this_week":
        start = now - td(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + td(days=7)
        stmt = stmt.where(ClassSession.starts_at_utc >= start, ClassSession.starts_at_utc < end)
        stmt = stmt.order_by(ClassSession.starts_at_utc.asc())
    elif filter_period == "this_month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        from calendar import monthrange
        last_day = monthrange(start.year, start.month)[1]
        end = start.replace(day=last_day, hour=23, minute=59, second=59)
        stmt = stmt.where(ClassSession.starts_at_utc >= start, ClassSession.starts_at_utc <= end)
        stmt = stmt.order_by(ClassSession.starts_at_utc.asc())
    elif filter_period == "past":
        stmt = stmt.where(ClassSession.starts_at_utc < now)
        stmt = stmt.order_by(ClassSession.starts_at_utc.desc())
    else:  # all
        stmt = stmt.order_by(ClassSession.starts_at_utc.asc())

    # Filtros adicionales
    if teacher_id:
        stmt = stmt.where(ClassSession.teacher_id == teacher_id)
    if course_id:
        stmt = stmt.where(ClassSession.course_id == course_id)
    if level_id:
        stmt = stmt.where(ClassSession.level_id == level_id)

    offset = (page - 1) * limit
    stmt = stmt.offset(offset).limit(limit)

    sessions = (await db.execute(stmt)).scalars().all()
    # V3.9.30: cuáles de estas clases son clases de prueba
    _ids = [s.id for s in sessions]
    trial_ids = set()
    if _ids:
        _tr = (await db.execute(
            select(TrialClass.session_id).where(TrialClass.session_id.in_(_ids))
        )).all()
        trial_ids = {x for (x,) in _tr if x}

    out = []
    for s in sessions:
        teacher_user = await db.get(User, s.teacher_id) if s.teacher_id else None
        course = await db.get(Course, s.course_id) if s.course_id else None
        level = await db.get(Level, s.level_id) if s.level_id else None
        out.append({
            "id": s.id, "title": s.title, "modality": s.modality.value if s.modality else None,
            "starts_at_utc": s.starts_at_utc.isoformat() if s.starts_at_utc else None,
            "ends_at_utc": s.ends_at_utc.isoformat() if s.ends_at_utc else None,
            "teacher_id": s.teacher_id, "teacher_name": teacher_user.full_name if teacher_user else None,
            "course_id": s.course_id, "course_name": course.name if course else None,
            "level_id": s.level_id, "level_code": level.code if level else None,
            "branch_id": s.branch_id, "classroom_id": s.classroom_id,
            "meeting_url": s.meeting_url, "capacity": s.capacity,
            "status": s.status.value if s.status else "scheduled",
            # V3.9.30 — para agrupar por tipo en el panel:
            #   evento · prueba · privada · serie · suelta
            "is_open_event": bool(s.is_open_event),
            "is_private": s.student_id is not None,
            "series_id": s.series_id,
            "is_trial": s.id in trial_ids,
            "kind": (
                "event" if s.is_open_event
                else "trial" if s.id in trial_ids
                else "private" if s.student_id is not None
                else "series" if s.series_id
                else "single"
            ),
            "video_provider": getattr(s, "video_provider", "meet") or "meet",
            # V3.9.62 — Módulo y profesor programado. Se exponen para que
            # Dirección pueda comprobar que al editar una serie recurrente
            # NO se perdió la rotación de módulos ni el histórico de quién
            # estaba asignado originalmente.
            "module_id": s.module_id,
            "scheduled_teacher_id": s.scheduled_teacher_id,
        })
    return {"items": out, "page": page, "limit": limit, "filter_period": filter_period}




async def _avisar_clase_nueva(db, s, es_serie: bool = False):
    """V3.9.32 — Avisa a los estudiantes Y al profesor que hay clase nueva.

    Antes no se avisaba nada: la clase aparecía en el calendario y nadie se
    enteraba hasta el recordatorio de 24 horas. Si programabas una clase para
    mañana temprano, el estudiante podía no verla nunca.
    """
    from zoneinfo import ZoneInfo as _ZI
    try:
        from app.services.push_service import notify_user

        st = s.starts_at_utc
        if st and st.tzinfo is None:
            st = st.replace(tzinfo=tz.utc)
        local = st.astimezone(_ZI("America/Santo_Domingo")) if st else None
        cuando = local.strftime("%d/%m a las %I:%M %p").lstrip("0") if local else ""

        # V3.9.38 — Misma fuente de verdad: respeta el grupo
        destinatarios = await _destinatarios_de_clase(db, s)

        titulo = "📅 Serie de clases programada" if es_serie else "📅 Tienes una clase nueva"
        cuerpo = f"'{s.title}' — {cuando}"

        for uid in destinatarios:
            db.add(Notification(
                user_id=uid, type=NotificationType.info,
                title=titulo, body=cuerpo, link="/dashboard/student",
            ))
            await notify_user(db, uid, titulo, cuerpo, "/dashboard/student", f"nueva:{s.id}")

        # Y al profesor, que hoy casi no recibe avisos
        if s.teacher_id:
            db.add(Notification(
                user_id=s.teacher_id, type=NotificationType.info,
                title="📅 Te asignaron una clase", body=cuerpo,
                link="/dashboard/teacher",
            ))
            await notify_user(db, s.teacher_id, "📅 Te asignaron una clase",
                              cuerpo, "/dashboard/teacher", f"nueva:{s.id}")
        await db.commit()
    except Exception:
        pass  # un fallo avisando nunca debe impedir crear la clase

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
        video_provider=("dorismon" if body.get("video_provider") == "dorismon" else "meet"),  # V3.9.26
        module_id=body.get("module_id"),  # V1.5
        is_open_event=body.get("is_open_event", False),
        # V3.9.43 — Una clase suelta puede pertenecer a un grupo. Antes no se
        # podía indicar, así que la clase quedaba sin dueño y NADIE la veía.
        series_id=body.get("series_id") or None,
        scheduled_teacher_id=body.get("teacher_id"),
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
    await db.flush()

    # V3.9.43 — Destinatarios explícitos de una clase suelta.
    # Si se indica una lista de estudiantes, SOLO ellos la verán. Resuelve el
    # caso de la clase que se creaba y no le aparecía a nadie.
    destinatarios = body.get("student_ids") or []
    if destinatarios and not s.series_id and not s.student_id:
        from app.models import SessionAudience
        for sid in destinatarios[:60]:
            db.add(SessionAudience(session_id=s.id, student_id=sid))

    await db.commit()
    await _avisar_clase_nueva(db, s)  # V3.9.32
    return {"id": s.id, "audience": len(destinatarios) or None}


@router.post("/events", status_code=201)
async def create_open_event(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.15: Crear un EVENTO ABIERTO de forma simple (webinar, club de
    conversación, taller). A diferencia de una clase, NO exige curso/nivel:
    los eventos abiertos son visibles para TODOS los estudiantes sin importar
    su nivel. Soporta modalidad híbrida (link para online + sede para presencial).

    Body:
    - title: str (requerido)
    - starts_at_utc, ends_at_utc: ISO (requeridos)
    - modality: online | presencial | hibrida (requerido)
    - teacher_id: str (anfitrión del evento, requerido)
    - description: str (opcional)
    - meeting_url: str (requerido si online o hibrida)
    - branch_id: int (requerido si presencial o hibrida)
    - classroom_id: int (opcional)
    - capacity: int (default 30)
    """
    for f in ("title", "starts_at_utc", "ends_at_utc", "modality", "teacher_id"):
        if not body.get(f):
            raise HTTPException(400, f"{f} requerido")

    modality = Modality(body["modality"])
    # V3.9.43 — Si el evento usa el video de Dorismon, NO hace falta un link
    # externo: la sala se genera sola. Antes se exigía igual y no se podía
    # crear un evento con video propio.
    _usa_video_propio = body.get("video_provider") == "dorismon"
    if (modality in (Modality.online, Modality.hibrida)
            and not body.get("meeting_url") and not _usa_video_propio):
        raise HTTPException(
            400,
            "Un evento online necesita un link, o elige el video de Dorismon.",
        )
    if modality in (Modality.presencial, Modality.hibrida) and not body.get("branch_id"):
        raise HTTPException(400, "Un evento presencial o híbrido necesita la sede (branch_id)")

    # Validar anfitrión
    t = await db.get(Teacher, body["teacher_id"])
    if not t:
        raise HTTPException(404, "Anfitrión (profesor) no encontrado")

    # Curso/nivel: irrelevantes para eventos abiertos (los ven todos), pero el
    # modelo los exige — usamos el primero disponible como relleno técnico.
    first_course = (await db.execute(select(Course).limit(1))).scalar_one_or_none()
    if not first_course:
        raise HTTPException(400, "No hay cursos en el sistema")
    first_level = (await db.execute(
        select(Level).where(Level.course_id == first_course.id).order_by(Level.order_index).limit(1)
    )).scalar_one_or_none()
    if not first_level:
        first_level = (await db.execute(select(Level).limit(1))).scalar_one_or_none()
    if not first_level:
        raise HTTPException(400, "No hay niveles en el sistema")

    starts_at = datetime.fromisoformat(body["starts_at_utc"].replace("Z", "+00:00"))
    ends_at = datetime.fromisoformat(body["ends_at_utc"].replace("Z", "+00:00"))
    if ends_at <= starts_at:
        raise HTTPException(400, "La hora de fin debe ser posterior a la de inicio")

    s = ClassSession(
        title=body["title"],
        description=body.get("description"),
        course_id=first_course.id,
        level_id=first_level.id,
        teacher_id=body["teacher_id"],
        modality=modality,
        starts_at_utc=starts_at,
        ends_at_utc=ends_at,
        meeting_url=body.get("meeting_url"),
        video_provider=("dorismon" if body.get("video_provider") == "dorismon" else "meet"),  # V3.9.32
        branch_id=body.get("branch_id"),
        classroom_id=body.get("classroom_id"),
        capacity=int(body.get("capacity", 30)),
        is_open_event=True,
    )
    db.add(s)
    await db.flush()

    await log_action(db, admin.user_id, "create_open_event", "admin", target_id=s.id)
    await db.commit()
    return {"id": s.id, "ok": True}


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
    # V3.9.20 FIX: antes el cancel del admin solo cambiaba el estado — no seteaba
    # cancelled_at (el aviso del dashboard del estudiante filtra por ese campo,
    # así que la cancelación quedaba invisible) ni notificaba a nadie.
    s.cancelled_at = datetime.now(tz.utc)
    s.cancelled_by_user_id = admin.user_id
    if not s.cancellation_reason:
        s.cancellation_reason = "Cancelada por administración"

    # Notificar a los estudiantes afectados (grupal: inscritos al nivel; privada: el asignado)
    try:
        starts_for_msg = s.starts_at_utc if s.starts_at_utc.tzinfo else s.starts_at_utc.replace(tzinfo=tz.utc)
        from zoneinfo import ZoneInfo as _ZIc
        when_local = starts_for_msg.astimezone(_ZIc("America/Santo_Domingo")).strftime("%d/%m/%Y %I:%M %p")
        affected_ids = set()
        if s.student_id:
            affected_ids.add(s.student_id)
        else:
            enr_rows = (await db.execute(
                select(Enrollment.student_id).where(
                    Enrollment.course_id == s.course_id,
                    Enrollment.level_id == s.level_id,
                    Enrollment.is_active.is_(True),
                )
            )).all()
            affected_ids.update(sid for (sid,) in enr_rows)
        for st_id in affected_ids:
            db.add(Notification(
                user_id=st_id, type=NotificationType.info,
                title="❌ Clase cancelada",
                body=f"Tu clase '{s.title}' del {when_local} fue cancelada. {s.cancellation_reason or ''}".strip(),
            ))
        # Avisar también al profe de la clase
        if s.teacher_id:
            db.add(Notification(
                user_id=s.teacher_id, type=NotificationType.info,
                title="❌ Clase cancelada por administración",
                body=f"Tu clase '{s.title}' del {when_local} fue cancelada.",
            ))
    except Exception:
        pass

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
            "series_id": getattr(e, "series_id", None),  # V3.9.33: su grupo
            "course_id": c.id, "course_name": c.name,
            "level_id": l.id, "level_code": l.code, "level_name": l.name,
            "teacher_id": e.teacher_id, "teacher_name": teacher_name,  # V1.5
            "plan_id": e.plan_id, "plan_name": plan_name,  # V1.5
            "modality": e.modality.value if e.modality else "online",  # V2.3
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

    # V2.3: Validar modality si se envía
    modality_val = Modality.online  # default
    if body.get("modality"):
        try:
            modality_val = Modality(body["modality"])
        except ValueError:
            raise HTTPException(400, "Modalidad inválida (online/presencial/hibrida)")

    e = Enrollment(
        student_id=body["student_id"], course_id=body["course_id"],
        level_id=body["level_id"], teacher_id=teacher_id,
        plan_id=body.get("plan_id"),
        modality=modality_val,  # V2.3
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
        body="Has sido inscrito en un curso. Revisa tu dashboard.",
        link="/dashboard/student",
    ))

    # V2.3: Email + notif al profe asignado (sea manual o auto)
    if teacher_id:
        st_user = await db.get(User, body["student_id"])
        level_obj = await db.get(Level, body["level_id"])
        teacher_user = await db.get(User, teacher_id)
        modality_label = {"online": "Online", "presencial": "Presencial", "hibrida": "Híbrida"}.get(modality_val.value, "")

        notif_body = f"{st_user.full_name if st_user else 'Estudiante'} fue asignado a tu grupo de {level_obj.code if level_obj else ''} ({modality_label})."
        if auto_assigned:
            notif_body = "Auto-asignación: " + notif_body

        db.add(Notification(
            user_id=teacher_id,
            type=NotificationType.info,
            title="👥 Nuevo estudiante asignado",
            body=notif_body,
            link="/dashboard/teacher/students",
        ))

        # Email al profe
        if teacher_user and teacher_user.email:
            from app.services.email_service import send_email, tpl_teacher_assigned, is_email_configured
            if is_email_configured() and st_user:
                try:
                    await send_email(
                        to=teacher_user.email,
                        subject=f"Nuevo estudiante asignado: {st_user.full_name}",
                        html=tpl_teacher_assigned(
                            teacher_user.full_name,
                            st_user.full_name,
                            level_obj.code if level_obj else "",
                        ),
                    )
                except Exception:
                    pass

        # Email al estudiante: "Tu profesor es X"
        if st_user and st_user.email and teacher_user:
            from app.services.email_service import send_email, tpl_teacher_assigned, is_email_configured
            if is_email_configured():
                try:
                    await send_email(
                        to=st_user.email,
                        subject=f"Tu profesor asignado: {teacher_user.full_name}",
                        html=tpl_teacher_assigned(
                            st_user.full_name,
                            teacher_user.full_name,
                            level_obj.code if level_obj else "",
                        ),
                    )
                except Exception:
                    pass

    await log_action(db, admin.user_id, "enroll", "admin", target_id=e.id,
                     details=f"auto_assigned={auto_assigned}, modality={modality_val.value}")
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
    year: int | None = None,
    month: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """V2.5: Resumen financiero del instituto (mejorado).

    Por defecto: mes actual. Con parámetros: cualquier mes/año específico.

    Devuelve estructura completa para dashboard financiero.
    """
    from calendar import monthrange
    now = datetime.now(tz.utc)
    target_year = year or now.year
    target_month = month or now.month
    last_day = monthrange(target_year, target_month)[1]
    start = datetime(target_year, target_month, 1, tzinfo=tz.utc)
    end = datetime(target_year, target_month, last_day, 23, 59, 59, tzinfo=tz.utc)

    # Ingresos del mes (pagos completados)
    paid_payments = (await db.execute(
        select(Payment).where(
            Payment.status == PaymentStatus.paid,
            Payment.paid_at >= start, Payment.paid_at <= end,
        )
    )).scalars().all()
    total_income = sum(float(p.amount or 0) for p in paid_payments)

    # Ingresos del año (acumulado)
    year_start = datetime(target_year, 1, 1, tzinfo=tz.utc)
    income_year = float((await db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == PaymentStatus.paid, Payment.paid_at >= year_start,
        )
    )).scalar() or 0)

    # Pendientes a cobrar (estudiantes que aún no pagaron)
    pending_payments = (await db.execute(
        select(Payment).where(
            Payment.status == PaymentStatus.pending,
            Payment.created_at >= start, Payment.created_at <= end,
        )
    )).scalars().all()
    pending_income = sum(float(p.amount or 0) for p in pending_payments)

    # Gastos del mes (pagos a profes — TeacherPayment solo existe cuando ya está pagado)
    teacher_payments_paid = (await db.execute(
        select(TeacherPayment).where(
            TeacherPayment.period_year == target_year,
            TeacherPayment.period_month == target_month,
        )
    )).scalars().all()
    total_expenses = sum(float(p.total_amount or 0) for p in teacher_payments_paid)

    # Pendientes a pagar a profes (calculado en vivo)
    pending_expenses = 0.0
    pending_expense_count = 0
    teachers_q = (await db.execute(
        select(Teacher, User).join(User, Teacher.user_id == User.id)
        .where(User.is_active.is_(True), User.role == UserRole.teacher)
    )).all()

    for t, _u in teachers_q:
        sessions_q = (await db.execute(
            select(ClassSession).where(
                ClassSession.teacher_id == t.user_id,
                ClassSession.starts_at_utc >= start,
                ClassSession.starts_at_utc <= end,
                ClassSession.status == SessionStatus.completed,
            )
        )).scalars().all()
        expected = 0.0
        for s in sessions_q:
            if s.student_id:
                expected += float(t.rate_private or 0)
            else:
                expected += float(t.rate_group or 0)
        already_paid = sum(
            float(p.total_amount or 0)
            for p in teacher_payments_paid
            if p.teacher_id == t.user_id
        )
        pending = expected - already_paid
        if pending > 0:
            pending_expenses += pending
            pending_expense_count += 1

    # Suscripciones activas (pagaron en los últimos 31 días)
    active_subscriptions = (await db.execute(
        select(func.count()).select_from(Payment).where(
            Payment.status == PaymentStatus.paid,
            Payment.paid_at > now - timedelta(days=31),
        )
    )).scalar() or 0

    net_balance = total_income - total_expenses
    projected_balance = (total_income + pending_income) - (total_expenses + pending_expenses)

    return {
        # Compatibilidad con UI vieja
        "income_month": round(total_income, 2),
        "income_year": round(income_year, 2),
        "pending_amount": round(pending_income, 2),
        "active_subscriptions": active_subscriptions,
        # V2.5: estructura nueva más completa
        "year": target_year,
        "month": target_month,
        "income": {
            "total": round(total_income, 2),
            "count": len(paid_payments),
            "pending_total": round(pending_income, 2),
            "pending_count": len(pending_payments),
        },
        "expenses": {
            "total": round(total_expenses, 2),
            "count": len(teacher_payments_paid),
            "pending_total": round(pending_expenses, 2),
            "pending_count": pending_expense_count,
        },
        "balance": {
            "net": round(net_balance, 2),
            "projected": round(projected_balance, 2),
        },
        "currency": "RD$",
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
    """Emitir un certificado.

    ⚠️ V3.9.57 — ORDEN CORRECTO. Antes parte de la validación corría ANTES de
    resolver la matrícula, y luego se volvía a buscar "la más reciente". Con
    un estudiante que repitió el nivel, el certificado podía validarse contra
    una matrícula y colgarse de otra.

    Ahora:
      1. campos básicos
      2. se RESUELVE la matrícula (explícita o única candidata)
      3. todo lo demás se valida contra ESE objeto
      4. se emite y se audita con esa misma matrícula
    """
    # ── 1. Campos básicos ──
    for f in ("student_id", "course_id", "level_id"):
        if not body.get(f):
            raise HTTPException(400, f"{f} es requerido")

    # ── 2. RESOLVER LA MATRÍCULA, una sola vez ──
    _pedido = (body.get("enrollment_id") or "").strip() or None

    if _pedido:
        enr = await db.get(Enrollment, _pedido)
        if not enr:
            raise HTTPException(404, "Matrícula no encontrada")
    else:
        _candidatas = (await db.execute(
            select(Enrollment).where(
                Enrollment.student_id == body["student_id"],
                Enrollment.course_id == body["course_id"],
                Enrollment.level_id == body["level_id"],
            ).order_by(Enrollment.enrolled_at.desc())
        )).scalars().all()

        if not _candidatas:
            raise HTTPException(400, {
                "mensaje": ("Ese estudiante no tiene ninguna matrícula de ese "
                            "curso y nivel."),
            })
        if len(_candidatas) > 1:
            # Repitió el nivel: elegir por él sería adivinar
            raise HTTPException(400, {
                "necesita_enrollment_id": True,
                "mensaje": (
                    f"Ese estudiante tiene {len(_candidatas)} matrículas de ese "
                    "nivel (repitió). Indica a cuál pertenece el certificado."
                ),
                "opciones": [{
                    "enrollment_id": x.id,
                    "academic_status": getattr(x, "academic_status", "active"),
                    "enrolled_at": x.enrolled_at.isoformat() if x.enrolled_at else None,
                    "completed_at": x.completed_at.isoformat() if x.completed_at else None,
                } for x in _candidatas],
            })
        enr = _candidatas[0]

    # ── 3. INTEGRIDAD: la matrícula debe coincidir con lo pedido ──
    #
    # Sin esto se podría mandar el enrollment de Spanish A2 con un cuerpo de
    # English B1 y emitir un certificado incoherente.
    if enr.student_id != body["student_id"]:
        raise HTTPException(400, "Esa matrícula no es de ese estudiante")
    if enr.course_id != body["course_id"]:
        raise HTTPException(400, "Esa matrícula no es de ese curso")
    if enr.level_id != body["level_id"]:
        raise HTTPException(400, "Esa matrícula no es de ese nivel")

    _estado_enr = getattr(enr, "academic_status", None) or "active"

    # ── 4. Un certificado activo por MATRÍCULA ──
    #
    # La protección es por matrícula, no por nivel: si repitió B1 y la
    # primera ya tiene certificado, la segunda puede tener el suyo. Es la
    # semántica de P3 (Certificate pertenece a Enrollment).
    _ya = (await db.execute(
        select(Certificate).where(
            Certificate.enrollment_id == enr.id,
            Certificate.revoked.is_(False),
        ).limit(1)
    )).scalar_one_or_none()
    if _ya:
        raise HTTPException(400, {
            "mensaje": (f"Esa matrícula ya tiene el certificado {_ya.code} "
                        "activo."),
        })

    # ── 5. Estado académico DE ESA MATRÍCULA ──
    if _estado_enr != "completed":
        if not body.get("confirmar_incompleto"):
            from app.services.academic_config import ESTADOS_ACADEMICOS
            raise HTTPException(409, {
                "necesita_confirmacion": True,
                "academic_status": _estado_enr,
                "enrollment_id": enr.id,
                "mensaje": (
                    "Esa matrícula todavía no tiene el nivel completado "
                    f"(está como «{ESTADOS_ACADEMICOS.get(_estado_enr, _estado_enr)}»). "
                    "Apruébala primero en Finalizaciones, o indica un motivo "
                    "para emitir por excepción."
                ),
            })

        # Excepción: exige motivo y se audita CON ESTA matrícula
        _motivo_exc = (body.get("exception_reason") or "").strip()
        if not _motivo_exc:
            raise HTTPException(400, {
                "necesita_motivo": True,
                "mensaje": ("Para emitir sin el nivel completado hace falta "
                            "explicar por qué. Queda registrado."),
            })
        await log_action(
            db, admin.user_id, "certificate_exception", "certificates",
            target_id=enr.id,
            details=(f"estado={_estado_enr} · nivel={body['level_id']} · "
                     f"motivo={_motivo_exc[:200]}"),
        )

    # ── 6. Emitir ──
    # Código único, como se generaba antes
    code = token_urlsafe(8).replace("_", "").replace("-", "").upper()[:12]
    while (await db.execute(
        select(Certificate).where(Certificate.code == code)
    )).scalar_one_or_none():
        code = token_urlsafe(8).replace("_", "").replace("-", "").upper()[:12]

    c = Certificate(
        code=code,
        student_id=body["student_id"],
        course_id=body["course_id"],
        level_id=body["level_id"],
        enrollment_id=enr.id,
        hours=body.get("hours", 120),
        final_grade=body.get("final_grade"),
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


@router.post("/certificates/{cert_id}/revoke")
async def revoke_cert(
    cert_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.28 — Anular un certificado emitido por error.

    NO se borra: queda el registro de que existió y fue anulado, con el
    motivo y la fecha. Eso es lo correcto para un documento oficial.

    Efectos: desaparece del panel del estudiante y su código deja de
    verificar como válido.
    """
    c = await db.get(Certificate, cert_id)
    if not c:
        raise HTTPException(404, "Certificado no encontrado")
    if c.revoked:
        raise HTTPException(400, "Ese certificado ya está anulado")

    motivo = (body.get("reason") or "").strip()
    if not motivo:
        raise HTTPException(400, "Indica el motivo de la anulación")

    c.revoked = True
    c.revoked_reason = motivo
    c.revoked_at = datetime.now(tz.utc)

    # Avisar al estudiante para que no quede con un certificado que ya no vale
    db.add(Notification(
        user_id=c.student_id, type=NotificationType.info,
        title="Certificado anulado",
        body=f"El certificado {c.code} fue anulado. Motivo: {motivo}. Si tienes dudas, escríbenos.",
        link="/dashboard/student/certificates",
    ))
    await log_action(db, admin.user_id, "revoke_certificate", "admin",
                     target_id=cert_id, details=motivo)
    await db.commit()
    return {"ok": True, "code": c.code}


@router.post("/certificates/{cert_id}/restore")
async def restore_cert(
    cert_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Deshacer la anulación, por si también eso fue un error."""
    c = await db.get(Certificate, cert_id)
    if not c:
        raise HTTPException(404, "Certificado no encontrado")
    c.revoked = False
    c.revoked_reason = None
    c.revoked_at = None
    await log_action(db, admin.user_id, "restore_certificate", "admin", target_id=cert_id)
    await db.commit()
    return {"ok": True}


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

    # V2.5: Validar tamaño del logo si es base64
    if "logo_url" in body and body["logo_url"]:
        logo = body["logo_url"]
        # Si es base64 data URL, validar tamaño
        if logo.startswith("data:"):
            # Estimación rápida: 1 byte de base64 = 0.75 bytes reales
            estimated_bytes = len(logo) * 0.75
            max_bytes = 800 * 1024  # 800 KB max
            if estimated_bytes > max_bytes:
                raise HTTPException(400,
                    f"El logo es muy pesado ({estimated_bytes/1024:.0f}KB). Máximo permitido: 800KB. "
                    "Comprime la imagen o usa una más pequeña.")
            # Validar que sea imagen
            if not (logo.startswith("data:image/png") or
                    logo.startswith("data:image/jpeg") or
                    logo.startswith("data:image/jpg") or
                    logo.startswith("data:image/webp") or
                    logo.startswith("data:image/svg")):
                raise HTTPException(400, "El logo debe ser PNG, JPG, WebP o SVG.")

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
    """V2.8: Auditoría con nombres legibles + acciones en español."""
    # V2.8: Diccionario de acciones → texto en español
    ACTION_LABELS = {
        "register": "Se registró en la plataforma",
        "login": "Inició sesión",
        "logout": "Cerró sesión",
        "update_profile": "Actualizó su perfil",
        "change_password": "Cambió su contraseña",
        "request_password_reset": "Solicitó recuperar contraseña",
        "reset_password": "Cambió contraseña con token",
        "verify_email": "Verificó su email",
        "create_user": "Creó un usuario",
        "update_user": "Editó un usuario",
        "delete_user": "Eliminó un usuario",
        "pause_student": "Pausó a un estudiante",
        "resume_student": "Reactivó a un estudiante",
        "create_session": "Creó una clase",
        "update_session": "Editó una clase",
        "cancel_session": "Canceló una clase",
        "delete_session": "Eliminó una clase",
        "create_class_series": "Creó una serie semanal",
        "update_class_series": "Editó una serie",
        "delete_class_series": "Eliminó una serie",
        "mark_attendance": "Registró asistencia",
        "create_assignment": "Creó una tarea",
        "grade_assignment": "Calificó una tarea",
        "create_quiz": "Creó un quiz",
        "create_material": "Subió material",
        "create_event": "Creó un evento",
        "create_certificate": "Generó un certificado",
        "create_plan": "Creó un plan",
        "update_plan": "Editó un plan",
        "delete_plan": "Eliminó un plan",
        "create_payment": "Registró un pago",
        "update_payment": "Editó un pago",
        "mark_teacher_paid": "Pagó a un profesor",
        "delete_teacher_payment": "Eliminó pago a profesor",
        "create_branch": "Creó una sucursal",
        "create_classroom": "Creó un aula",
        "update_settings": "Actualizó la configuración",
        "create_bank_account": "Creó una cuenta bancaria",
        "update_bank_account": "Editó una cuenta bancaria",
        "deactivate_bank_account": "Desactivó una cuenta bancaria",
        "approve_payment_proof": "Aprobó un pago por transferencia",
        "reject_payment_proof": "Rechazó un pago por transferencia",
        "submit_payment_proof": "Subió comprobante de pago",
        "request_trial_class": "Solicitó clase de prueba",
        "schedule_trial_class": "Agendó una clase de prueba",
        "send_message": "Envió un mensaje",
        "open_ticket": "Abrió un ticket",
        "close_ticket": "Cerró un ticket",
        "complete_placement": "Completó test de nivel",
    }

    MODULE_LABELS = {
        "auth": "Cuenta",
        "admin": "Administración",
        "student": "Estudiante",
        "teacher": "Profesor",
        "payments": "Pagos",
        "messages": "Mensajes",
        "placement": "Test de nivel",
        "events": "Eventos",
    }

    offset = (page - 1) * limit
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
    logs = (await db.execute(stmt)).scalars().all()

    items = []
    for l in logs:
        # Obtener nombre del usuario que hizo la acción
        actor_name = "?"
        actor_email = "?"
        actor_role = "?"
        if l.user_id:
            actor = await db.get(User, l.user_id)
            if actor:
                actor_name = actor.full_name
                actor_email = actor.email
                actor_role = actor.role.value if actor.role else "?"

        # Si la acción tiene target_id, obtener info del afectado
        target_name = None
        if l.target_id:
            # Intentar como User
            target = await db.get(User, l.target_id)
            if target:
                target_name = target.full_name

        action_label = ACTION_LABELS.get(l.action, l.action.replace("_", " ").capitalize())
        module_label = MODULE_LABELS.get(l.module, l.module.capitalize() if l.module else "?")

        items.append({
            "id": l.id,
            "user_id": l.user_id,
            "actor_name": actor_name,
            "actor_email": actor_email,
            "actor_role": actor_role,
            "action": l.action,
            "action_label": action_label,
            "module": l.module,
            "module_label": module_label,
            "target_id": l.target_id,
            "target_name": target_name,
            "details": l.details if hasattr(l, "details") else None,
            "ip": l.ip,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        })

    return {"items": items, "page": page, "limit": limit}


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
    # V3.9.21: guardar valores previos para detectar cambios que importan al estudiante
    old_starts = s.starts_at_utc
    old_teacher = s.teacher_id
    old_modality = s.modality
    old_url = s.meeting_url
    for field, value in body.items():
        if is_past and field not in allowed_past:
            continue  # ignorar campos no permitidos para clases pasadas
        if field == "starts_at_utc" and value:
            s.starts_at_utc = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif field == "ends_at_utc" and value:
            s.ends_at_utc = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif field == "modality" and value:
            s.modality = Modality(value)
        elif field == "video_provider":
            # V3.9.26: solo dos valores válidos; cualquier otra cosa cae en "meet"
            s.video_provider = "dorismon" if value == "dorismon" else "meet"
        elif hasattr(s, field):
            setattr(s, field, value)

    # V3.9.21: si cambió hora/profesor/modalidad/link de una clase FUTURA,
    # avisar a los estudiantes afectados (antes editabas y nadie se enteraba)
    if not is_past:
        cambios = []
        if s.starts_at_utc != old_starts:
            from zoneinfo import ZoneInfo as _ZIu
            nueva = (s.starts_at_utc if s.starts_at_utc.tzinfo else s.starts_at_utc.replace(tzinfo=tz.utc)).astimezone(_ZIu("America/Santo_Domingo"))
            cambios.append(f"nueva fecha/hora: {nueva.strftime('%d/%m/%Y %I:%M %p')}")
        if s.teacher_id != old_teacher:
            nt = await db.get(User, s.teacher_id)
            cambios.append(f"nuevo profesor: {nt.full_name if nt else '—'}")
        if s.modality != old_modality:
            cambios.append(f"nueva modalidad: {s.modality.value}")
        if s.meeting_url != old_url and s.meeting_url:
            cambios.append("nuevo link de clase")
        if cambios:
            try:
                affected = set()
                if s.student_id:
                    affected.add(s.student_id)
                else:
                    rows = (await db.execute(
                        select(Enrollment.student_id).where(
                            Enrollment.course_id == s.course_id,
                            Enrollment.level_id == s.level_id,
                            Enrollment.is_active.is_(True),
                        )
                    )).all()
                    affected.update(x for (x,) in rows)
                for st_id in affected:
                    db.add(Notification(
                        user_id=st_id, type=NotificationType.info,
                        title="🔄 Tu clase cambió",
                        body=f"'{s.title}': " + " · ".join(cambios) + ".",
                    ))
            except Exception:
                pass

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
    old_modality = enr.modality
    if "teacher_id" in body: enr.teacher_id = body["teacher_id"]
    if "plan_id" in body: enr.plan_id = body["plan_id"]
    if "level_id" in body: enr.level_id = body["level_id"]
    if "is_active" in body: enr.is_active = body["is_active"]
    # V2.3: cambiar modalidad
    if "modality" in body:
        try:
            enr.modality = Modality(body["modality"])
        except ValueError:
            raise HTTPException(400, "Modalidad inválida")
    # Notificar al estudiante del cambio
    changes = []
    if old_teacher != enr.teacher_id: changes.append("profesor")
    if old_plan != enr.plan_id: changes.append("plan")
    if old_level != enr.level_id: changes.append("nivel")
    if old_modality != enr.modality: changes.append("modalidad")
    if changes:
        db.add(Notification(
            user_id=enr.student_id,
            type=NotificationType.info,
            title="📝 Cambios en tu inscripción",
            body=f"Se actualizó tu {', '.join(changes)}. Consulta los detalles con un coordinador.",
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
    # V3.9.33 — EL BUG QUE ENCONTRÉ: antes solo se guardaba "feature" (el texto
    # que lee el estudiante) y NUNCA "feature_key" (lo que de verdad desbloquea).
    # Por eso podías escribir "Quizzes incluidos" en un plan y no pasaba nada.
    from app.services.feature_gates import FEATURE_KEYS

    llave = (body.get("feature_key") or "").strip() or None
    if llave and llave not in FEATURE_KEYS:
        raise HTTPException(400, f"Esa función no existe: {llave}")

    f = PlanFeature(
        plan_id=plan_id,
        feature=body.get("feature", ""),
        feature_key=llave,
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
    # V3.9.33: también se puede cambiar QUÉ desbloquea
    if "feature_key" in body:
        from app.services.feature_gates import FEATURE_KEYS
        llave = (body.get("feature_key") or "").strip() or None
        if llave and llave not in FEATURE_KEYS:
            raise HTTPException(400, f"Esa función no existe: {llave}")
        f.feature_key = llave
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
        # V2.1.1: Si el estudiante ya está inscripto, mostrar también su nivel ACTUAL
        # (puede ser distinto al sugerido si el admin lo cambió al inscribirlo)
        current_level = None
        current_level_code = None
        if s.current_level_id:
            cl = await db.get(Level, s.current_level_id)
            if cl:
                current_level = cl.id
                current_level_code = cl.code

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
            # V2.1.1: nivel actual real (puede diferir si admin lo cambió)
            "current_level_id": current_level,
            "current_level_code": current_level_code,
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
async def certification_candidates(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Quién puede recibir certificado.

    ⚠️ V3.9.54 — REESCRITO. Antes tenía su propio algoritmo (70% + módulos
    con la regla vieja), así que existían DOS criterios distintos de
    certificación y podían contradecirse.

    Ahora hay uno solo: el certificado se emite a quien tiene el nivel
    COMPLETADO por el flujo de P3 (profesor recomienda → Dirección aprueba).
    """
    filas = (await db.execute(
        select(Enrollment, User, Level, Course)
        .join(User, Enrollment.student_id == User.id)
        .join(Level, Enrollment.level_id == Level.id)
        .join(Course, Enrollment.course_id == Course.id)
        .where(Enrollment.academic_status == "completed")
        .order_by(Enrollment.completed_at.desc())
    )).all()

    out = []
    for e, u, nivel, curso in filas:
        ya = (await db.execute(
            select(Certificate).where(
                # V3.9.57 — Por MATRÍCULA, no por nivel: si repitió, cada
                # matrícula tiene (o no) el suyo.
                Certificate.enrollment_id == e.id,
                Certificate.revoked.is_(False),
            ).limit(1)
        )).scalar_one_or_none()

        out.append({
            "enrollment_id": e.id,
            "student_id": u.id, "student_name": u.full_name,
            "course_name": curso.name,
            "level_id": nivel.id, "level_code": nivel.code, "level_name": nivel.name,
            "completed_at": e.completed_at.isoformat() if e.completed_at else None,
            "final_score": float(e.final_score) if e.final_score is not None else None,
            "final_result": e.final_result,
            "already_has_certificate": bool(ya),
            "certificate_code": ya.code if ya else None,
            "hours": nivel.hours_required or 120,
        })

    pendientes = [x for x in out if not x["already_has_certificate"]]
    return {
        "items": out,
        "count": len(out),
        "pending_issue": len(pendientes),
        "criterio": ("Solo aparecen matrículas con el nivel COMPLETADO y "
                     "aprobado por Dirección."),
    }


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

    # ⚠️ V3.9.54 — UN SOLO CRITERIO DE CERTIFICACIÓN.
    #
    # Antes este endpoint tenía su propia regla (70% + ModuleProgress viejo).
    # Coexistían dos caminos para certificar, y podían contradecirse.
    #
    # Ahora exige lo mismo que el resto: nivel COMPLETADO por el flujo de P3
    # (el profesor recomienda, Dirección aprueba).
    _estado_leg = getattr(e, "academic_status", None) or "active"
    if _estado_leg != "completed":
        from app.services.academic_config import ESTADOS_ACADEMICOS
        raise HTTPException(400, {
            "necesita_completar": True,
            "academic_status": _estado_leg,
            "mensaje": (
                "Ese nivel todavía no está completado (está como "
                f"«{ESTADOS_ACADEMICOS.get(_estado_leg, _estado_leg)}»). "
                "Apruébalo primero en Finalizaciones."
            ),
        })

    # V3.9.57 — La duplicidad se comprueba por MATRÍCULA, no por nivel.
    #
    # Antes bastaba con que existiera un certificado del nivel para bloquear:
    # si Juan repitió B1, su segunda matrícula nunca podría certificarse
    # aunque la completara legítimamente.
    existing = (await db.execute(
        select(Certificate).where(
            Certificate.enrollment_id == e.id,
            Certificate.revoked.is_(False),
        ).limit(1)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            400,
            f"Esa matrícula ya tiene el certificado {existing.code} activo",
        )

    # Generar código único
    code = token_urlsafe(8).replace("_", "").replace("-", "").upper()[:12]
    while (await db.execute(select(Certificate).where(Certificate.code == code))).scalar_one_or_none():
        code = token_urlsafe(8).replace("_", "").replace("-", "").upper()[:12]

    final_grade = body.get("final_grade", 80.0)
    hours_completed = body.get("hours_completed", 60)

    # V3.9.54 — BUG PREEXISTENTE CORREGIDO.
    #
    # Esta ruta usaba `hours_completed` e `issued_by`, campos que Certificate
    # no tiene: cualquier intento de emitir por aquí terminaba en error 500.
    # La ruta legacy nunca llegó a funcionar. Ahora usa los campos reales y
    # guarda la matrícula de origen.
    cert = Certificate(
        code=code,
        student_id=e.student_id,
        course_id=e.course_id,
        level_id=e.level_id,
        enrollment_id=e.id,
        final_grade=final_grade,
        hours=hours_completed,
    )
    db.add(cert)

    # Notificar al estudiante
    level = await db.get(Level, e.level_id)
    course = await db.get(Course, e.course_id)
    db.add(Notification(
        user_id=e.student_id,
        type=NotificationType.info,
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

async def _video_efectivo_de_serie(db, series_id: str) -> dict:
    """V3.9.63 — Qué video usa HOY un grupo, leído de sus propias clases.

    La serie NO guarda `video_provider`: la fuente de verdad es cada
    ClassSession. Guardarlo también en la serie habría creado dos copias del
    mismo dato, que es como se empiezan a contradecir.

    QUÉ DEVUELVE Y POR QUÉ ESE ORDEN:

      1. La PRÓXIMA clase futura no cancelada. El editor de series sirve para
         cambiar el futuro, así que debe mostrar la configuración futura
         vigente. Si las clases pasadas fueron por Meet y las próximas son
         por Video Dorismon, lo correcto es "dorismon": es lo que el
         estudiante se va a encontrar.

      2. Si no hay futuras, la clase MÁS RECIENTE. El grupo terminó, pero su
         última configuración conocida sigue siendo la mejor respuesta.

      3. Si la serie no tiene ninguna clase, "meet": es el comportamiento
         histórico y lo que hace `ClassSession.video_provider` por defecto.

    Se devuelve también el `meeting_url` de esa misma clase, para que el
    editor precargue un par coherente y no mezcle el proveedor de una sesión
    con el enlace de otra.
    """
    from sqlalchemy import or_ as _or

    ahora = datetime.now(tz.utc)

    ref = (await db.execute(
        select(ClassSession).where(
            ClassSession.series_id == series_id,
            ClassSession.starts_at_utc > ahora,
            ClassSession.status != SessionStatus.cancelled,
        ).order_by(ClassSession.starts_at_utc.asc()).limit(1)
    )).scalar_one_or_none()

    if ref is None:
        # Sin futuras: la más reciente. Se prefiere una no cancelada, porque
        # una clase cancelada no dice cómo se daba el grupo.
        ref = (await db.execute(
            select(ClassSession).where(
                ClassSession.series_id == series_id,
                ClassSession.status != SessionStatus.cancelled,
            ).order_by(ClassSession.starts_at_utc.desc()).limit(1)
        )).scalar_one_or_none()

    if ref is None:
        ref = (await db.execute(
            select(ClassSession).where(ClassSession.series_id == series_id)
            .order_by(ClassSession.starts_at_utc.desc()).limit(1)
        )).scalar_one_or_none()

    if ref is None:
        return {"video_provider": "meet", "meeting_url": None, "source_session_id": None}

    return {
        "video_provider": getattr(ref, "video_provider", "meet") or "meet",
        "meeting_url": ref.meeting_url,
        "source_session_id": ref.id,
    }


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
    # V3.9.32: dónde ocurre el video de toda la serie
    _vp = "dorismon" if body.get("video_provider") == "dorismon" else "meet"
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
        # El enlace general del grupo. El PROVEEDOR no se guarda aquí: viaja
        # a cada ClassSession (`video_provider=_vp` más abajo) y se deriva de
        # ellas cuando hace falta. Una sola fuente de verdad.
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
    from zoneinfo import ZoneInfo
    _rd_tz = ZoneInfo("America/Santo_Domingo")
    for i, naive_dt in enumerate(dates):
        # V3.9.16 FIX: la hora que escribe el admin es hora DOMINICANA (UTC-4),
        # no UTC. Antes se guardaba como UTC crudo y las clases quedaban corridas
        # 4 horas (ej: escribías 10:00 y salían a las 06:00).
        starts_at = naive_dt.replace(tzinfo=_rd_tz).astimezone(tz.utc)
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
            video_provider=_vp,  # V3.9.32
            scheduled_teacher_id=body.get("teacher_id"),  # V3.9.44: histórico
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
        # V3.9.35 — Solo grupos VIVOS. Antes se listaban todas las series,
        # incluidas las canceladas y las que ya terminaron, así que al asignar
        # aparecían clases viejas que ya no existen.
        select(ClassSeries)
        .where(ClassSeries.is_active.is_(True))
        .order_by(ClassSeries.created_at.desc())
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
        _video = await _video_efectivo_de_serie(db, s.id)
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
            # V3.9.63: el modal de "Editar serie" necesita el estado REAL del
            # video para precargarlo. Sin esto, abrir el modal y guardar
            # borraba el enlace que ya tenía el grupo.
            #
            # El proveedor se DERIVA de la próxima clase futura (ver
            # `_video_efectivo_de_serie`): el editor cambia el futuro, así que
            # debe mostrar la configuración futura vigente, no un dato
            # duplicado en la serie que podría haberse quedado atrás.
            "meeting_url": _video["meeting_url"] or s.meeting_url,
            "video_provider": _video["video_provider"],
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


@router.patch("/class-series/{series_id}/reschedule")
async def reschedule_class_series(
    series_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.62 — Editar una serie recurrente COMPLETA, sin destruir nada.

    ══ QUÉ PROBLEMA RESUELVE ══

    Una serie con un Google Meet viejo que dejó de funcionar no se podía
    arreglar: el modal no dejaba tocar el enlace ni el proveedor de video, y
    lo único parecido —"reprogramar"— BORRABA todas las clases futuras y las
    volvía a crear. Cambiar un link costaba el historial del grupo.

    ══ LA REGLA NUEVA ══

    Se toca lo MÍNIMO necesario:

      · Solo cambia el video (proveedor y/o enlace) → se actualizan las
        clases futuras EN SITIO. No se borra ni una.
      · Cambia la hora y/o la duración, pero los días siguen siendo los
        mismos → también EN SITIO: se mueve la hora de cada clase futura
        conservando su fecha.
      · Cambian los DÍAS (la regla de recurrencia de verdad) → ahí sí hay
        que regenerar fechas, y se copia explícitamente todo lo que la
        clase traía: módulo, profesor programado, video, sede, aula, cupo,
        si cuenta para progreso y su título.

    Actualizar en sitio conserva los IDs de sesión, y con ellos la
    asistencia, las entregas, las grabaciones y los recordatorios ya
    enviados, que cuelgan de esos IDs.

    ══ LO QUE NUNCA SE TOCA ══

    Las clases PASADAS. Y las clases futuras ya canceladas: una cancelación
    es un hecho ocurrido, no un hueco que rellenar.

    Body (todo opcional — solo cambia lo que se envía):
      - days_of_week:    "mon,wed,fri"
      - start_time_hhmm: "17:00"          (hora dominicana)
      - duration_min:    90
      - teacher_id:      "uuid"           (cambio permanente de profesor)
      - modality:        "online" | "presencial" | "hibrida"
      - video_provider:  "meet" | "dorismon"
      - meeting_url:     "https://..."    (Meet, Zoom, Teams o cualquier HTTPS)
      - confirm_overlap: true             (si el profesor nuevo ya tiene clase)
    """
    from datetime import timedelta as td
    from zoneinfo import ZoneInfo

    _RD = ZoneInfo("America/Santo_Domingo")

    series = await db.get(ClassSeries, series_id)
    if not series:
        raise HTTPException(404, "Serie no encontrada")

    now = datetime.now(tz.utc)

    # ── 1. LEER Y VALIDAR LO QUE SE PIDE ────────────────────────────────
    #
    # Nada se escribe hasta que TODO esté validado. Una serie a medio
    # cambiar es peor que una serie sin cambiar.

    cambios_txt: list[str] = []   # para el aviso y la auditoría

    # Días
    dias_nuevos = None
    if body.get("days_of_week"):
        crudos = [d.strip().lower() for d in str(body["days_of_week"]).split(",") if d.strip()]
        invalidos = [d for d in crudos if d not in DAY_NAMES]
        if invalidos:
            raise HTTPException(400, f"Días inválidos: {', '.join(invalidos)}")
        if not crudos:
            raise HTTPException(400, "Indica al menos un día de la semana")
        # Ordenados de lunes a domingo, sin repetidos
        dias_nuevos = ",".join(sorted(set(crudos), key=lambda d: DAY_NAMES[d]))

    dias_actuales = ",".join(sorted(
        {d.strip().lower() for d in (series.days_of_week or "").split(",") if d.strip()},
        key=lambda d: DAY_NAMES.get(d, 99)))
    cambia_recurrencia = dias_nuevos is not None and dias_nuevos != dias_actuales

    # Hora
    hora_nueva = None
    if body.get("start_time_hhmm"):
        crudo = str(body["start_time_hhmm"]).strip()
        try:
            _hh, _mm = crudo.split(":")
            _hh, _mm = int(_hh), int(_mm)
            if not (0 <= _hh <= 23 and 0 <= _mm <= 59):
                raise ValueError
        except Exception:
            raise HTTPException(400, "Hora inválida (usa HH:MM, por ejemplo 19:00)")
        hora_nueva = f"{_hh:02d}:{_mm:02d}"

    # Duración
    dur_nueva = None
    if body.get("duration_min") is not None:
        try:
            dur_nueva = int(body["duration_min"])
        except Exception:
            raise HTTPException(400, "Duración inválida")
        if not (15 <= dur_nueva <= 480):
            raise HTTPException(400, "La duración debe estar entre 15 y 480 minutos")

    # Modalidad
    mod_nueva = None
    if body.get("modality"):
        try:
            mod_nueva = Modality(body["modality"])
        except Exception:
            raise HTTPException(400, "Modalidad inválida (online/presencial/hibrida)")

    # ── VIDEO: proveedor y enlace ───────────────────────────────────────
    #
    # V3.9.63 — El proveedor ACTUAL no se lee de la serie (la serie no lo
    # guarda): se deriva de la próxima clase futura no cancelada. Así el
    # editor razona sobre la configuración que el estudiante se va a
    # encontrar, no sobre un dato duplicado que pudo quedarse atrás.
    _video_actual = await _video_efectivo_de_serie(db, series_id)
    prov_actual = _video_actual["video_provider"]
    url_actual = _video_actual["meeting_url"] or series.meeting_url

    prov_nuevo = None
    if body.get("video_provider"):
        if body["video_provider"] not in ("meet", "dorismon"):
            raise HTTPException(400, "Proveedor de video inválido (meet/dorismon)")
        prov_nuevo = body["video_provider"]

    # `meeting_url` se lee con `in body` a propósito: mandar cadena vacía
    # significa "quítalo", y eso es distinto de no mandarlo.
    url_nueva = None
    url_enviada = "meeting_url" in body
    if url_enviada:
        url_nueva = (body.get("meeting_url") or "").strip() or None
        # Solo https. Un enlace de clase que no cifra no se acepta.
        if url_nueva and not url_nueva.lower().startswith("https://"):
            raise HTTPException(400, "El enlace debe empezar con https://")

    prov_final = prov_nuevo or prov_actual
    url_final = url_nueva if url_enviada else url_actual

    # ⚠️ Se distingue CAMBIAR EL PROVEEDOR de CAMBIAR EL ENLACE.
    #
    # Si solo se toca el enlace, NO se escribe `video_provider` en ninguna
    # sesión. Sin esta separación, abrir el editor de un grupo que da clase
    # por Video Dorismon y guardar solo un enlace de respaldo habría
    # reescrito el proveedor de todas sus clases futuras.
    cambia_proveedor = prov_nuevo is not None and prov_nuevo != prov_actual
    cambia_url = url_enviada and url_final != url_actual

    # Un enlace externo SIN enlace no es una clase, es un callejón sin
    # salida. Con Video Dorismon el enlace es opcional (es el respaldo).
    if prov_final == "meet" and not url_final:
        raise HTTPException(
            400, "Con enlace externo hace falta el link de la reunión "
                 "(Meet, Zoom, Teams u otro https://)")

    # Profesor (se delega al flujo existente más abajo)
    profe_nuevo = (body.get("teacher_id") or "").strip() or None
    if profe_nuevo and profe_nuevo == series.teacher_id:
        profe_nuevo = None  # ya es el suyo: no es un cambio

    # ── 2. QUÉ CLASES ENTRAN ────────────────────────────────────────────
    #
    # Solo futuras y no canceladas. Una clase cancelada del futuro es un
    # hecho registrado: no se mueve, no se borra, no se le cambia el link.
    futuras = (await db.execute(
        select(ClassSession).where(
            ClassSession.series_id == series_id,
            ClassSession.starts_at_utc > now,
            ClassSession.status != SessionStatus.cancelled,
        ).order_by(ClassSession.starts_at_utc)
    )).scalars().all()

    pasadas = (await db.execute(
        select(func.count()).select_from(ClassSession).where(
            ClassSession.series_id == series_id,
            ClassSession.starts_at_utc <= now,
        )
    )).scalar() or 0

    if cambia_recurrencia and not futuras:
        raise HTTPException(
            400, "Esta serie no tiene clases futuras: no hay fechas que regenerar. "
                 "Si quieres reactivar el grupo, crea una serie nueva.")

    # ── 3. APLICAR A LA SERIE ───────────────────────────────────────────
    if dias_nuevos and cambia_recurrencia:
        series.days_of_week = dias_nuevos
        _lbl = {"mon": "Lun", "tue": "Mar", "wed": "Mié", "thu": "Jue",
                "fri": "Vie", "sat": "Sáb", "sun": "Dom"}
        cambios_txt.append("días: " + ", ".join(_lbl.get(d, d) for d in dias_nuevos.split(",")))

    cambia_hora = hora_nueva is not None and hora_nueva != (series.start_time_hhmm or "")
    if cambia_hora:
        series.start_time_hhmm = hora_nueva
        try:
            _h12 = datetime(2000, 1, 1, int(hora_nueva[:2]), int(hora_nueva[3:5]))
            cambios_txt.append(f"hora: {_h12.strftime('%I:%M %p').lstrip('0')}")
        except Exception:
            cambios_txt.append(f"hora: {hora_nueva}")

    cambia_duracion = dur_nueva is not None and dur_nueva != (series.duration_min or 90)
    if cambia_duracion:
        series.duration_min = dur_nueva
        cambios_txt.append(f"duración: {dur_nueva} min")

    cambia_modalidad = mod_nueva is not None and mod_nueva != series.modality
    if cambia_modalidad:
        series.modality = mod_nueva
        cambios_txt.append(f"modalidad: {mod_nueva.value}")

    cambia_video = cambia_proveedor or cambia_url
    if cambia_url:
        # La serie SÍ guarda el enlace general (columna que ya existía), para
        # que las clases que se creen después lo hereden. El proveedor no.
        series.meeting_url = url_final
    if cambia_proveedor:
        cambios_txt.append(
            "video: clase dentro de Dorismon" if prov_final == "dorismon"
            else "video: enlace externo")
    elif cambia_url:
        cambios_txt.append("nuevo enlace de la clase")

    # ── 4. LAS CLASES FUTURAS ───────────────────────────────────────────
    actualizadas = 0
    regeneradas = 0
    no_movidas = 0   # su hora nueva ya pasó: se quedan donde estaban

    if not cambia_recurrencia:
        # ═══ CAMINO EN SITIO ═══
        #
        # Sin borrar nada. Se conservan los IDs y con ellos asistencia,
        # entregas, grabaciones, recordatorios enviados e histórico.
        duracion = series.duration_min or 90
        for s in futuras:
            tocada = False

            # Cada cosa por separado: cambiar el enlace NO reescribe el
            # proveedor de la clase, y viceversa.
            if cambia_proveedor:
                s.video_provider = prov_final
                tocada = True
            if cambia_url:
                s.meeting_url = url_final
                tocada = True

            if cambia_modalidad:
                s.modality = mod_nueva
                tocada = True

            if cambia_hora or cambia_duracion:
                inicio = s.starts_at_utc
                if inicio.tzinfo is None:
                    inicio = inicio.replace(tzinfo=tz.utc)

                movida = False
                if cambia_hora:
                    fecha_rd = inicio.astimezone(_RD).date()
                    hh, mm = (series.start_time_hhmm or "00:00").split(":")
                    nuevo_inicio = datetime(
                        fecha_rd.year, fecha_rd.month, fecha_rd.day,
                        int(hh), int(mm), tzinfo=_RD).astimezone(tz.utc)

                    # Si la hora nueva de ESE día ya pasó, la clase se queda
                    # en su hora original. Mover una clase al pasado sería
                    # inventar historia: figuraría como dada sin ocurrir.
                    if nuevo_inicio <= now:
                        no_movidas += 1
                    else:
                        s.starts_at_utc = nuevo_inicio
                        inicio = nuevo_inicio
                        movida = True

                # La duración se aplica igual, se haya movido o no: si la
                # clase se queda a su hora, su duración nueva sí es válida.
                if movida or cambia_duracion:
                    s.ends_at_utc = inicio + td(minutes=duracion)
                    tocada = True

            if tocada:
                actualizadas += 1
    else:
        # ═══ CAMINO DE REGENERACIÓN ═══
        #
        # Solo aquí, y solo porque los días cambiaron de verdad. Antes de
        # borrar se copia TODO lo que cada clase traía, para devolvérselo a
        # la clase que ocupa su lugar. Lo que no se copia, se pierde.
        conservar = [{
            "title": s.title,
            "description": s.description,
            "module_id": s.module_id,
            "scheduled_teacher_id": s.scheduled_teacher_id or s.teacher_id,
            "teacher_id": s.teacher_id,
            "counts_for_progress": s.counts_for_progress,
            "capacity": s.capacity,
            "branch_id": s.branch_id,
            "classroom_id": s.classroom_id,
            # Se conserva el proveedor DE CADA CLASE salvo que se haya
            # pedido cambiarlo explícitamente. Igual con el enlace.
            "video_provider": prov_final if cambia_proveedor else (
                getattr(s, "video_provider", "meet") or "meet"),
            "meeting_url": url_final if cambia_url else s.meeting_url,
            "modality": mod_nueva if cambia_modalidad else s.modality,
        } for s in futuras]

        cuantas = len(futuras)
        for s in futuras:
            await db.delete(s)
        await db.flush()

        # V3.9.17 — Si la hora nueva de HOY todavía no pasó, la primera
        # clase puede ser HOY. Antes siempre saltaba a mañana y si movías
        # "hoy 7pm → hoy 7am" la clase de hoy desaparecía.
        ahora_rd = now.astimezone(_RD)
        try:
            _hh, _mm = (series.start_time_hhmm or "00:00").split(":")
            hoy_a_la_hora = ahora_rd.replace(
                hour=int(_hh), minute=int(_mm), second=0, microsecond=0)
            desde = ahora_rd.date() if hoy_a_la_hora > ahora_rd else (ahora_rd + td(days=1)).date()
        except Exception:
            desde = (ahora_rd + td(days=1)).date()

        nuevas_fechas = _generate_session_dates(
            desde, None, cuantas, series.days_of_week, series.start_time_hhmm)
        if not nuevas_fechas:
            raise HTTPException(400, "No se pudieron generar fechas con esos días y hora")

        duracion = series.duration_min or 90
        for i, naive in enumerate(nuevas_fechas):
            base = conservar[i] if i < len(conservar) else conservar[-1]
            inicio = naive.replace(tzinfo=_RD).astimezone(tz.utc)
            db.add(ClassSession(
                course_id=series.course_id,
                level_id=series.level_id,
                teacher_id=base["teacher_id"] or series.teacher_id,
                title=base["title"] or f"{series.name} — Clase {pasadas + i + 1}",
                description=base["description"],
                modality=base["modality"] or series.modality,
                starts_at_utc=inicio,
                ends_at_utc=inicio + td(minutes=duracion),
                meeting_url=base["meeting_url"],
                video_provider=base["video_provider"],
                branch_id=base["branch_id"],
                classroom_id=base["classroom_id"],
                capacity=base["capacity"] if base["capacity"] is not None else series.capacity,
                module_id=base["module_id"],
                counts_for_progress=base["counts_for_progress"],
                scheduled_teacher_id=base["scheduled_teacher_id"],
                series_id=series.id,
            ))
            regeneradas += 1

    await db.flush()

    # ── 5. PROFESOR: SE DELEGA, NO SE DUPLICA ───────────────────────────
    #
    # El cambio permanente de profesor tiene su propio flujo (avisos a las
    # tres partes, detección de choques de horario, histórico). Se reutiliza
    # tal cual. Va DESPUÉS de mover el horario para que los choques se
    # busquen contra las horas NUEVAS, no contra las viejas.
    profe_resultado = None
    if profe_nuevo:
        profe_resultado = await _aplicar_cambio_profesor_serie(
            db, series, profe_nuevo, admin.user_id,
            confirm_overlap=bool(body.get("confirm_overlap")),
        )
        cambios_txt.append(f"profesor: {profe_resultado['teacher']}")

    if not cambios_txt:
        raise HTTPException(400, "No se envió ningún cambio")

    detalle = "; ".join(cambios_txt)
    await log_action(
        db, admin.user_id, "update_class_series", "sessions", target_id=series_id,
        details=(f"editar serie [{detalle}] — en_sitio={actualizadas}, "
                 f"regeneradas={regeneradas}, sin_mover={no_movidas}, pasadas_intactas={pasadas}"))
    await db.commit()

    # ── 6. AVISAR AL GRUPO REAL ─────────────────────────────────────────
    #
    # V3.9.62 — Antes el aviso salía por course_id + level_id: cambiabas el
    # horario de B1 Mañana y le llegaba también a B1 Noche, que no se enteró
    # de nada porque su horario no cambió. Ahora se usa el servicio central
    # de audiencia, la MISMA regla que decide qué clases ve el estudiante.
    try:
        from app.services.audience import destinatarios_de_serie
        from app.services.push_service import notify_user

        solo_video = cambia_video and not (
            cambia_recurrencia or cambia_hora or cambia_duracion
            or cambia_modalidad or profe_nuevo)
        titulo = "🔗 Nuevo enlace para tu clase" if solo_video else "🔄 Cambió el horario de tu clase"
        cuerpo = f"'{series.name}': {detalle}. Revisa tu calendario."

        destinatarios = await destinatarios_de_serie(db, series.id)
        for uid in destinatarios:
            db.add(Notification(
                user_id=uid, type=NotificationType.info,
                title=titulo, body=cuerpo, link="/dashboard/student",
            ))
            await notify_user(db, uid, titulo, cuerpo,
                              "/dashboard/student", f"serie:{series.id}")

        # El profesor también necesita enterarse de que su clase se movió.
        # Si hubo cambio de profesor, de eso ya avisó el flujo delegado.
        if series.teacher_id and not profe_nuevo:
            db.add(Notification(
                user_id=series.teacher_id, type=NotificationType.info,
                title=titulo, body=f"'{series.name}': {detalle}.",
                link="/dashboard/teacher",
            ))
        await db.commit()
    except Exception:
        # Un aviso que falla no debe deshacer un cambio ya guardado.
        pass

    return {
        "ok": True,
        "modo": "regenerada" if cambia_recurrencia else "en_sitio",
        "cambios": cambios_txt,
        "updated_classes": actualizadas,
        "regenerated_classes": regeneradas,
        "kept_past_classes": pasadas,
        "not_moved_classes": no_movidas,
        # Se devuelve el estado EFECTIVO tras el cambio, derivado igual que
        # lo hace el listado, para que el frontend refresque con el mismo
        # criterio con el que precargó el editor.
        "video_provider": prov_final,
        "meeting_url": url_final,
        "teacher_conflicts": (profe_resultado or {}).get("had_conflicts", 0),
    }


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
        video_provider=("dorismon" if body.get("video_provider") == "dorismon" else "meet"),  # V3.9.32
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

    # V2.9.1: NO permitir pagar montos cero o negativos (evita pagos fantasma)
    if period["total_amount"] <= 0:
        raise HTTPException(400, "No hay clases pagables en este período. El monto a pagar es 0.")

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

    # V2.9.1: commit temprano para capturar violación del constraint único
    # (protege contra doble-click / doble request simultáneo)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Este período acaba de ser marcado como pagado. Recarga la página.")

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


# ============= V2.2 — PERFIL DETALLADO DE ESTUDIANTE (ADMIN) =============

@router.get("/students/{student_id}/profile")
async def admin_get_student_profile(
    student_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.2: Admin obtiene el perfil completo de un estudiante."""
    u = await db.get(User, student_id)
    if not u or u.role != UserRole.student:
        raise HTTPException(404, "Estudiante no encontrado")
    s = await db.get(Student, student_id)

    age = None
    is_minor = False
    if s and s.birth_date:
        today = date.today()
        age = today.year - s.birth_date.year - ((today.month, today.day) < (s.birth_date.month, s.birth_date.day))
        is_minor = age < 18

    return {
        "user_id": u.id, "email": u.email, "full_name": u.full_name, "phone": u.phone,
        "gender": u.gender, "avatar_url": u.avatar_url, "email_verified": u.email_verified,
        "is_active": u.is_active,
        "birth_date": s.birth_date.isoformat() if s and s.birth_date else None,
        "age": age, "is_minor": is_minor,
        "document_type": s.document_type if s else None,
        "document_number": s.document_number if s else None,
        "address": s.address if s else None,
        "city": s.city if s else None,
        "sector": s.sector if s else None,
        "nationality": s.nationality if s else None,
        "emergency_contact_name": s.emergency_contact_name if s else None,
        "emergency_contact_relationship": s.emergency_contact_relationship if s else None,
        "emergency_contact_phone": s.emergency_contact_phone if s else None,
        "tutor_name": s.tutor_name if s else None,
        "tutor_relationship": s.tutor_relationship if s else None,
        "tutor_document": s.tutor_document if s else None,
        "tutor_phone": s.tutor_phone if s else None,
        "tutor_email": s.tutor_email if s else None,
        "how_found_us": s.how_found_us if s else None,
        "referred_by": s.referred_by if s else None,
        "special_notes": s.special_notes if s else None,
        "enrolled_at": s.enrolled_at.isoformat() if s and s.enrolled_at else None,
        "is_paused": s.is_paused if s else False,
        # V3.9.9: nivel actual del estudiante (para poder cambiarlo)
        "current_level_id": s.current_level_id if s else None,
        "current_level_code": (await db.get(Level, s.current_level_id)).code if s and s.current_level_id else None,
    }


@router.patch("/students/{student_id}/profile")
async def admin_update_student_profile(
    student_id: str, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.2: Admin edita el perfil completo de un estudiante."""
    s = await db.get(Student, student_id)
    if not s:
        raise HTTPException(404, "Estudiante no encontrado")

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

    if "birth_date" in body:
        val = body["birth_date"]
        if val:
            try:
                s.birth_date = date.fromisoformat(val)
            except Exception:
                raise HTTPException(400, "Fecha de nacimiento inválida")
        else:
            s.birth_date = None

    await log_action(db, admin.user_id, "admin_update_student_profile", "students", target_id=student_id)
    await db.commit()
    return {"ok": True}


@router.patch("/students/{student_id}/level")
async def admin_change_student_level(
    student_id: str, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.9: Cambiar el nivel actual de un estudiante (ej: empezar de cero,
    subir/bajar de nivel por decisión pedagógica). No toca inscripciones ni pagos.

    Body:
    - level_id: int (nuevo nivel)
    - reason: str opcional (motivo, queda en el log)
    """
    s = await db.get(Student, student_id)
    if not s:
        raise HTTPException(404, "Estudiante no encontrado")

    level_id = body.get("level_id")
    if not level_id:
        raise HTTPException(400, "level_id requerido")

    level = await db.get(Level, level_id)
    if not level:
        raise HTTPException(404, "Nivel no encontrado")

    old_level_id = s.current_level_id
    old_level = await db.get(Level, old_level_id) if old_level_id else None
    old_label = old_level.code if old_level else "sin nivel"

    s.current_level_id = level_id

    # V3.9.11 FIX: actualizar también las INSCRIPCIONES activas al nuevo nivel.
    # Esto es lo que hace que el cambio se refleje de verdad: el dashboard del
    # estudiante, sus clases, tareas y la vista del profesor leen de la inscripción
    # (Enrollment.level_id), no de current_level_id. Sin esto, el nivel cambiaba
    # "en la etiqueta" pero el estudiante seguía viendo las clases del nivel viejo.
    active_enrollments = (await db.execute(
        select(Enrollment).where(
            Enrollment.student_id == student_id,
            Enrollment.is_active.is_(True),
        )
    )).scalars().all()

    # El nuevo nivel pertenece a un curso; usamos ese curso para la inscripción
    enrollments_updated = 0
    for enr in active_enrollments:
        enr.level_id = level_id
        if level.course_id:
            enr.course_id = level.course_id
        enrollments_updated += 1

    reason = body.get("reason", "")
    # Notificar al estudiante del cambio
    db.add(Notification(
        user_id=student_id,
        type=NotificationType.info,
        title="📚 Tu nivel fue actualizado",
        body=f"Tu nivel ahora es {level.code} ({level.name})." + (f" Motivo: {reason}" if reason else ""),
    ))

    await log_action(db, admin.user_id, "change_student_level", "students",
                     target_id=student_id, details=f"{old_label} → {level.code}, enrollments_updated={enrollments_updated}" + (f" ({reason})" if reason else ""))
    await db.commit()
    return {
        "ok": True,
        "old_level": old_label,
        "new_level": level.code,
        "new_level_name": level.name,
        "enrollments_updated": enrollments_updated,
    }




@router.get("/students-by-teacher")
async def admin_students_by_teacher(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.9: Lista cada profesor con SUS estudiantes (los que tiene asignados
    vía inscripciones activas). Para que el admin vea de un vistazo quién tiene
    a quién.
    """
    # Todos los profesores activos
    teachers = (await db.execute(
        select(User).where(User.role == UserRole.teacher, User.is_active.is_(True))
    )).scalars().all()

    result = []
    for t in teachers:
        # Estudiantes con inscripción activa con este profesor
        enroll_rows = (await db.execute(
            select(Enrollment.student_id, Level.code)
            .outerjoin(Level, Enrollment.level_id == Level.id)
            .where(Enrollment.teacher_id == t.id, Enrollment.is_active.is_(True))
        )).all()

        students = []
        seen = set()
        for student_id, level_code in enroll_rows:
            if student_id in seen:
                continue
            seen.add(student_id)
            stu_user = await db.get(User, student_id)
            if stu_user:
                students.append({
                    "id": stu_user.id,
                    "full_name": stu_user.full_name,
                    "email": stu_user.email,
                    "level_code": level_code,
                })

        result.append({
            "teacher_id": t.id,
            "teacher_name": t.full_name,
            "teacher_email": t.email,
            "student_count": len(students),
            "students": students,
        })

    # Ordenar: los que tienen más estudiantes primero
    result.sort(key=lambda x: x["student_count"], reverse=True)
    return {"teachers": result}


@router.get("/finance/transactions")
async def admin_finance_transactions(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    year: int | None = None,
    month: int | None = None,
    db: AsyncSession = Depends(get_db),
):
    """V2.5: Lista de TODAS las transacciones del período (ingresos + gastos).

    Mezcla pagos de estudiantes Y pagos a profesores, ordenados por fecha.
    Útil para ver el flujo de caja del mes.
    """
    from datetime import datetime, timezone as tz
    now = datetime.now(tz.utc)
    target_year = year or now.year
    target_month = month or now.month

    from calendar import monthrange
    last_day = monthrange(target_year, target_month)[1]
    start = datetime(target_year, target_month, 1, tzinfo=tz.utc)
    end = datetime(target_year, target_month, last_day, 23, 59, 59, tzinfo=tz.utc)

    transactions = []

    # Ingresos (pagos de estudiantes)
    payments = (await db.execute(
        select(Payment).where(
            Payment.created_at >= start, Payment.created_at <= end,
        ).order_by(Payment.created_at.desc())
    )).scalars().all()

    for p in payments:
        student = await db.get(User, p.student_id) if p.student_id else None
        plan = await db.get(Plan, p.plan_id) if p.plan_id else None
        transactions.append({
            "type": "income",
            "id": p.id,
            "date": (p.paid_at or p.created_at).isoformat() if (p.paid_at or p.created_at) else None,
            "description": f"Pago: {student.full_name if student else '?'} ({plan.name if plan else 'sin plan'})",
            "amount": float(p.amount or 0),
            "status": p.status.value if p.status else "pending",
            "method": p.method,
            "reference": p.reference,
        })

    # Gastos (pagos a profes — solo los YA pagados existen en la tabla)
    from app.models import TeacherPayment
    teacher_pmts = (await db.execute(
        select(TeacherPayment).where(
            TeacherPayment.period_year == target_year,
            TeacherPayment.period_month == target_month,
        ).order_by(TeacherPayment.paid_at.desc())
    )).scalars().all()

    for tp in teacher_pmts:
        teacher_user = await db.get(User, tp.teacher_id) if tp.teacher_id else None
        transactions.append({
            "type": "expense",
            "id": tp.id,
            "date": tp.paid_at.isoformat() if tp.paid_at else None,
            "description": f"Pago a profe: {teacher_user.full_name if teacher_user else '?'} ({tp.period_year}-{tp.period_month:02d})",
            "amount": float(tp.total_amount or 0),
            "status": "paid",  # Si existe el registro, ya está pagado
            "method": tp.payment_method,
            "reference": tp.reference or tp.notes,
        })

    # Ordenar por fecha desc
    transactions.sort(key=lambda x: x["date"] or "", reverse=True)

    return {
        "year": target_year,
        "month": target_month,
        "total": len(transactions),
        "transactions": transactions,
    }


# ============= V2.6 — CUENTAS BANCARIAS =============

@router.get("/bank-accounts")
async def list_bank_accounts(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Lista todas las cuentas bancarias del instituto."""
    accounts = (await db.execute(
        select(BankAccount).order_by(BankAccount.is_active.desc(), BankAccount.bank_name)
    )).scalars().all()
    return [
        {
            "id": a.id,
            "bank_name": a.bank_name,
            "account_type": a.account_type.value if a.account_type else "savings",
            "account_number": a.account_number,
            "holder_name": a.holder_name,
            "holder_document": a.holder_document,
            "notes": a.notes,
            "is_active": a.is_active,
        }
        for a in accounts
    ]


@router.post("/bank-accounts", status_code=201)
async def create_bank_account(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Crear cuenta bancaria."""
    for f in ("bank_name", "account_number", "holder_name", "holder_document"):
        if not body.get(f):
            raise HTTPException(400, f"{f} requerido")

    acc_type = body.get("account_type", "savings")
    if acc_type not in ("savings", "checking"):
        acc_type = "savings"

    acc = BankAccount(
        bank_name=body["bank_name"].strip(),
        account_type=BankAccountType(acc_type),
        account_number=body["account_number"].strip(),
        holder_name=body["holder_name"].strip(),
        holder_document=body["holder_document"].strip(),
        notes=body.get("notes"),
        is_active=body.get("is_active", True),
    )
    db.add(acc)
    await log_action(db, admin.user_id, "create_bank_account", "admin", details=acc.bank_name)
    await db.commit()
    await db.refresh(acc)
    return {"id": acc.id, "ok": True}


@router.patch("/bank-accounts/{account_id}")
async def update_bank_account(
    account_id: int, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Editar cuenta bancaria."""
    acc = await db.get(BankAccount, account_id)
    if not acc:
        raise HTTPException(404)
    for f in ("bank_name", "account_number", "holder_name", "holder_document", "notes", "is_active"):
        if f in body:
            setattr(acc, f, body[f])
    if "account_type" in body and body["account_type"] in ("savings", "checking"):
        acc.account_type = BankAccountType(body["account_type"])
    await log_action(db, admin.user_id, "update_bank_account", "admin", target_id=str(account_id))
    await db.commit()
    return {"ok": True}


@router.delete("/bank-accounts/{account_id}")
async def delete_bank_account(
    account_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Eliminar cuenta bancaria (solo si nunca tuvo pagos asociados)."""
    acc = await db.get(BankAccount, account_id)
    if not acc:
        raise HTTPException(404)
    # Mejor desactivar que borrar (para histórico)
    acc.is_active = False
    await log_action(db, admin.user_id, "deactivate_bank_account", "admin", target_id=str(account_id))
    await db.commit()
    return {"ok": True}


# ============= V2.6 — VERIFICAR PRUEBAS DE PAGO =============

@router.get("/payment-proofs")
async def list_payment_proofs(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Lista pruebas de pago. Por defecto: solo las pendientes."""
    stmt = select(PaymentProof, User, Plan).join(
        User, PaymentProof.student_id == User.id
    ).outerjoin(Plan, PaymentProof.plan_id == Plan.id)

    if status:
        try:
            stmt = stmt.where(PaymentProof.status == PaymentProofStatus(status))
        except ValueError:
            raise HTTPException(400, "Estado inválido")
    else:
        # Default: solo pendientes
        stmt = stmt.where(PaymentProof.status == PaymentProofStatus.pending)

    stmt = stmt.order_by(PaymentProof.created_at.desc())
    rows = (await db.execute(stmt)).all()

    return [
        {
            "id": p.id,
            "student_id": p.student_id,
            "student_name": u.full_name,
            "student_email": u.email,
            "plan_id": p.plan_id,
            "plan_name": plan.name if plan else "Sin plan",
            "amount": float(p.amount),
            "currency": p.currency,
            "method": p.method.value if p.method else "bank_transfer",
            "bank_origin": p.bank_origin,
            "payment_date": p.payment_date.isoformat() if p.payment_date else None,
            "reference_number": p.reference_number,
            "voucher_url": p.voucher_url,
            "status": p.status.value if p.status else "pending",
            "student_notes": p.student_notes,
            "admin_notes": p.admin_notes,
            "modality": p.modality.value if p.modality else "online",
            "level_id": p.level_id,
            "course_id": p.course_id,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
        }
        for p, u, plan in rows
    ]


@router.post("/payment-proofs/{proof_id}/approve")
async def approve_payment_proof(
    proof_id: str,
    body: dict | None = None,
    admin: Annotated[CurrentUser, Depends(require_admin)] = None,
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Aprobar prueba de pago.

    Acciones automáticas:
    1. Marca el PaymentProof como approved
    2. Crea la inscripción del estudiante en el plan
    3. Asigna nivel + modalidad
    4. Crea un Payment como "paid"
    5. Envía email al estudiante de confirmación
    """
    proof = await db.get(PaymentProof, proof_id)
    if not proof:
        raise HTTPException(404, "Prueba de pago no encontrada")
    if proof.status != PaymentProofStatus.pending:
        raise HTTPException(400, f"Esta prueba ya está {proof.status.value}")

    student = await db.get(Student, proof.student_id)
    student_user = await db.get(User, proof.student_id)
    if not student or not student_user:
        raise HTTPException(404, "Estudiante no encontrado")

    plan = await db.get(Plan, proof.plan_id)
    if not plan:
        raise HTTPException(404, "Plan no encontrado")

    # Determinar level_id (si no viene, usar el current del estudiante o default)
    level_id = proof.level_id or student.current_level_id
    if not level_id:
        # Default A1
        lvl = (await db.execute(select(Level).where(Level.code == "A1"))).scalar_one_or_none()
        if lvl:
            level_id = lvl.id

    # Determinar course_id (default curso principal si no viene)
    course_id = proof.course_id
    if not course_id:
        course = (await db.execute(select(Course).limit(1))).scalar_one_or_none()
        if course:
            course_id = course.id

    if not level_id or not course_id:
        raise HTTPException(400, "No se pudo determinar curso/nivel. Configura nivel y curso por defecto.")

    # 1. Crear inscripción
    enrollment = Enrollment(
        student_id=proof.student_id,
        course_id=course_id,
        level_id=level_id,
        plan_id=proof.plan_id,
        modality=proof.modality,
        is_active=True,
    )
    db.add(enrollment)
    await db.flush()

    # 2. Actualizar nivel del estudiante
    student.current_level_id = level_id
    if not student.placement_done:
        student.placement_done = True

    # 3. Crear Payment correspondiente
    payment = Payment(
        student_id=proof.student_id,
        plan_id=proof.plan_id,
        amount=proof.amount,
        currency=proof.currency,
        status=PaymentStatus.paid,
        method=proof.method.value if proof.method else "bank_transfer",
        reference=proof.reference_number,
        paid_at=datetime.now(tz.utc),
    )
    db.add(payment)

    # 4. Actualizar proof
    proof.status = PaymentProofStatus.approved
    proof.enrollment_id = enrollment.id
    proof.reviewed_by_admin_id = admin.user_id
    proof.reviewed_at = datetime.now(tz.utc)
    if body and body.get("admin_notes"):
        proof.admin_notes = body["admin_notes"]

    # 5. Notificación in-app
    db.add(Notification(
        user_id=proof.student_id,
        type=NotificationType.info,
        title="✅ ¡Pago aprobado! Estás inscrito",
        body=f"Tu pago de RD${float(proof.amount):,.2f} fue confirmado. Ya tienes acceso a tu plan {plan.name}.",
        link="/dashboard/student",
    ))

    # 6. Email de confirmación
    try:
        from app.services.email_service import send_email, is_email_configured, _base_html
        if is_email_configured() and student_user.email:
            html = _base_html(f"""
                <h2>¡Hola, {student_user.full_name}! 🎉</h2>
                <p>Tu pago ha sido <strong>confirmado</strong> y tu inscripción está activa.</p>
                <p><strong>Detalles de tu inscripción:</strong></p>
                <ul style="line-height: 1.8;">
                    <li><strong>Plan:</strong> {plan.name}</li>
                    <li><strong>Monto pagado:</strong> RD${float(proof.amount):,.2f}</li>
                    <li><strong>Modalidad:</strong> {proof.modality.value if proof.modality else 'online'}</li>
                </ul>
                <p>Ya puedes acceder a todas las funciones de tu plan.</p>
                <p style="text-align: center; margin-top: 24px;">
                    <a href="https://dorismon.com/dashboard" class="button">Ir a mi dashboard</a>
                </p>
                <p style="font-size: 12px; color: #64748b;">
                    Pronto te asignaremos un profesor y empezarán tus clases. Te avisamos por email cuando esté listo.
                </p>
            """)
            await send_email(
                to=student_user.email,
                subject="✅ Pago confirmado — Inscripción activa | Dorismon",
                html=html,
            )
    except Exception:
        pass  # No bloquear si email falla

    await log_action(db, admin.user_id, "approve_payment_proof", "admin", target_id=proof_id,
                     details=f"enrollment={enrollment.id}")
    await db.commit()
    return {"ok": True, "enrollment_id": enrollment.id}


@router.post("/payment-proofs/{proof_id}/reject")
async def reject_payment_proof(
    proof_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Rechazar prueba de pago. Requiere motivo."""
    reason = body.get("reason", "").strip()
    if not reason or len(reason) < 10:
        raise HTTPException(400, "Debes proporcionar un motivo del rechazo (mínimo 10 caracteres)")

    proof = await db.get(PaymentProof, proof_id)
    if not proof:
        raise HTTPException(404)
    if proof.status != PaymentProofStatus.pending:
        raise HTTPException(400, f"Esta prueba ya está {proof.status.value}")

    proof.status = PaymentProofStatus.rejected
    proof.admin_notes = reason
    proof.reviewed_by_admin_id = admin.user_id
    proof.reviewed_at = datetime.now(tz.utc)

    # Notificar al estudiante
    student_user = await db.get(User, proof.student_id)
    db.add(Notification(
        user_id=proof.student_id,
        type=NotificationType.info,
        title="❌ Pago rechazado",
        body=f"Motivo: {reason}. Por favor verifica los datos y sube una nueva prueba.",
        link="/dashboard/student/payments",
    ))

    # Email
    try:
        from app.services.email_service import send_email, is_email_configured, _base_html
        if is_email_configured() and student_user and student_user.email:
            html = _base_html(f"""
                <h2>Hola, {student_user.full_name}</h2>
                <p>Lamentablemente no pudimos verificar tu pago. Te explicamos abajo:</p>
                <div style="background:#fef2f2;border-left:4px solid #ef4444;padding:12px;margin:16px 0;border-radius:6px;">
                    <p style="margin:0;font-size:14px;"><strong>Motivo:</strong></p>
                    <p style="margin:8px 0 0 0;">{reason}</p>
                </div>
                <p>Por favor verifica los datos y vuelve a subir tu prueba de pago.</p>
                <p style="text-align: center; margin-top: 24px;">
                    <a href="https://dorismon.com/checkout" class="button">Volver a enviar pago</a>
                </p>
                <p style="font-size: 12px; color: #64748b;">
                    Si crees que hay un error, escríbenos por la sección Ayuda de la plataforma.
                </p>
            """)
            await send_email(
                to=student_user.email,
                subject="No pudimos verificar tu pago | Dorismon",
                html=html,
            )
    except Exception:
        pass

    await log_action(db, admin.user_id, "reject_payment_proof", "admin", target_id=proof_id,
                     details=reason[:100])
    await db.commit()
    return {"ok": True}


# ============= V2.6 — CLASES DE PRUEBA =============

@router.get("/trial-classes")
async def list_trial_classes(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Lista solicitudes de clases de prueba.
    V3.0.4: incluye las que pidieron reagenda (no_show + reschedule_requested).
    """
    from sqlalchemy import or_ as _or
    stmt = select(TrialClass, User).join(User, TrialClass.student_id == User.id)
    if status and status != "all":
        stmt = stmt.where(TrialClass.status == status)
    elif status == "all":
        pass  # V3.0.4: traer todas (historial completo)
    else:
        # Default: las que requieren acción del admin:
        # - requested (nueva solicitud)
        # - scheduled (ya agendada, futura)
        # - no_show con reagenda pedida (el estudiante quiere otra fecha)
        stmt = stmt.where(
            _or(
                TrialClass.status.in_(["requested", "scheduled"]),
                (TrialClass.status == "no_show") & (TrialClass.reschedule_requested.is_(True)),
            )
        )
    stmt = stmt.order_by(TrialClass.created_at.desc())
    rows = (await db.execute(stmt)).all()

    return [
        {
            "id": tc.id,
            "student_id": tc.student_id,
            "student_name": u.full_name,
            "student_email": u.email,
            "modality": tc.modality.value if tc.modality else "online",
            "preferred_level": tc.preferred_level,
            "preferred_date": tc.preferred_date.isoformat() if tc.preferred_date else None,
            "preferred_time": tc.preferred_time,
            "notes": tc.notes,
            "status": tc.status,
            "teacher_id": tc.teacher_id,
            "scheduled_at": tc.scheduled_at.isoformat() if tc.scheduled_at else None,
            "created_at": tc.created_at.isoformat() if tc.created_at else None,
            # V3.0.4: marcar si es una solicitud de reagenda
            "reschedule_requested": tc.reschedule_requested,
            "reschedule_count": tc.reschedule_count,
        }
        for tc, u in rows
    ]


@router.post("/trial-classes/{trial_id}/schedule")
async def schedule_trial_class(
    trial_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Admin agenda la clase de prueba con un profesor.

    Body: teacher_id, scheduled_at (ISO datetime), meeting_url (opcional)
    """
    teacher_id = body.get("teacher_id")
    scheduled_at_str = body.get("scheduled_at")
    if not teacher_id or not scheduled_at_str:
        raise HTTPException(400, "teacher_id y scheduled_at son requeridos")

    tc = await db.get(TrialClass, trial_id)
    if not tc:
        raise HTTPException(404)

    try:
        scheduled_at = datetime.fromisoformat(scheduled_at_str.replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(400, "scheduled_at inválido (formato ISO)")

    tc.teacher_id = teacher_id
    tc.scheduled_at = scheduled_at
    # V3.0.4: si venía de una reagenda (no_show), contar la reagenda y limpiar el flag
    if tc.status == "no_show" and tc.reschedule_requested:
        tc.reschedule_count = (tc.reschedule_count or 0) + 1
    tc.reschedule_requested = False
    tc.completed_at = None  # limpiar por si estaba marcada
    tc.status = "scheduled"

    # V3.0.1: Crear una ClassSession REAL para que aparezca en el calendario del estudiante
    # Necesitamos course_id y level_id (no nulos). Usamos el nivel preferido del trial
    # o el primero disponible como fallback.
    from datetime import timedelta as _td
    meeting_url = body.get("meeting_url")
    # Resolver nivel
    level_obj = None
    if tc.preferred_level:
        level_obj = (await db.execute(
            select(Level).where(Level.code == tc.preferred_level).limit(1)
        )).scalar_one_or_none()
    if not level_obj:
        level_obj = (await db.execute(select(Level).order_by(Level.id).limit(1))).scalar_one_or_none()
    course_obj = None
    if level_obj:
        course_obj = await db.get(Course, level_obj.course_id) if hasattr(level_obj, "course_id") else None
    if not course_obj:
        course_obj = (await db.execute(select(Course).order_by(Course.id).limit(1))).scalar_one_or_none()

    trial_session = None
    # V3.0.5: si ya existe una sesión vinculada (doble click / reagenda), reutilizarla
    # en vez de crear otra. Esto evita clases de prueba duplicadas en el calendario.
    existing_session = None
    if tc.session_id:
        existing_session = await db.get(ClassSession, tc.session_id)

    if existing_session:
        # Actualizar la sesión existente con los nuevos datos
        existing_session.teacher_id = teacher_id
        existing_session.starts_at_utc = scheduled_at
        existing_session.ends_at_utc = scheduled_at + _td(hours=1)
        existing_session.meeting_url = meeting_url
        existing_session.modality = tc.modality
        existing_session.status = SessionStatus.scheduled
        trial_session = existing_session
        await db.flush()
    elif level_obj and course_obj:
        # Antes de crear, una verificación extra: ¿hay ya una sesión de prueba
        # para este estudiante a esta misma hora? (protege contra doble click sin session_id)
        dup = (await db.execute(
            select(ClassSession).where(
                ClassSession.student_id == tc.student_id,
                ClassSession.starts_at_utc == scheduled_at,
                ClassSession.counts_for_progress.is_(False),
                ClassSession.status == SessionStatus.scheduled,
            )
        )).scalar_one_or_none()
        if dup:
            trial_session = dup
            dup.teacher_id = teacher_id
            dup.meeting_url = meeting_url
        else:
            ends_at = scheduled_at + _td(hours=1)
            trial_session = ClassSession(
                course_id=course_obj.id,
                level_id=level_obj.id,
                teacher_id=teacher_id,
                title="🎁 Clase de prueba",
                description="Clase de prueba gratis para conocer la metodología.",
                modality=tc.modality,
                starts_at_utc=scheduled_at,
                ends_at_utc=ends_at,
                meeting_url=meeting_url,
                capacity=1,
                student_id=tc.student_id,  # privada para este estudiante
                counts_for_progress=False,  # no cuenta para CEFR
                status=SessionStatus.scheduled,
            )
            db.add(trial_session)
            await db.flush()
        # Guardar referencia en el trial si tiene el campo
        if hasattr(tc, "session_id"):
            tc.session_id = trial_session.id

    # Notificar
    student_user = await db.get(User, tc.student_id)
    teacher_user = await db.get(User, teacher_id)
    db.add(Notification(
        user_id=tc.student_id,
        type=NotificationType.info,
        title="🎁 Tu clase de prueba está agendada",
        body=f"Profesor: {teacher_user.full_name if teacher_user else '?'}. Fecha: {scheduled_at.strftime('%d/%m/%Y %H:%M')}",
        link="/dashboard/student/calendar",
    ))
    db.add(Notification(
        user_id=teacher_id,
        type=NotificationType.info,
        title="🎁 Tienes una clase de prueba",
        body=f"Estudiante: {student_user.full_name if student_user else '?'}. Fecha: {scheduled_at.strftime('%d/%m/%Y %H:%M')}",
        link="/dashboard/teacher",
    ))

    # V3.0.1: Enviar email al estudiante
    if student_user and student_user.email:
        try:
            from app.services.email_service import send_trial_class_scheduled_email
            await send_trial_class_scheduled_email(
                to_email=student_user.email,
                student_name=student_user.full_name,
                teacher_name=teacher_user.full_name if teacher_user else "Tu profesor",
                when_local=scheduled_at.strftime("%d/%m/%Y a las %H:%M UTC"),
                modality=tc.modality.value if tc.modality else "online",
                meeting_url=meeting_url,
            )
        except Exception:
            pass  # no bloquear si el email falla

    # V3.6: Avisar al MAESTRO por email que le asignaron esta clase de prueba
    if teacher_user and teacher_user.email:
        try:
            from app.services.email_service import send_teacher_class_assigned_email
            await send_teacher_class_assigned_email(
                teacher_email=teacher_user.email,
                teacher_name=teacher_user.full_name,
                class_title="🎁 Clase de prueba",
                when_local=scheduled_at.strftime("%d/%m/%Y a las %H:%M UTC"),
                modality=tc.modality.value if tc.modality else "online",
                is_trial=True,
            )
        except Exception:
            pass

    await log_action(db, admin.user_id, "schedule_trial_class", "admin", target_id=trial_id)
    await db.commit()
    return {"ok": True, "session_created": trial_session is not None}


@router.post("/trial-classes/{trial_id}/result")
async def set_trial_result(
    trial_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.10: Cerrar una clase de prueba marcando el resultado.

    Body:
    - attended: bool (True = asistió → completed, False = no asistió → no_show)
    - notes: str opcional (cómo fue la prueba, nivel sugerido, etc.)

    Tras cerrarla, el admin ve el siguiente paso sugerido:
    - Si asistió → "inscribir al estudiante"
    - Si no asistió → "reagendar o descartar"
    """
    tc = await db.get(TrialClass, trial_id)
    if not tc:
        raise HTTPException(404, "Clase de prueba no encontrada")

    attended = body.get("attended")
    if attended is None:
        raise HTTPException(400, "Indica si el estudiante asistió (attended: true/false)")

    tc.status = "completed" if attended else "no_show"
    tc.completed_at = datetime.now(tz.utc) if hasattr(tc, "completed_at") else None
    if body.get("notes"):
        tc.notes = (tc.notes or "") + f"\n[Resultado] {body['notes']}"

    # Notificar al estudiante según el resultado
    stu = await db.get(User, tc.student_id)
    if stu:
        if attended:
            db.add(Notification(
                user_id=tc.student_id,
                type=NotificationType.info,
                title="✅ ¡Gracias por tu clase de prueba!",
                body="Esperamos que la hayas disfrutado. Pronto te contactaremos para que continúes aprendiendo con nosotros.",
            ))
        else:
            db.add(Notification(
                user_id=tc.student_id,
                type=NotificationType.info,
                title="Te extrañamos en tu clase de prueba",
                body="No pudiste asistir a tu clase de prueba. Si quieres, puedes reagendarla desde tu panel.",
            ))

    await log_action(db, admin.user_id, "set_trial_result", "trial_classes",
                     target_id=trial_id, details=f"attended={attended}")
    await db.commit()

    return {
        "ok": True,
        "status": tc.status,
        "student_id": tc.student_id,
        "student_name": stu.full_name if stu else None,
        "next_step": "enroll" if attended else "reschedule_or_discard",
        "next_step_label": (
            "El estudiante asistió. Siguiente paso: inscribirlo en un plan."
            if attended else
            "El estudiante no asistió. Siguiente paso: reagendar o descartar."
        ),
    }


# ============= V2.8 — SOFT DELETE DE USUARIOS =============

@router.delete("/users/{user_id}")
async def soft_delete_user(
    user_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.8: Soft delete de usuario (profesor, estudiante o admin).

    NO borra físicamente. Marca como inactivo + email rename para liberarlo.
    El usuario desaparece de listas activas pero su historial (clases, pagos, asistencia)
    se conserva para auditoría y contabilidad.

    Restricciones de seguridad:
    - No se puede borrar a SÍ mismo
    - No se puede borrar al último admin activo
    """
    if user_id == admin.user_id:
        raise HTTPException(400, "No puedes eliminar tu propia cuenta")

    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")

    if not u.is_active:
        raise HTTPException(400, "Este usuario ya está inactivo")

    # Si es admin, verificar que no sea el último activo
    if u.role == UserRole.super_admin:
        active_admins = (await db.execute(
            select(func.count()).select_from(User).where(
                User.role == UserRole.super_admin,
                User.is_active.is_(True),
            )
        )).scalar() or 0
        if active_admins <= 1:
            raise HTTPException(400, "No puedes eliminar al último administrador activo")

    # Soft delete: desactivar + liberar email
    old_email = u.email
    u.is_active = False
    # Rename email para que se pueda crear otro usuario con ese email después
    timestamp = int(datetime.now(tz.utc).timestamp())
    u.email = f"deleted_{timestamp}_{old_email}"

    # Si es profe: cancelar sesiones futuras sin asignar
    if u.role == UserRole.teacher:
        future_sessions = (await db.execute(
            select(ClassSession).where(
                ClassSession.teacher_id == user_id,
                ClassSession.starts_at_utc > datetime.now(tz.utc),
                ClassSession.status == SessionStatus.scheduled,
            )
        )).scalars().all()
        for s in future_sessions:
            s.status = SessionStatus.cancelled

    # Si es estudiante: pausar enrollments activos
    if u.role == UserRole.student:
        st = await db.get(Student, user_id)
        if st:
            st.is_paused = True
            st.paused_at = datetime.now(tz.utc)
            st.pause_reason = "Usuario eliminado por administrador"

    await log_action(db, admin.user_id, "delete_user", "admin",
                     target_id=user_id, details=f"role={u.role.value} email={old_email}")
    await db.commit()
    return {"ok": True, "deleted_email": old_email}


# ============= V2.9 — RECORDATORIOS AUTOMÁTICOS DE CLASE =============

from fastapi import Header


@router.post("/send-class-reminders", include_in_schema=False)
async def send_class_reminders(
    db: AsyncSession = Depends(get_db),
    x_cron_secret: str | None = Header(None),
):
    """V2.9: Dispara emails recordatorio 24h antes para clases que NO los recibieron aún.

    Protegido con header `X-Cron-Secret` (env REMINDER_CRON_SECRET).
    Diseñado para ser llamado cada 1 hora por un cron externo (cron-job.org / uptimerobot).

    Lógica:
    - Busca clases con starts_at_utc entre (ahora + 23h) y (ahora + 25h)
    - Solo las que tienen reminder_24h_sent_at IS NULL y status=scheduled
    - Envía email a estudiantes inscritos + notificación in-app
    - Marca reminder_24h_sent_at = now() para no duplicar
    """
    import os
    expected = os.getenv("REMINDER_CRON_SECRET", "")
    if not expected or x_cron_secret != expected:
        raise HTTPException(401, "Invalid cron secret")

    from datetime import timedelta as td
    from app.services.email_service import send_class_reminder_24h_email
    now = datetime.now(tz.utc)
    window_start = now + td(hours=23)
    window_end = now + td(hours=25)

    # Buscar clases que necesitan recordatorio
    stmt = select(ClassSession).where(
        ClassSession.starts_at_utc >= window_start,
        ClassSession.starts_at_utc <= window_end,
        ClassSession.reminder_24h_sent_at.is_(None),
        ClassSession.status == SessionStatus.scheduled,
    )
    sessions = (await db.execute(stmt)).scalars().all()

    total_emails_sent = 0
    sessions_processed = 0

    for s in sessions:
        teacher_user = await db.get(User, s.teacher_id) if s.teacher_id else None
        teacher_name = teacher_user.full_name if teacher_user else "Tu profesor"
        when_local = s.starts_at_utc.strftime("%d/%m/%Y a las %H:%M UTC")
        classroom_info = None
        if s.classroom_id:
            cr = await db.get(Classroom, s.classroom_id)
            br = await db.get(Branch, cr.branch_id) if cr and cr.branch_id else None
            if cr and br:
                classroom_info = f"{br.name} — Aula {cr.name}"
                if br.address:
                    classroom_info += f" ({br.address})"

        # Buscar estudiantes inscritos
        # V3.9.38 FIX — Antes se avisaba a TODOS los inscritos del nivel, sin
        # mirar el grupo. Por eso a Marioli le llegó el correo de una clase de
        # otro grupo. Ahora se respeta el mismo criterio que usa el estudiante
        # para ver sus clases: su grupo, o su clase propia.
        # V3.9.38 — Una sola fuente de verdad: respeta el GRUPO del estudiante.
        # Antes se avisaba a todos los del nivel y por eso llegaban correos
        # de clases de otros grupos.
        student_ids = await _destinatarios_de_clase(db, s)

        for sid in student_ids:
            stu = await db.get(User, sid)
            if not stu or not stu.is_active:
                continue
            # Notificación in-app
            db.add(Notification(
                user_id=sid,
                type=NotificationType.class_reminder_24h if hasattr(NotificationType, "class_reminder_24h") else NotificationType.reminder,
                title="Recordatorio: tu clase es mañana",
                body=f"'{s.title}' — {when_local} — con {teacher_name}",
                link=f"/dashboard/student/sessions/{s.id}",
            ))
            # Email
            if stu.email_verified:
                try:
                    sent = await send_class_reminder_24h_email(
                        to_email=stu.email,
                        student_name=stu.full_name,
                        class_title=s.title,
                        when_local=when_local,
                        teacher_name=teacher_name,
                        meeting_url=s.meeting_url,
                        classroom_info=classroom_info,
                    )
                    if sent:
                        total_emails_sent += 1
                except Exception:
                    pass

        # V3.9.29: además del correo, avisar al teléfono de quien lo activó
        try:
            from app.services.push_service import notify_user
            from zoneinfo import ZoneInfo as _ZI
            _st = s.starts_at_utc
            if _st and _st.tzinfo is None:
                _st = _st.replace(tzinfo=tz.utc)
            _hora = _st.astimezone(_ZI("America/Santo_Domingo")).strftime("%I:%M %p").lstrip("0") if _st else ""
            for _sid in student_ids:
                await notify_user(
                    db, _sid, "📚 Tu clase es mañana",
                    f"{s.title} — mañana a las {_hora}",
                    "/dashboard/student", f"clase:{s.id}",
                )
        except Exception:
            pass

        # Marcar como enviado (aunque algunos emails hayan fallado, evita reintentos infinitos)
        s.reminder_24h_sent_at = now
        sessions_processed += 1

    await db.commit()

    # V3.9.21: RECORDATORIO DE TAREAS por vencer (mismo cron, cero config extra).
    # Tareas con fecha límite entre 23h y 25h desde ahora → aviso a estudiantes
    # del curso/nivel que NO han entregado. Dedup vía Notification.link.
    tasks_processed = 0
    task_notifs = 0
    try:
        from zoneinfo import ZoneInfo
        t_start = now + timedelta(hours=23)
        t_end = now + timedelta(hours=25)
        due_assignments = (await db.execute(
            select(Assignment).where(
                Assignment.due_at.is_not(None),
                Assignment.due_at >= t_start,
                Assignment.due_at <= t_end,
            )
        )).scalars().all()
        for a in due_assignments:
            # Estudiantes del nivel con inscripción activa (Assignment se ata a level)
            conds = [Enrollment.is_active.is_(True)]
            if a.level_id:
                conds.append(Enrollment.level_id == a.level_id)
            enr_rows = (await db.execute(select(Enrollment.student_id).where(*conds))).all()
            student_ids = {x for (x,) in enr_rows}
            if not student_ids:
                continue
            # Excluir quienes YA entregaron
            subs = (await db.execute(
                select(AssignmentSubmission.student_id).where(
                    AssignmentSubmission.assignment_id == a.id,
                )
            )).all()
            entregaron = {x for (x,) in subs}
            pendientes = student_ids - entregaron
            due_local = a.due_at
            if due_local.tzinfo is None:
                due_local = due_local.replace(tzinfo=tz.utc)
            due_txt = due_local.astimezone(ZoneInfo("America/Santo_Domingo")).strftime("%d/%m %I:%M %p")
            for st_id in pendientes:
                # Dedup: una sola vez por tarea+estudiante
                ya = (await db.execute(
                    select(Notification).where(
                        Notification.user_id == st_id,
                        Notification.link == f"taskrem:{a.id}",
                    )
                )).scalar_one_or_none()
                if ya:
                    continue
                db.add(Notification(
                    user_id=st_id, type=NotificationType.info,
                    title="📝 Tu tarea vence mañana",
                    body=f"'{a.title}' vence el {due_txt}. ¡Aún estás a tiempo de entregarla!",
                    link=f"taskrem:{a.id}",
                ))
                # V3.9.29: y al teléfono
                try:
                    from app.services.push_service import notify_user
                    await notify_user(
                        db, st_id, "📝 Tu tarea vence mañana",
                        f"{a.title} — vence el {due_txt}",
                        "/dashboard/student/assignments", f"tarea:{a.id}",
                    )
                except Exception:
                    pass
                task_notifs += 1
            tasks_processed += 1
        await db.commit()
    except Exception:
        pass

    # ========================================================================
    # V3.9.32 — AVISO DE 30 MINUTOS ANTES ("prepárate, ya casi empieza")
    # ========================================================================
    # Se busca en una ventana amplia (10 a 45 min) para que funcione aunque
    # el cron corra cada 15 minutos y no justo en el minuto exacto.
    # El campo reminder_30m_sent_at evita repetirlo.
    pronto_procesadas = 0
    pronto_avisos = 0
    try:
        from app.services.push_service import notify_user
        from zoneinfo import ZoneInfo as _ZI

        desde = now + timedelta(minutes=10)
        hasta = now + timedelta(minutes=45)
        proximas = (await db.execute(
            select(ClassSession).where(
                ClassSession.starts_at_utc >= desde,
                ClassSession.starts_at_utc <= hasta,
                ClassSession.status == SessionStatus.scheduled,
                ClassSession.reminder_30m_sent_at.is_(None),
            )
        )).scalars().all()

        for s in proximas:
            _st = s.starts_at_utc
            if _st and _st.tzinfo is None:
                _st = _st.replace(tzinfo=tz.utc)
            minutos = int((_st - now).total_seconds() // 60) if _st else 30
            hora_txt = (
                _st.astimezone(_ZI("America/Santo_Domingo")).strftime("%I:%M %p").lstrip("0")
                if _st else ""
            )

            # V3.9.38 — Misma fuente de verdad: respeta el grupo
            destinatarios = await _destinatarios_de_clase(db, s)

            cuerpo = f"'{s.title}' empieza a las {hora_txt}. ¡Prepárate!"
            for uid in destinatarios:
                db.add(Notification(
                    user_id=uid, type=NotificationType.info,
                    title=f"⏰ Tu clase empieza en {minutos} minutos",
                    body=cuerpo, link="/dashboard/student",
                ))
                await notify_user(
                    db, uid, f"⏰ Tu clase empieza en {minutos} minutos",
                    cuerpo, "/dashboard/student", f"pronto:{s.id}",
                )
                pronto_avisos += 1

            # Al profesor también: hoy casi no recibe avisos
            if s.teacher_id:
                db.add(Notification(
                    user_id=s.teacher_id, type=NotificationType.info,
                    title=f"⏰ Tu clase empieza en {minutos} minutos",
                    body=cuerpo, link="/dashboard/teacher",
                ))
                await notify_user(
                    db, s.teacher_id, f"⏰ Tu clase empieza en {minutos} minutos",
                    cuerpo, "/dashboard/teacher", f"pronto:{s.id}",
                )
                pronto_avisos += 1

            s.reminder_30m_sent_at = now
            pronto_procesadas += 1
        await db.commit()
    except Exception:
        pass

    # V3.9.43 — Detectar clases donde el profesor no entró (solo alerta)
    alertas_profe = 0
    try:
        from app.models import VideoPresence
        _lim = now - timedelta(minutes=MINUTOS_PARA_ALERTA_PROFESOR)
        _ses = (await db.execute(
            select(ClassSession).where(
                ClassSession.starts_at_utc <= _lim,
                ClassSession.ends_at_utc >= now,
                ClassSession.status == SessionStatus.scheduled,
                ClassSession.teacher_absent_alert_at.is_(None),
            )
        )).scalars().all()
        for _s in _ses:
            if not _s.teacher_id:
                continue
            _entro = (await db.execute(
                select(VideoPresence).where(
                    VideoPresence.session_id == _s.id,
                    VideoPresence.user_id == _s.teacher_id,
                )
            )).scalar_one_or_none()
            if _entro:
                continue
            _profe = await db.get(User, _s.teacher_id)
            _cuerpo = (f"'{_s.title}' ya empezó y "
                       f"{_profe.full_name if _profe else 'el profesor'} no ha entrado.")
            for _a in (await db.execute(
                select(User).where(User.role == UserRole.super_admin)
            )).scalars().all():
                db.add(Notification(
                    user_id=_a.id, type=NotificationType.reminder,
                    title="🚨 URGENTE: clase sin iniciar", body=_cuerpo,
                    link="/dashboard/admin/sessions",
                ))
            db.add(Notification(
                user_id=_s.teacher_id, type=NotificationType.reminder,
                title="⏰ Tu clase ya empezó",
                body=f"'{_s.title}' ya empezó. Entra cuanto antes.",
                link="/dashboard/teacher",
            ))
            _s.teacher_absent_alert_at = now
            alertas_profe += 1
        await db.commit()
    except Exception:
        pass

    return {
        "ok": True,
        "teacher_absent_alerts": alertas_profe,
        "sessions_processed": sessions_processed,
        "emails_sent": total_emails_sent,
        "task_reminders": {"assignments": tasks_processed, "notified": task_notifs},
        "starting_soon": {"classes": pronto_procesadas, "notified": pronto_avisos},
        "now_utc": now.isoformat(),
    }


# ============= V2.9.2 — LIMPIEZA OPERATIVA (empezar limpio en producción) =============

@router.post("/maintenance/clean-operational-data")
async def clean_operational_data(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.9.2: Limpia datos OPERATIVOS de prueba para empezar producción en limpio.

    BORRA (residuo de prueba):
    - Asistencias (session_attendance)
    - Registros a eventos (event_registrations)
    - Notas/observaciones de clase (observations)
    - Clases programadas (class_sessions)
    - Series semanales (class_series)
    - Pagos a profesores (teacher_payments)
    - Comprobantes de pago (payment_proofs)
    - Clases de prueba (trial_classes)
    - Inscripciones (enrollments)

    CONSERVA (datos reales):
    - Usuarios, perfiles, placement, niveles asignados
    - Cursos, módulos, lecciones, planes, sedes, aulas
    - Cuentas bancarias

    SEGURIDAD:
    - Solo admin
    - dry_run=true (default): solo CUENTA, no borra
    - Para borrar de verdad: dry_run=false Y confirm="BORRAR DATOS DE PRUEBA"
    """
    dry_run = body.get("dry_run", True)
    confirm = body.get("confirm", "")

    from sqlalchemy import text as sa_text

    # Tablas a limpiar EN ORDEN (respetando foreign keys: hijos primero)
    # (nombre_tabla, etiqueta legible)
    tables_in_order = [
        ("session_attendance", "Asistencias registradas"),
        ("event_registrations", "Registros a eventos"),
        ("observations", "Notas de clase"),
        ("trial_classes", "Clases de prueba"),
        ("teacher_payments", "Pagos a profesores"),
        ("payment_proofs", "Comprobantes de pago"),
        ("class_sessions", "Clases programadas"),
        ("class_series", "Series semanales"),
        ("enrollments", "Inscripciones"),
    ]

    # Contar registros actuales de cada tabla
    counts = {}
    for table, label in tables_in_order:
        try:
            n = (await db.execute(sa_text(f"SELECT COUNT(*) FROM {table}"))).scalar() or 0
            counts[table] = {"label": label, "count": n}
        except Exception:
            counts[table] = {"label": label, "count": 0}

    total = sum(c["count"] for c in counts.values())

    # DRY RUN: solo mostrar qué se borraría
    if dry_run:
        return {
            "dry_run": True,
            "message": "Esto es una simulación. NADA fue borrado.",
            "total_records_to_delete": total,
            "detail": [
                {"tabla": v["label"], "registros": v["count"]}
                for v in counts.values()
            ],
            "instrucciones": "Para borrar de verdad, envía dry_run=false y confirm='BORRAR DATOS DE PRUEBA'",
        }

    # EJECUCIÓN REAL: requiere confirmación exacta
    if confirm != "BORRAR DATOS DE PRUEBA":
        raise HTTPException(
            400,
            "Confirmación incorrecta. Para borrar, envía confirm='BORRAR DATOS DE PRUEBA'",
        )

    # Borrar en orden
    deleted = {}
    for table, label in tables_in_order:
        try:
            result = await db.execute(sa_text(f"DELETE FROM {table}"))
            deleted[table] = {"label": label, "deleted": counts[table]["count"]}
        except Exception as e:
            # Si una tabla falla, hacer rollback total y reportar
            await db.rollback()
            raise HTTPException(
                500,
                f"Error borrando '{label}': {str(e)[:100]}. NO se borró nada (rollback).",
            )

    await log_action(
        db, admin.user_id, "clean_operational_data", "admin",
        details=f"total_deleted={total}",
    )
    await db.commit()

    return {
        "dry_run": False,
        "ok": True,
        "message": "Datos operativos de prueba eliminados. Usuarios, placement y niveles conservados.",
        "total_deleted": total,
        "detail": [
            {"tabla": v["label"], "borrados": v["deleted"]}
            for v in deleted.values()
        ],
    }


# ============= V2.9.2 — REACTIVAR USUARIO (cualquier rol) =============

@router.post("/users/{user_id}/reactivate")
async def reactivate_user(
    user_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.9.2: Reactiva un usuario desactivado (profesor, estudiante o admin).

    - Pone is_active = True
    - Restaura el email original (quita el prefijo deleted_TIMESTAMP_)
    - Si es estudiante: lo des-pausa
    - NOTA: las clases que se cancelaron al desactivarlo NO se reactivan
      automáticamente (el admin las reprograma si las necesita), para evitar
      reactivar clases con fechas ya pasadas.
    """
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")

    if u.is_active:
        raise HTTPException(400, "Este usuario ya está activo")

    # Restaurar email original si tiene el prefijo deleted_
    if u.email.startswith("deleted_"):
        # formato: deleted_{timestamp}_{email_original}
        parts = u.email.split("_", 2)
        if len(parts) == 3:
            original_email = parts[2]
            # Verificar que no exista otro usuario activo con ese email
            existing = (await db.execute(
                select(User).where(User.email == original_email, User.id != user_id)
            )).scalar_one_or_none()
            if existing:
                raise HTTPException(
                    400,
                    f"No se puede restaurar el email '{original_email}' porque ya está en uso por otro usuario.",
                )
            u.email = original_email

    u.is_active = True

    # Si es estudiante, des-pausar
    if u.role == UserRole.student:
        st = await db.get(Student, user_id)
        if st and st.is_paused:
            st.is_paused = False
            st.paused_at = None
            st.pause_reason = None

    await log_action(db, admin.user_id, "reactivate_user", "admin",
                     target_id=user_id, details=f"role={u.role.value}")
    await db.commit()
    return {"ok": True, "email": u.email, "role": u.role.value}


# ============= V3.0.1 — AGENDA POR PROFESOR (admin) =============

@router.get("/teachers-schedule")
async def teachers_schedule(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.0.1: Resumen de la agenda de cada profesor.

    Para que el admin sepa de un vistazo qué tiene cada maestro:
    - Clase en curso ahora (si hay)
    - Próxima clase
    - Cuántas clases tiene hoy / esta semana
    """
    from datetime import timedelta as td
    now = datetime.now(tz.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + td(days=1)
    week_start = (now - td(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + td(days=7)

    # Todos los profes activos
    teachers = (await db.execute(
        select(User).where(User.role == UserRole.teacher, User.is_active.is_(True))
        .order_by(User.full_name)
    )).scalars().all()

    out = []
    for t in teachers:
        # Clases del profe (no canceladas)
        base = select(ClassSession).where(
            ClassSession.teacher_id == t.id,
            ClassSession.status != SessionStatus.cancelled,
        )
        # En curso ahora
        in_progress = (await db.execute(
            base.where(
                ClassSession.starts_at_utc <= now,
                ClassSession.ends_at_utc > now,
            ).limit(1)
        )).scalar_one_or_none()
        # Próxima clase futura
        next_class = (await db.execute(
            base.where(ClassSession.starts_at_utc > now)
            .order_by(ClassSession.starts_at_utc.asc()).limit(1)
        )).scalar_one_or_none()
        # Conteo hoy
        today_count = (await db.execute(
            select(func.count()).select_from(ClassSession).where(
                ClassSession.teacher_id == t.id,
                ClassSession.status != SessionStatus.cancelled,
                ClassSession.starts_at_utc >= today_start,
                ClassSession.starts_at_utc < today_end,
            )
        )).scalar() or 0
        # Conteo semana
        week_count = (await db.execute(
            select(func.count()).select_from(ClassSession).where(
                ClassSession.teacher_id == t.id,
                ClassSession.status != SessionStatus.cancelled,
                ClassSession.starts_at_utc >= week_start,
                ClassSession.starts_at_utc < week_end,
            )
        )).scalar() or 0

        def _fmt(cs):
            if not cs:
                return None
            course = None
            level = None
            return {
                "id": cs.id, "title": cs.title,
                "starts_at_utc": cs.starts_at_utc.isoformat() if cs.starts_at_utc else None,
                "ends_at_utc": cs.ends_at_utc.isoformat() if cs.ends_at_utc else None,
                "modality": cs.modality.value if cs.modality else None,
                "meeting_url": cs.meeting_url,
                "classroom_id": cs.classroom_id,
            }

        # Resolver aula/sede de la clase en curso o próxima para saber "dónde está"
        location = None
        ref = in_progress or next_class
        if ref and ref.classroom_id:
            cr = await db.get(Classroom, ref.classroom_id)
            br = await db.get(Branch, cr.branch_id) if cr and cr.branch_id else None
            if cr:
                location = f"{br.name} — {cr.name}" if br else cr.name
        elif ref and ref.modality and ref.modality.value == "online":
            location = "Online"

        out.append({
            "teacher_id": t.id,
            "teacher_name": t.full_name,
            "email": t.email,
            "in_progress": _fmt(in_progress),
            "next_class": _fmt(next_class),
            "today_count": today_count,
            "week_count": week_count,
            "current_location": location,
        })

    return {"teachers": out, "now_utc": now.isoformat()}


# ============================================================================
# V3.9.23 — IMÁGENES DEL SITIO (subida desde el admin vía Cloudinary)
# ============================================================================

@router.get("/site-images")
async def list_site_images(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Lista los espacios de imagen de la página pública con su foto actual.

    Devuelve también si Cloudinary está configurado, para que el panel
    pueda avisar en vez de fallar en silencio."""
    from app.services.cloudinary_service import SITE_IMAGE_SLOTS, cloudinary_ready, optimized_url

    rows = (await db.execute(select(SiteImage))).scalars().all()
    current = {r.slot: r for r in rows}

    items = []
    for spec in SITE_IMAGE_SLOTS:
        row = current.get(spec["slot"])
        items.append({
            **spec,
            # V3.9.25: vista previa liviana (el panel no necesita el original)
            "url": optimized_url(row.url, 600) if row else None,
            "updated_at": row.updated_at.isoformat() if row and row.updated_at else None,
        })
    return {"items": items, "cloudinary_ready": cloudinary_ready()}


@router.post("/site-images/{slot}")
async def upload_site_image(
    slot: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Sube (o reemplaza) la imagen de un espacio de la página pública."""
    from starlette.concurrency import run_in_threadpool
    from app.services.cloudinary_service import (
        SLOT_KEYS, cloudinary_ready, upload_image_sync,
    )

    if slot not in SLOT_KEYS:
        raise HTTPException(404, "Ese espacio de imagen no existe")
    # Primero lo que depende del usuario (mensaje más útil), luego la config
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "El archivo debe ser una imagen (JPG o PNG)")
    if not cloudinary_ready():
        raise HTTPException(
            503,
            "Falta configurar Cloudinary. Agrega la variable CLOUDINARY_URL en Render y vuelve a intentar.",
        )

    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "La imagen es muy pesada. El máximo son 10 MB.")
    if not data:
        raise HTTPException(400, "El archivo llegó vacío")

    try:
        res = await run_in_threadpool(
            upload_image_sync, data, f"site_{slot}", "dorismon/site"
        )
    except Exception as e:
        raise HTTPException(502, f"Cloudinary rechazó la imagen: {e}")

    existing = await db.get(SiteImage, slot)
    if existing:
        existing.url = res["url"]
        existing.public_id = res["public_id"]
        existing.updated_at = datetime.now(tz.utc)
    else:
        db.add(SiteImage(slot=slot, url=res["url"], public_id=res["public_id"]))

    await log_action(db, admin.user_id, "upload_site_image", "site_images", target_id=slot)
    await db.commit()
    return {"ok": True, "slot": slot, "url": res["url"]}


@router.delete("/site-images/{slot}")
async def delete_site_image(
    slot: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Quita la imagen de un espacio (la página vuelve a su estado sin foto)."""
    from starlette.concurrency import run_in_threadpool
    from app.services.cloudinary_service import delete_image_sync

    row = await db.get(SiteImage, slot)
    if not row:
        raise HTTPException(404, "Ese espacio no tiene imagen")
    if row.public_id:
        await run_in_threadpool(delete_image_sync, row.public_id)
    await db.delete(row)
    await log_action(db, admin.user_id, "delete_site_image", "site_images", target_id=slot)
    await db.commit()
    return {"ok": True}


# ============================================================================
# V3.9.23 — TESTIMONIOS (la sección solo aparece si hay al menos uno activo)
# ============================================================================

@router.get("/testimonials")
async def list_testimonials_admin(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(Testimonial).order_by(Testimonial.sort_order, Testimonial.created_at)
    )).scalars().all()
    return {"items": [{
        "id": t.id, "name": t.name, "role": t.role, "text": t.text,
        "photo_url": t.photo_url, "rating": t.rating,
        "is_active": t.is_active, "sort_order": t.sort_order,
    } for t in rows]}


@router.post("/testimonials", status_code=201)
async def create_testimonial(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    name = (body.get("name") or "").strip()
    text = (body.get("text") or "").strip()
    if not name or not text:
        raise HTTPException(400, "El nombre y el testimonio son obligatorios")
    rating = body.get("rating", 5)
    try:
        rating = max(1, min(5, int(rating)))
    except (TypeError, ValueError):
        rating = 5

    t = Testimonial(
        name=name, role=(body.get("role") or "").strip() or None,
        text=text, rating=rating,
        is_active=bool(body.get("is_active", True)),
        sort_order=int(body.get("sort_order") or 0),
    )
    db.add(t)
    await log_action(db, admin.user_id, "create_testimonial", "testimonials", target_id=t.id)
    await db.commit()
    return {"id": t.id, "ok": True}


@router.patch("/testimonials/{tid}")
async def update_testimonial(
    tid: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    t = await db.get(Testimonial, tid)
    if not t:
        raise HTTPException(404, "Testimonio no encontrado")
    if "name" in body and (body["name"] or "").strip():
        t.name = body["name"].strip()
    if "role" in body:
        t.role = (body["role"] or "").strip() or None
    if "text" in body and (body["text"] or "").strip():
        t.text = body["text"].strip()
    if "rating" in body:
        try:
            t.rating = max(1, min(5, int(body["rating"])))
        except (TypeError, ValueError):
            pass
    if "is_active" in body:
        t.is_active = bool(body["is_active"])
    if "sort_order" in body:
        try:
            t.sort_order = int(body["sort_order"])
        except (TypeError, ValueError):
            pass
    await log_action(db, admin.user_id, "update_testimonial", "testimonials", target_id=tid)
    await db.commit()
    return {"ok": True}


@router.post("/testimonials/{tid}/photo")
async def upload_testimonial_photo(
    tid: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Foto del estudiante. Se muestra en círculo, así que conviene cuadrada."""
    from starlette.concurrency import run_in_threadpool
    from app.services.cloudinary_service import cloudinary_ready, upload_image_sync

    t = await db.get(Testimonial, tid)
    if not t:
        raise HTTPException(404, "Testimonio no encontrado")
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "El archivo debe ser una imagen")
    if not cloudinary_ready():
        raise HTTPException(503, "Falta configurar Cloudinary (variable CLOUDINARY_URL en Render)")

    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "La imagen es muy pesada. El máximo son 10 MB.")

    try:
        res = await run_in_threadpool(
            upload_image_sync, data, f"testimonial_{tid}", "dorismon/testimonials"
        )
    except Exception as e:
        raise HTTPException(502, f"Cloudinary rechazó la imagen: {e}")

    t.photo_url = res["url"]
    t.photo_public_id = res["public_id"]
    await db.commit()
    return {"ok": True, "url": res["url"]}


@router.delete("/testimonials/{tid}")
async def delete_testimonial(
    tid: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    from starlette.concurrency import run_in_threadpool
    from app.services.cloudinary_service import delete_image_sync

    t = await db.get(Testimonial, tid)
    if not t:
        raise HTTPException(404, "Testimonio no encontrado")
    if t.photo_public_id:
        await run_in_threadpool(delete_image_sync, t.photo_public_id)
    await db.delete(t)
    await log_action(db, admin.user_id, "delete_testimonial", "testimonials", target_id=tid)
    await db.commit()
    return {"ok": True}


# ============================================================================
# V3.9.29 — REACTIVACIÓN: gente que ya mostró interés y se está perdiendo
# ============================================================================

@router.get("/reactivation")
async def reactivation_panel(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    dias_inactivo: int = 21,
):
    """Las dos fugas de dinero del negocio, en un solo lugar:

    1. LEADS FRÍOS: hicieron el test de nivel y NUNCA se inscribieron.
       Ya mostraron interés; solo les faltó el empujón.
    2. ESTUDIANTES APAGADOS: inscritos que no asisten hace semanas.
       Cuesta mucho menos recuperar uno que conseguir uno nuevo.

    Cada uno viene con su teléfono listo para escribirle por WhatsApp.
    """
    from app.models import PlacementTest

    now = datetime.now(tz.utc)
    hoy = date.today()

    # ---------- 1. Hicieron el test y no se inscribieron ----------
    q = (
        select(PlacementTest, User, Student, Level)
        .join(Student, PlacementTest.student_id == Student.user_id)
        .join(User, Student.user_id == User.id)
        .outerjoin(Level, PlacementTest.suggested_level_id == Level.id)
        .where(PlacementTest.completed_at.is_not(None))
        .order_by(PlacementTest.completed_at.desc())
    )
    rows = (await db.execute(q)).all()

    leads = []
    for test, u, s, lvl in rows:
        inscrito = (await db.execute(
            select(func.count()).select_from(Enrollment).where(
                Enrollment.student_id == u.id, Enrollment.is_active.is_(True)
            )
        )).scalar() > 0
        if inscrito:
            continue
        completado = test.completed_at
        if completado and completado.tzinfo is None:
            completado = completado.replace(tzinfo=tz.utc)
        dias = (now - completado).days if completado else None
        leads.append({
            "student_id": u.id,
            "name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "level_code": lvl.code if lvl else None,
            "days_ago": dias,
            "completed_at": completado.isoformat() if completado else None,
        })

    # ---------- 2. Inscritos que dejaron de venir ----------
    corte = now - timedelta(days=dias_inactivo)
    activos = (await db.execute(
        select(Enrollment, User, Student)
        .join(User, Enrollment.student_id == User.id)
        .join(Student, Student.user_id == User.id)
        .where(Enrollment.is_active.is_(True))
    )).all()

    vistos = set()
    apagados = []
    for enr, u, s in activos:
        if u.id in vistos:
            continue
        vistos.add(u.id)
        if s.is_paused:
            continue  # está en pausa a propósito, no es un abandono
        ultima = (await db.execute(
            select(func.max(ClassSession.starts_at_utc))
            .select_from(SessionAttendance)
            .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
            .where(
                SessionAttendance.student_id == u.id,
                SessionAttendance.state == AttendanceState.present,
            )
        )).scalar()
        if ultima and ultima.tzinfo is None:
            ultima = ultima.replace(tzinfo=tz.utc)
        if ultima and ultima > corte:
            continue  # vino hace poco, todo bien
        apagados.append({
            "student_id": u.id,
            "name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "last_class": ultima.isoformat() if ultima else None,
            "days_ago": (now - ultima).days if ultima else None,
            "never_attended": ultima is None,
        })
    apagados.sort(key=lambda x: (x["days_ago"] is None, -(x["days_ago"] or 0)))

    return {
        "leads": leads,
        "inactive": apagados,
        "dias_inactivo": dias_inactivo,
        "totals": {"leads": len(leads), "inactive": len(apagados)},
    }


@router.post("/reactivation/{student_id}/contacted")
async def mark_contacted(
    student_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Deja constancia de que ya se le escribió, para no repetir el mensaje.

    Se guarda en el registro de acciones, así queda el historial de a quién
    se contactó y cuándo.
    """
    u = await db.get(User, student_id)
    if not u:
        raise HTTPException(404, "Estudiante no encontrado")
    via = (body.get("via") or "whatsapp").strip()
    await log_action(db, admin.user_id, "contacted_student", "reactivation",
                     target_id=student_id, details=f"via={via}")
    await db.commit()
    return {"ok": True}


# ============================================================================
# V3.9.30 — ALERTAS QUE SE PUEDEN RESOLVER
# ============================================================================

@router.get("/alerts")
async def get_alerts(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Lo que requiere atención HOY, agrupado y con salida.

    Reglas para no generar ruido:
    - Una falta suelta NO es alerta. Dos seguidas, sí.
    - Si el estudiante avisó con tiempo, se marca aparte (no es urgente).
    - Si el estudiante está en pausa a propósito, no aparece.
    - Lo ya resuelto o descartado desaparece; lo pospuesto vuelve después.
    - Si el estudiante vuelve a asistir, la alerta se resuelve SOLA.
    """
    now = datetime.now(tz.utc)

    # Lo que el admin ya atendió
    acciones = (await db.execute(select(AlertAction))).scalars().all()
    ocultas = set()
    for a in acciones:
        if a.action in ("resolved", "dismissed"):
            ocultas.add(a.alert_key)
        elif a.action == "snoozed" and a.snooze_until:
            hasta = a.snooze_until
            if hasta.tzinfo is None:
                hasta = hasta.replace(tzinfo=tz.utc)
            if hasta > now:
                ocultas.add(a.alert_key)

    grupos = []

    # ---------- Estudiantes con problema de asistencia ----------
    # V3.9.31: se amplió la ventana a 60 días y se detectan DOS señales,
    # porque con 21 días y solo faltas seguidas no se detectaba casi nada:
    #   a) 2 o más ausencias SEGUIDAS (dejó de venir)
    #   b) 3 o más ausencias en sus últimas 10 clases (viene salteado)
    desde = now - timedelta(days=60)
    filas = (await db.execute(
        select(SessionAttendance, ClassSession, User)
        .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
        .join(User, SessionAttendance.student_id == User.id)
        .where(ClassSession.starts_at_utc >= desde)
        .order_by(ClassSession.starts_at_utc.desc())
    )).all()

    porestudiante: dict[str, list] = {}
    for att, ses, u in filas:
        porestudiante.setdefault(u.id, []).append((att, ses, u))

    faltones = []
    for uid, registros in porestudiante.items():
        u = registros[0][2]
        st = await db.get(Student, uid)
        if st and st.is_paused:
            continue  # en pausa a propósito

        # Las más recientes primero (la consulta viene ordenada así)
        ultimas = registros[:10]

        # Señal A: ausencias seguidas desde la clase más reciente.
        # Si volvió a asistir, la racha se corta y la alerta muere sola.
        seguidas = 0
        for att, _ses, _u in ultimas:
            if att.state == AttendanceState.absent:
                seguidas += 1
            elif att.state == AttendanceState.present:
                break

        # Señal B: cuántas faltó de sus últimas 10 clases
        total_faltas = sum(
            1 for att, _s, _u in ultimas if att.state == AttendanceState.absent
        )

        if seguidas < 2 and total_faltas < 3:
            continue
        # ¿Avisó que faltaría?
        avisos = (await db.execute(
            select(func.count()).select_from(AbsenceNotice).where(
                AbsenceNotice.student_id == uid,
                AbsenceNotice.created_at >= desde,
            )
        )).scalar() or 0
        aviso_previo = avisos > 0
        clave = f"riesgo:{uid}"
        if clave in ocultas:
            continue
        if seguidas >= 2:
            detalle = f"{seguidas} ausencias seguidas"
        else:
            detalle = f"{total_faltas} faltas en sus últimas {len(ultimas)} clases"
        if aviso_previo:
            detalle += " · avisó con tiempo"

        faltones.append({
            "key": clave,
            "student_id": uid,
            "name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            "misses": max(seguidas, total_faltas),
            "consecutive": seguidas,
            "total_absences": total_faltas,
            "notified": aviso_previo,
            "detail": detalle,
            "urgency": "low" if aviso_previo else "high",
        })

    faltones.sort(key=lambda x: (x["urgency"] != "high", -x["misses"]))
    if faltones:
        grupos.append({
            "type": "attendance_risk",
            "title": (
                "1 estudiante con ausencias seguidas" if len(faltones) == 1
                else f"{len(faltones)} estudiantes con ausencias seguidas"
            ),
            "icon": "user-off",
            "tone": "warning",
            "items": faltones,
        })

    # ---------- Tareas esperando calificación ----------
    pendientes = (await db.execute(
        select(AssignmentSubmission, Assignment)
        .join(Assignment, AssignmentSubmission.assignment_id == Assignment.id)
        .where(
            AssignmentSubmission.submitted_at.is_not(None),
            AssignmentSubmission.score.is_(None),
        )
        .order_by(AssignmentSubmission.submitted_at)
    )).all()
    if pendientes:
        primera = pendientes[0][0].submitted_at
        if primera and primera.tzinfo is None:
            primera = primera.replace(tzinfo=tz.utc)
        dias = (now - primera).days if primera else 0
        clave = "sin_calificar"
        if clave not in ocultas:
            grupos.append({
                "type": "ungraded",
                "title": (
                    "1 tarea esperando calificación" if len(pendientes) == 1
                    else f"{len(pendientes)} tareas esperando calificación"
                ),
                "subtitle": f"La más antigua lleva {dias} día{'s' if dias != 1 else ''}",
                "icon": "file-check",
                "tone": "accent",
                "key": clave,
                "count": len(pendientes),
                "items": [],
            })

    # ---------- Clases sin asistencia registrada (no se cobran) ----------
    hace7 = now - timedelta(days=7)
    sesiones = (await db.execute(
        select(ClassSession).where(
            ClassSession.ends_at_utc <= now,
            ClassSession.ends_at_utc >= hace7,
            ClassSession.status != SessionStatus.cancelled,
        )
    )).scalars().all()
    ids = [s.id for s in sesiones]
    con_asistencia = set()
    if ids:
        rows = (await db.execute(
            select(SessionAttendance.session_id).where(
                SessionAttendance.session_id.in_(ids),
                SessionAttendance.state.is_not(None),
            ).distinct()
        )).all()
        con_asistencia = {x for (x,) in rows}
    sin_lista = [s for s in sesiones if s.id not in con_asistencia]
    if sin_lista and "sin_asistencia" not in ocultas:
        grupos.append({
            "type": "no_attendance",
            "title": (
                "1 clase sin asistencia registrada" if len(sin_lista) == 1
                else f"{len(sin_lista)} clases sin asistencia registrada"
            ),
            "subtitle": "Sin la lista, esas clases no cuentan para el pago del profesor",
            "icon": "clipboard-x",
            "tone": "warning",
            "key": "sin_asistencia",
            "count": len(sin_lista),
            "items": [],
        })

    # Cuántas se resolvieron esta semana (motiva seguir usándolo)
    semana = now - timedelta(days=7)
    resueltas = (await db.execute(
        select(func.count()).select_from(AlertAction).where(
            AlertAction.created_at >= semana,
            AlertAction.action.in_(["resolved", "dismissed"]),
        )
    )).scalar() or 0

    total = sum(len(g.get("items") or []) or 1 for g in grupos)
    return {"groups": grupos, "pending": total, "resolved_this_week": resueltas}


@router.post("/alerts/action")
async def act_on_alert(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Resolver, descartar o posponer una alerta.

    - resolved:  ya lo atendí (le escribí, lo hablé)
    - dismissed: no aplica, no quiero verlo
    - snoozed:   recordámelo en X días
    """
    clave = (body.get("key") or "").strip()
    accion = (body.get("action") or "").strip()
    if not clave:
        raise HTTPException(400, "Falta indicar la alerta")
    if accion not in ("resolved", "dismissed", "snoozed"):
        raise HTTPException(400, "Acción no válida")

    hasta = None
    if accion == "snoozed":
        dias = body.get("days", 3)
        try:
            dias = max(1, min(30, int(dias)))
        except (TypeError, ValueError):
            dias = 3
        hasta = datetime.now(tz.utc) + timedelta(days=dias)

    # Si ya había una acción para esta alerta, se reemplaza
    previa = (await db.execute(
        select(AlertAction).where(AlertAction.alert_key == clave)
    )).scalars().all()
    for p in previa:
        await db.delete(p)

    db.add(AlertAction(
        alert_key=clave, action=accion,
        note=(body.get("note") or "")[:250] or None,
        by_user_id=admin.user_id, snooze_until=hasta,
    ))
    await log_action(db, admin.user_id, f"alert_{accion}", "alerts", target_id=clave)
    await db.commit()
    return {"ok": True, "action": accion}


async def _aplicar_cambio_profesor_serie(
    db: AsyncSession,
    series: ClassSeries,
    nuevo_id: str,
    actor_id: str,
    desde: datetime | None = None,
    confirm_overlap: bool = False,
    commit: bool = False,
):
    """V3.9.62 — El cambio permanente de profesor de una serie, en un solo sitio.

    Antes esta lógica vivía dentro del endpoint `change-teacher`. Al permitir
    también cambiar el profesor desde "Editar serie" habrían quedado DOS
    copias de la misma regla, y con el tiempo se habrían separado: una
    avisando a quien toca y la otra no, una detectando choques y la otra no.

    Es la MISMA función para los dos caminos. Si mañana cambia la política de
    sustituciones, cambia una vez.

    No hace `commit` por defecto: quien la llama decide cuándo cerrar la
    transacción, para que mover el horario y cambiar el profesor sean UN solo
    cambio y no dos a medias.
    """
    from app.services.audience import destinatarios_de_serie
    from app.services.push_service import notify_user

    if not nuevo_id:
        raise HTTPException(400, "Indica el profesor nuevo")
    if nuevo_id == series.teacher_id:
        raise HTTPException(400, "Ese ya es el profesor de la serie")

    nuevo = await db.get(User, nuevo_id)
    if not nuevo or nuevo.role != UserRole.teacher:
        raise HTTPException(404, "Profesor no encontrado")

    corte = desde or datetime.now(tz.utc)

    futuras = (await db.execute(
        select(ClassSession).where(
            ClassSession.series_id == series.id,
            ClassSession.starts_at_utc >= corte,
            ClassSession.status != SessionStatus.cancelled,
        ).order_by(ClassSession.starts_at_utc)
    )).scalars().all()

    if not futuras:
        raise HTTPException(400, "Esta serie no tiene clases futuras que cambiar")

    # ¿El profesor nuevo ya está ocupado en alguna de esas horas?
    # Se avisa ANTES de tocar nada. Se puede confirmar igual (a veces hay
    # razones), pero a propósito, no por accidente.
    choques = []
    for s in futuras:
        ini, fin = s.starts_at_utc, s.ends_at_utc
        if not ini or not fin:
            continue
        ocupado = (await db.execute(
            select(ClassSession).where(
                ClassSession.teacher_id == nuevo_id,
                ClassSession.id != s.id,
                ClassSession.status != SessionStatus.cancelled,
                ClassSession.starts_at_utc < fin,
                ClassSession.ends_at_utc > ini,
            ).limit(1)
        )).scalar_one_or_none()
        if ocupado:
            local = ini.replace(tzinfo=tz.utc) if ini.tzinfo is None else ini
            choques.append({
                "session_id": s.id,
                "starts_at_utc": local.isoformat(),
                "conflict_title": ocupado.title,
            })

    if choques and not confirm_overlap:
        raise HTTPException(409, {
            "necesita_confirmacion": True,
            "conflicts": choques[:5],
            "mensaje": (
                f"{nuevo.full_name} ya tiene clase a esa hora en "
                f"{len(choques)} de las {len(futuras)} fechas."
            ),
        })

    anterior = await db.get(User, series.teacher_id) if series.teacher_id else None
    anterior_id = series.teacher_id
    for s in futuras:
        s.teacher_id = nuevo_id
    series.teacher_id = nuevo_id

    # Avisar a los tres lados
    try:
        cuerpo = f"'{series.name}': ahora la imparte {nuevo.full_name}."

        db.add(Notification(
            user_id=nuevo_id, type=NotificationType.info,
            title="📅 Te asignaron una serie de clases",
            body=f"'{series.name}' — {len(futuras)} clases.",
            link="/dashboard/teacher",
        ))
        await notify_user(db, nuevo_id, "📅 Te asignaron una serie de clases",
                          f"'{series.name}' — {len(futuras)} clases.",
                          "/dashboard/teacher", f"serie:{series.id}")

        if anterior:
            db.add(Notification(
                user_id=anterior.id, type=NotificationType.info,
                title="📅 Ya no impartes esta serie",
                body=f"'{series.name}' pasó a {nuevo.full_name}.",
                link="/dashboard/teacher",
            ))

        # V3.9.62 — Solo el GRUPO real, vía el servicio central de audiencia.
        # Antes se avisaba por course_id + level_id: B1 Noche recibía el
        # cambio de profesor de B1 Mañana, que no era suyo.
        for uid in await destinatarios_de_serie(db, series.id):
            db.add(Notification(
                user_id=uid, type=NotificationType.info,
                title="👨‍🏫 Cambio de profesor", body=cuerpo,
                link="/dashboard/student",
            ))
            await notify_user(db, uid, "👨‍🏫 Cambio de profesor", cuerpo,
                              "/dashboard/student", f"profe:{series.id}")
    except Exception:
        pass

    await log_action(db, actor_id, "change_series_teacher", "sessions",
                     target_id=series.id,
                     details=f"{anterior_id} → {nuevo_id}, {len(futuras)} clases")
    if commit:
        await db.commit()

    return {
        "ok": True,
        "changed": len(futuras),
        "teacher": nuevo.full_name,
        "had_conflicts": len(choques),
    }


# ============================================================================
# V3.9.32 — CAMBIAR EL PROFESOR DE UNA SERIE (cuando uno no está disponible)
# ============================================================================

@router.post("/class-series/{series_id}/change-teacher")
async def change_series_teacher(
    series_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Pasa las clases futuras de una serie a otro profesor.

    ANTES había que editar clase por clase. Si un profesor se enfermaba una
    semana, eran diez ediciones a mano.

    AVISA DE CHOQUES: si el profesor nuevo ya tiene clase a esa hora, lo dice
    ANTES de hacer el cambio. Se puede confirmar igual (a veces hay razones),
    pero a propósito, no por accidente.

    V3.9.62: la lógica vive en `_aplicar_cambio_profesor_serie`, compartida
    con "Editar serie". Este endpoint es la puerta HTTP, nada más.
    """
    series = await db.get(ClassSeries, series_id)
    if not series:
        raise HTTPException(404, "Serie no encontrada")

    desde = None
    if body.get("from_date"):
        try:
            desde = datetime.fromisoformat(body["from_date"].replace("Z", "+00:00"))
        except ValueError:
            desde = None

    return await _aplicar_cambio_profesor_serie(
        db, series,
        nuevo_id=(body.get("teacher_id") or "").strip(),
        actor_id=admin.user_id,
        desde=desde,
        confirm_overlap=bool(body.get("confirm_overlap")),
        commit=True,
    )


@router.get("/teachers/{teacher_id}/availability")
async def teacher_availability(
    teacher_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    days: int = 14,
):
    """Qué tiene ocupado un profesor en los próximos días.

    Sirve para decidir a quién pasarle una clase sin cruzarle el horario.
    """
    now = datetime.now(tz.utc)
    hasta = now + timedelta(days=max(1, min(60, days)))
    filas = (await db.execute(
        select(ClassSession).where(
            ClassSession.teacher_id == teacher_id,
            ClassSession.starts_at_utc >= now,
            ClassSession.starts_at_utc <= hasta,
            ClassSession.status != SessionStatus.cancelled,
        ).order_by(ClassSession.starts_at_utc)
    )).scalars().all()
    return {"items": [{
        "id": s.id, "title": s.title,
        "starts_at_utc": s.starts_at_utc.isoformat() if s.starts_at_utc else None,
        "ends_at_utc": s.ends_at_utc.isoformat() if s.ends_at_utc else None,
    } for s in filas], "count": len(filas)}


# ============================================================================
# V3.9.33 — GRUPOS: a qué horario pertenece cada estudiante
# ============================================================================

@router.get("/groups")
async def list_groups(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Los grupos (series de clases) con cuánta gente tiene cada uno.

    Con esto sabes de un vistazo: el B1 de la mañana tiene 4 de 6 cupos, el
    de la noche está lleno. Antes no había forma de saberlo.
    """
    series = (await db.execute(
        select(ClassSeries).order_by(ClassSeries.created_at.desc())
    )).scalars().all()

    ahora = datetime.now(tz.utc)
    out = []
    for s in series:
        # V3.9.35 — Un grupo sin clases futuras ya no sirve para asignar a
        # nadie: o terminó, o le borraron las clases. No debe ofrecerse.
        futuras = (await db.execute(
            select(func.count()).select_from(ClassSession).where(
                ClassSession.series_id == s.id,
                ClassSession.starts_at_utc >= ahora,
                ClassSession.status != SessionStatus.cancelled,
            )
        )).scalar() or 0
        if futuras == 0:
            continue

        inscritos = (await db.execute(
            select(func.count()).select_from(Enrollment).where(
                Enrollment.series_id == s.id,
                Enrollment.is_active.is_(True),
            )
        )).scalar() or 0
        curso = await db.get(Course, s.course_id) if s.course_id else None
        nivel = await db.get(Level, s.level_id) if s.level_id else None
        profe = await db.get(User, s.teacher_id) if s.teacher_id else None
        cupo = s.capacity or 6
        out.append({
            "id": s.id,
            "name": s.name,
            "course_name": curso.name if curso else None,
            "level_id": s.level_id,
            "level_code": nivel.code if nivel else None,
            "teacher_name": profe.full_name if profe else None,
            "days_of_week": s.days_of_week,
            "start_time_hhmm": s.start_time_hhmm,
            "modality": s.modality.value if s.modality else None,
            "students": inscritos,
            "capacity": cupo,
            "is_full": inscritos >= cupo,
            "spots_left": max(0, cupo - inscritos),
            "upcoming_classes": futuras,  # V3.9.35
        })
    return {"items": out}


@router.get("/groups/{series_id}/students")
async def group_students(
    series_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Quiénes están en este grupo."""
    rows = (await db.execute(
        select(Enrollment, User)
        .join(User, Enrollment.student_id == User.id)
        .where(Enrollment.series_id == series_id, Enrollment.is_active.is_(True))
        .order_by(User.full_name)
    )).all()
    return {"items": [{
        "enrollment_id": e.id, "student_id": u.id,
        "name": u.full_name, "email": u.email,
    } for e, u in rows]}


@router.post("/enrollments/{enrollment_id}/assign-group")
async def assign_to_group(
    enrollment_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Pone (o cambia) a un estudiante en un grupo.

    Al asignarlo, deja de ver todas las clases de su nivel y solo ve las de
    su grupo. Para sacarlo del grupo, se manda series_id vacío.

    AVISA SI ESTÁ LLENO: se puede confirmar igual, pero a propósito.
    """
    enr = await db.get(Enrollment, enrollment_id)
    if not enr:
        raise HTTPException(404, "Inscripción no encontrada")

    series_id = (body.get("series_id") or "").strip() or None

    # Sacarlo del grupo
    if not series_id:
        enr.series_id = None
        await log_action(db, admin.user_id, "unassign_group", "enrollments",
                         target_id=enrollment_id)
        await db.commit()
        return {"ok": True, "assigned": False}

    serie = await db.get(ClassSeries, series_id)
    if not serie:
        raise HTTPException(404, "Grupo no encontrado")

    # Debe ser del mismo nivel, si no el estudiante vería clases que no le tocan
    if serie.level_id != enr.level_id:
        raise HTTPException(
            400,
            "Ese grupo es de otro nivel. Cambia primero el nivel del estudiante.",
        )

    if enr.series_id != series_id:
        actuales = (await db.execute(
            select(func.count()).select_from(Enrollment).where(
                Enrollment.series_id == series_id,
                Enrollment.is_active.is_(True),
            )
        )).scalar() or 0
        cupo = serie.capacity or 6
        if actuales >= cupo and not body.get("confirm_full"):
            raise HTTPException(409, {
                "necesita_confirmacion": True,
                "mensaje": (
                    f"El grupo '{serie.name}' ya tiene {actuales} de {cupo} cupos. "
                    "¿Lo agregas igual?"
                ),
            })

    enr.series_id = series_id
    if serie.teacher_id:
        enr.teacher_id = serie.teacher_id  # el profe del grupo pasa a ser el suyo

    # Avisarle su horario
    try:
        from app.services.push_service import notify_user
        dias_map = {"mon": "Lun", "tue": "Mar", "wed": "Mié", "thu": "Jue",
                    "fri": "Vie", "sat": "Sáb", "sun": "Dom"}
        dias = ", ".join(
            dias_map.get(d.strip(), d)
            for d in (serie.days_of_week or "").split(",") if d.strip()
        )
        try:
            _h, _m = (serie.start_time_hhmm or "00:00").split(":")
            hora = datetime(2000, 1, 1, int(_h), int(_m)).strftime("%I:%M %p").lstrip("0")
        except Exception:
            hora = serie.start_time_hhmm or ""
        cuerpo = f"Tu grupo es '{serie.name}': {dias} a las {hora}."
        db.add(Notification(
            user_id=enr.student_id, type=NotificationType.info,
            title="👥 Ya tienes tu grupo y horario", body=cuerpo,
            link="/dashboard/student",
        ))
        await notify_user(db, enr.student_id, "👥 Ya tienes tu grupo y horario",
                          cuerpo, "/dashboard/student", f"grupo:{series_id}")
    except Exception:
        pass

    await log_action(db, admin.user_id, "assign_group", "enrollments",
                     target_id=enrollment_id, details=series_id)
    await db.commit()
    return {"ok": True, "assigned": True, "group": serie.name}


@router.get("/feature-keys")
async def list_feature_keys(
    admin: Annotated[CurrentUser, Depends(require_admin)],
):
    """Las funciones que un plan puede desbloquear, con nombre en español.

    El panel muestra esta lista como casillas, para que no haya que escribir
    la llave a mano (que era donde estaba el error).
    """
    catalogo = [
        {"key": "grupal_classes", "label": "Clases grupales", "group": "Clases"},
        {"key": "private_classes", "label": "Clases privadas 1 a 1", "group": "Clases"},
        {"key": "assignments", "label": "Tareas con corrección", "group": "Aprendizaje"},
        {"key": "quizzes", "label": "Quizzes evaluativos", "group": "Aprendizaje"},
        {"key": "course_route", "label": "Ruta de curso personalizada", "group": "Aprendizaje"},
        {"key": "placement_test", "label": "Test de nivel", "group": "Aprendizaje"},
        {"key": "library_basic", "label": "Biblioteca básica", "group": "Materiales"},
        {"key": "library_full", "label": "Biblioteca completa", "group": "Materiales"},
        {"key": "materials_premium", "label": "Materiales descargables", "group": "Materiales"},
        {"key": "certificates", "label": "Certificado al terminar el nivel", "group": "Extras"},
        {"key": "events_view", "label": "Ver los eventos", "group": "Extras"},
        {"key": "events_free", "label": "Entrar gratis a los eventos", "group": "Extras"},
        {"key": "priority_support", "label": "Soporte prioritario", "group": "Extras"},
    ]
    # Lo que se recomienda incluir en TODOS los planes: si el profesor lo
    # asigna en clase, todos deben poder abrirlo. Si la mitad del grupo no
    # puede hacer el quiz que puso el profe, es un desastre en vivo.
    basicas = {"grupal_classes", "assignments", "quizzes", "certificates",
               "placement_test", "course_route", "events_view"}
    for f in catalogo:
        f["recommended_for_all"] = f["key"] in basicas
    return {"items": catalogo}


@router.get("/students/{student_id}/what-they-see")
async def what_student_sees(
    student_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.35 — Por qué este estudiante ve las clases que ve.

    Sirve para diagnosticar sin adivinar: te dice si está filtrando por grupo,
    por profesor o por nivel, y qué clases le van a aparecer. Si algo no
    cuadra, aquí se ve el motivo.
    """
    u = await db.get(User, student_id)
    if not u:
        raise HTTPException(404, "Estudiante no encontrado")

    ahora = datetime.now(tz.utc)
    filas = (await db.execute(
        select(Enrollment, Course, Level)
        .join(Course, Enrollment.course_id == Course.id)
        .join(Level, Enrollment.level_id == Level.id)
        .where(Enrollment.student_id == student_id, Enrollment.is_active.is_(True))
    )).all()

    inscripciones = []
    for e, curso, nivel in filas:
        grupo = await db.get(ClassSeries, e.series_id) if getattr(e, "series_id", None) else None
        profe = await db.get(User, e.teacher_id) if getattr(e, "teacher_id", None) else None

        if grupo:
            criterio = "grupo"
            explicacion = f"Solo ve las clases del grupo '{grupo.name}'."
            cond = ClassSession.series_id == grupo.id
        elif profe:
            criterio = "profesor"
            explicacion = (
                f"Solo ve las clases de {profe.full_name} en {nivel.code}. "
                "Si ese profesor no tiene clases proyectadas, no verá ninguna."
            )
            cond = (ClassSession.teacher_id == profe.id) & (ClassSession.level_id == nivel.id)
        else:
            criterio = "nivel"
            explicacion = (
                f"⚠️ Ve TODAS las clases de {nivel.code}, de cualquier profesor. "
                "Asígnale un profesor o un grupo para que solo vea las suyas."
            )
            cond = ClassSession.level_id == nivel.id

        proximas = (await db.execute(
            select(ClassSession).where(
                cond,
                ClassSession.student_id.is_(None),
                ClassSession.is_open_event.is_(False),
                ClassSession.starts_at_utc >= ahora,
                ClassSession.status == SessionStatus.scheduled,
            ).order_by(ClassSession.starts_at_utc).limit(5)
        )).scalars().all()

        clases = []
        for s in proximas:
            pr = await db.get(User, s.teacher_id) if s.teacher_id else None
            clases.append({
                "id": s.id, "title": s.title,
                "starts_at_utc": s.starts_at_utc.isoformat() if s.starts_at_utc else None,
                "teacher_name": pr.full_name if pr else None,
            })

        inscripciones.append({
            "enrollment_id": e.id,
            "course_name": curso.name,
            "level_code": nivel.code,
            "group_name": grupo.name if grupo else None,
            "teacher_name": profe.full_name if profe else None,
            "criterio": criterio,
            "explicacion": explicacion,
            "upcoming": clases,
            "upcoming_count": len(clases),
        })

    # Sus clases privadas (esas siempre las ve, sin importar el filtro)
    privadas = (await db.execute(
        select(ClassSession).where(
            ClassSession.student_id == student_id,
            ClassSession.starts_at_utc >= ahora,
            ClassSession.status == SessionStatus.scheduled,
        ).order_by(ClassSession.starts_at_utc).limit(5)
    )).scalars().all()

    return {
        "student": {"id": u.id, "name": u.full_name, "email": u.email},
        "enrollments": inscripciones,
        "private_classes": [{
            "id": s.id, "title": s.title,
            "starts_at_utc": s.starts_at_utc.isoformat() if s.starts_at_utc else None,
        } for s in privadas],
    }


# ============================================================================
# V3.9.36 — REPOSICIONES Y ESTUDIANTES SIN HORARIO
# ============================================================================

@router.get("/makeup-requests")
async def list_makeups(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    status: str = "pending",
):
    """Las solicitudes de reposición de clases perdidas."""
    from app.models import MakeupRequest

    # V3.9.37: la clase original es opcional (el admin puede reponer algo que
    # nunca se proyectó), así que el join tiene que ser opcional.
    q = (
        select(MakeupRequest, User, ClassSession)
        .join(User, MakeupRequest.student_id == User.id)
        .outerjoin(ClassSession, MakeupRequest.original_session_id == ClassSession.id)
        .order_by(MakeupRequest.created_at.desc())
    )
    if status and status != "all":
        q = q.where(MakeupRequest.status == status)

    filas = (await db.execute(q)).all()
    out = []
    for r, u, orig in filas:
        profe = await db.get(User, orig.teacher_id) if (orig and orig.teacher_id) else None
        nueva = await db.get(ClassSession, r.makeup_session_id) if r.makeup_session_id else None
        out.append({
            "id": r.id,
            "student_id": u.id, "student_name": u.full_name, "student_email": u.email,
            "original_session_id": orig.id if orig else None,
            "original_title": orig.title if orig else "Sin clase original",
            "original_date": (orig.starts_at_utc.isoformat()
                              if (orig and orig.starts_at_utc) else None),
            "course_id": orig.course_id if orig else None,
            "level_id": orig.level_id if orig else None,
            "teacher_id": orig.teacher_id if orig else None,
            "teacher_name": profe.full_name if profe else None,
            # V3.9.37 — quién la originó y si suma al temario
            "created_by": getattr(r, "created_by", "student"),
            "created_by_label": ("La agendó el instituto"
                                 if getattr(r, "created_by", "student") == "admin"
                                 else "La pidió el estudiante"),
            "counts_for_progress": bool(getattr(r, "counts_for_progress", False)),
            "status": r.status, "reason": r.reason,
            "missed_by": r.missed_by,
            "missed_by_label": "El profesor faltó" if r.missed_by == "teacher" else "El estudiante faltó",
            "preferred_date": r.preferred_date,
            "makeup_date": nueva.starts_at_utc.isoformat() if nueva and nueva.starts_at_utc else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return {"items": out, "count": len(out)}


@router.post("/makeup-requests/{req_id}/schedule")
async def schedule_makeup(
    req_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Aprueba la reposición y agenda la clase de recuperación.

    IMPORTANTE: se crea una clase SUELTA para ese estudiante, sin tocar la
    serie. La recurrencia sigue exactamente igual; solo se agrega una clase
    extra en la fecha acordada.
    """
    from app.models import MakeupRequest

    r = await db.get(MakeupRequest, req_id)
    if not r:
        raise HTTPException(404, "Solicitud no encontrada")
    if r.status == "scheduled":
        raise HTTPException(400, "Esa reposición ya tiene fecha")

    starts = body.get("starts_at_utc")
    if not starts:
        raise HTTPException(400, "Indica la fecha y hora de la reposición")
    try:
        ini = datetime.fromisoformat(starts.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "La fecha no tiene un formato válido")
    if ini <= datetime.now(tz.utc):
        raise HTTPException(400, "La reposición debe ser en el futuro")

    dur = body.get("duration_min", 60)
    try:
        dur = max(15, min(240, int(dur)))
    except (TypeError, ValueError):
        dur = 60

    orig = await db.get(ClassSession, r.original_session_id)
    if not orig:
        raise HTTPException(404, "La clase original ya no existe")

    profe_id = body.get("teacher_id") or orig.teacher_id

    # Clase suelta para ese estudiante: NO toca la serie
    nueva = ClassSession(
        title=f"🔄 Reposición — {orig.title}",
        description=f"Clase de recuperación. Original: {orig.starts_at_utc.strftime('%d/%m') if orig.starts_at_utc else ''}",
        course_id=orig.course_id, level_id=orig.level_id,
        teacher_id=profe_id,
        student_id=r.student_id,   # suya, no del grupo
        starts_at_utc=ini,
        ends_at_utc=ini + timedelta(minutes=dur),
        modality=orig.modality,
        meeting_url=body.get("meeting_url") or orig.meeting_url,
        video_provider=("dorismon" if body.get("video_provider") == "dorismon"
                        else getattr(orig, "video_provider", "meet")),
        capacity=1,
        status=SessionStatus.scheduled,
    )
    db.add(nueva)
    await db.flush()

    # V3.9.36 — Si el que faltó fue el PROFESOR, la ausencia no debe contar
    # contra el estudiante ni afectar su porcentaje de asistencia.
    if r.missed_by == "teacher":
        try:
            att = (await db.execute(
                select(SessionAttendance).where(
                    SessionAttendance.session_id == r.original_session_id,
                    SessionAttendance.student_id == r.student_id,
                )
            )).scalar_one_or_none()
            if att and att.state == AttendanceState.absent:
                att.state = AttendanceState.excused
                att.notes = ((att.notes or "") + " · Falta del profesor, se repuso").strip()
        except Exception:
            pass

    r.status = "scheduled"
    r.makeup_session_id = nueva.id
    r.admin_note = (body.get("note") or "")[:250] or None
    r.resolved_at = datetime.now(tz.utc)

    # Avisar al estudiante y al profesor
    try:
        from app.services.push_service import notify_user
        from zoneinfo import ZoneInfo as _ZI
        local = ini.astimezone(_ZI("America/Santo_Domingo"))
        cuando = local.strftime("%d/%m a las %I:%M %p").lstrip("0")
        cuerpo = f"Tu clase de recuperación quedó para el {cuando}."
        db.add(Notification(
            user_id=r.student_id, type=NotificationType.info,
            title="🔄 Ya tienes fecha de reposición", body=cuerpo,
            link="/dashboard/student",
        ))
        await notify_user(db, r.student_id, "🔄 Ya tienes fecha de reposición",
                          cuerpo, "/dashboard/student", f"makeup:{r.id}")
        if profe_id:
            db.add(Notification(
                user_id=profe_id, type=NotificationType.info,
                title="🔄 Clase de reposición asignada",
                body=f"Reposición el {cuando}.", link="/dashboard/teacher",
            ))
    except Exception:
        pass

    await log_action(db, admin.user_id, "schedule_makeup", "sessions", target_id=req_id)
    await db.commit()
    return {"ok": True, "session_id": nueva.id, "status": "scheduled"}


@router.post("/makeup-requests/{req_id}/reject")
async def reject_makeup(
    req_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """No aprobar una reposición, con el motivo (el estudiante lo verá)."""
    from app.models import MakeupRequest

    r = await db.get(MakeupRequest, req_id)
    if not r:
        raise HTTPException(404, "Solicitud no encontrada")
    motivo = (body.get("note") or "").strip()
    if not motivo:
        raise HTTPException(400, "Explica por qué, para que el estudiante entienda")

    r.status = "rejected"
    r.admin_note = motivo[:250]
    r.resolved_at = datetime.now(tz.utc)

    db.add(Notification(
        user_id=r.student_id, type=NotificationType.info,
        title="Sobre tu solicitud de reposición",
        body=motivo[:200], link="/dashboard/student",
    ))
    await log_action(db, admin.user_id, "reject_makeup", "sessions", target_id=req_id)
    await db.commit()
    return {"ok": True}


@router.get("/students-without-schedule")
async def students_without_schedule(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.36 — Estudiantes que NO están viendo ninguna clase.

    Con la regla estricta, quien no tiene grupo ni clases propias no ve nada.
    Eso es correcto, pero hay que enterarse: este listado te avisa para que
    nadie quede olvidado sin horario.
    """
    ahora = datetime.now(tz.utc)
    filas = (await db.execute(
        select(Enrollment, User, Level)
        .join(User, Enrollment.student_id == User.id)
        .join(Level, Enrollment.level_id == Level.id)
        .where(Enrollment.is_active.is_(True))
    )).all()

    sin_nada = []
    vistos = set()
    for e, u, nivel in filas:
        if u.id in vistos:
            continue
        vistos.add(u.id)

        st = await db.get(Student, u.id)
        if st and st.is_paused:
            continue  # en pausa a propósito

        # ¿Tiene clases de su grupo?
        clases = 0
        if getattr(e, "series_id", None):
            clases = (await db.execute(
                select(func.count()).select_from(ClassSession).where(
                    ClassSession.series_id == e.series_id,
                    ClassSession.starts_at_utc >= ahora,
                    ClassSession.status == SessionStatus.scheduled,
                )
            )).scalar() or 0

        # ¿Tiene clases propias (privadas o sueltas)?
        propias = (await db.execute(
            select(func.count()).select_from(ClassSession).where(
                ClassSession.student_id == u.id,
                ClassSession.starts_at_utc >= ahora,
                ClassSession.status == SessionStatus.scheduled,
            )
        )).scalar() or 0

        if clases + propias > 0:
            continue

        grupo = await db.get(ClassSeries, e.series_id) if getattr(e, "series_id", None) else None
        profe = await db.get(User, e.teacher_id) if getattr(e, "teacher_id", None) else None
        sin_nada.append({
            "student_id": u.id, "name": u.full_name, "email": u.email, "phone": u.phone,
            "enrollment_id": e.id,
            "level_id": nivel.id, "level_code": nivel.code,
            "group_name": grupo.name if grupo else None,
            "teacher_name": profe.full_name if profe else None,
            "motivo": (
                "Su grupo no tiene clases futuras" if grupo
                else "No está en ningún grupo ni tiene clases propias"
            ),
        })

    return {"items": sin_nada, "count": len(sin_nada)}


@router.post("/makeup-requests/direct", status_code=201)
async def create_makeup_direct(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.37 — El admin agenda una reposición directamente.

    PARA QUÉ: no siempre el estudiante la pide. A veces le debes una clase,
    hubo un problema, o simplemente quieres dársela. Antes había que esperar
    a que la solicitara.

    La clase original es OPCIONAL: se puede reponer algo que ni siquiera se
    llegó a proyectar.

    Como siempre: se crea una clase SUELTA. La serie no se toca.
    """
    from app.models import MakeupRequest

    student_id = (body.get("student_id") or "").strip()
    if not student_id:
        raise HTTPException(400, "Indica a qué estudiante")

    u = await db.get(User, student_id)
    if not u:
        raise HTTPException(404, "Estudiante no encontrado")

    starts = body.get("starts_at_utc")
    if not starts:
        raise HTTPException(400, "Indica la fecha y hora de la clase")
    try:
        ini = datetime.fromisoformat(starts.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, "La fecha no tiene un formato válido")
    if ini <= datetime.now(tz.utc):
        raise HTTPException(400, "La reposición debe ser en el futuro")

    dur = body.get("duration_min", 60)
    try:
        dur = max(15, min(240, int(dur)))
    except (TypeError, ValueError):
        dur = 60

    # Si indica una clase original, se toman sus datos; si no, los de su
    # inscripción activa.
    orig = None
    if body.get("original_session_id"):
        orig = await db.get(ClassSession, body["original_session_id"])

    enr = (await db.execute(
        select(Enrollment).where(
            Enrollment.student_id == student_id,
            Enrollment.is_active.is_(True),
        ).limit(1)
    )).scalar_one_or_none()

    course_id = (orig.course_id if orig else None) or (enr.course_id if enr else None)
    level_id = (orig.level_id if orig else None) or (enr.level_id if enr else None)
    if not course_id or not level_id:
        raise HTTPException(
            400,
            "Ese estudiante no tiene una inscripción activa. Inscríbelo primero.",
        )

    teacher_id = (body.get("teacher_id") or "").strip() or \
        (orig.teacher_id if orig else None) or (enr.teacher_id if enr else None)
    if not teacher_id:
        raise HTTPException(400, "Indica qué profesor la va a dar")

    cuenta = bool(body.get("counts_for_progress", False))
    titulo = (body.get("title") or "").strip() or (
        f"🔄 Reposición — {orig.title}" if orig else "🔄 Clase de reposición"
    )

    nueva = ClassSession(
        title=titulo[:150],
        description=(body.get("description") or "").strip() or "Clase de recuperación",
        course_id=course_id, level_id=level_id,
        teacher_id=teacher_id,
        student_id=student_id,     # es SUYA, no del grupo
        starts_at_utc=ini,
        ends_at_utc=ini + timedelta(minutes=dur),
        modality=Modality(body.get("modality") or (orig.modality.value if orig else "online")),
        meeting_url=body.get("meeting_url") or (orig.meeting_url if orig else None),
        video_provider=("dorismon" if body.get("video_provider") == "dorismon" else "meet"),
        capacity=1,
        counts_for_progress=cuenta,
        status=SessionStatus.scheduled,
    )
    db.add(nueva)
    await db.flush()

    # Queda registrada como reposición, para llevar la cuenta
    req = MakeupRequest(
        student_id=student_id,
        original_session_id=orig.id if orig else None,
        status="scheduled",
        reason=(body.get("reason") or "Agendada por el instituto")[:500],
        missed_by=("teacher" if body.get("missed_by") == "teacher" else "student"),
        makeup_session_id=nueva.id,
        admin_note=(body.get("note") or "")[:250] or None,
        created_by="admin",
        counts_for_progress=cuenta,
        resolved_at=datetime.now(tz.utc),
    )
    db.add(req)

    # Si faltó el profesor, la ausencia no cuenta contra el estudiante
    if orig and req.missed_by == "teacher":
        try:
            att = (await db.execute(
                select(SessionAttendance).where(
                    SessionAttendance.session_id == orig.id,
                    SessionAttendance.student_id == student_id,
                )
            )).scalar_one_or_none()
            if att and att.state == AttendanceState.absent:
                att.state = AttendanceState.excused
                att.notes = ((att.notes or "") + " · Falta del profesor, se repuso").strip()
        except Exception:
            pass

    # Avisar al estudiante y al profesor
    try:
        from app.services.push_service import notify_user
        from zoneinfo import ZoneInfo as _ZI
        local = ini.astimezone(_ZI("America/Santo_Domingo"))
        cuando = local.strftime("%d/%m a las %I:%M %p").lstrip("0")
        cuerpo = f"Tienes una clase de reposición el {cuando}."
        db.add(Notification(
            user_id=student_id, type=NotificationType.info,
            title="🔄 Clase de reposición agendada", body=cuerpo,
            link="/dashboard/student",
        ))
        await notify_user(db, student_id, "🔄 Clase de reposición agendada",
                          cuerpo, "/dashboard/student", f"makeup:{req.id}")
        db.add(Notification(
            user_id=teacher_id, type=NotificationType.info,
            title="🔄 Clase de reposición asignada",
            body=f"Con {u.full_name} el {cuando}.", link="/dashboard/teacher",
        ))
        await notify_user(db, teacher_id, "🔄 Clase de reposición asignada",
                          f"Con {u.full_name} el {cuando}.",
                          "/dashboard/teacher", f"makeup:{req.id}")
    except Exception:
        pass

    await log_action(db, admin.user_id, "create_makeup_direct", "sessions",
                     target_id=nueva.id, details=f"estudiante={student_id}")
    await db.commit()
    return {
        "ok": True, "id": req.id, "session_id": nueva.id,
        "counts_for_progress": cuenta,
    }


@router.get("/students/{student_id}/missed-classes")
async def student_missed_classes(
    student_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Las clases a las que faltó este estudiante, para elegir cuál reponer."""
    ahora = datetime.now(tz.utc)
    desde = ahora - timedelta(days=90)
    filas = (await db.execute(
        select(SessionAttendance, ClassSession)
        .join(ClassSession, SessionAttendance.session_id == ClassSession.id)
        .where(
            SessionAttendance.student_id == student_id,
            SessionAttendance.state == AttendanceState.absent,
            ClassSession.starts_at_utc >= desde,
        ).order_by(ClassSession.starts_at_utc.desc()).limit(20)
    )).all()

    from app.models import MakeupRequest
    ya = {x for (x,) in (await db.execute(
        select(MakeupRequest.original_session_id).where(
            MakeupRequest.student_id == student_id,
            MakeupRequest.original_session_id.is_not(None),
        )
    )).all()}

    return {"items": [{
        "session_id": s.id, "title": s.title,
        "starts_at_utc": s.starts_at_utc.isoformat() if s.starts_at_utc else None,
        "already_requested": s.id in ya,
    } for a, s in filas]}


@router.get("/students-for-makeup")
async def students_for_makeup(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.39 — Los estudiantes a los que SÍ se les puede reponer una clase.

    ANTES el selector mostraba a cualquiera con cuenta de estudiante: gente
    que se registró y nunca se inscribió, los que hicieron el test y no
    siguieron, cuentas de prueba... Una reposición se le agenda a alguien que
    YA está estudiando, no a cualquiera.

    Ahora solo salen los que tienen inscripción activa, y con el contexto que
    hace falta para decidir: su nivel, su grupo y su profesor.
    """
    filas = (await db.execute(
        select(Enrollment, User, Level, Course)
        .join(User, Enrollment.student_id == User.id)
        .join(Level, Enrollment.level_id == Level.id)
        .join(Course, Enrollment.course_id == Course.id)
        .where(Enrollment.is_active.is_(True), User.is_active.is_(True))
        .order_by(User.full_name)
    )).all()

    vistos = set()
    out = []
    for e, u, nivel, curso in filas:
        if u.id in vistos:
            continue
        vistos.add(u.id)

        grupo = await db.get(ClassSeries, e.series_id) if getattr(e, "series_id", None) else None
        profe = await db.get(User, e.teacher_id) if getattr(e, "teacher_id", None) else None
        st = await db.get(Student, u.id)

        out.append({
            "student_id": u.id,
            "name": u.full_name,
            "email": u.email,
            "enrollment_id": e.id,
            "course_id": curso.id, "course_name": curso.name,
            "level_id": nivel.id, "level_code": nivel.code,
            "group_id": grupo.id if grupo else None,
            "group_name": grupo.name if grupo else None,
            "teacher_id": profe.id if profe else None,
            "teacher_name": profe.full_name if profe else None,
            "is_paused": bool(st.is_paused) if st else False,
            # Para mostrarlo ordenado en el selector
            "display": (
                f"{u.full_name} — {nivel.code}"
                + (f" · {grupo.name}" if grupo else " · sin grupo")
                + (f" · {profe.full_name}" if profe else "")
            ),
        })
    return {"items": out, "count": len(out)}


@router.post("/sessions/{session_id}/substitute-teacher")
async def substitute_teacher(
    session_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.39 — Poner un profesor sustituto en UNA clase.

    ANTES solo se podía cambiar el profesor de toda la serie. Si uno no podía
    venir un día concreto, había que editar la clase a mano o cancelarla.

    💰 SOBRE EL PAGO: cobra QUIEN DA LA CLASE, con SU tarifa.
    El sistema paga según quién registró la asistencia, así que al cambiar el
    profesor de la clase, el sustituto queda como responsable y cobra su
    propia tarifa. La serie NO se toca: la próxima semana vuelve el titular.

    AVISA DE CHOQUES: si el sustituto ya tiene clase a esa hora, lo dice antes.
    """
    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    if s.status == SessionStatus.cancelled:
        raise HTTPException(400, "Esa clase está cancelada")

    fin = s.ends_at_utc
    if fin and fin.tzinfo is None:
        fin = fin.replace(tzinfo=tz.utc)
    if fin and fin < datetime.now(tz.utc):
        raise HTTPException(400, "Esa clase ya pasó")

    nuevo_id = (body.get("teacher_id") or "").strip()
    if not nuevo_id:
        raise HTTPException(400, "Indica qué profesor la va a dar")
    if nuevo_id == s.teacher_id:
        raise HTTPException(400, "Ese ya es el profesor de la clase")

    nuevo = await db.get(User, nuevo_id)
    if not nuevo or nuevo.role != UserRole.teacher:
        raise HTTPException(404, "Profesor no encontrado")

    # ¿El sustituto está libre a esa hora?
    ini = s.starts_at_utc
    if ini and ini.tzinfo is None:
        ini = ini.replace(tzinfo=tz.utc)
    ocupado = (await db.execute(
        select(ClassSession).where(
            ClassSession.teacher_id == nuevo_id,
            ClassSession.id != session_id,
            ClassSession.status != SessionStatus.cancelled,
            ClassSession.starts_at_utc < fin,
            ClassSession.ends_at_utc > ini,
        ).limit(1)
    )).scalar_one_or_none()

    if ocupado and not body.get("confirm_overlap"):
        raise HTTPException(409, {
            "necesita_confirmacion": True,
            "mensaje": (
                f"{nuevo.full_name} ya tiene '{ocupado.title}' a esa misma hora. "
                "¿Lo asignas igual?"
            ),
        })

    anterior = await db.get(User, s.teacher_id) if s.teacher_id else None
    s.teacher_id = nuevo_id

    # Avisar a los tres lados
    try:
        from app.services.push_service import notify_user
        from zoneinfo import ZoneInfo as _ZI
        cuando = (ini.astimezone(_ZI("America/Santo_Domingo")).strftime("%d/%m a las %I:%M %p").lstrip("0")
                  if ini else "")

        db.add(Notification(
            user_id=nuevo_id, type=NotificationType.info,
            title="👨‍🏫 Te asignaron una clase como sustituto",
            body=f"'{s.title}' — {cuando}.", link="/dashboard/teacher",
        ))
        await notify_user(db, nuevo_id, "👨‍🏫 Te asignaron una clase como sustituto",
                          f"'{s.title}' — {cuando}.", "/dashboard/teacher", f"sust:{s.id}")

        if anterior:
            db.add(Notification(
                user_id=anterior.id, type=NotificationType.info,
                title="Tu clase la dará un sustituto",
                body=f"'{s.title}' del {cuando} la dará {nuevo.full_name}.",
                link="/dashboard/teacher",
            ))

        # A los estudiantes que les toca esa clase
        for uid in await _destinatarios_de_clase(db, s):
            cuerpo = f"'{s.title}' del {cuando} la dará {nuevo.full_name}."
            db.add(Notification(
                user_id=uid, type=NotificationType.info,
                title="👨‍🏫 Cambio de profesor en tu próxima clase",
                body=cuerpo, link="/dashboard/student",
            ))
            await notify_user(db, uid, "👨‍🏫 Cambio de profesor en tu próxima clase",
                              cuerpo, "/dashboard/student", f"sust:{s.id}")
    except Exception:
        pass

    await log_action(db, admin.user_id, "substitute_teacher", "sessions",
                     target_id=session_id,
                     details=f"{anterior.full_name if anterior else '?'} → {nuevo.full_name}")
    await db.commit()
    return {
        "ok": True,
        "teacher_name": nuevo.full_name,
        "had_conflict": bool(ocupado),
        # Se aclara para que quede constancia de la regla
        "nota": "El sustituto cobra su propia tarifa al registrar la asistencia.",
    }


@router.get("/sessions/{session_id}/available-teachers")
async def available_teachers_for_session(
    session_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Qué profesores están LIBRES a la hora de esta clase.

    Para elegir sustituto sin cruzar horarios ni tener que revisar a mano.
    """
    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")

    ini, fin = s.starts_at_utc, s.ends_at_utc
    if ini and ini.tzinfo is None:
        ini = ini.replace(tzinfo=tz.utc)
    if fin and fin.tzinfo is None:
        fin = fin.replace(tzinfo=tz.utc)

    profes = (await db.execute(
        select(User, Teacher)
        .join(Teacher, Teacher.user_id == User.id)
        .where(User.role == UserRole.teacher, User.is_active.is_(True))
        .order_by(User.full_name)
    )).all()

    out = []
    for u, t in profes:
        if u.id == s.teacher_id:
            continue  # es el titular
        choque = (await db.execute(
            select(ClassSession).where(
                ClassSession.teacher_id == u.id,
                ClassSession.id != session_id,
                ClassSession.status != SessionStatus.cancelled,
                ClassSession.starts_at_utc < fin,
                ClassSession.ends_at_utc > ini,
            ).limit(1)
        )).scalar_one_or_none()
        out.append({
            "teacher_id": u.id, "name": u.full_name,
            "available": choque is None,
            "conflict": choque.title if choque else None,
            # Su tarifa, que es la que cobrará
            "rate_group": float(t.rate_group or 0),
            "rate_private": float(t.rate_private or 0),
        })

    out.sort(key=lambda x: (not x["available"], x["name"]))
    return {"items": out}


# ============================================================================
# V3.9.40 — ASISTENCIA VISTA DESDE EL ADMIN
# ============================================================================

@router.get("/attendance-overview")
async def attendance_overview(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    days: int = 14,
):
    """V3.9.40 — Quién asistió, quién no, y qué profesores no pasaron lista.

    ANTES el admin solo veía la asistencia entrando estudiante por estudiante.
    No había forma de ver el panorama, ni de saber qué clases quedaron sin
    lista (que son las que NO se le pagan al profesor).
    """
    ahora = datetime.now(tz.utc)
    desde = ahora - timedelta(days=max(1, min(90, days)))

    sesiones = (await db.execute(
        select(ClassSession).where(
            ClassSession.starts_at_utc >= desde,
            ClassSession.starts_at_utc <= ahora,
            ClassSession.status != SessionStatus.cancelled,
        ).order_by(ClassSession.starts_at_utc.desc())
    )).scalars().all()

    ids = [s.id for s in sesiones]
    registros: dict[str, list] = {}
    if ids:
        filas = (await db.execute(
            select(SessionAttendance, User)
            .join(User, SessionAttendance.student_id == User.id)
            .where(SessionAttendance.session_id.in_(ids))
        )).all()
        for a, u in filas:
            registros.setdefault(a.session_id, []).append((a, u))

    clases = []
    sin_lista_por_profe: dict[str, dict] = {}
    total_presentes = total_ausentes = 0

    for s in sesiones:
        profe = await db.get(User, s.teacher_id) if s.teacher_id else None
        marcas = registros.get(s.id, [])
        con_estado = [(a, u) for a, u in marcas if a.state]

        presentes = sum(1 for a, _ in con_estado if a.state == AttendanceState.present)
        ausentes = sum(1 for a, _ in con_estado if a.state == AttendanceState.absent)
        justificados = sum(1 for a, _ in con_estado if a.state == AttendanceState.excused)
        total_presentes += presentes
        total_ausentes += ausentes

        tiene_lista = len(con_estado) > 0
        if not tiene_lista and s.teacher_id:
            d = sin_lista_por_profe.setdefault(s.teacher_id, {
                "teacher_id": s.teacher_id,
                "teacher_name": profe.full_name if profe else "—",
                "teacher_email": profe.email if profe else None,
                "count": 0, "classes": [],
            })
            d["count"] += 1
            d["classes"].append({
                "id": s.id, "title": s.title,
                "starts_at_utc": s.starts_at_utc.isoformat() if s.starts_at_utc else None,
            })

        clases.append({
            "id": s.id, "title": s.title,
            "starts_at_utc": s.starts_at_utc.isoformat() if s.starts_at_utc else None,
            "teacher_id": s.teacher_id,
            "teacher_name": profe.full_name if profe else None,
            "has_attendance": tiene_lista,
            "present": presentes, "absent": ausentes, "excused": justificados,
            "total": len(con_estado),
            "students": [{
                "student_id": u.id, "name": u.full_name,
                "state": a.state.value if a.state else None,
            } for a, u in con_estado],
            # Lo que importa para el pago
            "billable": tiene_lista,
        })

    sin_lista = sorted(sin_lista_por_profe.values(), key=lambda x: -x["count"])
    total_sin = sum(x["count"] for x in sin_lista)

    return {
        "days": days,
        "classes": clases,
        "totals": {
            "classes": len(clases),
            "with_attendance": len(clases) - total_sin,
            "without_attendance": total_sin,
            "present": total_presentes,
            "absent": total_ausentes,
            "attendance_rate": (
                round(total_presentes * 100 / (total_presentes + total_ausentes), 1)
                if (total_presentes + total_ausentes) else None
            ),
        },
        "teachers_missing_attendance": sin_lista,
    }


@router.post("/remind-attendance")
async def remind_attendance(
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.40 — Recordarle a un profesor que pase la lista.

    Le llega por correo, en la campana y al teléfono. Con el detalle de qué
    clases le faltan, para que no tenga que buscarlas.

    Si no se indica teacher_id, se le avisa a TODOS los que tienen clases sin
    lista en los últimos días.
    """
    from app.services.push_service import notify_user
    from app.services.email_service import send_email
    from zoneinfo import ZoneInfo as _ZI

    dias = body.get("days", 14)
    try:
        dias = max(1, min(90, int(dias)))
    except (TypeError, ValueError):
        dias = 14

    ahora = datetime.now(tz.utc)
    desde = ahora - timedelta(days=dias)
    solo = (body.get("teacher_id") or "").strip() or None

    q = select(ClassSession).where(
        ClassSession.starts_at_utc >= desde,
        ClassSession.starts_at_utc <= ahora,
        ClassSession.status != SessionStatus.cancelled,
    )
    if solo:
        q = q.where(ClassSession.teacher_id == solo)
    sesiones = (await db.execute(q)).scalars().all()

    ids = [s.id for s in sesiones]
    con_lista = set()
    if ids:
        rows = (await db.execute(
            select(SessionAttendance.session_id).where(
                SessionAttendance.session_id.in_(ids),
                SessionAttendance.state.is_not(None),
            ).distinct()
        )).all()
        con_lista = {x for (x,) in rows}

    pendientes: dict[str, list] = {}
    for s in sesiones:
        if s.id in con_lista or not s.teacher_id:
            continue
        pendientes.setdefault(s.teacher_id, []).append(s)

    if not pendientes:
        return {"ok": True, "notified": 0, "mensaje": "No hay clases sin lista pendientes."}

    avisados = 0
    for teacher_id, lista in pendientes.items():
        profe = await db.get(User, teacher_id)
        if not profe:
            continue
        n = len(lista)
        titulo = "📋 Tienes clases sin pasar lista"
        cuerpo = (
            f"Tienes {n} clase{'s' if n != 1 else ''} sin asistencia registrada. "
            "Sin la lista, esas clases no cuentan para tu pago."
        )

        db.add(Notification(
            user_id=teacher_id, type=NotificationType.reminder,
            title=titulo, body=cuerpo, link="/dashboard/teacher",
        ))
        try:
            await notify_user(db, teacher_id, titulo, cuerpo,
                              "/dashboard/teacher", "asistencia")
        except Exception:
            pass

        # Correo con el detalle, para que no tenga que buscarlas
        try:
            if profe.email:
                filas_html = ""
                for s in lista[:15]:
                    st = s.starts_at_utc
                    if st and st.tzinfo is None:
                        st = st.replace(tzinfo=tz.utc)
                    cuando = (st.astimezone(_ZI("America/Santo_Domingo"))
                              .strftime("%d/%m a las %I:%M %p").lstrip("0") if st else "")
                    filas_html += f"<li>{s.title} — {cuando}</li>"
                await send_email(
                    to=profe.email,
                    subject=f"Tienes {n} clase{'s' if n != 1 else ''} sin pasar lista",
                    html=(
                        f"<p>Hola {profe.full_name},</p>"
                        f"<p>Notamos que estas clases quedaron <strong>sin asistencia "
                        f"registrada</strong>:</p><ul>{filas_html}</ul>"
                        f"<p><strong>Importante:</strong> sin la lista, esas clases no "
                        f"cuentan para tu pago del mes.</p>"
                        f"<p>Entra a la plataforma y pásala en un minuto: en tu panel "
                        f"aparecen bajo <em>“Clases sin asistencia”</em>.</p>"
                        f"<p>— Dorismon Language Institute</p>"
                    ),
                )
        except Exception:
            pass

        avisados += 1

    await log_action(db, admin.user_id, "remind_attendance", "teachers",
                     details=f"{avisados} profesores")
    await db.commit()
    return {
        "ok": True, "notified": avisados,
        "mensaje": f"Se le avisó a {avisados} profesor{'es' if avisados != 1 else ''}.",
    }


# ============================================================================
# V3.9.43 — P0: PROFESOR AUSENTE Y REPOSICIÓN GRUPAL
# ============================================================================

# Minutos tras el inicio sin que el profesor entre, antes de avisar a Dirección
MINUTOS_PARA_ALERTA_PROFESOR = 7


@router.post("/detect-teacher-absent")
async def detect_teacher_absent(
    db: AsyncSession = Depends(get_db),
    x_cron_secret: str | None = Header(None),
):
    """V3.9.43 — Detecta clases empezadas donde el profesor NO entró.

    REGLA DE LUIS: esto NO cancela nada. Solo genera la alerta para que
    Dirección decida (esperar, contactar, sustituto, o reprogramar). Una
    alerta nunca debe convertirse sola en una cancelación irreversible.

    Lo llama el mismo cron que los recordatorios.
    """
    # V3.9.45 SEGURIDAD — Misma política que send-class-reminders: si el
    # secreto NO está configurado, se DENIEGA. Antes, con el env vacío, este
    # endpoint quedaba abierto a cualquiera.
    import os as _os
    expected = _os.getenv("REMINDER_CRON_SECRET", "")
    if not expected or x_cron_secret != expected:
        raise HTTPException(401, "Invalid cron secret")

    from app.models import VideoPresence

    ahora = datetime.now(tz.utc)
    limite = ahora - timedelta(minutes=MINUTOS_PARA_ALERTA_PROFESOR)

    # Clases que ya empezaron, no terminaron, y aún no se avisó
    sesiones = (await db.execute(
        select(ClassSession).where(
            ClassSession.starts_at_utc <= limite,
            ClassSession.ends_at_utc >= ahora,
            ClassSession.status == SessionStatus.scheduled,
            ClassSession.teacher_absent_alert_at.is_(None),
        )
    )).scalars().all()

    alertas = 0
    for s in sesiones:
        if not s.teacher_id:
            continue

        # ¿Entró el profesor al video?
        entro = (await db.execute(
            select(VideoPresence).where(
                VideoPresence.session_id == s.id,
                VideoPresence.user_id == s.teacher_id,
            )
        )).scalar_one_or_none()
        if entro:
            continue

        # ¿Hay estudiantes esperando? Es lo que hace urgente el aviso
        esperando = (await db.execute(
            select(func.count()).select_from(VideoPresence).where(
                VideoPresence.session_id == s.id,
                VideoPresence.user_id != s.teacher_id,
            )
        )).scalar() or 0

        profe = await db.get(User, s.teacher_id)
        minutos = int((ahora - (s.starts_at_utc.replace(tzinfo=tz.utc)
                                if s.starts_at_utc.tzinfo is None else s.starts_at_utc)
                       ).total_seconds() // 60)

        cuerpo = (
            f"'{s.title}' empezó hace {minutos} min y "
            f"{profe.full_name if profe else 'el profesor'} no ha entrado."
            + (f" Hay {esperando} estudiante(s) esperando." if esperando else "")
        )

        admins = (await db.execute(
            select(User).where(User.role == UserRole.super_admin)
        )).scalars().all()
        for a in admins:
            db.add(Notification(
                user_id=a.id, type=NotificationType.reminder,
                title="🚨 URGENTE: clase sin iniciar", body=cuerpo,
                link="/dashboard/admin/sessions",
            ))
            try:
                from app.services.push_service import notify_user
                await notify_user(db, a.id, "🚨 URGENTE: clase sin iniciar",
                                  cuerpo, "/dashboard/admin/sessions", f"ausente:{s.id}")
            except Exception:
                pass

        # Recordarle al profesor, por si se le pasó la hora
        db.add(Notification(
            user_id=s.teacher_id, type=NotificationType.reminder,
            title="⏰ Tu clase ya empezó",
            body=f"'{s.title}' empezó hace {minutos} minutos. Entra cuanto antes.",
            link="/dashboard/teacher",
        ))
        try:
            from app.services.push_service import notify_user
            await notify_user(db, s.teacher_id, "⏰ Tu clase ya empezó",
                              f"'{s.title}' empezó hace {minutos} min.",
                              "/dashboard/teacher", f"tarde:{s.id}")
        except Exception:
            pass

        s.teacher_absent_alert_at = ahora
        alertas += 1

    await db.commit()
    return {"ok": True, "alerts": alertas, "checked": len(sesiones)}


@router.post("/sessions/{session_id}/cancel-by-teacher")
async def cancel_by_teacher(
    session_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.43 — Cancelar una clase porque el profesor no pudo darla, y
    generar la reposición para TODO EL GRUPO.

    REGLA DE LUIS: si la culpa es del instituto, los estudiantes NO deben
    pedir la reposición uno por uno. Dirección la programa para todos.

    La clase original queda cancelada (no cuenta como completada) y la
    reposición la reemplaza académicamente: el contenido se cuenta UNA vez.
    """
    from app.models import MakeupRequest
    from app.services.audience import destinatarios_de_clase

    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    if s.status == SessionStatus.cancelled:
        raise HTTPException(400, "Esa clase ya está cancelada")

    motivo = (body.get("reason") or "El profesor no pudo impartir la clase").strip()

    # Cancelar la original, dejando constancia del motivo
    s.status = SessionStatus.cancelled
    s.cancel_reason = "cancelled_by_teacher"

    afectados = await destinatarios_de_clase(db, s)

    # Si mandan fecha, se agenda la reposición grupal de una vez
    nueva_id = None
    nueva_cuando = None
    if body.get("makeup_starts_at_utc"):
        try:
            ini = datetime.fromisoformat(
                body["makeup_starts_at_utc"].replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, "La fecha de reposición no es válida")
        if ini <= datetime.now(tz.utc):
            raise HTTPException(400, "La reposición debe ser en el futuro")

        dur = 60
        if s.starts_at_utc and s.ends_at_utc:
            dur = max(15, int((s.ends_at_utc - s.starts_at_utc).total_seconds() // 60))

        nueva = ClassSession(
            title=f"🔄 Reposición — {s.title}",
            description=f"Repone la clase del {s.starts_at_utc.strftime('%d/%m') if s.starts_at_utc else ''} (el profesor no pudo darla).",
            course_id=s.course_id, level_id=s.level_id,
            teacher_id=body.get("teacher_id") or s.teacher_id,
            scheduled_teacher_id=body.get("teacher_id") or s.teacher_id,
            series_id=s.series_id,   # sigue siendo del MISMO grupo
            starts_at_utc=ini,
            ends_at_utc=ini + timedelta(minutes=dur),
            modality=s.modality,
            meeting_url=s.meeting_url,
            video_provider=getattr(s, "video_provider", "meet"),
            capacity=s.capacity,
            # La original no ocurrió: esta la reemplaza académicamente
            counts_for_progress=True,
            status=SessionStatus.scheduled,
        )
        db.add(nueva)
        await db.flush()
        nueva_id = nueva.id

        from zoneinfo import ZoneInfo as _ZI
        nueva_cuando = ini.astimezone(_ZI("America/Santo_Domingo")).strftime(
            "%A %d/%m a las %I:%M %p").lstrip("0")

        # Constancia por estudiante, para que los reportes cuadren
        for uid in afectados:
            db.add(MakeupRequest(
                student_id=uid,
                original_session_id=s.id,
                status="scheduled",
                reason=motivo[:500],
                missed_by="teacher",
                makeup_session_id=nueva.id,
                created_by="admin",
                counts_for_progress=True,
                resolved_at=datetime.now(tz.utc),
            ))

    # Avisar a todos los afectados
    try:
        from app.services.push_service import notify_user
        if nueva_cuando:
            titulo = "🔄 Tu clase fue reprogramada"
            cuerpo = (f"La clase '{s.title}' se repone el {nueva_cuando}. "
                      "No perdiste esta clase ni cuenta como ausencia.")
        else:
            titulo = "⚠️ Clase cancelada"
            cuerpo = (f"'{s.title}' fue cancelada. Te avisaremos la nueva fecha. "
                      "No cuenta como ausencia tuya.")

        for uid in afectados:
            db.add(Notification(
                user_id=uid, type=NotificationType.class_cancelled,
                title=titulo, body=cuerpo, link="/dashboard/student",
            ))
            await notify_user(db, uid, titulo, cuerpo,
                              "/dashboard/student", f"cancel:{s.id}")

        if s.teacher_id:
            db.add(Notification(
                user_id=s.teacher_id, type=NotificationType.info,
                title="Clase cancelada",
                body=f"'{s.title}' quedó cancelada. {motivo}",
                link="/dashboard/teacher",
            ))
    except Exception:
        pass

    await log_action(db, admin.user_id, "cancel_by_teacher", "sessions",
                     target_id=session_id, details=motivo)
    await db.commit()
    return {
        "ok": True,
        "students_affected": len(afectados),
        "makeup_session_id": nueva_id,
        "makeup_when": nueva_cuando,
        "mensaje": (
            f"Clase cancelada. {len(afectados)} estudiante(s) avisados"
            + (f" · reposición el {nueva_cuando}" if nueva_cuando else
               " · falta programar la reposición")
        ),
    }


# ============================================================================
# V3.9.44 — INTENTO EXTRA DE QUIZ (excepción individual)
# ============================================================================

@router.post("/quizzes/{quiz_id}/grant-attempt", status_code=201)
async def grant_quiz_attempt(
    quiz_id: int,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Concede intentos extra a UN estudiante en un quiz.

    REGLA DE LUIS: no se toca el `max_attempts` global del quiz para darle
    una oportunidad a una sola persona. Se registra una excepción individual
    con quién la autorizó y por qué.
    """
    from app.models import QuizAttemptGrant, Quiz

    q = await db.get(Quiz, quiz_id)
    if not q:
        raise HTTPException(404, "Quiz no encontrado")

    student_id = (body.get("student_id") or "").strip()
    if not student_id:
        raise HTTPException(400, "Indica a qué estudiante")
    u = await db.get(User, student_id)
    if not u:
        raise HTTPException(404, "Estudiante no encontrado")

    extras = body.get("extra_attempts", 1)
    try:
        extras = max(1, min(5, int(extras)))
    except (TypeError, ValueError):
        extras = 1

    motivo = (body.get("reason") or "").strip()
    if not motivo:
        raise HTTPException(400, "Explica por qué se concede el intento extra")

    g = QuizAttemptGrant(
        quiz_id=quiz_id, student_id=student_id,
        extra_attempts=extras, reason=motivo[:250],
        granted_by=admin.user_id,
    )
    db.add(g)

    try:
        from app.services.push_service import notify_user
        cuerpo = f"Puedes volver a intentar '{q.title}'. {motivo}"
        db.add(Notification(
            user_id=student_id, type=NotificationType.info,
            title="🔄 Intento adicional concedido", body=cuerpo,
            link="/dashboard/student/quizzes",
        ))
        await notify_user(db, student_id, "🔄 Intento adicional concedido",
                          cuerpo, "/dashboard/student/quizzes", f"grant:{quiz_id}")
    except Exception:
        pass

    await log_action(db, admin.user_id, "grant_quiz_attempt", "quizzes",
                     target_id=str(quiz_id), details=f"{student_id}: +{extras} · {motivo}")
    await db.commit()
    return {"ok": True, "id": g.id, "extra_attempts": extras}


@router.get("/quizzes/{quiz_id}/grants")
async def list_quiz_grants(
    quiz_id: int,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Las excepciones concedidas en este quiz, para auditarlas."""
    from app.models import QuizAttemptGrant

    filas = (await db.execute(
        select(QuizAttemptGrant, User)
        .join(User, QuizAttemptGrant.student_id == User.id)
        .where(QuizAttemptGrant.quiz_id == quiz_id)
        .order_by(QuizAttemptGrant.created_at.desc())
    )).all()
    return {"items": [{
        "id": g.id, "student_id": u.id, "student_name": u.full_name,
        "extra_attempts": g.extra_attempts, "reason": g.reason,
        "revoked": g.revoked,
        "created_at": g.created_at.isoformat() if g.created_at else None,
    } for g, u in filas]}


@router.post("/quiz-grants/{grant_id}/revoke")
async def revoke_quiz_grant(
    grant_id: str,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Revocar un intento extra que aún no se usó."""
    from app.models import QuizAttemptGrant

    g = await db.get(QuizAttemptGrant, grant_id)
    if not g:
        raise HTTPException(404, "Concesión no encontrada")
    if g.used:
        raise HTTPException(400, "Ese intento ya se usó, no se puede revocar")
    g.revoked = True
    await log_action(db, admin.user_id, "revoke_quiz_grant", "quizzes", target_id=grant_id)
    await db.commit()
    return {"ok": True}


# ============================================================================
# V3.9.49 P2 — PANEL ACADÉMICO DEL ADMIN
# ============================================================================

@router.get("/at-risk-overview")
async def at_risk_overview(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Estudiantes que necesitan atención, con el MOTIVO de cada uno.

    No es una etiqueta suelta: cada estudiante trae las señales que lo
    pusieron ahí (ausencias, tareas sin entregar, promedio bajo, inactividad)
    y las reglas usadas, para que se entienda por qué aparece.
    """
    from app.services.tracking import estudiantes_en_riesgo

    return await estudiantes_en_riesgo(db, None)


@router.get("/academic-overview")
async def academic_overview(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    days: int = 30,
):
    """Panorama académico: entrega, quizzes y trabajo pendiente por profesor.

    Sirve para ver si el instituto va bien sin entrar a cada clase.
    """
    from app.services.tracking import estudiantes_en_riesgo

    ahora = datetime.now(tz.utc)
    desde = ahora - timedelta(days=max(1, min(180, days)))

    # --- Tareas del periodo ---
    tareas = (await db.execute(
        select(Assignment).where(Assignment.created_at >= desde)
    )).scalars().all() if hasattr(Assignment, "created_at") else (
        await db.execute(select(Assignment))
    ).scalars().all()

    ids = [t.id for t in tareas]
    entregadas = calificadas = 0
    if ids:
        entregadas = (await db.execute(
            select(func.count()).select_from(AssignmentSubmission).where(
                AssignmentSubmission.assignment_id.in_(ids),
                AssignmentSubmission.submitted_at.is_not(None),
            )
        )).scalar() or 0
        calificadas = (await db.execute(
            select(func.count()).select_from(AssignmentSubmission).where(
                AssignmentSubmission.assignment_id.in_(ids),
                AssignmentSubmission.score.is_not(None),
            )
        )).scalar() or 0

    # --- Trabajo pendiente por profesor ---
    pendientes = (await db.execute(
        select(Assignment.teacher_id, func.count())
        .select_from(AssignmentSubmission)
        .join(Assignment, AssignmentSubmission.assignment_id == Assignment.id)
        .where(
            AssignmentSubmission.submitted_at.is_not(None),
            AssignmentSubmission.score.is_(None),
        ).group_by(Assignment.teacher_id)
    )).all()

    por_profesor = []
    for tid, n in pendientes:
        if not tid:
            continue
        u = await db.get(User, tid)
        por_profesor.append({
            "teacher_id": tid,
            "teacher_name": u.full_name if u else "—",
            "pending_grading": n,
        })
    por_profesor.sort(key=lambda x: -x["pending_grading"])

    riesgo = await estudiantes_en_riesgo(db, None)

    return {
        "days": days,
        "assignments": {
            "total": len(tareas),
            "submitted": entregadas,
            "graded": calificadas,
            "pending_grading": max(0, entregadas - calificadas),
        },
        "teachers_pending": por_profesor,
        "at_risk": {
            "count": riesgo["count"],
            "top": riesgo["items"][:5],
        },
        "reglas_riesgo": riesgo["reglas"],
    }


# ============================================================================
# V3.9.53 P3 — APROBACIÓN DE NIVEL (Dirección)
# ============================================================================

@router.get("/completion-queue")
async def completion_queue(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """La cola de estudiantes esperando aprobación de Dirección.

    Trae las métricas completas para poder decidir sin abrir cada expediente.
    """
    from app.services.progression import elegibilidad_de_enrollment
    from app.models import CompletionReview

    filas = (await db.execute(
        select(Enrollment, User, Level, Course)
        .join(User, Enrollment.student_id == User.id)
        .join(Level, Enrollment.level_id == Level.id)
        .join(Course, Enrollment.course_id == Course.id)
        .where(
            Enrollment.is_active.is_(True),
            Enrollment.academic_status.in_(
                ["completion_review", "requires_reevaluation"]),
        )
    )).all()

    items = []
    for e, u, nivel, curso in filas:
        elegib = await elegibilidad_de_enrollment(db, e)
        rev = (await db.execute(
            select(CompletionReview)
            .where(CompletionReview.enrollment_id == e.id)
            .order_by(CompletionReview.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        profe = await db.get(User, rev.teacher_id) if rev else None

        items.append({
            "enrollment_id": e.id,
            "student_id": u.id, "student_name": u.full_name,
            "course_name": curso.name,
            "level_code": nivel.code, "level_name": nivel.name,
            "academic_status": e.academic_status,
            "eligible": elegib["eligible"],
            "pending": elegib["pending"],
            "requirements": elegib["requirements"],
            "metrics": elegib["metrics"],
            "teacher_name": profe.full_name if profe else None,
            "recommendation": rev.recommendation if rev else None,
            "recommendation_comment": rev.comment if rev else None,
            "recommended_at": (rev.created_at.isoformat()
                               if rev and rev.created_at else None),
        })

    # Los que ya cumplen todo, primero
    items.sort(key=lambda x: (not x["eligible"], x["student_name"]))
    return {"items": items, "count": len(items)}


@router.post("/enrollments/{enrollment_id}/approve-completion")
async def approve_completion(
    enrollment_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Dirección aprueba oficialmente la finalización del nivel.

    ⚠️ Es el ÚNICO punto donde una matrícula pasa a `completed`. Ni el
    cálculo ni la recomendación del profesor lo hacen.

    Guarda un SNAPSHOT de todo lo que se usó para decidir, para poder
    responder dentro de dos años por qué se aprobó, aunque los datos cambien.
    """
    from app.services.progression import (
        elegibilidad_de_enrollment, construir_snapshot,
    )
    from app.services.academic_config import puede_pasar_a
    from app.models import CompletionReview, AcademicException

    enr = await db.get(Enrollment, enrollment_id)
    if not enr:
        raise HTTPException(404, "Matrícula no encontrada")

    estado = getattr(enr, "academic_status", "active") or "active"
    if estado == "completed":
        raise HTTPException(400, "Ese nivel ya está completado")

    st = await db.get(Student, enr.student_id)
    if st and st.is_paused:
        raise HTTPException(
            400,
            "Ese estudiante está en pausa. Reactívalo antes de cerrar su nivel.",
        )

    elegib = await elegibilidad_de_enrollment(db, enr)
    pendientes = [r for r in elegib["requirements"] if not r["met"]]

    if pendientes and not body.get("approve_exception"):
        raise HTTPException(400, {
            "necesita_excepcion": True,
            "mensaje": ("Todavía no cumple: "
                        + ", ".join(r["label"] for r in pendientes)
                        + ". Puedes aprobar por excepción indicando el motivo."),
            "pending": [r["label"] for r in pendientes],
        })

    # Excepción: se registra, NO se baja el requisito
    excepciones_creadas = []
    if pendientes:
        motivo = (body.get("exception_reason") or "").strip()
        if not motivo:
            raise HTTPException(400, "Explica por qué se aprueba la excepción")
        for r in pendientes:
            ex = AcademicException(
                enrollment_id=enrollment_id,
                requirement=r["key"],
                required_value=r.get("required"),
                actual_value=r.get("actual"),
                reason=motivo[:1000],
                approved_by=admin.user_id,
                metrics_snapshot=construir_snapshot(elegib),
            )
            db.add(ex)
            excepciones_creadas.append({
                "requirement": r["label"],
                "required": r.get("required"), "actual": r.get("actual"),
                "reason": motivo,
            })

    rev = (await db.execute(
        select(CompletionReview)
        .where(CompletionReview.enrollment_id == enrollment_id)
        .order_by(CompletionReview.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    if not puede_pasar_a(estado, "completed"):
        raise HTTPException(
            400,
            f"No se puede completar desde el estado '{estado}'. "
            "Falta la recomendación del profesor.",
        )

    # Nota final: la que indique Dirección, o el promedio de lo medible
    final = body.get("final_score")
    if final is None:
        partes = [v for v in (elegib["metrics"].get("attendance_pct"),
                              elegib["metrics"].get("assignments_pct"),
                              elegib["metrics"].get("quiz_average")) if v is not None]
        final = round(sum(partes) / len(partes), 1) if partes else None

    enr.academic_status = "completed"
    enr.completed_at = datetime.now(tz.utc)
    enr.approved_by = admin.user_id
    enr.final_result = (body.get("final_result") or "passed")
    enr.final_score = final
    enr.completion_snapshot = construir_snapshot(
        elegib,
        recomendacion=rev.recommendation if rev else None,
        aprobado_por=admin.user_id,
        excepciones=excepciones_creadas,
    )

    try:
        from app.services.push_service import notify_user
        nivel = await db.get(Level, enr.level_id)
        cuerpo = (f"¡Felicidades! Completaste {nivel.code if nivel else 'tu nivel'}. "
                  "Tu certificado estará disponible pronto.")
        db.add(Notification(
            user_id=enr.student_id, type=NotificationType.info,
            title="🎓 ¡Nivel completado!", body=cuerpo,
            link="/dashboard/student/certificates",
        ))
        await notify_user(db, enr.student_id, "🎓 ¡Nivel completado!",
                          cuerpo, "/dashboard/student/certificates",
                          f"nivel:{enrollment_id}")
    except Exception:
        pass

    await log_action(db, admin.user_id, "approve_completion", "progression",
                     target_id=enrollment_id,
                     details=(f"nota={final}"
                              + (f" · excepciones={len(excepciones_creadas)}"
                                 if excepciones_creadas else "")))
    await db.commit()
    return {
        "ok": True,
        "academic_status": "completed",
        "final_score": final,
        "exceptions": excepciones_creadas,
        "mensaje": "Nivel completado. Ya se puede emitir el certificado.",
    }


@router.post("/enrollments/{enrollment_id}/return-to-teacher")
async def return_to_teacher(
    enrollment_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Devolver el caso al profesor: refuerzo o reevaluación."""
    from app.services.academic_config import puede_pasar_a

    enr = await db.get(Enrollment, enrollment_id)
    if not enr:
        raise HTTPException(404, "Matrícula no encontrada")

    destino = body.get("status") or "requires_reinforcement"
    if destino not in ("requires_reinforcement", "requires_reevaluation"):
        raise HTTPException(400, "Estado no válido")

    motivo = (body.get("reason") or "").strip()
    if not motivo:
        raise HTTPException(400, "Explica qué falta")

    actual = getattr(enr, "academic_status", "active") or "active"
    if not puede_pasar_a(actual, destino):
        raise HTTPException(400, f"No se puede pasar de '{actual}' a '{destino}'")
    enr.academic_status = destino

    try:
        if getattr(enr, "teacher_id", None):
            u = await db.get(User, enr.student_id)
            db.add(Notification(
                user_id=enr.teacher_id, type=NotificationType.info,
                title="📋 Caso devuelto por Dirección",
                body=f"{u.full_name if u else 'Un estudiante'}: {motivo[:150]}",
                link="/dashboard/teacher",
            ))
    except Exception:
        pass

    await log_action(db, admin.user_id, "return_to_teacher", "progression",
                     target_id=enrollment_id, details=f"{destino}: {motivo[:100]}")
    await db.commit()
    return {"ok": True, "academic_status": destino}


@router.post("/enrollments/{enrollment_id}/next-level", status_code=201)
async def create_next_enrollment(
    enrollment_id: str,
    body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """Crear la matrícula del siguiente nivel.

    ⚠️ La anterior NO se toca: queda como COMPLETED con su historia. Se crea
    una NUEVA con su propio progreso, asistencia, tareas y certificado.

    El grupo NO se asigna solo: puede no haber cupo o convenir otro horario.
    Si no se indica, la matrícula queda sin grupo y aparece en el aviso de
    "estudiantes sin horario" para que Dirección la coloque.
    """
    from app.services.progression import siguiente_nivel

    anterior = await db.get(Enrollment, enrollment_id)
    if not anterior:
        raise HTTPException(404, "Matrícula no encontrada")

    if (getattr(anterior, "academic_status", "") or "") != "completed":
        raise HTTPException(
            400,
            "Ese nivel todavía no está completado. Apruébalo antes de crear el "
            "siguiente.",
        )

    level_id = body.get("level_id")
    if not level_id:
        sug = await siguiente_nivel(db, anterior)
        if not sug:
            raise HTTPException(
                400,
                "No hay un nivel siguiente en ese curso. Indica el nivel a mano.",
            )
        level_id = sug.id

    nivel = await db.get(Level, int(level_id))
    if not nivel:
        raise HTTPException(404, "Nivel no encontrado")

    course_id = body.get("course_id") or anterior.course_id

    # ⚠️ V3.9.54 — El nivel debe PERTENECER a ese curso.
    #
    # Sin esto se podía crear "curso de inglés + nivel de español" mandando
    # los IDs por API. El frontend no lo ofrece, pero eso no es una
    # protección: el backend tiene que rechazarlo.
    if nivel.course_id != course_id:
        _curso_real = await db.get(Course, nivel.course_id)
        raise HTTPException(400, {
            "mensaje": (
                f"El nivel {nivel.code} pertenece a "
                f"«{_curso_real.name if _curso_real else 'otro curso'}», "
                "no al curso indicado."
            ),
        })

    # El grupo, si se indica, debe ser de ese curso y ese nivel
    _serie = (body.get("series_id") or "").strip() or None
    _serie_obj = None
    if _serie:
        _s = await db.get(ClassSeries, _serie)
        if not _s:
            raise HTTPException(404, "Grupo no encontrado")
        _serie_obj = _s
        if _s.level_id != nivel.id or _s.course_id != course_id:
            raise HTTPException(400, {
                "mensaje": (
                    f"El grupo «{_s.name}» no es de ese curso y nivel. "
                    "Elige uno que corresponda."
                ),
            })
        # Y el profesor, coherente con el grupo
        _profe = (body.get("teacher_id") or "").strip() or None
        if _profe and _s.teacher_id and _profe != _s.teacher_id:
            _p = await db.get(User, _s.teacher_id)
            raise HTTPException(400, {
                "mensaje": (
                    f"Ese grupo lo imparte {_p.full_name if _p else 'otro profesor'}. "
                    "Déjalo en blanco para usar el del grupo."
                ),
            })

    # No duplicar una matrícula que ya exista
    ya = (await db.execute(
        select(Enrollment).where(
            Enrollment.student_id == anterior.student_id,
            Enrollment.course_id == course_id,
            Enrollment.level_id == int(level_id),
            Enrollment.is_active.is_(True),
        )
    )).scalar_one_or_none()
    if ya:
        raise HTTPException(400, "Ese estudiante ya tiene una matrícula activa en ese nivel")

    nueva = Enrollment(
        student_id=anterior.student_id,
        course_id=course_id,
        level_id=int(level_id),
        # V3.9.55 — Si se eligió un grupo y no se indicó profesor, se toma el
        # TITULAR DEL GRUPO. Antes la validación decía "déjalo en blanco para
        # usar el del grupo", pero luego se guardaba None: la matrícula
        # quedaba sin profesor y el estudiante sin ver sus clases.
        teacher_id=(body.get("teacher_id") or None
                    or (_serie_obj.teacher_id if _serie_obj else None)),
        series_id=_serie,
        plan_id=body.get("plan_id") or anterior.plan_id,
        modality=anterior.modality,
        is_active=True,
        academic_status="active",
        previous_enrollment_id=enrollment_id,
    )
    db.add(nueva)
    await db.flush()

    # El nivel actual del estudiante sí avanza (es su nivel de hoy)
    try:
        st = await db.get(Student, anterior.student_id)
        if st:
            st.current_level_id = int(level_id)
    except Exception:
        pass

    try:
        from app.services.push_service import notify_user
        cuerpo = f"Ya estás inscrito en {nivel.code} — {nivel.name}."
        db.add(Notification(
            user_id=anterior.student_id, type=NotificationType.info,
            title="🚀 ¡Bienvenido a tu siguiente nivel!", body=cuerpo,
            link="/dashboard/student",
        ))
        await notify_user(db, anterior.student_id,
                          "🚀 ¡Bienvenido a tu siguiente nivel!", cuerpo,
                          "/dashboard/student", f"nuevo:{nueva.id}")
    except Exception:
        pass

    await log_action(db, admin.user_id, "create_next_enrollment", "progression",
                     target_id=nueva.id, details=f"desde={enrollment_id}")
    await db.commit()
    return {
        "ok": True,
        "enrollment_id": nueva.id,
        "level_code": nivel.code,
        "needs_group": not nueva.series_id,
        "mensaje": ("Matrícula creada. Falta asignarle grupo y horario."
                    if not nueva.series_id else "Matrícula creada."),
    }
