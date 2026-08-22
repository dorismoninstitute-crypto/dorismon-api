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
        # ══ V3.9.69 — LA COMBINACIÓN, NO LAS LISTAS SUELTAS ══
        #
        # Las listas de arriba se comparaban por separado, y eso abría un
        # hueco real en cuanto un alumno tenía DOS matrículas:
        #
        #   Enrollment 1: A2 con Luis
        #   Enrollment 2: B1 con Ana
        #   -> level_ids = [A2, B1] ; teacher_ids = [Luis, Ana]
        #
        # Una clase suelta de B1 con LUIS pasaba el filtro: B1 está en sus
        # niveles y Luis está en sus profesores. Pero él nunca ha tenido a
        # Luis en B1 — esa combinación no existe en ninguna matrícula suya.
        #
        # `combos` guarda las ternas REALES (curso, nivel, profesor). Una
        # clase suelta solo es suya si UNA matrícula las satisface las tres a
        # la vez.
        "combos": [
            (e.course_id, e.level_id, getattr(e, "teacher_id", None))
            for e in filas
            if e.course_id and e.level_id and getattr(e, "teacher_id", None)
        ],
        # V3.9.51 — necesario para resolver la audiencia explícita en SQL
        "_student_id": student_id,
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

    # 5. Clase suelta sin destinatarios: tiene que coincidir la TERNA
    #    completa (curso + nivel + profesor) con UNA matrícula suya.
    #    V3.9.69 — Antes se comparaban profesor y nivel por separado, así que
    #    con dos matrículas se colaban combinaciones que él nunca tuvo.
    if sesion.teacher_id:
        return (sesion.course_id, sesion.level_id, sesion.teacher_id) in ctx["combos"]

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

    # Clase suelta: los del nivel que tienen a ese profesor.
    # V3.9.69 — La terna COMPLETA, para que quien la recibe sea exactamente
    # quien puede verla y entrar. Si `destinatarios_de_clase` y
    # `puede_acceder_a_clase` usaran reglas distintas, el roster y el acceso
    # se contradirían: gente en la lista del profesor sin poder entrar, o al
    # revés.
    condiciones = [
        Enrollment.course_id == sesion.course_id,
        Enrollment.level_id == sesion.level_id,
        Enrollment.is_active.is_(True),
    ]
    if sesion.teacher_id:
        condiciones.append(Enrollment.teacher_id == sesion.teacher_id)
    else:
        # Sin profesor no hay terna posible: nadie la recibe por esta vía.
        return set()
    filas = (await db.execute(select(Enrollment.student_id).where(*condiciones))).all()
    return {x for (x,) in filas}


def tiene_entrada_online(sesion) -> bool:
    """¿Esta clase ofrece entrada por videollamada?

    ⚠️ V3.9.67 — REGLA ÚNICA PARA TODO EL BACKEND.

    Desde v3.9.65 una sesión suelta puede ser una EXCEPCIÓN presencial dentro
    de una serie virtual, y CONSERVA a propósito su `meeting_url` y su
    `video_provider` heredados: si mañana vuelve a virtual, la videollamada
    regresa sola.

    El riesgo de conservarlos es que alguien los use igualmente, y pasó: se
    corrigieron las tres pantallas, pero el enlace seguía saliendo por otros
    caminos —el correo de recordatorio de 24h, el .ics, el link de Google
    Calendar y la propia sala de video—. La pantalla decía "Presencial" y el
    correo del día anterior decía "Entrar a la clase".

    Esta función existe para que la regla viva en UN solo sitio:

        presencial -> NUNCA entrada online. Se va al aula.
        online     -> entrada online.
        hibrida    -> entrada online, además de la sede.

    Tener `meeting_url` NO basta. La modalidad manda.
    """
    if sesion is None:
        return False
    modalidad = getattr(sesion, "modality", None)
    valor = getattr(modalidad, "value", modalidad)
    if valor == "presencial":
        return False
    return bool(getattr(sesion, "meeting_url", None)) or \
        getattr(sesion, "video_provider", None) == "dorismon"


