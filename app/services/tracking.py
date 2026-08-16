"""V3.9.49 P2 — SEGUIMIENTO ACADÉMICO.

QUÉ RESUELVE
============
Hasta ahora el sistema sabía lo que PASÓ (quién entregó, quién asistió) pero
no lo que NO pasó. Y en enseñanza, lo que no pasa es justamente lo que hay
que atender:

  · Quién NO entregó
  · Quién NO hizo el quiz
  · Quién agotó los intentos sin aprobar
  · Quién está dejando de venir

PRINCIPIO
=========
El roster de una actividad se calcula SIEMPRE con `audience.py`. Si una tarea
va al grupo de la mañana, el seguimiento muestra a los de la mañana — no a
todo el nivel. Nunca se cuenta a quien no le tocaba.
"""
from __future__ import annotations

from datetime import datetime, timezone as tz, timedelta

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession


# ============================================================================
# ESTADOS DE UNA ACTIVIDAD
# ============================================================================
#
# Se CALCULAN a partir de las marcas de tiempo; no se guardan. Así nunca hay
# un estado guardado que contradiga los hechos.

# Estados de QUIZ. Se separan de los de tarea porque son hechos distintos:
# una tarea se califica a mano, un quiz se aprueba o no.
ESTADOS_QUIZ = {
    "assigned":     "No lo ha intentado",
    "started":      "Empezado, sin enviar",
    "passed":       "Aprobado",
    "retry":        "No aprobó — le quedan intentos",
    "needs_review": "Necesita refuerzo",
    "overdue":      "Venció sin hacerlo",
}

ESTADOS = {
    "assigned":      "Asignada",
    "viewed":        "La vio",
    "in_progress":   "Empezada",
    "submitted":     "Entregada",
    "graded":        "Calificada",
    "overdue":       "Atrasada",
    "needs_review":  "Necesita refuerzo",
}


def estado_de_entrega(sub, vence_at=None, ahora=None) -> str:
    """En qué punto está la tarea de un estudiante.

    ORDEN: lo definitivo primero (calificada, entregada), luego lo que está
    en curso, y al final lo que falta.
    """
    ahora = ahora or datetime.now(tz.utc)

    if sub is None:
        # Ni siquiera abrió la tarea
        if vence_at and _aware(vence_at) < ahora:
            return "overdue"
        return "assigned"

    if sub.graded_at:
        return "graded"
    if sub.submitted_at:
        return "submitted"

    # No entregó: ¿se le pasó la fecha?
    if vence_at and _aware(vence_at) < ahora:
        return "overdue"

    if getattr(sub, "started_at", None):
        return "in_progress"
    if getattr(sub, "viewed_at", None):
        return "viewed"
    return "assigned"


def _aware(dt):
    """Las fechas sin zona se tratan como UTC, que es como se guardan."""
    if dt and dt.tzinfo is None:
        return dt.replace(tzinfo=tz.utc)
    return dt


# ============================================================================
# SEGUIMIENTO DE UNA TAREA
# ============================================================================

async def seguimiento_de_tarea(db: AsyncSession, tarea) -> dict:
    """Quién entregó y quién no, sobre el roster REAL de la tarea.

    El roster sale de `audience.py`: si la tarea va a un grupo, solo cuenta a
    ese grupo. Antes se listaban solo las entregas que existían, así que
    quien no entregó simplemente no aparecía.
    """
    from app.models import AssignmentSubmission, User
    from app.services.audience import destinatarios_de_actividad

    ahora = datetime.now(tz.utc)
    roster = await destinatarios_de_actividad(db, tarea)
    if not roster:
        return {"items": [], "resumen": _resumen_vacio()}

    entregas = {
        s.student_id: s
        for s in (await db.execute(
            select(AssignmentSubmission).where(
                AssignmentSubmission.assignment_id == tarea.id,
                AssignmentSubmission.student_id.in_(roster),
            )
        )).scalars().all()
    }

    usuarios = {
        u.id: u
        for u in (await db.execute(select(User).where(User.id.in_(roster)))).scalars().all()
    }

    items = []
    for sid in roster:
        u = usuarios.get(sid)
        if not u:
            continue
        sub = entregas.get(sid)
        estado = estado_de_entrega(sub, tarea.due_at, ahora)
        items.append({
            "student_id": sid,
            "name": u.full_name,
            "email": u.email,
            "estado": estado,
            "estado_label": ESTADOS.get(estado, estado),
            "submitted_at": _iso(sub.submitted_at) if sub else None,
            "graded_at": _iso(sub.graded_at) if sub else None,
            "score": float(sub.score) if (sub and sub.score is not None) else None,
            "max_score": float(tarea.max_score) if getattr(tarea, "max_score", None) else 100.0,
            "has_file": bool(sub and sub.file_url),
            "submission_id": sub.id if sub else None,
        })

    # Primero lo que requiere acción del profesor, después lo resuelto
    orden = {"submitted": 0, "overdue": 1, "in_progress": 2,
             "viewed": 3, "assigned": 4, "graded": 5}
    items.sort(key=lambda x: (orden.get(x["estado"], 9), x["name"]))

    return {"items": items, "resumen": _contar(items)}


