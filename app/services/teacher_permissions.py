"""V3.9.47 — PERMISOS DEL PROFESOR.

QUÉ RESUELVE
============
`audience.py` decide qué VE un estudiante. Este archivo decide qué puede
GESTIONAR un profesor.

El hueco que cierra: un profesor podía mandar por API el `series_id` de otro
profesor, o dirigir material privado a un estudiante ajeno, o publicar el
quiz de un colega. El frontend no le mostraba esos IDs, pero eso no es una
protección: basta con enviarlos.

REGLA GENERAL
=============
El admin puede todo. El profesor solo puede tocar lo que es académicamente
suyo: sus grupos, sus estudiantes, su contenido.
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def es_admin(db: AsyncSession, user_id: str) -> bool:
    from app.models import User, UserRole

    u = await db.get(User, user_id)
    return bool(u and u.role == UserRole.super_admin)


# ============================================================================
# GRUPOS
# ============================================================================

async def grupos_del_profesor(db: AsyncSession, teacher_id: str) -> set[str]:
    """Los grupos de los que este profesor es RESPONSABLE ACADÉMICO.

    ⚠️ V3.9.48 — CORRECCIÓN IMPORTANTE.

    Antes también contaba los grupos donde el profesor tuviera CUALQUIER
    clase. Eso convertía a un sustituto en dueño del grupo: si Andrea cubría
    UNA clase de Carlos, quedaba con derecho permanente a crear tareas,
    quizzes y materiales para todo B1 Mañana, y a tratar a sus estudiantes
    como propios. Para siempre.

    LA REGLA CORRECTA:

        Sustituir UNA sesión ≠ ser responsable del grupo.

    Andrea gestiona la SESIÓN que sustituye (entrar al video, pasar lista,
    finalizarla) porque eso se autoriza a nivel de ClassSession. Pero el
    grupo sigue siendo de Carlos.

    Si el cambio es PERMANENTE, se transfiere el grupo y entonces
    `ClassSeries.teacher_id` pasa a ser el nuevo responsable — que es lo que
    hace el endpoint de cambiar profesor de una serie.
    """
    from app.models import ClassSeries

    filas = (await db.execute(
        select(ClassSeries.id).where(ClassSeries.teacher_id == teacher_id)
    )).all()
    return {x for (x,) in filas}


async def puede_gestionar_grupo(db: AsyncSession, user_id: str, series_id: str) -> bool:
    """¿Puede este usuario dirigir contenido a este grupo?"""
    if not series_id:
        return True  # sin grupo = a todos sus estudiantes, no hace falta permiso
    if await es_admin(db, user_id):
        return True
    return series_id in await grupos_del_profesor(db, user_id)


async def exigir_grupo_propio(db: AsyncSession, user_id: str, series_id: str | None):
    """Corta la petición si el grupo no es suyo.

    Se usa al crear tareas, quizzes y materiales dirigidos a un grupo.
    """
    if not series_id:
        return
    if not await puede_gestionar_grupo(db, user_id, series_id):
        raise HTTPException(
            403,
            "Ese grupo no es tuyo. Solo puedes dirigir contenido a tus propios grupos.",
        )


# ============================================================================
# ESTUDIANTES
# ============================================================================

async def estudiantes_del_profesor(db: AsyncSession, teacher_id: str) -> set[str]:
    """Los estudiantes académicamente relacionados con este profesor.

    Son los inscritos con él, más los de sus grupos.
    """
    from app.models import Enrollment

    ids = set()

    filas = (await db.execute(
        select(Enrollment.student_id).where(
            Enrollment.teacher_id == teacher_id,
            Enrollment.is_active.is_(True),
        )
    )).all()
    ids |= {x for (x,) in filas}

    grupos = await grupos_del_profesor(db, teacher_id)
    if grupos:
        filas2 = (await db.execute(
            select(Enrollment.student_id).where(
                Enrollment.series_id.in_(grupos),
                Enrollment.is_active.is_(True),
            )
        )).all()
        ids |= {x for (x,) in filas2}

    return ids


async def exigir_estudiante_propio(db: AsyncSession, user_id: str, student_id: str | None):
    """Corta la petición si el estudiante no es suyo.

    Se usa al dirigir material individual: un profesor no debe poder mandarle
    material privado a un estudiante de otro.
    """
    if not student_id:
        return
    if await es_admin(db, user_id):
        return
    if student_id not in await estudiantes_del_profesor(db, user_id):
        raise HTTPException(
            403,
            "Ese estudiante no es tuyo. Solo puedes dirigir contenido a tus estudiantes.",
        )


# ============================================================================
# CONTENIDO (quiz, tarea, material)
# ============================================================================

async def puede_gestionar_actividad(db: AsyncSession, user_id: str, recurso) -> bool:
    """¿Puede este usuario modificar esta tarea o este quiz?

    Es suya si la creó, o si es de un grupo que ahora imparte (caso del
    profesor que recibe un grupo: puede seguir el trabajo sin borrar la
    autoría del anterior).
    """
    if await es_admin(db, user_id):
        return True

    if getattr(recurso, "teacher_id", None) == user_id:
        return True

    # Si la actividad es de un grupo, solo el responsable de ese grupo puede
    # gestionarla. Un sustituto de sesión no hereda las actividades.
    grupo = getattr(recurso, "series_id", None)
    if grupo:
        return grupo in await grupos_del_profesor(db, user_id)

    return False


async def exigir_actividad_propia(db: AsyncSession, user_id: str, recurso, nombre="recurso"):
    """Corta la petición si la actividad no es suya.

    Devuelve 404 y no 403: no hace falta confirmarle a nadie que ese ID
    existe.
    """
    if not await puede_gestionar_actividad(db, user_id, recurso):
        raise HTTPException(404, f"{nombre.capitalize()} no encontrado")


async def exigir_clase_propia(db: AsyncSession, user_id: str, sesion):
    """Corta la petición si la clase no es de este profesor."""
    if await es_admin(db, user_id):
        return
    if sesion.teacher_id != user_id:
        raise HTTPException(404, "Clase no encontrada")


# ============================================================================
# MATERIAL INSTITUCIONAL
# ============================================================================

async def resolver_audiencia_material(db: AsyncSession, user_id: str, body: dict) -> dict:
    """Decide la audiencia REAL de un material, sin confiar en lo que llega.

    REGLA: "institucional" significa contenido oficial de Dorismon. Solo el
    admin puede crearlo. Si un profesor lo pide, se guarda como material
    suyo — no se rechaza la subida, simplemente no se le concede un alcance
    que no le corresponde.
    """
    admin = await es_admin(db, user_id)

    pedido = (body.get("audience_kind") or "").strip()
    student_id = (body.get("student_id") or "").strip() or None
    series_id = (body.get("series_id") or "").strip() or None

    if student_id:
        tipo = "student"
        series_id = None
    elif pedido == "institutional":
        # V3.9.48 — Antes se convertía en silencio a "teacher": se respondía
        # 201 pero se guardaba otra audiencia. Ahora se dice claramente.
        if not admin:
            raise HTTPException(
                403,
                "Solo Dirección puede crear material institucional. "
                "Súbelo como material tuyo o pídeselo a Dirección.",
            )
        tipo = "institutional"
    elif pedido in ("teacher", "student"):
        tipo = pedido
    else:
        tipo = "institutional" if admin else "teacher"

    if tipo == "institutional":
        series_id = None
        student_id = None

    return {
        "audience_kind": tipo,
        "series_id": series_id,
        "student_id": student_id,
        # Ya no hay conversión silenciosa: si no puede, se corta arriba
    }


def filtro_materiales_del_profesor(Material, teacher_id: str, grupos: set[str]):
    """Qué materiales puede LISTAR un profesor.

    ANTES el listado traía todo: un profesor veía el material privado que
    otro había subido para un estudiante suyo, con su URL incluida.

    Ahora ve: lo que él subió · lo institucional · lo de sus grupos.
    """
    from sqlalchemy import or_, and_

    opciones = [
        Material.uploaded_by == teacher_id,  # lo suyo
        or_(  # lo institucional (incluye los históricos)
            Material.audience_kind == "institutional",
            Material.audience_kind.is_(None),
        ),
    ]
    if grupos:
        opciones.append(and_(
            Material.audience_kind == "teacher",
            Material.series_id.in_(grupos),
        ))
    return or_(*opciones)