async def destinatarios_de_serie(db: AsyncSession, series_id: str) -> set[str]:
    """A QUIÉNES les corresponde una serie completa (el grupo real).

    ⚠️ V3.9.62 — POR QUÉ EXISTE ESTA FUNCIÓN.

    Cuando se reprogramaba una serie o se le cambiaba el profesor, el aviso
    se mandaba a todo el que tuviera ese `course_id + level_id`. Es decir:
    cambiabas el horario de B1 Mañana y le llegaba también a B1 Noche, que
    no se enteró de nada porque su horario no cambió.

    La regla correcta es la MISMA que ya usa `destinatarios_de_clase` para
    una sesión de grupo: pertenece al grupo quien tiene esa matrícula, no
    quien comparte nivel.

    Devuelve un `set` vacío si el grupo no tiene a nadie matriculado todavía.
    Eso es un resultado legítimo, no un error: una serie recién creada aún
    no tiene estudiantes y no hay a quién avisar.
    """
    from app.models import Enrollment

    if not series_id:
        return set()

    filas = (await db.execute(
        select(Enrollment.student_id).where(
            Enrollment.series_id == series_id,
            Enrollment.is_active.is_(True),
        )
    )).all()
    return {x for (x,) in filas}


async def destinatarios_de_actividad(db: AsyncSession, recurso,
                                     tipo: str = "assignment") -> set[str]:
    """A quiénes les toca una tarea o un quiz.

    ⚠️ V3.9.51 — MISMO ORDEN DE PRECISIÓN que `puede_acceder_a_tarea`.

    Antes esta función NO miraba `ActivityAudience`, así que una tarea
    dirigida a estudiantes concretos o a varios grupos se le notificaba (y se
    contaba en el seguimiento) a gente que no podía ni abrirla. Dos reglas
    distintas para la misma pregunta.

    El orden es el mismo, siempre:
      1. ActivityAudience explícita → varios grupos o estudiantes concretos
      2. series_id                  → un grupo
      3. teacher_id + level_id      → los del profesor en ese nivel
      4. institucional              → todos los del nivel
    """
    from app.models import Enrollment

    # 1. Audiencia ampliada: si está definida, manda ella
    ampliada = await _audiencia_explicita(db, tipo, recurso.id)
    if ampliada is not None:
        destinatarios = set(ampliada["students"])
        if ampliada["series"]:
            filas = (await db.execute(
                select(Enrollment.student_id).where(
                    Enrollment.series_id.in_(ampliada["series"]),
                    Enrollment.is_active.is_(True),
                )
            )).all()
            destinatarios |= {x for (x,) in filas}
        return destinatarios

    condiciones = [
        Enrollment.level_id == recurso.level_id,
        Enrollment.is_active.is_(True),
    ]

    # 2. Un grupo concreto
    grupo = getattr(recurso, "series_id", None)
    if grupo:
        condiciones.append(Enrollment.series_id == grupo)
    # 3. Del profesor, en ese nivel
    elif getattr(recurso, "teacher_id", None):
        condiciones.append(Enrollment.teacher_id == recurso.teacher_id)
    # 4. Institucional: todos los del nivel

    filas = (await db.execute(select(Enrollment.student_id).where(*condiciones))).all()
    return {x for (x,) in filas}


