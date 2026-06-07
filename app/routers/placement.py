"""Placement test — diseño preparado para futuras mejoras (adaptativo, audio, IA)."""
from typing import Annotated
from datetime import datetime, timezone as tz
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, CurrentUser
from app.core.db import get_db
from app.models import (
    Student, User, Level, PlacementTest, PlacementQuestion, PlacementAnswer,
    Notification, NotificationType,
)

router = APIRouter(prefix="/placement", tags=["placement"])


@router.get("/status")
async def placement_status(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """¿El estudiante ya hizo el placement?"""
    if user.role != "student":
        return {"required": False, "completed": True}
    student = await db.get(Student, user.user_id)
    if not student:
        return {"required": True, "completed": False}
    return {
        "required": True,
        "completed": student.placement_done,
        "suggested_level_id": student.current_level_id,
    }


@router.get("/questions")
async def get_placement_questions(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Devuelve las preguntas del placement (sin la respuesta correcta).
    Diseño preparado para adaptativo: se podría devolver una a una.
    """
    if user.role != "student":
        raise HTTPException(403, "Solo estudiantes")
    questions = (await db.execute(
        select(PlacementQuestion).where(PlacementQuestion.is_active.is_(True))
        .order_by(PlacementQuestion.order_index)
    )).scalars().all()
    return [{
        "id": q.id,
        "statement": q.statement,
        "option_a": q.option_a, "option_b": q.option_b,
        "option_c": q.option_c, "option_d": q.option_d,
        "skill": q.skill,
        "audio_url": q.audio_url, "image_url": q.image_url,
    } for q in questions]


@router.post("/submit")
async def submit_placement(
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Recibe respuestas y calcula el nivel sugerido.

    body = {answers: [{question_id, selected_option}]}

    Algoritmo:
    - Cuenta correctas
    - 0-3 → A1, 4-6 → A2, 7-9 → B1, 10-12 → B2, 13-15 → C1
    - Asigna current_level_id del primer curso (default Inglés General)
    """
    if user.role != "student":
        raise HTTPException(403, "Solo estudiantes")
    student = await db.get(Student, user.user_id)
    if not student:
        raise HTTPException(404, "Perfil de estudiante no encontrado")
    if student.placement_done:
        raise HTTPException(400, "Ya completaste el placement test")

    answers = body.get("answers", [])
    if not isinstance(answers, list) or len(answers) == 0:
        raise HTTPException(400, "Debes enviar respuestas")

    # Crear test
    test = PlacementTest(student_id=user.user_id)
    db.add(test)
    await db.flush()

    correct_count = 0
    correct_by_level = {"A1": 0, "A2": 0, "B1": 0, "B2": 0, "C1": 0}
    total_by_level = {"A1": 0, "A2": 0, "B1": 0, "B2": 0, "C1": 0}
    correct_by_skill = {"grammar": 0, "vocabulary": 0, "reading": 0, "listening": 0}

    for ans in answers:
        qid = ans.get("question_id")
        sel = (ans.get("selected_option") or "").lower().strip()
        q = await db.get(PlacementQuestion, qid)
        if not q:
            continue
        is_correct = sel == q.correct_option.lower()
        if q.difficulty_level in total_by_level:
            total_by_level[q.difficulty_level] += 1
            if is_correct:
                correct_by_level[q.difficulty_level] += 1
        if is_correct:
            correct_count += 1
            if q.skill in correct_by_skill:
                correct_by_skill[q.skill] += 1
        db.add(PlacementAnswer(
            placement_test_id=test.id, question_id=qid,
            selected_option=sel, is_correct=is_correct,
        ))

    # Algoritmo de nivel sugerido
    total = len(answers)
    score_pct = (correct_count / total * 100) if total else 0
    if correct_count <= 3:
        suggested_code = "A1"
    elif correct_count <= 6:
        suggested_code = "A2"
    elif correct_count <= 9:
        suggested_code = "B1"
    elif correct_count <= 12:
        suggested_code = "B2"
    else:
        suggested_code = "C1"

    # Buscar el level_id del primer curso activo con ese código (default Inglés General)
    level = (await db.execute(
        select(Level).where(Level.code == suggested_code).order_by(Level.course_id).limit(1)
    )).scalar_one_or_none()

    # Guardar resultado en placement_tests
    test.grammar_score = round((correct_by_skill.get("grammar", 0) / max(1, total)) * 100, 2)
    test.reading_score = round((correct_by_skill.get("reading", 0) / max(1, total)) * 100, 2)
    test.listening_score = round((correct_by_skill.get("listening", 0) / max(1, total)) * 100, 2)
    test.writing_score = 0.0
    test.speaking_score = 0.0
    test.suggested_level_id = level.id if level else None
    test.completed_at = datetime.now(tz.utc)

    # Marcar el estudiante como con placement completo
    student.placement_done = True
    student.current_level_id = level.id if level else None
    student.grammar_score = test.grammar_score if hasattr(student, 'grammar_score') else None
    student.reading_score = test.reading_score
    student.listening_score = test.listening_score
    student.writing_score = 50.0  # default por ahora
    student.speaking_score = 50.0  # default por ahora

    # Notificación de bienvenida con nivel asignado
    db.add(Notification(
        user_id=user.user_id,
        type=NotificationType.info,
        title="🎯 Test de nivel completado",
        body=f"Tu nivel sugerido es {suggested_code}. Pronto te asignaremos un profesor.",
        link="/dashboard/student",
    ))

    await db.commit()

    return {
        "test_id": test.id,
        "correct_count": correct_count,
        "total_questions": total,
        "score_pct": round(score_pct, 2),
        "suggested_level_code": suggested_code,
        "suggested_level_id": level.id if level else None,
        "suggested_level_name": level.name if level else suggested_code,
        "skill_breakdown": {
            "grammar": test.grammar_score,
            "reading": test.reading_score,
            "listening": test.listening_score,
        },
        "level_breakdown": {
            lvl: {
                "correct": correct_by_level[lvl],
                "total": total_by_level[lvl],
            } for lvl in correct_by_level
        },
    }


@router.get("/my-result")
async def my_placement_result(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Resultado del placement del estudiante actual."""
    if user.role != "student":
        raise HTTPException(403)
    test = (await db.execute(
        select(PlacementTest).where(
            PlacementTest.student_id == user.user_id,
            PlacementTest.completed_at.is_not(None),
        ).order_by(PlacementTest.completed_at.desc()).limit(1)
    )).scalar_one_or_none()
    if not test:
        return None
    level = await db.get(Level, test.suggested_level_id) if test.suggested_level_id else None
    return {
        "test_id": test.id,
        "completed_at": test.completed_at.isoformat() if test.completed_at else None,
        "grammar_score": float(test.grammar_score) if test.grammar_score else 0,
        "reading_score": float(test.reading_score) if test.reading_score else 0,
        "listening_score": float(test.listening_score) if test.listening_score else 0,
        "suggested_level_code": level.code if level else None,
        "suggested_level_name": level.name if level else None,
    }
