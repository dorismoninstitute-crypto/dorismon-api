"""Progreso académico del estudiante: módulos, ruta visual."""
from typing import Annotated
from datetime import datetime, timezone as tz
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, or_, false
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, CurrentUser
from app.core.db import get_db
from app.models import (
    User, Student, Module, Lesson, Level, Course, Enrollment,
    SessionAttendance, ClassSession, ModuleProgress, Quiz, QuizAttempt,
    AttendanceState, Branch, Classroom, SessionStatus, ClassConfirmation,
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
        # ══ V3.9.67 — SIN MATRÍCULA TODAVÍA, PERO CON CLASE ══
        #
        # Antes esto cortaba aquí. Una estudiante nueva a la que se le creaba
        # una clase de refuerzo la veía en el calendario, pero el bloque
        # grande "Tu próxima clase / Entrar a la clase" del panel no aparecía,
        # porque depende de este endpoint. Podía verla y no usarla.
        #
        # Una clase asignada explícitamente tiene que ser USABLE, no solo
        # visible. Se devuelve `enrolled: False` igual que antes (no se
        # inventa una matrícula), pero con su próxima clase real.
        from app.services.audience import (
            contexto_academico as _ctx_fn,
            filtro_clases_del_estudiante as _filtro_clases,
        )
        _ctx = await _ctx_fn(db, user.user_id)
        _ahora = datetime.now(tz.utc)
        _prox = (await db.execute(
            select(ClassSession).where(
                or_(
                    _filtro_clases(_ctx, ClassSession, user.user_id),
                    ClassSession.student_id == user.user_id,
                ),
                ClassSession.ends_at_utc > _ahora,
                ClassSession.status == SessionStatus.scheduled,
            ).order_by(ClassSession.starts_at_utc).limit(1)
        )).scalar_one_or_none()

        _data = None
        if _prox:
            _prof = await db.get(User, _prox.teacher_id) if _prox.teacher_id else None
            _loc = None
            if _prox.branch_id or _prox.classroom_id:
                _b = await db.get(Branch, _prox.branch_id) if _prox.branch_id else None
                _cr = await db.get(Classroom, _prox.classroom_id) if _prox.classroom_id else None
                if _cr and not _b and _cr.branch_id:
                    _b = await db.get(Branch, _cr.branch_id)
                _loc = {
                    "branch_name": _b.name if _b else None,
                    "address": _b.address if _b else None,
                    "classroom_name": _cr.name if _cr else None,
                }
            _data = {
                "id": _prox.id, "title": _prox.title,
                "starts_at_utc": _prox.starts_at_utc.isoformat() if _prox.starts_at_utc else None,
                "ends_at_utc": _prox.ends_at_utc.isoformat() if _prox.ends_at_utc else None,
                "modality": _prox.modality.value,
                "meeting_url": _prox.meeting_url,
                "video_provider": getattr(_prox, "video_provider", "meet") or "meet",
                "status": _prox.status.value if _prox.status else "scheduled",
                "teacher_name": _prof.full_name if _prof else None,
                "teacher_notes": _prox.teacher_notes,
                "is_private": _prox.student_id is not None,
                "location": _loc,
            }
        return {"enrolled": False, "next_session": _data}

    level = await db.get(Level, enr.level_id)
    course = await db.get(Course, enr.course_id)

    # Obtener módulos del nivel
    modules = (await db.execute(
        select(Module).where(Module.level_id == enr.level_id).order_by(Module.order_index)
    )).scalars().all()

    # V3.9.56 — El progreso de módulos es DE SU MATRÍCULA. Antes se traía
    # todo el del estudiante: quien repetía el nivel veía los módulos ya
    # completados de la vez anterior, sin haber vuelto a estudiar.
    progress_map = {}
    progress = (await db.execute(
        select(ModuleProgress).where(
            ModuleProgress.student_id == user.user_id,
            # V3.9.57 — Solo de SU matrícula. El legacy NULL no puede hacer
            # que una matrícula nueva aparezca con módulos ya completados.
            ModuleProgress.enrollment_id == (enr.id if enr else None),
        )
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
    # V3.9.33 — Si el estudiante está asignado a un GRUPO (serie), solo ve las
    # clases de ESE grupo. Antes veía todas las de su nivel: con dos grupos de
    # B1 (mañana y noche), a todos les aparecían los dos horarios.
    # Si no tiene grupo asignado, sigue viendo todo su nivel (compatibilidad).
    # V3.9.36 — REGLA ESTRICTA (decisión de Luis):
    # Si al estudiante no se le proyectó SU clase, NO VE NADA.
    #
    # Antes había un filtro por profesor como respaldo, pero eso fallaba
    # cuando un profesor daba dos horarios distintos del mismo nivel: Juan
    # (lunes/miércoles) veía también las clases de Pedro (martes/jueves).
    #
    # Ahora solo se muestran clases que son SUYAS de verdad:
    #   1. Las de su grupo asignado
    #   2. Sus clases privadas o sueltas
    # Cualquier otro caso: pantalla limpia.
    #
    # Es preferible que no vea nada a que se presente a una clase ajena.
    # ══ V3.9.68 — LA PRÓXIMA CLASE SALE DEL FILTRO CENTRAL ══
    #
    # ANTES esta consulta tenía su propia lógica: "clases de mi grupo" O "mis
    # privadas". Correcta para el caso normal, pero le faltaba una rama que ya
    # existía en el resto del sistema: SessionAudience.
    #
    # EL CASO REAL QUE FALLABA:
    #   María tiene Enrollment A2 activo, con profesor asignado,
    #   pero todavía SIN grupo (series_id = NULL).
    #   Admin le crea un "Refuerzo A2" con student_ids = [María].
    #
    #   -> Próximas clases: aparecía ✅
    #   -> Calendario: aparecía ✅
    #   -> Autorización de API: la dejaba entrar ✅
    #   -> Tarjeta "Tu próxima clase / Entrar": NO aparecía ❌
    #
    # En v3.9.67 esto se arregló SOLO para quien no tenía Enrollment. Pero
    # tener matrícula sin grupo es un estado igual de real —y más común— y esa
    # rama seguía con la lógica manual.
    #
    # Usar `filtro_clases_del_estudiante` elimina la divergencia de raíz: es
    # la MISMA regla que decide el calendario, el dashboard y el acceso al
    # video. Si el sistema dice que la clase es suya, la tarjeta la muestra.
    #
    # NO afloja nada: ese filtro sigue exigiendo pertenencia real (grupo,
    # audiencia explícita, privada, o clase suelta de SU profesor en SU
    # nivel). Compartir level_id nunca bastó y sigue sin bastar.
    from app.services.audience import (
        contexto_academico as _ctx_prox,
        filtro_clases_del_estudiante as _filtro_prox,
    )
    _ctx_ns = await _ctx_prox(db, user.user_id)

    next_session = (await db.execute(
        select(ClassSession).where(
            or_(
                _filtro_prox(_ctx_ns, ClassSession, user.user_id),
                # Privada para este estudiante
                ClassSession.student_id == user.user_id,
            ),
            ClassSession.ends_at_utc > datetime.now(tz.utc),  # V1.6.4
            ClassSession.is_open_event.is_(False),
            # V3.9.20 FIX: solo clases PROGRAMADAS — una finalizada por el profe
            # o cancelada ya no es "tu próxima clase" (antes seguía apareciendo
            # como EN CURSO para el estudiante aunque el profe la finalizara)
            ClassSession.status == SessionStatus.scheduled,
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
        # V3.9.21: ¿este estudiante ya confirmó su asistencia a esta clase?
        my_conf = (await db.execute(
            select(ClassConfirmation).where(
                ClassConfirmation.session_id == next_session.id,
                ClassConfirmation.student_id == user.user_id,
            )
        )).scalar_one_or_none()
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
            "status": next_session.status.value if next_session.status else "scheduled",  # V3.9.21
            "my_confirmed": my_conf is not None,  # V3.9.21
            "video_provider": getattr(next_session, "video_provider", "meet") or "meet",  # V3.9.26
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
        # V3.9.56 — El progreso del módulo es de UNA matrícula. Si repite el
        # nivel, la nueva empieza en cero en vez de heredar la anterior.
        _enr_mp = (await db.execute(
            select(Enrollment).where(
                Enrollment.student_id == user.user_id,
                Enrollment.level_id == m.level_id,
                Enrollment.is_active.is_(True),
            ).order_by(Enrollment.enrolled_at.desc()).limit(1)
        )).scalar_one_or_none()

        mp = (await db.execute(
            select(ModuleProgress).where(
                ModuleProgress.student_id == user.user_id,
                ModuleProgress.module_id == m.id,
                # V3.9.57 — Solo de esta matrícula
                ModuleProgress.enrollment_id == (_enr_mp.id if _enr_mp else None),
            ).limit(1)
        )).scalar_one_or_none()

        if not mp:
            mp = ModuleProgress(
                student_id=user.user_id, module_id=m.id,
                status="locked", attended_count=0, quiz_passed=False,
                enrollment_id=_enr_mp.id if _enr_mp else None,
            )
            db.add(mp)

        # V3.9.57 — NO se adopta el legacy. Antes, al tocar un registro sin
        # matrícula se le asignaba la actual: eso convertía datos ambiguos de
        # un curso anterior en evidencia de la matrícula nueva. El legacy
        # queda intacto y, si hace falta, se crea uno propio.
        mp.attended_count = attended

        # ── V3.9.54 — ELIMINADA la regla "1 asistencia = módulo completado" ──
        #
        # Antes bastaba con asistir una vez para marcarlo COMPLETED, y eso
        # inflaba la elegibilidad del nivel: alguien que fue a una sola clase
        # aparecía con el módulo terminado.
        #
        # Ahora el estado lo decide `estado_de_modulo()` en progression.py,
        # que mira asistencia, tareas y quizzes DEL MÓDULO. Aquí solo se
        # registra que empezó.
        from app.services.progression import estado_de_modulo

        if attended >= 1 and mp.status in (None, "locked"):
            mp.status = "in_progress"

        # El estado real, con evidencia
        try:
            _enr = (await db.execute(
                select(Enrollment).where(
                    Enrollment.student_id == user.user_id,
                    Enrollment.level_id == m.level_id,
                    Enrollment.is_active.is_(True),
                ).limit(1)
            )).scalar_one_or_none()
            if _enr:
                _est = await estado_de_modulo(db, _enr, m)
                if _est["status"] == "completed":
                    mp.status = "completed"
                    if not mp.completed_at:
                        mp.completed_at = datetime.now(tz.utc)
                elif mp.status == "completed":
                    # Dejó de cumplir (p. ej. se corrigió una asistencia):
                    # el estado vuelve atrás en vez de quedar mintiendo.
                    mp.status = "in_progress"
                    mp.completed_at = None
        except Exception:
            # Si el cálculo falla, se conserva lo que había: nunca se marca
            # completado sin evidencia.
            pass

    await db.commit()
    return {"ok": True}
