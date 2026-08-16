"""V3.9.53 P3 — PROGRESIÓN ACADÉMICA.

Responde: ¿este estudiante está listo para terminar su nivel? Y sobre todo,
**qué le falta** — no un porcentaje suelto.

REGLA ABSOLUTA
==============
Todo se calcula POR MATRÍCULA (`enrollment_id`), reutilizando los helpers de
P2 (`actividades_del_enrollment`, `sesiones_del_enrollment`). Nunca por
`student_id` global: Juan puede llevar English B1 y Spanish A2, y completar
uno no puede tocar el otro.

EL SISTEMA CALCULA · EL PROFESOR RECOMIENDA · DIRECCIÓN APRUEBA
Cumplir los números NO promueve a nadie. Solo lo pone en la cola de revisión.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone as tz

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.academic_config import (
    requisitos_de, NOMBRES_HABILIDADES, ESTADOS_ACADEMICOS,
)


# ============================================================================
# LAS CUATRO MÉTRICAS, POR MATRÍCULA
# ============================================================================

async def metricas_de_enrollment(db: AsyncSession, enr) -> dict:
    """Asistencia, tareas, quizzes y habilidades de ESTA matrícula.

    Cada una se calcula sobre lo que realmente le correspondía: sus clases,
    sus tareas, sus quizzes. Los eventos opcionales no cuentan (faltar al
    Conversation Club no es faltar a clase).
    """
    from app.models import (
        SessionAttendance, AttendanceState, Assignment, AssignmentSubmission,
        Quiz, QuizAttempt, SkillAssessment, SessionStatus,
    )
    from app.services.audience import (
        actividades_del_enrollment, sesiones_del_enrollment,
    )

    ahora = datetime.now(tz.utc)

    # ── ASISTENCIA ──
    # Solo las clases que ya ocurrieron y no se cancelaron. Una clase
    # cancelada por el instituto no puede contar como falta del estudiante.
    sesiones = await sesiones_del_enrollment(db, enr, hasta=ahora, limite=0)
    sesiones = [s for s in sesiones if s.status != SessionStatus.cancelled]
    sids = [s.id for s in sesiones]

    presentes = ausentes = justificados = 0
    if sids:
        marcas = (await db.execute(
            select(SessionAttendance).where(
                SessionAttendance.student_id == enr.student_id,
                SessionAttendance.session_id.in_(sids),
                SessionAttendance.state.is_not(None),
            )
        )).scalars().all()
        for a in marcas:
            if a.state in (AttendanceState.present, AttendanceState.late):
                presentes += 1
            elif a.state == AttendanceState.excused:
                justificados += 1
            elif a.state == AttendanceState.absent:
                ausentes += 1

    # Las justificadas no cuentan en contra: no se le penaliza por avisar
    base_asistencia = presentes + ausentes
    pct_asistencia = round(presentes * 100 / base_asistencia, 1) if base_asistencia else None

    # ── TAREAS ──
    tareas = await actividades_del_enrollment(db, Assignment, enr, "assignment")
    entregadas = 0
    notas_tareas = []
    for t in tareas:
        sub = (await db.execute(
            select(AssignmentSubmission).where(
                AssignmentSubmission.assignment_id == t.id,
                AssignmentSubmission.student_id == enr.student_id,
            )
        )).scalar_one_or_none()
        if sub and sub.submitted_at:
            entregadas += 1
            if sub.score is not None:
                notas_tareas.append(float(sub.score))
    pct_tareas = round(entregadas * 100 / len(tareas), 1) if tareas else None

    # ── QUIZZES ──
    quizzes = await actividades_del_enrollment(
        db, Quiz, enr, "quiz", extra=Quiz.is_published.is_(True))
    mejores = []
    quizzes_hechos = 0
    for q in quizzes:
        intentos = (await db.execute(
            select(QuizAttempt).where(
                QuizAttempt.quiz_id == q.id,
                QuizAttempt.student_id == enr.student_id,
                QuizAttempt.submitted_at.is_not(None),
            )
        )).scalars().all()
        if not intentos:
            continue
        quizzes_hechos += 1
        mejor = max((float(a.score) for a in intentos if a.score is not None),
                    default=None)
        if mejor is not None:
            mejores.append(mejor)
    promedio_quizzes = round(sum(mejores) / len(mejores), 1) if mejores else None

    # ── HABILIDADES ──
    # La nota vigente de cada habilidad es la ÚLTIMA evaluación de esta
    # matrícula. Las anteriores se conservan como historial.
    filas = (await db.execute(
        select(SkillAssessment)
        .where(SkillAssessment.enrollment_id == enr.id)
        .order_by(SkillAssessment.evaluated_at)
    )).scalars().all()
    habilidades = {}
    for s in filas:
        habilidades[s.skill] = {
            "score": float(s.score),
            "evaluated_at": s.evaluated_at.isoformat() if s.evaluated_at else None,
            "source": s.source,
            "notes": s.notes,
        }

    return {
        "attendance_pct": pct_asistencia,
        "attendance_detail": {
            "present": presentes, "absent": ausentes,
            "excused": justificados, "total_classes": len(sesiones),
        },
        "assignments_pct": pct_tareas,
        "assignments_detail": {
            "submitted": entregadas, "total": len(tareas),
            "average_score": (round(sum(notas_tareas) / len(notas_tareas), 1)
                              if notas_tareas else None),
        },
        "quiz_average": promedio_quizzes,
        "quiz_detail": {"taken": quizzes_hechos, "total": len(quizzes)},
        "skills": habilidades,
    }


# ============================================================================
# ELEGIBILIDAD
# ============================================================================

async def elegibilidad_de_enrollment(db: AsyncSession, enr) -> dict:
    """¿Está listo para terminar el nivel? Y si no, QUÉ le falta.

    No devuelve un porcentaje suelto: devuelve requisito por requisito, con
    lo que se pide, lo que tiene, y si lo cumple. Eso es lo que se le puede
    explicar a un estudiante.
    """
    from app.models import AcademicException

    req = requisitos_de(enr.course_id, enr.level_id)
    m = await metricas_de_enrollment(db, enr)

    # Excepciones ya aprobadas por Dirección para esta matrícula
    excepciones = {
        e.requirement: e
        for e in (await db.execute(
            select(AcademicException).where(
                AcademicException.enrollment_id == enr.id)
        )).scalars().all()
    }

    def _chequeo(clave, etiqueta, actual, minimo, unidad="%"):
        cumple = actual is not None and actual >= minimo
        exc = excepciones.get(clave)
        return {
            "key": clave,
            "label": etiqueta,
            "required": minimo,
            "actual": actual,
            "unit": unidad,
            "met": bool(cumple or exc),
            "met_by_exception": bool(exc and not cumple),
            "exception_reason": exc.reason if exc else None,
            "missing": (None if (cumple or exc or actual is None)
                        else round(minimo - actual, 1)),
            "no_data": actual is None,
        }

    requisitos = [
        _chequeo("attendance", "Asistencia",
                 m["attendance_pct"], req.asistencia_minima),
        _chequeo("assignments", "Tareas entregadas",
                 m["assignments_pct"], req.tareas_minimas),
        _chequeo("quizzes", "Promedio de quizzes",
                 m["quiz_average"], req.promedio_quizzes_minimo),
    ]

    # ── V3.9.54 — MÓDULOS COMPLETADOS ──
    #
    # Faltaba este requisito: se podía ser elegible para terminar el nivel
    # con módulos a medias. Ahora cuenta como los demás, y admite excepción
    # auditada igual que el resto.
    #
    # Si el nivel no tiene módulos definidos, el requisito NO aplica: no se
    # puede exigir completar algo que el curso no tiene.
    mods = await modulos_de_enrollment(db, enr)
    if mods["applies"]:
        exc_mod = excepciones.get("modules")
        requisitos.append({
            "key": "modules",
            "label": "Módulos completados",
            "required": mods["total"],
            "actual": mods["completed"],
            "unit": f"de {mods['total']}",
            "met": bool(mods["completed"] >= mods["total"] or exc_mod),
            "met_by_exception": bool(exc_mod and mods["completed"] < mods["total"]),
            "exception_reason": exc_mod.reason if exc_mod else None,
            "missing": (None if mods["completed"] >= mods["total"]
                        else mods["total"] - mods["completed"]),
            "no_data": False,
            "pending_modules": [d["module_title"] for d in mods["modules"]
                                if d["status"] != "completed"],
        })

    # Habilidades: deben estar EVALUADAS
    faltan_skills = []
    for skill in req.habilidades_requeridas:
        nota = m["skills"].get(skill, {}).get("score")
        if nota is None:
            faltan_skills.append(skill)
    exc_skills = excepciones.get("skills")
    requisitos.append({
        "key": "skills",
        "label": "Habilidades evaluadas",
        "required": len(req.habilidades_requeridas),
        "actual": len(req.habilidades_requeridas) - len(faltan_skills),
        "unit": "de 4",
        "met": bool(not faltan_skills or exc_skills),
        "met_by_exception": bool(exc_skills and faltan_skills),
        "exception_reason": exc_skills.reason if exc_skills else None,
        "missing_skills": [NOMBRES_HABILIDADES.get(s, s) for s in faltan_skills],
        "no_data": len(faltan_skills) == len(req.habilidades_requeridas),
    })

    elegible = all(r["met"] for r in requisitos)
    pendientes = [r for r in requisitos if not r["met"]]

    return {
        "enrollment_id": enr.id,
        "academic_status": getattr(enr, "academic_status", "active") or "active",
        "academic_status_label": ESTADOS_ACADEMICOS.get(
            getattr(enr, "academic_status", "active") or "active", "Cursando"),
        "eligible": elegible,
        "requirements": requisitos,
        "pending": [r["label"] for r in pendientes],
        "metrics": m,
        "modules": mods,
        "config": req.como_dict(),
        # Cuántos requisitos cumple, para ordenar la cola de revisión
        "met_count": sum(1 for r in requisitos if r["met"]),
        "total_count": len(requisitos),
    }


# ============================================================================
# SNAPSHOT — la memoria de la decisión
# ============================================================================

def construir_snapshot(elegibilidad: dict, recomendacion=None,
                       aprobado_por=None, excepciones=None) -> str:
    """La FOTO de por qué se aprobó a alguien.

    Dentro de dos años hay que poder responder "¿por qué Juan obtuvo su B1?"
    sin depender de datos que pudieron cambiar. Por eso se congela aquí todo
    lo que se usó para decidir, en vez de recalcularlo después.
    """
    return json.dumps({
        "generated_at": datetime.now(tz.utc).isoformat(),
        "eligible": elegibilidad.get("eligible"),
        "requirements": elegibilidad.get("requirements"),
        "metrics": elegibilidad.get("metrics"),
        "config_used": elegibilidad.get("config"),
        "teacher_recommendation": recomendacion,
        "approved_by": aprobado_por,
        "exceptions": excepciones or [],
    }, ensure_ascii=False, default=str)


def leer_snapshot(texto: str | None) -> dict | None:
    if not texto:
        return None
    try:
        return json.loads(texto)
    except (json.JSONDecodeError, TypeError):
        return None


# ============================================================================
# SIGUIENTE NIVEL
# ============================================================================

async def siguiente_nivel(db: AsyncSession, enr):
    """Qué nivel viene después, según el orden del curso.

    Solo RECOMIENDA. Dirección confirma curso, nivel, grupo y profesor: puede
    no haber cupo, o convenir otro horario. El sistema no inventa un grupo.
    """
    from app.models import Level

    actual = await db.get(Level, enr.level_id)
    if not actual:
        return None

    return (await db.execute(
        select(Level).where(
            Level.course_id == enr.course_id,
            Level.order_index > actual.order_index,
        ).order_by(Level.order_index).limit(1)
    )).scalar_one_or_none()


# ============================================================================
# V3.9.54 — ESTADO DE UN MÓDULO, POR MATRÍCULA
# ============================================================================
#
# ⚠️ LA REGLA QUE SE ELIMINA:
#
#     if attended >= 1: status = "completed"
#
# Asistir una vez a una clase del módulo lo marcaba como completado. Eso no
# es cierto y contamina la elegibilidad del nivel: un estudiante que fue a
# una sola clase aparecía con el módulo terminado.
#
# ESTA ES LA ÚNICA FUNCIÓN que decide si un módulo está completo.

async def estado_de_modulo(db: AsyncSession, enr, modulo) -> dict:
    """¿Este módulo está completo para esta matrícula? Y con qué evidencia.

    ⚠️ V3.9.55 — EXIGE COBERTURA DEL CONTENIDO.

    Antes la asistencia se medía solo sobre las clases YA registradas: si el
    módulo tenía una sola clase creada y el estudiante fue, salía 1/1 = 100%
    y el módulo se daba por completo. Eso reproducía por otra vía el error
    que queríamos eliminar.

    Ahora un módulo se completa si:
      1. Se cubrió su CONTENIDO — sus lecciones publicadas, que son la
         definición real de lo que el módulo enseña
      2. Y se cumplen los requisitos medibles (asistencia, tareas, quizzes)

    Sin lecciones publicadas no hay forma de saber qué abarca el módulo, así
    que **no se puede afirmar que esté completo**: queda en progreso.
    """
    from app.models import (
        SessionStatus, SessionAttendance, AttendanceState,
        Assignment, AssignmentSubmission, Quiz, QuizAttempt,
        Lesson, LessonProgress,
    )
    from app.services.audience import (
        sesiones_del_enrollment, actividades_del_enrollment,
    )
    from sqlalchemy import or_

    req = requisitos_de(enr.course_id, enr.level_id, modulo.id)
    requisitos = []

    # ── 1. COBERTURA DEL CONTENIDO ──
    # Las lecciones publicadas del módulo son lo que el módulo enseña.
    lecciones = (await db.execute(
        select(Lesson).where(
            Lesson.module_id == modulo.id,
            Lesson.is_published.is_(True),
        )
    )).scalars().all()

    lecciones_hechas = 0
    if lecciones:
        ids_lec = [l.id for l in lecciones]
        # Solo el progreso de ESTA matrícula. Los registros legacy (sin
        # enrollment_id) NO cuentan: no se sabe de qué matrícula eran, y
        # darlos por buenos permitiría completar un nivel sin estudiarlo.
        progresos = (await db.execute(
            select(LessonProgress).where(
                LessonProgress.student_id == enr.student_id,
                LessonProgress.lesson_id.in_(ids_lec),
                LessonProgress.is_completed.is_(True),
                # V3.9.57 — SOLO esta matrícula. Antes se aceptaba también el
                # legacy NULL, así que un progreso viejo y ambiguo podía
                # completar el módulo de una matrícula nueva.
                LessonProgress.enrollment_id == enr.id,
            )
        )).scalars().all()
        lecciones_hechas = len({p.lesson_id for p in progresos})

        requisitos.append({
            "key": "content",
            "label": "Contenido del módulo",
            "required": len(lecciones),
            "actual": lecciones_hechas,
            "met": lecciones_hechas >= len(lecciones),
            "detail": f"{lecciones_hechas} de {len(lecciones)} lecciones",
            "measurable": True,
            # Este requisito manda: sin contenido cubierto no hay módulo
            "is_coverage": True,
        })

    # ── 2. ASISTENCIA A LAS CLASES DEL MÓDULO ──
    sesiones = await sesiones_del_enrollment(db, enr, limite=0)
    del_modulo = [s for s in sesiones
                  if s.module_id == modulo.id and s.status != SessionStatus.cancelled]

    # ── V3.9.56 — Las clases YA DADAS sin asistencia bloquean el requisito ──
    #
    # Antes se calculaba sobre las marcas existentes: con 5 clases dadas, 1
    # presente y 4 sin registrar, salía 1/1 = 100%. Un dato que falta no es
    # un dato favorable.
    #
    # Las clases FUTURAS no cuentan: todavía no tenían que ocurrir.
    ahora_att = datetime.now(tz.utc)
    presentes = 0
    if del_modulo:
        def _pasada(s):
            fin = s.ends_at_utc or s.starts_at_utc
            if fin and fin.tzinfo is None:
                fin = fin.replace(tzinfo=tz.utc)
            return bool(fin and fin < ahora_att)

        ya_dadas = [s for s in del_modulo if _pasada(s)]
        ids = [s.id for s in ya_dadas]

        marcas = []
        if ids:
            marcas = (await db.execute(
                select(SessionAttendance).where(
                    SessionAttendance.student_id == enr.student_id,
                    SessionAttendance.session_id.in_(ids),
                    SessionAttendance.state.is_not(None),
                )
            )).scalars().all()

        presentes = sum(1 for a in marcas
                        if a.state in (AttendanceState.present, AttendanceState.late))
        ausentes = sum(1 for a in marcas if a.state == AttendanceState.absent)
        justificadas = sum(1 for a in marcas if a.state == AttendanceState.excused)
        registradas = presentes + ausentes + justificadas
        sin_registrar = max(0, len(ya_dadas) - registradas)

        base = presentes + ausentes
        pct = round(presentes * 100 / base, 1) if base else None

        # Si falta asistencia por registrar, el requisito NO se puede dar por
        # cumplido: el profesor tiene que pasar lista primero.
        datos_completos = sin_registrar == 0 and len(ya_dadas) > 0

        requisitos.append({
            "key": "attendance",
            "label": "Asistencia del módulo",
            "required": req.asistencia_minima,
            "actual": pct,
            "met": bool(datos_completos and pct is not None
                        and pct >= req.asistencia_minima),
            "detail": (f"{presentes} de {len(ya_dadas)} clases dadas"
                       + (f" · faltan {sin_registrar} por registrar"
                          if sin_registrar else "")),
            "measurable": len(ya_dadas) > 0,
            # Para que la pantalla pueda distinguir "va mal" de "falta el dato"
            "incomplete_data": sin_registrar > 0,
            "sessions_done": len(ya_dadas),
            "sessions_registered": registradas,
            "sessions_missing": sin_registrar,
        })

    # ── 3. TAREAS DEL MÓDULO ──
    #
    # V3.9.55 — Por AUDIENCIA, no por module_id + level_id. Antes se tomaban
    # todas las del nivel, así que una tarea del grupo de la mañana afectaba
    # al módulo del estudiante de la noche.
    tareas = []
    if hasattr(Assignment, "module_id"):
        tareas = [t for t in await actividades_del_enrollment(
            db, Assignment, enr, "assignment")
            if getattr(t, "module_id", None) == modulo.id]

    if tareas:
        entregadas = 0
        for t in tareas:
            sub = (await db.execute(
                select(AssignmentSubmission).where(
                    AssignmentSubmission.assignment_id == t.id,
                    AssignmentSubmission.student_id == enr.student_id,
                )
            )).scalar_one_or_none()
            if sub and sub.submitted_at:
                entregadas += 1
        pct_t = round(entregadas * 100 / len(tareas), 1)
        requisitos.append({
            "key": "assignments",
            "label": "Tareas del módulo",
            "required": req.tareas_minimas,
            "actual": pct_t,
            "met": pct_t >= req.tareas_minimas,
            "detail": f"{entregadas} de {len(tareas)}",
            "measurable": True,
        })

    # ── 4. QUIZZES DEL MÓDULO ── (misma regla de audiencia)
    quizzes = []
    if hasattr(Quiz, "module_id"):
        quizzes = [q for q in await actividades_del_enrollment(
            db, Quiz, enr, "quiz", extra=Quiz.is_published.is_(True))
            if getattr(q, "module_id", None) == modulo.id]

    if quizzes:
        mejores = []
        for q in quizzes:
            intentos = (await db.execute(
                select(QuizAttempt).where(
                    QuizAttempt.quiz_id == q.id,
                    QuizAttempt.student_id == enr.student_id,
                    QuizAttempt.submitted_at.is_not(None),
                )
            )).scalars().all()
            if intentos:
                mejor = max((float(a.score) for a in intentos if a.score is not None),
                            default=None)
                if mejor is not None:
                    mejores.append(mejor)
        prom = round(sum(mejores) / len(mejores), 1) if mejores else None
        requisitos.append({
            "key": "quizzes",
            "label": "Quizzes del módulo",
            "required": req.promedio_quizzes_minimo,
            "actual": prom,
            "met": prom is not None and prom >= req.promedio_quizzes_minimo,
            "detail": f"{len(mejores)} de {len(quizzes)} hechos",
            "measurable": bool(mejores),
        })

    # ── EL VEREDICTO ──
    medibles = [r for r in requisitos if r.get("measurable")]
    cobertura = next((r for r in requisitos if r.get("is_coverage")), None)
    hay_actividad = bool(presentes or lecciones_hechas)

    if cobertura is None:
        # Sin lecciones publicadas no se sabe qué abarca el módulo. Se puede
        # decir que empezó, nunca que terminó.
        estado = "in_progress" if hay_actividad else "locked"
        motivo = ("Este módulo no tiene lecciones publicadas: no se puede "
                  "dar por completado" if hay_actividad
                  else "El módulo aún no ha empezado")
    elif not cobertura["met"]:
        estado = "in_progress" if hay_actividad else "locked"
        faltan_l = cobertura["required"] - cobertura["actual"]
        motivo = (f"Faltan {faltan_l} lección{'es' if faltan_l != 1 else ''} "
                  "por completar" if hay_actividad
                  else "El módulo aún no ha empezado")
    elif all(r["met"] for r in medibles):
        estado = "completed"
        motivo = "Contenido cubierto y requisitos cumplidos"
    else:
        estado = "in_progress"
        # Si el problema es que falta registrar asistencia, se dice así: no
        # es que el estudiante vaya mal, es que falta el dato.
        _sin_datos = [r for r in medibles if r.get("incomplete_data")]
        if _sin_datos:
            n = _sin_datos[0].get("sessions_missing", 0)
            motivo = (f"Faltan {n} clase{'s' if n != 1 else ''} por registrar "
                      "asistencia")
        else:
            faltan = [r["label"] for r in medibles if not r["met"]]
            motivo = "Falta: " + ", ".join(faltan)

    return {
        "module_id": modulo.id,
        "module_title": getattr(modulo, "title", None) or getattr(modulo, "name", ""),
        "status": estado,
        "reason": motivo,
        "requirements": requisitos,
        "measurable_count": len(medibles),
        "classes_in_module": len(del_modulo),
        "lessons_total": len(lecciones),
        "lessons_completed": lecciones_hechas,
    }


async def modulos_de_enrollment(db: AsyncSession, enr) -> dict:
    """Todos los módulos del nivel, con su estado real."""
    from app.models import Module

    modulos = (await db.execute(
        select(Module).where(Module.level_id == enr.level_id)
        .order_by(Module.order_index)
    )).scalars().all()

    detalle = [await estado_de_modulo(db, enr, m) for m in modulos]
    completados = sum(1 for d in detalle if d["status"] == "completed")

    return {
        "modules": detalle,
        "total": len(modulos),
        "completed": completados,
        # Sin módulos definidos el requisito no aplica: no se puede exigir
        # completar algo que el curso no tiene.
        "applies": len(modulos) > 0,
    }
