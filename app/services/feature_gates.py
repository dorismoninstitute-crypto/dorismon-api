"""V2.9 — Feature Gates por Plan.

Lógica central para verificar si un estudiante tiene acceso a una funcionalidad
según el plan al que está inscrito.
"""
from datetime import datetime, timezone as tz
from typing import Iterable
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Enrollment, PlanFeature, User, UserRole


# Feature keys oficiales — TODAS las funcionalidades gateables
FEATURE_KEYS = {
    "grupal_classes",       # Acceso a clases grupales
    "private_classes",      # Acceso a clases privadas 1-a-1
    "library_basic",        # Biblioteca básica de lecciones
    "library_full",         # Biblioteca completa
    "assignments",          # Tareas con feedback
    "quizzes",              # Quizzes evaluativos
    "materials_premium",    # Materiales descargables premium
    "certificates",         # Certificados oficiales
    "events_view",          # Ver eventos del instituto
    "events_free",          # Asistir gratis a eventos
    "priority_support",     # Soporte prioritario
    "course_route",         # Ruta curricular personalizada
    "placement_test",       # Test de nivel CEFR
}


async def get_student_feature_keys(db: AsyncSession, user_id: str) -> set[str]:
    """Devuelve el set de feature_keys que tiene el estudiante en base a sus enrollments activos.

    Un estudiante puede tener varios planes activos a la vez (ej: Académico + Privadas).
    Las features se UNEN (si cualquier plan la incluye, el estudiante la tiene).
    """
    # 1. Buscar enrollments activos
    stmt = select(Enrollment).where(
        Enrollment.student_id == user_id,
        Enrollment.is_active.is_(True),
    )
    enrollments = (await db.execute(stmt)).scalars().all()

    if not enrollments:
        return set()

    # 2. Para cada plan, traer sus features incluidas
    feature_keys: set[str] = set()
    plan_ids = {e.plan_id for e in enrollments if e.plan_id}

    if not plan_ids:
        return feature_keys

    stmt = select(PlanFeature).where(
        PlanFeature.plan_id.in_(plan_ids),
        PlanFeature.is_included.is_(True),
        PlanFeature.feature_key.isnot(None),
    )
    rows = (await db.execute(stmt)).scalars().all()
    for r in rows:
        if r.feature_key:
            feature_keys.add(r.feature_key)

    return feature_keys


async def student_has_feature(db: AsyncSession, user_id: str, feature_key: str) -> bool:
    """Verifica si un estudiante tiene acceso a una feature específica."""
    keys = await get_student_feature_keys(db, user_id)
    return feature_key in keys


async def user_has_feature(db: AsyncSession, user_id: str, feature_key: str) -> bool:
    """Versión universal: profes y admins SIEMPRE tienen todas las features.
    Solo aplica el gate a estudiantes.
    """
    u = await db.get(User, user_id)
    if not u:
        return False
    # Admin y profes ven todo
    if u.role in (UserRole.super_admin, UserRole.teacher):
        return True
    # Estudiantes: chequear plan
    return await student_has_feature(db, user_id, feature_key)


# ============= DEPENDENCIA FASTAPI =============

from fastapi import Depends, HTTPException
from app.routers.auth import CurrentUser, get_current_user
from app.core.db import get_db


def require_feature(feature_key: str):
    """V2.9: Dependencia FastAPI para proteger endpoints por feature.

    Uso:
        @router.get("/private-classes")
        async def list_private(
            current: Annotated[CurrentUser, Depends(get_current_user)],
            _: None = Depends(require_feature("private_classes")),
        ): ...

    Profes y admins SIEMPRE pasan.
    Estudiantes solo si su plan incluye esa feature.
    """
    async def check(
        current: CurrentUser = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ):
        u = await db.get(User, current.user_id)
        if not u:
            raise HTTPException(401, "No autenticado")
        if u.role in (UserRole.super_admin, UserRole.teacher):
            return None
        # Estudiante: verificar
        if await student_has_feature(db, current.user_id, feature_key):
            return None
        raise HTTPException(
            status_code=403,
            detail={
                "error": "feature_not_in_plan",
                "feature_key": feature_key,
                "message": "Esta funcionalidad no está incluida en tu plan actual.",
            },
        )
    return check
