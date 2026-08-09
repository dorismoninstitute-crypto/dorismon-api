"""V3.9.31 — Generar contenido con IA. El admin/profesor revisa antes de publicar."""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, CurrentUser
from app.core.db import get_db
from app.models import User, UserRole
from app.services.ai_service import (
    ai_ready, generar_quiz, generar_leccion, generar_tarea,
)

router = APIRouter(prefix="/ai", tags=["ai"])

NIVELES = {"A1", "A2", "B1", "B2", "C1", "C2"}


async def _solo_staff(user: CurrentUser, db: AsyncSession) -> User:
    """Solo el admin y los profesores generan contenido."""
    u = await db.get(User, user.user_id)
    if not u or u.role not in (UserRole.super_admin, UserRole.teacher):
        raise HTTPException(403, "Solo el equipo puede generar contenido")
    return u


@router.get("/status")
async def ai_status(user: Annotated[CurrentUser, Depends(get_current_user)]):
    """¿Está lista la IA? Lo usa el panel para mostrar u ocultar la sección."""
    return {"ready": ai_ready()}


def _validar(body: dict) -> tuple[str, str]:
    tema = (body.get("topic") or "").strip()
    nivel = (body.get("level") or "").strip().upper()
    if not tema:
        raise HTTPException(400, "Escribe sobre qué tema quieres el contenido")
    if len(tema) > 150:
        raise HTTPException(400, "El tema es muy largo")
    if nivel not in NIVELES:
        raise HTTPException(400, "Elige un nivel válido (A1 a C2)")
    return tema, nivel


@router.post("/quiz")
async def crear_quiz(
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Propone un quiz completo. NO lo publica: lo devuelve para revisar."""
    await _solo_staff(user, db)
    tema, nivel = _validar(body)
    cantidad = body.get("count", 10)
    try:
        cantidad = max(3, min(20, int(cantidad)))
    except (TypeError, ValueError):
        cantidad = 10
    try:
        return await generar_quiz(tema, nivel, cantidad)
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/lesson")
async def crear_leccion(
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Propone una lección de gramática para revisar y publicar."""
    await _solo_staff(user, db)
    tema, nivel = _validar(body)
    try:
        return await generar_leccion(tema, nivel)
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@router.post("/assignment")
async def crear_tarea(
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Propone una tarea para revisar y publicar."""
    await _solo_staff(user, db)
    tema, nivel = _validar(body)
    try:
        return await generar_tarea(tema, nivel)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