def _resumen_vacio():
    return {"total": 0, **{k: 0 for k in ESTADOS}, "sin_entregar": 0,
            "pendientes_calificar": 0, "promedio": None}


def _contar(items) -> dict:
    r = {"total": len(items), **{k: 0 for k in ESTADOS}}
    notas = []
    for x in items:
        r[x["estado"]] = r.get(x["estado"], 0) + 1
        if x.get("score") is not None:
            notas.append(x["score"])
    r["sin_entregar"] = r["assigned"] + r["viewed"] + r["in_progress"] + r["overdue"]
    r["pendientes_calificar"] = r["submitted"]
    r["promedio"] = round(sum(notas) / len(notas), 1) if notas else None
    return r


def _iso(dt):
    return dt.isoformat() if dt else None


# ============================================================================
# SEGUIMIENTO DE UN QUIZ
# ============================================================================

async def seguimiento_de_quiz(db: AsyncSession, quiz) -> dict:
    """Quién lo hizo, con qué nota, y quién no lo ha intentado.

    Antes solo se contaban los intentos: no había forma de saber quién no lo
    había hecho, ni quién agotó los intentos sin aprobar.
    """
    from app.models import QuizAttempt, User, QuizAttemptGrant
    from app.services.audience import destinatarios_de_actividad

    roster = await destinatarios_de_actividad(db, quiz, tipo="quiz")
    if not roster:
        return {"items": [], "resumen": {"total": 0}}

    # V3.9.50 — Se traen TODOS los intentos, incluidos los que se empezaron y
    # no se enviaron. Antes solo se miraban los enviados, así que un quiz
    # empezado y abandonado se veía igual que uno nunca abierto.
    enviados: dict[str, list] = {}
    en_curso: dict[str, list] = {}
    for a in (await db.execute(
        select(QuizAttempt).where(
            QuizAttempt.quiz_id == quiz.id,
            QuizAttempt.student_id.in_(roster),
        ).order_by(QuizAttempt.started_at)
    )).scalars().all():
        if a.submitted_at:
            enviados.setdefault(a.student_id, []).append(a)
        else:
            en_curso.setdefault(a.student_id, []).append(a)
    intentos = enviados

    # Intentos extra concedidos individualmente
    extras: dict[str, int] = {}
    for g in (await db.execute(
        select(QuizAttemptGrant).where(
            QuizAttemptGrant.quiz_id == quiz.id,
            QuizAttemptGrant.revoked.is_(False),
        )
    )).scalars().all():
        extras[g.student_id] = extras.get(g.student_id, 0) + int(g.extra_attempts or 0)

    usuarios = {
        u.id: u
        for u in (await db.execute(select(User).where(User.id.in_(roster)))).scalars().all()
    }

    base = int(quiz.max_attempts or 3)
    minimo = float(quiz.passing_score or 60)

    items = []
    for sid in roster:
        u = usuarios.get(sid)
        if not u:
            continue
        mios = intentos.get(sid, [])
        permitidos = base + extras.get(sid, 0)
        usados = len(mios)
        mejor = max((float(a.score) for a in mios if a.score is not None), default=None)
        aprobo = any(a.passed for a in mios) if mios else False

        # V3.9.50 — Estados propios del quiz. Cada uno responde a un hecho:
        #
        #   assigned      → no hay ningún intento
        #   started       → hay un intento abierto sin enviar
        #   passed        → algún intento aprobado
        #   needs_review  → gastó todos los intentos sin aprobar
        #   retry         → falló pero todavía le quedan intentos
        #
        # Ya NO se usa "graded" para el quiz: aquí no hay nada que calificar
        # a mano, se aprueba o no se aprueba.
        abierto = bool(en_curso.get(sid))
        if not mios and not abierto:
            estado = "assigned"
        elif aprobo:
            estado = "passed"
        elif not mios and abierto:
            estado = "started"
        elif usados >= permitidos:
            estado = "needs_review"
        else:
            estado = "retry"

        items.append({
            "student_id": sid,
            "name": u.full_name,
            "estado": estado,
            "estado_label": ESTADOS_QUIZ.get(estado, estado),
            "attempts_used": usados,
            "attempts_allowed": permitidos,
            "extra_granted": extras.get(sid, 0),
            "best_score": mejor,
            "passed": aprobo,
            "passing_score": minimo,
            "last_attempt_at": _iso(mios[-1].submitted_at) if mios else None,
            "in_progress": abierto,
        })

    orden = {"needs_review": 0, "retry": 1, "started": 2, "assigned": 3, "passed": 4}
    items.sort(key=lambda x: (orden.get(x["estado"], 9), x["name"]))

    hechos = [x for x in items if x["attempts_used"] > 0]
    notas = [x["best_score"] for x in hechos if x["best_score"] is not None]

    # V3.9.51 — El resumen se cuenta POR ESTADO, y los estados son
    # excluyentes: cada estudiante cae en uno y solo uno.
    #
    # Antes `sin_intentar` usaba `attempts_used == 0`, pero un intento
    # empezado y no enviado también tiene 0 envíos: el mismo estudiante
    # aparecía a la vez como "empezado sin enviar" y como "sin intentar", y
    # los totales no cuadraban.
    por_estado = {}
    for x in items:
        por_estado[x["estado"]] = por_estado.get(x["estado"], 0) + 1

    return {
        "items": items,
        "resumen": {
            "total": len(items),
            "sin_intentar": por_estado.get("assigned", 0),
            "empezados_sin_enviar": por_estado.get("started", 0),
            "aprobados": por_estado.get("passed", 0),
            "con_intentos_restantes": por_estado.get("retry", 0),
            "necesitan_refuerzo": por_estado.get("needs_review", 0),
            "promedio": round(sum(notas) / len(notas), 1) if notas else None,
            # Los cinco estados suman exactamente el total
            "cuadra": (por_estado.get("assigned", 0)
                       + por_estado.get("started", 0)
                       + por_estado.get("passed", 0)
                       + por_estado.get("retry", 0)
                       + por_estado.get("needs_review", 0)) == len(items),
        },
    }