async def actividades_del_estudiante(db: AsyncSession, Modelo, student_id: str,
                                     tipo: str = "assignment", extra=None) -> list:
    """Las tareas (o quizzes) que le tocan a ESTE estudiante.

    V3.9.51 — Helper central para que `tracking.py` no vuelva a decidir
    audiencia por su cuenta. Antes AT_RISK repetía la lógica a mano con
    level_id/series_id/teacher_id, y eso es exactamente cómo aparecen dos
    fuentes de verdad que se desincronizan.
    """
    from sqlalchemy import and_

    ctx = await contexto_academico(db, student_id)
    cond = [filtro_actividades_del_estudiante(ctx, Modelo)]
    if extra is not None:
        cond.append(extra)

    candidatas = (await db.execute(select(Modelo).where(and_(*cond)))).scalars().all()

    # Las que tienen audiencia ampliada se comprueban una a una: el filtro SQL
    # no puede expresar "solo estos estudiantes" sin complicar la consulta.
    salida = []
    for a in candidatas:
        ampliada = await _audiencia_explicita(db, tipo, a.id)
        if ampliada is None:
            salida.append(a)
        elif student_id in ampliada["students"] or (
                ampliada["series"] & set(ctx["series_ids"])):
            salida.append(a)

    # Y las dirigidas explícitamente a él, que el filtro SQL no alcanza
    ids = {a.id for a in salida}
    try:
        from app.models import ActivityAudience
        extra_ids = {
            x for (x,) in (await db.execute(
                select(ActivityAudience.activity_id).where(
                    ActivityAudience.activity_type == tipo,
                    ActivityAudience.student_id == student_id,
                )
            )).all()
        }
        faltan = extra_ids - ids
        if faltan:
            cond2 = [Modelo.id.in_(faltan)]
            if extra is not None:
                cond2.append(extra)
            salida += (await db.execute(
                select(Modelo).where(and_(*cond2))
            )).scalars().all()
    except ImportError:
        pass

    return salida


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

    # Clases sueltas de su profesor, en su nivel — por TERNA COMPLETA.
    # V3.9.69: antes esto cruzaba `teacher_ids` con `level_ids` por separado.
    if ctx.get("combos"):
        try:
            from app.models import SessionAudience
            _con_audiencia = select(SessionAudience.session_id)
        except ImportError:
            _con_audiencia = None

        # Una rama OR por cada matrícula real: (curso Y nivel Y profesor).
        _ternas = or_(*[
            and_(
                ClassSession.course_id == _c,
                ClassSession.level_id == _l,
                ClassSession.teacher_id == _t,
            )
            for (_c, _l, _t) in ctx["combos"]
        ])
        cond = [
            ClassSession.series_id.is_(None),
            ClassSession.student_id.is_(None),
            ClassSession.is_open_event.is_(False),
            _ternas,
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
    from sqlalchemy import and_, or_, false, true

    if not ctx["level_ids"]:
        return false()

    base = Modelo.level_id.in_(ctx["level_ids"])

    # V3.9.51 — Las actividades con AUDIENCIA EXPLÍCITA se excluyen de las
    # reglas generales: si alguien definió a quién va, manda esa definición.
    #
    # Antes el listado no lo miraba (solo el acceso individual), así que una
    # tarea dirigida a estudiantes concretos le APARECÍA a los demás aunque
    # no pudieran abrirla. Dos reglas para la misma pregunta.
    tipo_act = "quiz" if getattr(Modelo, "__tablename__", "") == "quizzes" else "assignment"
    try:
        from app.models import ActivityAudience
        con_audiencia = select(ActivityAudience.activity_id).where(
            ActivityAudience.activity_type == tipo_act
        )
        sin_audiencia_explicita = ~Modelo.id.in_(con_audiencia)

        # Las que SÍ tienen audiencia y le corresponden a él
        mias_explicitas = Modelo.id.in_(
            select(ActivityAudience.activity_id).where(
                ActivityAudience.activity_type == tipo_act,
                or_(
                    ActivityAudience.student_id == ctx.get("_student_id"),
                    ActivityAudience.series_id.in_(ctx["series_ids"])
                    if ctx["series_ids"] else false(),
                ),
            )
        )
    except ImportError:
        sin_audiencia_explicita = true()
        mias_explicitas = false()

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

    # Reglas generales: solo para las que NO tienen audiencia explícita
    generales = and_(sin_audiencia_explicita, or_(de_su_grupo, sin_grupo))
    return and_(base, or_(generales, mias_explicitas))


# ============================================================================
# V3.9.46 P1 — MATERIALES
# ============================================================================

def filtro_materiales_del_estudiante(ctx: dict, Material, student_id: str):
    """Qué materiales puede ver este estudiante.

    Tres tipos de audiencia:

      INSTITUCIONAL → de Dorismon, para todos / un curso / un nivel.
                      Es como se comportaban TODOS los materiales antes, y es
                      lo que quedan siendo los existentes.
      DEL PROFESOR  → si tiene `series_id`, solo ese grupo; si no, todos los
                      estudiantes de ese profesor.
      INDIVIDUAL    → solo el estudiante indicado (feedback, refuerzo).
    """
    from sqlalchemy import and_, or_, false

    opciones = []

    # 1. Institucional: público y del curso/nivel del estudiante (o general)
    inst = [Material.is_public.is_(True)]
    if ctx["level_ids"]:
        inst.append(or_(
            Material.level_id.is_(None),
            Material.level_id.in_(ctx["level_ids"]),
        ))
    else:
        inst.append(Material.level_id.is_(None))
    opciones.append(and_(
        or_(Material.audience_kind == "institutional",
            Material.audience_kind.is_(None)),  # históricos
        *inst,
    ))

    # 2. Del profesor: de su grupo, o de su profesor sin grupo definido
    if ctx["series_ids"]:
        opciones.append(and_(
            Material.audience_kind == "teacher",
            Material.series_id.in_(ctx["series_ids"]),
        ))
    if ctx["teacher_ids"]:
        opciones.append(and_(
            Material.audience_kind == "teacher",
            Material.series_id.is_(None),
            Material.uploaded_by.in_(ctx["teacher_ids"]),
        ))

    # 3. Individual: solo suyo
    opciones.append(and_(
        Material.audience_kind == "student",
        Material.student_id == student_id,
    ))

    return or_(*opciones) if opciones else false()


async def puede_acceder_a_material(db: AsyncSession, student_id: str, material) -> bool:
    """¿Puede este estudiante abrir este material?

    Se usa al descargar por ID: no basta con que no aparezca en el listado.
    """
    tipo = getattr(material, "audience_kind", None) or "institutional"
    ctx = await contexto_academico(db, student_id)

    if tipo == "student":
        return material.student_id == student_id

    if tipo == "teacher":
        grupo = getattr(material, "series_id", None)
        if grupo:
            return grupo in ctx["series_ids"]
        return material.uploaded_by in ctx["teacher_ids"]

    # Institucional (incluye los históricos)
    if not material.is_public:
        return False
    if material.level_id and material.level_id not in ctx["level_ids"]:
        return False
    return True


# ============================================================================
# V3.9.52 — ÁMBITO DE UNA MATRÍCULA
# ============================================================================
#
# Dorismon es una academia de idiomas: Juan puede llevar English B1 con Carlos
# y Spanish A2 con Andrea al mismo tiempo. Todo lo académico —tareas,
# quizzes, asistencia, actividad— pertenece a UNA matrícula, no a la persona.
#
# Sin esto, las señales de un curso contaminan al otro: Juan aparece "en
# riesgo" en inglés por faltas que en realidad tuvo en español.

def _ctx_de_enrollment(enr) -> dict:
    """El contexto académico de UNA matrícula, con la forma que esperan los
    filtros existentes. Así se reutiliza la misma regla sin duplicarla."""
    return {
        "enrollments": [enr],
        "level_ids": [enr.level_id] if enr.level_id else [],
        "course_ids": [enr.course_id] if enr.course_id else [],
        "series_ids": ([enr.series_id] if getattr(enr, "series_id", None) else []),
        "teacher_ids": ([enr.teacher_id] if getattr(enr, "teacher_id", None) else []),
        "sin_grupo": not getattr(enr, "series_id", None),
        "_student_id": enr.student_id,
    }


async def actividades_del_enrollment(db: AsyncSession, Modelo, enr,
                                     tipo: str = "assignment", extra=None) -> list:
    """Las tareas (o quizzes) que le tocan a ESTA matrícula.

    Respeta curso, nivel, grupo, profesor responsable y `ActivityAudience`.
    Es la misma regla que usa el acceso individual: si no puede abrirla,
    tampoco cuenta para su riesgo.
    """
    from sqlalchemy import and_, or_

    ctx = _ctx_de_enrollment(enr)
    if not ctx["level_ids"]:
        return []

    cond = [filtro_actividades_del_estudiante(ctx, Modelo)]
    if enr.course_id and hasattr(Modelo, "course_id"):
        cond.append(or_(Modelo.course_id == enr.course_id,
                        Modelo.course_id.is_(None)))
    if extra is not None:
        cond.append(extra)

    candidatas = (await db.execute(select(Modelo).where(and_(*cond)))).scalars().all()

    # Las de audiencia ampliada se comprueban una a una
    salida = []
    for a in candidatas:
        ampliada = await _audiencia_explicita(db, tipo, a.id)
        if ampliada is None:
            salida.append(a)
        elif enr.student_id in ampliada["students"] or (
                ampliada["series"] & set(ctx["series_ids"])):
            salida.append(a)
    return salida


async def sesiones_del_enrollment(db: AsyncSession, enr, desde=None, hasta=None,
                                  limite: int = 10) -> list:
    """Las clases que le correspondían a ESTA matrícula.

    EXCLUYE los eventos abiertos: faltar al Conversation Club no es faltar a
    clase, y no debe contar para el riesgo académico.
    """
    from sqlalchemy import and_, desc, or_
    from app.models import ClassSession

    cond = [
        ClassSession.course_id == enr.course_id,
        ClassSession.level_id == enr.level_id,
        # Los eventos opcionales no cuentan como clase
        ClassSession.is_open_event.is_(False),
    ]

    grupo = getattr(enr, "series_id", None)
    if grupo:
        # De su grupo, o privadas suyas
        cond.append(or_(
            ClassSession.series_id == grupo,
            ClassSession.student_id == enr.student_id,
        ))
    elif getattr(enr, "teacher_id", None):
        cond.append(or_(
            and_(ClassSession.series_id.is_(None),
                 ClassSession.teacher_id == enr.teacher_id),
            ClassSession.student_id == enr.student_id,
        ))
    else:
        cond.append(ClassSession.student_id == enr.student_id)

    if desde is not None:
        cond.append(ClassSession.starts_at_utc >= desde)
    if hasta is not None:
        cond.append(ClassSession.starts_at_utc <= hasta)

    q = select(ClassSession).where(and_(*cond)).order_by(
        desc(ClassSession.starts_at_utc))
    if limite:
        q = q.limit(limite)
    return (await db.execute(q)).scalars().all()
