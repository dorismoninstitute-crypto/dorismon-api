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
    """V1.4: Devuelve 15 preguntas aleatorias balanceadas por destreza,
    con orden de opciones mezclado para evitar trampas.

    V2.1.1: Bloquea si el email no está verificado (cuando Resend está configurado).

    Distribución:
    - 8 Grammar
    - 5 Reading Comprehension
    - 2 Use of English

    Por nivel de dificultad:
    - 2 A1, 2 A2, 4 B1, 4 B2, 3 C1 (curva de Cambridge style)
    """
    import random
    if user.role != "student":
        raise HTTPException(403, "Solo estudiantes")

    # V2.4: Verificación de email es OPCIONAL — el usuario puede hacer placement
    # sin verificar. La verificación se hace en segundo plano por banner suave.
    # NO bloqueamos al usuario para no perder conversión.

    # Obtener TODAS las preguntas activas agrupadas
    all_questions = (await db.execute(
        select(PlacementQuestion).where(PlacementQuestion.is_active.is_(True))
    )).scalars().all()

    # Agrupar por (skill, difficulty)
    by_skill_level = {}
    for q in all_questions:
        key = (q.skill, q.difficulty_level)
        by_skill_level.setdefault(key, []).append(q)

    # Plan de selección: cantidad por (skill, nivel)
    selection_plan = [
        # Grammar (8 total)
        ("grammar", "A1", 1), ("grammar", "A2", 1),
        ("grammar", "B1", 2), ("grammar", "B2", 2), ("grammar", "C1", 2),
        # Reading (5 total)
        ("reading", "A1", 1), ("reading", "A2", 1),
        ("reading", "B1", 1), ("reading", "B2", 1), ("reading", "C1", 1),
        # Use of English (2 total)
        ("use_of_english", "B1", 1), ("use_of_english", "B2", 1),
    ]

    selected = []
    for skill, level, count in selection_plan:
        bucket = by_skill_level.get((skill, level), [])
        if bucket:
            picks = random.sample(bucket, min(count, len(bucket)))
            selected.extend(picks)

    # Si quedaron menos de 15 (algún bucket vacío), rellenar con random del resto
    if len(selected) < 15:
        already_ids = {q.id for q in selected}
        remaining = [q for q in all_questions if q.id not in already_ids]
        fill_needed = min(15 - len(selected), len(remaining))
        selected.extend(random.sample(remaining, fill_needed))

    # V2.1.1: Ordenar de fácil a difícil (A1 → A2 → B1 → B2 → C1 → C2)
    LEVEL_ORDER = {"A1": 0, "A2": 1, "B1": 2, "B2": 3, "C1": 4, "C2": 5}
    selected.sort(key=lambda q: (LEVEL_ORDER.get(q.difficulty_level, 99), q.id))

    # Para cada pregunta, mezclar el orden de las opciones a/b/c/d
    out = []
    for q in selected:
        # Construir lista de [(letra original, texto)]
        options = [
            ("a", q.option_a), ("b", q.option_b),
            ("c", q.option_c), ("d", q.option_d),
        ]
        random.shuffle(options)
        # Re-mapear: la nueva posición es a, b, c, d
        # Guardamos la respuesta correcta DENTRO de la sesión usando un mapping
        # PERO no podemos guardar estado del lado servidor sin sesión,
        # así que devolvemos el mapping al cliente y al evaluar comparamos.
        # Mejor enfoque: devolver options ya en orden mezclado con letras nuevas,
        # y guardar correct_option mapeada a nueva letra.
        new_correct_letter = None
        new_options = {}
        for new_idx, (orig_letter, text) in enumerate(options):
            new_letter = ["a", "b", "c", "d"][new_idx]
            new_options[new_letter] = text
            if orig_letter == q.correct_option:
                new_correct_letter = new_letter

        # En lugar de exponer correct, mandamos un "answer_key" cifrado simple
        # Para simplificar, devolvemos el nuevo correct_option encriptado simple (base64 del id+letter)
        # Pero más simple aún: el frontend manda el orden que vio, y el backend re-mapea al evaluar.
        # → Devolvemos el orden de letras originales para que el frontend nos lo devuelva al submit.
        out.append({
            "id": q.id,
            "statement": q.statement,
            "option_a": new_options.get("a", ""),
            "option_b": new_options.get("b", ""),
            "option_c": new_options.get("c", ""),
            "option_d": new_options.get("d", ""),
            "skill": q.skill,
            "difficulty_level": q.difficulty_level,
            "audio_url": q.audio_url, "image_url": q.image_url,
            # mapping privado: nueva letra → letra original
            # Se devuelve al backend en el submit
            "_option_map": {
                "a": options[0][0], "b": options[1][0],
                "c": options[2][0], "d": options[3][0],
            },
        })
    return out


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
    correct_by_skill = {"grammar": 0, "reading": 0, "use_of_english": 0, "vocabulary": 0, "listening": 0}
    total_by_skill = {"grammar": 0, "reading": 0, "use_of_english": 0, "vocabulary": 0, "listening": 0}

    for ans in answers:
        qid = ans.get("question_id")
        sel = (ans.get("selected_option") or "").lower().strip()
        # V1.4: El frontend devuelve _option_map para que mapeemos la nueva letra
        # a la letra original que tiene la respuesta correcta en la DB.
        option_map = ans.get("option_map") or {}  # {"a":"c","b":"a","c":"d","d":"b"}
        q = await db.get(PlacementQuestion, qid)
        if not q:
            continue
        # Si hay option_map, traducir la letra seleccionada a la letra original
        if option_map and sel in option_map:
            sel_original = option_map[sel].lower()
        else:
            sel_original = sel
        is_correct = sel_original == q.correct_option.lower()
        if q.difficulty_level in total_by_level:
            total_by_level[q.difficulty_level] += 1
            if is_correct:
                correct_by_level[q.difficulty_level] += 1
        if q.skill in total_by_skill:
            total_by_skill[q.skill] += 1
        if is_correct:
            correct_count += 1
            if q.skill in correct_by_skill:
                correct_by_skill[q.skill] += 1
        db.add(PlacementAnswer(
            placement_test_id=test.id, question_id=qid,
            selected_option=sel_original, is_correct=is_correct,
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

    # V1.4: Guardar scores REALES por destreza (correct/total de esa skill, no del total)
    def pct(skill_key):
        t = total_by_skill.get(skill_key, 0)
        if t == 0: return None
        return round((correct_by_skill.get(skill_key, 0) / t) * 100, 2)

    test.grammar_score = pct("grammar")
    test.reading_score = pct("reading")
    # use_of_english se mezcla en vocabulary para el modelo
    use_eng_score = pct("use_of_english")
    test.listening_score = None  # V1.4: NO evaluamos (honestidad)
    test.writing_score = None
    test.speaking_score = None
    test.suggested_level_id = level.id if level else None
    test.completed_at = datetime.now(tz.utc)

    # Marcar el estudiante como con placement completo
    student.placement_done = True
    student.current_level_id = level.id if level else None
    student.grammar_score = test.grammar_score
    student.reading_score = test.reading_score
    student.listening_score = None
    student.writing_score = None
    student.speaking_score = None

    # Notificación al estudiante
    db.add(Notification(
        user_id=user.user_id,
        type=NotificationType.info,
        title="🎯 Test de nivel completado",
        body=f"Tu nivel sugerido es {suggested_code}. Pronto te asignaremos un profesor.",
        link="/dashboard/student",
    ))

    # V1.4: Notificación a TODOS los admins
    from app.models import User, UserRole
    admins = (await db.execute(
        select(User).where(User.role == UserRole.super_admin, User.is_active.is_(True))
    )).scalars().all()
    user_data = await db.get(User, user.user_id)
    student_name = user_data.full_name if user_data else "Un estudiante"
    for admin in admins:
        db.add(Notification(
            user_id=admin.id,
            type=NotificationType.info,
            title=f"🎯 Nuevo placement: {student_name}",
            body=f"Completó el test → nivel sugerido {suggested_code}. Pendiente de inscripción.",
            link="/dashboard/admin/placement-results",
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
            "use_of_english": use_eng_score,
            # listening / speaking / writing → NO evaluados, los muestra el front
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
    """Resultado del placement del estudiante.

    V1.4.1: Si no hay test pero hay nivel asignado, devuelve el nivel
    (caso: admin asignó nivel sin que el estudiante hiciera test).
    """
    if user.role != "student":
        raise HTTPException(403)
    test = (await db.execute(
        select(PlacementTest).where(
            PlacementTest.student_id == user.user_id,
            PlacementTest.completed_at.is_not(None),
        ).order_by(PlacementTest.completed_at.desc()).limit(1)
    )).scalar_one_or_none()

    # Caso 1: hay test completado
    if test:
        level = await db.get(Level, test.suggested_level_id) if test.suggested_level_id else None
        return {
            "completed": True,
            "has_test": True,
            "test_id": test.id,
            "completed_at": test.completed_at.isoformat() if test.completed_at else None,
            "grammar_score": float(test.grammar_score) if test.grammar_score is not None else None,
            "reading_score": float(test.reading_score) if test.reading_score is not None else None,
            "listening_score": None,
            "writing_score": None,
            "speaking_score": None,
            "score_pct": None,  # no se guarda en el modelo, lo calculamos si quieres
            "correct_count": None, "total_questions": None,
            "suggested_level_code": level.code if level else None,
            "suggested_level_name": level.name if level else None,
        }

    # Caso 2: no hay test pero hay nivel asignado por admin
    st = await db.get(Student, user.user_id)
    level = None
    if st and st.current_level_id:
        level = await db.get(Level, st.current_level_id)
    else:
        # V1.4.1 fallback: buscar el nivel desde una inscripción activa
        from app.models import Enrollment
        active_enr = (await db.execute(
            select(Enrollment).where(
                Enrollment.student_id == user.user_id,
                Enrollment.is_active.is_(True),
            ).order_by(Enrollment.enrolled_at.desc()).limit(1)
        )).scalar_one_or_none()
        if active_enr:
            level = await db.get(Level, active_enr.level_id)

    if level:
        return {
            "completed": True,
            "has_test": False,
            "assigned_by_admin": True,
            "suggested_level_code": level.code,
            "suggested_level_name": level.name,
            "completed_at": None,
            "grammar_score": None, "reading_score": None,
            "listening_score": None, "writing_score": None, "speaking_score": None,
        }

    # Caso 3: no hay test ni nivel
    return {"completed": False, "has_test": False}