# ============================================================================
# ESTUDIANTES EN RIESGO
# ============================================================================
#
# ⚠️ REGLAS EXPLÍCITAS, no ocultas en el código.
#
# Un estudiante entra en riesgo si cumple AL MENOS UNA. Cada señal se reporta
# aparte, para que Dirección vea el motivo y no solo una etiqueta.
#
# Estos números son el punto de partida: se ajustan aquí, en un solo lugar.

RIESGO_AUSENCIAS_SEGUIDAS = 2      # faltas consecutivas
RIESGO_AUSENCIAS_DE_10 = 3         # faltas en sus últimas 10 clases
RIESGO_TAREAS_SIN_ENTREGAR = 2     # tareas vencidas sin entregar
RIESGO_QUIZ_AGOTADO = 1            # quizzes con intentos agotados sin aprobar
RIESGO_PROMEDIO_BAJO = 60.0        # promedio de calificaciones
RIESGO_DIAS_SIN_ACTIVIDAD = 14     # días sin entregar ni asistir


async def estudiantes_en_riesgo(db: AsyncSession, teacher_id: str | None = None) -> dict:
    """Quiénes necesitan atención, con el MOTIVO de cada uno.

    ⚠️ V3.9.51 — SE CALCULA POR MATRÍCULA, no por persona.

    Dorismon es una academia de idiomas: Juan puede estar en English B1 y en
    Spanish A2 a la vez. Antes se analizaba una sola matrícula por persona,
    así que **si iba bien en inglés, su problema en español quedaba oculto**.

    Ahora cada matrícula activa se evalúa por separado y trae su curso,
    nivel, grupo y profesor. El panel puede agrupar por estudiante si quiere,
    pero el dato no se pierde.

    Si se indica `teacher_id`, solo las matrículas de sus estudiantes.
    """
    from app.models import (
        Enrollment, User, Student, SessionAttendance, ClassSession,
        AttendanceState, Assignment, AssignmentSubmission, Level,
        Course, ClassSeries,
    )
    from sqlalchemy import and_
    from app.services.teacher_permissions import estudiantes_del_profesor

    ahora = datetime.now(tz.utc)

    cond = [Enrollment.is_active.is_(True)]
    if teacher_id:
        # V3.9.52 PRIVACIDAD — Se filtra por MATRÍCULA, no por estudiante.
        #
        # Antes: si Juan llevaba English con Carlos y Spanish con Andrea,
        # bastaba con que Juan fuera "estudiante de Carlos" para que Carlos
        # recibiera TAMBIÉN los datos de Spanish. Eso es información de otra
        # profesora sobre otro curso.
        #
        # Ahora Carlos ve solo las matrículas de las que es responsable:
        # las suyas por `teacher_id`, o las de sus grupos. Un sustituto de
        # una sesión no entra (ver `grupos_del_profesor`); una transferencia
        # permanente sí, porque cambia el titular de la serie.
        from app.services.teacher_permissions import es_admin, grupos_del_profesor
        if not await es_admin(db, teacher_id):
            _mis_grupos = await grupos_del_profesor(db, teacher_id)
            _propias = [Enrollment.teacher_id == teacher_id]
            if _mis_grupos:
                _propias.append(Enrollment.series_id.in_(_mis_grupos))
            cond.append(or_(*_propias))

    filas = (await db.execute(
        select(Enrollment, User, Level)
        .join(User, Enrollment.student_id == User.id)
        .join(Level, Enrollment.level_id == Level.id)
        .where(*cond)
    )).all()

    # ── V3.9.50 — PRECARGA para no hacer N consultas por estudiante ──
    #
    # Antes se consultaban quizzes e intentos dentro del bucle. Con 200
    # estudiantes eso eran cientos de consultas. Ahora se traen de una vez y
    # se agrupan en memoria.
    #
    # Deuda documentada: asistencia y tareas siguen consultándose por
    # estudiante (necesitan las 10 últimas de cada uno, que no se resuelve
    # bien en una sola consulta portable). Con los volúmenes actuales de
    # Dorismon es correcto; si se pasa de ~500 estudiantes activos habrá que
    # pasar este cálculo a un proceso nocturno.
    from app.models import Quiz, QuizAttempt, QuizAttemptGrant

    _niveles = {e.level_id for e, _u, _l in filas if e.level_id}
    quizzes_nivel: dict = {}
    intentos_por_quiz: dict = {}
    extras_quiz: dict = {}

    if _niveles:
        _qs = (await db.execute(
            select(Quiz).where(
                Quiz.level_id.in_(_niveles),
                Quiz.is_published.is_(True),
            )
        )).scalars().all()
        for q in _qs:
            quizzes_nivel.setdefault(q.level_id, []).append(q)

        _qids = [q.id for q in _qs]
        if _qids:
            for a in (await db.execute(
                select(QuizAttempt).where(
                    QuizAttempt.quiz_id.in_(_qids),
                    QuizAttempt.submitted_at.is_not(None),
                )
            )).scalars().all():
                intentos_por_quiz.setdefault((a.quiz_id, a.student_id), []).append(a)

            for g in (await db.execute(
                select(QuizAttemptGrant).where(
                    QuizAttemptGrant.quiz_id.in_(_qids),
                    QuizAttemptGrant.revoked.is_(False),
                )
            )).scalars().all():
                k = (g.quiz_id, g.student_id)
                extras_quiz[k] = extras_quiz.get(k, 0) + int(g.extra_attempts or 0)

    resultado = []

    # V3.9.51 — Sin `vistos`: cada matrícula se analiza. Antes se saltaba la
    # segunda matrícula del mismo estudiante y su riesgo quedaba invisible.
    for e, u, nivel in filas:
        st = await db.get(Student, u.id)
        if st and st.is_paused:
            continue  # en pausa a propósito: no es abandono

        señales = []

        # --- Asistencia DE ESTA MATRÍCULA ---
        #
        # V3.9.52 — Antes se miraban las últimas 10 asistencias del ESTUDIANTE,
        # mezclando cursos: las faltas de Spanish marcaban a English en riesgo.
        # Ahora solo cuentan las clases que pertenecían a esta matrícula, y
        # los eventos opcionales quedan fuera (faltar al Conversation Club no
        # es faltar a clase).
        from app.services.audience import (
            actividades_del_enrollment, sesiones_del_enrollment,
        )
        _sesiones = await sesiones_del_enrollment(db, e, limite=10)
        _sids = [s.id for s in _sesiones]

        asistencias = []
        if _sids:
            _marcas = {
                a.session_id: a
                for a in (await db.execute(
                    select(SessionAttendance).where(
                        SessionAttendance.student_id == u.id,
                        SessionAttendance.session_id.in_(_sids),
                        SessionAttendance.state.is_not(None),
                    )
                )).scalars().all()
            }
            # En el mismo orden de las clases: de la más reciente hacia atrás
            asistencias = [(_marcas[s.id], s) for s in _sesiones if s.id in _marcas]

        # V3.9.50 — "Consecutivas" significa ABSENT seguido de ABSENT.
        # Cualquier otra cosa rompe la cadena: si vino (present), llegó tarde
        # (late) o justificó (excused), no es una racha de faltas.
        # Antes solo `present` la rompía, así que ABSENT-EXCUSED-ABSENT
        # contaba como 2 seguidas, que no es cierto.
        seguidas = 0
        for a, _s in asistencias:
            if a.state == AttendanceState.absent:
                seguidas += 1
            else:
                break
        faltas = sum(1 for a, _s in asistencias if a.state == AttendanceState.absent)

        if seguidas >= RIESGO_AUSENCIAS_SEGUIDAS:
            señales.append({
                "tipo": "ausencias_seguidas", "valor": seguidas,
                "texto": f"{seguidas} ausencias seguidas",
            })
        elif faltas >= RIESGO_AUSENCIAS_DE_10:
            señales.append({
                "tipo": "ausencias", "valor": faltas,
                "texto": f"{faltas} faltas en sus últimas {len(asistencias)} clases",
            })

        # --- Tareas vencidas sin entregar ---
        #
        # V3.9.51 — Se usa el helper CENTRAL. Antes se replicaba aquí la
        # lógica de audiencia (level_id/series_id/teacher_id) y eso era una
        # segunda fuente de verdad: si cambiaba la regla en audience.py, este
        # cálculo quedaba desincronizado sin que nadie lo notara.
        # V3.9.52 — Solo las tareas de ESTA matrícula (curso, nivel, grupo,
        # profesor y audiencia explícita). Antes se tomaban las del
        # estudiante en general y una tarea de otro curso podía marcarlo.
        tareas = await actividades_del_enrollment(
            db, Assignment, e, "assignment",
            extra=and_(Assignment.due_at.is_not(None), Assignment.due_at < ahora),
        )

        sin_entregar = 0
        notas = []
        for t in tareas:
            sub = (await db.execute(
                select(AssignmentSubmission).where(
                    AssignmentSubmission.assignment_id == t.id,
                    AssignmentSubmission.student_id == u.id,
                )
            )).scalar_one_or_none()
            if not sub or not sub.submitted_at:
                sin_entregar += 1
            elif sub.score is not None:
                notas.append(float(sub.score))

        if sin_entregar >= RIESGO_TAREAS_SIN_ENTREGAR:
            señales.append({
                "tipo": "tareas", "valor": sin_entregar,
                "texto": f"{sin_entregar} tareas vencidas sin entregar",
            })

        # --- Quizzes agotados sin aprobar ---
        # V3.9.51 — También por el helper central, no a mano.
        quizzes_agotados = 0
        # V3.9.52 — Igual para los quizzes: uno reprobado en Spanish no puede
        # marcar English como riesgo.
        _mis_quizzes = await actividades_del_enrollment(
            db, Quiz, e, "quiz", extra=Quiz.is_published.is_(True),
        )
        for q in _mis_quizzes:
            mios = intentos_por_quiz.get((q.id, u.id), [])
            if not mios:
                continue
            if any(a.passed for a in mios):
                continue
            permitidos = int(q.max_attempts or 3) + extras_quiz.get((q.id, u.id), 0)
            if len(mios) >= permitidos:
                quizzes_agotados += 1

        if quizzes_agotados >= RIESGO_QUIZ_AGOTADO:
            señales.append({
                "tipo": "quiz_reprobado", "valor": quizzes_agotados,
                "texto": (f"Agotó los intentos de {quizzes_agotados} quiz"
                          f"{'zes' if quizzes_agotados != 1 else ''} sin aprobar"),
            })

        promedio = round(sum(notas) / len(notas), 1) if notas else None
        if promedio is not None and promedio < RIESGO_PROMEDIO_BAJO:
            señales.append({
                "tipo": "promedio", "valor": promedio,
                "texto": f"Promedio de {promedio} (mínimo {RIESGO_PROMEDIO_BAJO:.0f})",
            })

        # --- Sin actividad ---
        ultima = None
        if asistencias:
            pres = [s.starts_at_utc for a, s in asistencias
                    if a.state == AttendanceState.present]
            if pres:
                ultima = _aware(max(pres))
        # V3.9.52 — La actividad de una matrícula NO resetea la otra. Si Juan
        # entregó ayer en Spanish pero lleva 20 días sin tocar English,
        # English debe seguir marcando inactividad.
        _ids_tareas = [t.id for t in await actividades_del_enrollment(
            db, Assignment, e, "assignment")]
        ult_entrega = None
        if _ids_tareas:
            ult_entrega = (await db.execute(
                select(func.max(AssignmentSubmission.submitted_at)).where(
                    AssignmentSubmission.student_id == u.id,
                    AssignmentSubmission.assignment_id.in_(_ids_tareas),
                )
            )).scalar()
        if ult_entrega:
            ult_entrega = _aware(ult_entrega)
            ultima = max(ultima, ult_entrega) if ultima else ult_entrega

        if ultima:
            dias = (ahora - ultima).days
            if dias >= RIESGO_DIAS_SIN_ACTIVIDAD:
                señales.append({
                    "tipo": "inactividad", "valor": dias,
                    "texto": f"{dias} días sin actividad",
                })

        if not señales:
            continue

        profe = await db.get(User, e.teacher_id) if getattr(e, "teacher_id", None) else None
        curso = await db.get(Course, e.course_id) if e.course_id else None
        grupo_obj = (await db.get(ClassSeries, e.series_id)
                     if getattr(e, "series_id", None) else None)
        resultado.append({
            "student_id": u.id,
            "name": u.full_name,
            "email": u.email,
            "phone": u.phone,
            # V3.9.51 — Cada fila es una MATRÍCULA, no una persona
            "enrollment_id": e.id,
            "course_id": e.course_id,
            "course_name": curso.name if curso else None,
            "level_id": e.level_id,
            "level_code": nivel.code,
            "group_id": getattr(e, "series_id", None),
            "group_name": grupo_obj.name if grupo_obj else None,
            "teacher_id": getattr(e, "teacher_id", None),
            "teacher_name": profe.full_name if profe else None,
            "señales": señales,
            "motivo_principal": señales[0]["texto"],
            "gravedad": len(señales),
            "promedio": promedio,
        })

    resultado.sort(key=lambda x: -x["gravedad"])
    return {"items": resultado, "count": len(resultado), "reglas": _reglas()}


def _reglas() -> dict:
    """Las reglas visibles, para que se sepa por qué alguien está en riesgo."""
    return {
        "ausencias_seguidas": RIESGO_AUSENCIAS_SEGUIDAS,
        "ausencias_de_10": RIESGO_AUSENCIAS_DE_10,
        "tareas_sin_entregar": RIESGO_TAREAS_SIN_ENTREGAR,
        "promedio_minimo": RIESGO_PROMEDIO_BAJO,
        "dias_sin_actividad": RIESGO_DIAS_SIN_ACTIVIDAD,
        "explicacion": (
            "Un estudiante aparece en riesgo si cumple al menos una señal. "
            "Los pausados a propósito no se cuentan."
        ),
    }
