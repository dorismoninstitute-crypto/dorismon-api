"""V3.9.43 — SERVICIO CENTRAL DE AUDIENCIA.

QUÉ RESUELVE
============
Hasta ahora cada archivo decidía por su cuenta quién podía ver qué. `student.py`
pensaba una cosa, `video.py` otra, `admin.py` otra. Por eso el mismo error
reaparecía en pantallas distintas: a Juan le salía la clase de Marioli, a una
estudiante le salieron los quizzes de otro profesor, y el correo de recordatorio
llegaba a todo el nivel.

Este archivo es LA ÚNICA FUENTE DE VERDAD. La regla que decide si un estudiante
recibe una tarea es la MISMA que decide si aparece en su calendario, si le llega
la notificación, si puede abrirla y si puede entregarla.

LA REGLA
========
La identidad académica de un estudiante no es solo su nivel. Es:

    Estudiante → Curso → Nivel → GRUPO → Profesor

Un recurso (clase, tarea, quiz, material) llega a un estudiante si:

  1. Es SUYO en particular          → student_id coincide
  2. Es de SU GRUPO                 → series_id coincide
  3. Es de SU PROFESOR en su nivel  → teacher_id + level_id coinciden
  4. Es institucional del nivel     → sin dueño, para todo el nivel

Nunca basta con que coincida el nivel: dos profesores pueden dar B1 y sus
estudiantes NO deben mezclarse.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


# ============================================================================
# La identidad académica del estudiante
# ============================================================================

async def contexto_academico(db: AsyncSession, student_id: str) -> dict:
    """Todo lo que define qué puede ver este estudiante.

    Devuelve sus niveles, sus grupos y sus profesores. Es la base de todas
    las comprobaciones de este archivo.
    """
    from app.models import Enrollment

    filas = (await db.execute(
        select(Enrollment).where(
            Enrollment.student_id == student_id,
            Enrollment.is_active.is_(True),
        )
    )).scalars().all()

    return {
        "enrollments": filas,
        "level_ids": [e.level_id for e in filas if e.level_id],
        "course_ids": [e.course_id for e in filas if e.course_id],
        "series_ids": [e.series_id for e in filas if getattr(e, "series_id", None)],
        "teacher_ids": [e.teacher_id for e in filas if getattr(e, "teacher_id", None)],
        # Si no está en ningún grupo, ve el contenido "suelto" de su nivel
        "sin_grupo": not any(getattr(e, "series_id", None) for e in filas),
    }


# ============================================================================
# ¿Puede este estudiante acceder a este recurso?
# ============================================================================

async def puede_acceder_a_clase(db: AsyncSession, student_id: str, sesion) -> bool:
    """¿Le corresponde esta clase a este estudiante?

    Se usa TANTO para mostrarla en pantalla COMO para dejarlo entrar al video.
    Antes eran dos reglas distintas y por eso el video dejaba pasar a quien no
    debía.
    """
    from app.models import EventRegistration

    # 1. Clase privada, de prueba o reposición: es suya y punto
    if sesion.student_id:
        return sesion.student_id == student_id

    # 2. EVENTO ABIERTO.
    #
    # SEMÁNTICA ACTUAL, dicha sin adornos: `is_open_event = True` significa
    # "abierto a TODOS los estudiantes activos de Dorismon". Esta función NO
    # comprueba curso ni nivel para los eventos, y no lo pretende.
    #
    # La inscripción sirve para saber cuántos vienen y respetar el cupo, NO
    # para bloquear: antes, quien no se anotaba no entraba nunca aunque
    # llegara a tiempo y hubiera lugar. El cupo se verifica al registrarlo.
    #
    # SI EN EL FUTURO hace falta un evento solo para B1, para un curso o para
    # ciertos grupos, la forma de hacerlo sin rehacer nada es:
    #   - usar `session_audience` (ya existe) para grupos o estudiantes, o
    #   - añadir aquí una comprobación de level_id/course_id cuando el evento
    #     los tenga definidos
    # No se implementa todavía porque hoy todos los eventos son abiertos.
    if sesion.is_open_event:
        return True

    ctx = await contexto_academico(db, student_id)

    # 3. Clase de un grupo: solo si pertenece a ESE grupo
    if sesion.series_id:
        return sesion.series_id in ctx["series_ids"]

    # 4. Clase suelta con destinatarios explícitos
    destinatarios = await _destinatarios_explicitos(db, sesion.id)
    if destinatarios is not None:
        return student_id in destinatarios

    # 5. Clase suelta sin destinatarios: del profesor, en el nivel del alumno
    if sesion.teacher_id and sesion.teacher_id in ctx["teacher_ids"]:
        return sesion.level_id in ctx["level_ids"]

    return False


async def _destinatarios_explicitos(db: AsyncSession, session_id: str):
    """Los destinatarios que se le pusieron a mano a una clase suelta.

    Devuelve None si la clase no tiene destinatarios definidos (para poder
    distinguir "no tiene" de "tiene una lista vacía").
    """
    try:
        from app.models import SessionAudience
    except ImportError:
        return None

    filas = (await db.execute(
        select(SessionAudience.student_id).where(
            SessionAudience.session_id == session_id
        )
    )).all()
    if not filas:
        return None
    return {x for (x,) in filas}


async def _audiencia_explicita(db: AsyncSession, tipo: str, activity_id: int):
    """Las filas de audiencia ampliada de una actividad.

    Devuelve None si no tiene ninguna (para distinguir "no definida" de
    "definida y vacía").
    """
    try:
        from app.models import ActivityAudience
    except ImportError:
        return None

    filas = (await db.execute(
        select(ActivityAudience).where(
            ActivityAudience.activity_type == tipo,
            ActivityAudience.activity_id == activity_id,
        )
    )).scalars().all()
    if not filas:
        return None
    return {
        "series": {f.series_id for f in filas if f.series_id},
        "students": {f.student_id for f in filas if f.student_id},
    }


async def puede_acceder_a_tarea(db: AsyncSession, student_id: str, tarea,
                                tipo: str = "assignment") -> bool:
    """¿Le corresponde esta tarea a este estudiante?

    ORDEN DE PRECISIÓN (de lo más específico a lo más general):

      1. Audiencia ampliada  → varios grupos o estudiantes concretos
      2. series_id           → un grupo en particular
      3. teacher_id + nivel  → todos los del profesor en ese nivel
      4. institucional       → todos los del nivel

    EL HUECO QUE CIERRA EL PASO 2: antes se saltaba directo del 1 al 3, y por
    eso dos grupos del MISMO profesor en el MISMO nivel se veían el contenido
    entre sí. Carlos con B1 mañana y B1 noche: una tarea para uno le llegaba
    a los dos.
    """
    ctx = await contexto_academico(db, student_id)

    if tarea.level_id and tarea.level_id not in ctx["level_ids"]:
        return False

    # 1. Audiencia ampliada: si está definida, manda ella
    ampliada = await _audiencia_explicita(db, tipo, tarea.id)
    if ampliada is not None:
        if student_id in ampliada["students"]:
            return True
        return bool(ampliada["series"] & set(ctx["series_ids"]))

    # 2. Dirigida a UN grupo: solo los de ese grupo
    grupo = getattr(tarea, "series_id", None)
    if grupo:
        return grupo in ctx["series_ids"]

    # 3. Del profesor del estudiante, en su nivel
    if getattr(tarea, "teacher_id", None):
        if tarea.teacher_id in ctx["teacher_ids"]:
            return True
        # Si aún no tiene profesor asignado, se le deja ver el contenido de su
        # nivel para no dejarlo sin nada mientras se le asigna uno
        return not ctx["teacher_ids"]

    # 4. Institucional del nivel
    return True


async def puede_acceder_a_quiz(db: AsyncSession, student_id: str, quiz) -> bool:
    """¿Le corresponde este quiz? Misma regla que las tareas.

    Además: un quiz sin publicar no lo ve nadie.
    """
    if not getattr(quiz, "is_published", True):
        return False
    return await puede_acceder_a_tarea(db, student_id, quiz, tipo="quiz")


# ============================================================================
# Al revés: dado un recurso, ¿a quiénes les toca?
# ============================================================================

async def destinatarios_de_clase(db: AsyncSession, sesion) -> set[str]:
    """A QUIÉNES les corresponde esta clase.

    Se usa para los avisos (correo, campana, teléfono). Es la MISMA regla que
    decide qué ve el estudiante: si le aparece la clase, le llega el aviso; si
    no le aparece, no le llega.
    """
    from app.models import Enrollment, EventRegistration

    if sesion.student_id:
        return {sesion.student_id}

    if sesion.is_open_event:
        filas = (await db.execute(
            select(EventRegistration.student_id).where(
                EventRegistration.session_id == sesion.id,
                EventRegistration.cancelled_at.is_(None),
            )
        )).all()
        return {x for (x,) in filas}

    # Clase de un grupo → solo ese grupo
    if sesion.series_id:
        filas = (await db.execute(
            select(Enrollment.student_id).where(
                Enrollment.series_id == sesion.series_id,
                Enrollment.is_active.is_(True),
            )
        )).all()
        return {x for (x,) in filas}

    # Clase suelta con destinatarios explícitos
    explicitos = await _destinatarios_explicitos(db, sesion.id)
    if explicitos is not None:
        return explicitos

    # Clase suelta: los del nivel que tienen a ese profesor
    condiciones = [
        Enrollment.course_id == sesion.course_id,
        Enrollment.level_id == sesion.level_id,
        Enrollment.is_active.is_(True),
    ]
    if sesion.teacher_id:
        condiciones.append(Enrollment.teacher_id == sesion.teacher_id)
    filas = (await db.execute(select(Enrollment.student_id).where(*condiciones))).all()
    return {x for (x,) in filas}


async def destinatarios_de_actividad(db: AsyncSession, recurso) -> set[str]:
    """A quiénes les toca una tarea o un quiz.

    ANTES se avisaba a todos los inscritos del nivel, sin mirar el profesor.
    Por eso llegaban avisos de contenido ajeno.
    """
    from app.models import Enrollment

    condiciones = [
        Enrollment.level_id == recurso.level_id,
        Enrollment.is_active.is_(True),
    ]

    # V3.9.45 — Si va a un grupo, SOLO a ese grupo. Antes se avisaba a todos
    # los del profesor en ese nivel, así que el otro grupo también recibía el
    # aviso de una tarea que no le tocaba.
    grupo = getattr(recurso, "series_id", None)
    if grupo:
        condiciones.append(Enrollment.series_id == grupo)
    elif getattr(recurso, "teacher_id", None):
        condiciones.append(Enrollment.teacher_id == recurso.teacher_id)

    filas = (await db.execute(select(Enrollment.student_id).where(*condiciones))).all()
    return {x for (x,) in filas}


# ============================================================================
# Filtros para las consultas de listado
# ============================================================================

def filtro_clases_del_estudiante(ctx: dict, ClassSession, student_id: str):
    """Condición SQL para listar SOLO las clases de este estudiante."""
    from sqlalchemy import or_, and_, false

    opciones = [ClassSession.student_id == student_id]  # sus privadas

    if ctx["series_ids"]:
        opciones.append(and_(
            ClassSession.series_id.in_(ctx["series_ids"]),
            ClassSession.student_id.is_(None),
        ))

    # V3.9.44 — Clases sueltas dirigidas explícitamente a este estudiante.
    # Sin esto, una clase creada "para Juan y María" no le aparecía a ninguno.
    try:
        from app.models import SessionAudience
        opciones.append(and_(
            ClassSession.student_id.is_(None),
            ClassSession.id.in_(
                select(SessionAudience.session_id).where(
                    SessionAudience.student_id == student_id
                )
            ),
        ))
    except ImportError:
        pass

    # Clases sueltas de su profesor, en su nivel
    if ctx["teacher_ids"] and ctx["level_ids"]:
        try:
            from app.models import SessionAudience
            _con_audiencia = select(SessionAudience.session_id)
        except ImportError:
            _con_audiencia = None

        cond = [
            ClassSession.series_id.is_(None),
            ClassSession.student_id.is_(None),
            ClassSession.is_open_event.is_(False),
            ClassSession.teacher_id.in_(ctx["teacher_ids"]),
            ClassSession.level_id.in_(ctx["level_ids"]),
        ]
        # Si la clase tiene destinatarios definidos, ya entró por la rama de
        # arriba: aquí solo van las abiertas a todo el grupo del profesor.
        if _con_audiencia is not None:
            cond.append(~ClassSession.id.in_(_con_audiencia))
        opciones.append(and_(*cond))

    return or_(*opciones) if opciones else false()


def filtro_actividades_del_estudiante(ctx: dict, Modelo):
    """Condición SQL para listar tareas o quizzes de este estudiante.

    Aplica el MISMO orden de precisión que `puede_acceder_a_tarea`, para que
    lo que se lista sea exactamente lo que se puede abrir:

      - Si la actividad va a un grupo → solo si es SU grupo
      - Si no tiene grupo → del profesor del estudiante, en su nivel
      - Institucional → del nivel

    V3.9.45: antes faltaba la primera regla y por eso dos grupos del mismo
    profesor se veían el contenido entre sí.
    """
    from sqlalchemy import and_, or_, false

    if not ctx["level_ids"]:
        return false()

    base = Modelo.level_id.in_(ctx["level_ids"])

    # Actividades dirigidas a un grupo: solo las de SUS grupos
    if ctx["series_ids"]:
        de_su_grupo = Modelo.series_id.in_(ctx["series_ids"])
    else:
        de_su_grupo = false()

    # Actividades sin grupo (van a todos los del profesor en ese nivel)
    sin_grupo = Modelo.series_id.is_(None)
    if ctx["teacher_ids"]:
        sin_grupo = and_(
            sin_grupo,
            or_(
                Modelo.teacher_id.in_(ctx["teacher_ids"]),
                Modelo.teacher_id.is_(None),  # institucional
            ),
        )

    return and_(base, or_(de_su_grupo, sin_grupo))
