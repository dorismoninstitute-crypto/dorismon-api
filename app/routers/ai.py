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


@router.post("/quiz/create")
async def crear_quiz_directo(
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.33 — Genera el quiz Y LO GUARDA, listo para revisar y publicar.

    ANTES había que generar, copiar y armarlo a mano en otra pantalla — medio
    trabajo. Ahora el profesor genera, revisa y publica en un toque.

    Se guarda SIN publicar: nadie lo ve hasta que el profesor lo apruebe.
    """
    from app.models import Quiz, QuizQuestion, QuestionType

    u = await _solo_staff(user, db)
    datos = body.get("quiz")
    if not datos or not datos.get("questions"):
        raise HTTPException(400, "Falta el quiz generado")

    level_id = body.get("level_id")
    if not level_id:
        raise HTTPException(400, "Indica a qué nivel pertenece el quiz")

    # El quiz queda a nombre de quien lo crea (si es admin, del profe indicado)
    teacher_id = body.get("teacher_id") or user.user_id

    q = Quiz(
        title=str(datos.get("title") or "Quiz")[:150],
        description=str(datos.get("description") or "")[:400],
        level_id=int(level_id),
        teacher_id=teacher_id,
        series_id=(body.get("series_id") or None),  # V3.9.45
        is_published=False,  # el profesor lo revisa antes de publicarlo
    )
    db.add(q)
    await db.flush()

    guardadas = 0
    for i, pregunta in enumerate(datos["questions"][:20]):
        opciones = pregunta.get("options") or []
        idx = pregunta.get("correct_index")
        if len(opciones) != 4 or not isinstance(idx, int) or not (0 <= idx < 4):
            continue
        db.add(QuizQuestion(
            quiz_id=q.id,
            type=QuestionType.multiple_choice,
            statement=str(pregunta.get("text") or "")[:500],
            options=[str(o)[:200] for o in opciones],
            correct_answer=str(opciones[idx])[:200],
            points=10.0,
            order_index=i,
        ))
        guardadas += 1

    if guardadas == 0:
        raise HTTPException(400, "Ninguna pregunta era válida. Genera de nuevo.")

    await db.commit()
    return {
        "ok": True, "quiz_id": q.id, "title": q.title,
        "questions": guardadas, "published": False,
    }
