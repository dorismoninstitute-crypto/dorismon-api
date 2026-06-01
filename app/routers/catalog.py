"""Catálogo académico — cursos, niveles, módulos, lecciones (públicos para vistas)."""
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, CurrentUser
from app.core.db import get_db
from app.models import Course, Level, Module, Lesson, LessonProgress, Material

router = APIRouter(tags=["catalog"])


@router.get("/courses")
async def list_courses(db: AsyncSession = Depends(get_db)):
    courses = (await db.execute(
        select(Course).where(Course.is_active.is_(True)).order_by(Course.order_index)
    )).scalars().all()
    return [{
        "id": c.id, "code": c.code, "name": c.name,
        "description": c.description, "image_url": c.image_url,
        "color": c.color,
    } for c in courses]


@router.get("/courses/{course_id}")
async def get_course(course_id: int, db: AsyncSession = Depends(get_db)):
    course = await db.get(Course, course_id)
    if not course or not course.is_active:
        raise HTTPException(404, "Curso no encontrado")
    levels = (await db.execute(
        select(Level).where(Level.course_id == course_id).order_by(Level.order_index)
    )).scalars().all()
    return {
        "id": course.id, "code": course.code, "name": course.name,
        "description": course.description, "image_url": course.image_url,
        "color": course.color,
        "levels": [{
            "id": l.id, "code": l.code, "name": l.name,
            "description": l.description, "hours_required": l.hours_required,
        } for l in levels],
    }


@router.get("/levels/{level_id}/modules")
async def level_modules(level_id: int, db: AsyncSession = Depends(get_db)):
    modules = (await db.execute(
        select(Module).where(Module.level_id == level_id).order_by(Module.order_index)
    )).scalars().all()
    out = []
    for m in modules:
        lessons = (await db.execute(
            select(Lesson).where(Lesson.module_id == m.id, Lesson.is_published.is_(True))
            .order_by(Lesson.order_index)
        )).scalars().all()
        out.append({
            "id": m.id, "name": m.name, "description": m.description,
            "lessons": [{
                "id": l.id, "title": l.title, "duration_min": l.duration_min,
                "can_do": l.can_do, "has_video": bool(l.video_url),
                "has_pdf": bool(l.pdf_url), "has_audio": bool(l.audio_url),
            } for l in lessons],
        })
    return out


@router.get("/lessons/{lesson_id}")
async def get_lesson(
    lesson_id: int,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    lesson = await db.get(Lesson, lesson_id)
    if not lesson:
        raise HTTPException(404, "Lección no encontrada")
    module = await db.get(Module, lesson.module_id)
    level = await db.get(Level, module.level_id) if module else None
    course = await db.get(Course, level.course_id) if level else None

    progress = (await db.execute(
        select(LessonProgress).where(
            LessonProgress.student_id == user.user_id,
            LessonProgress.lesson_id == lesson_id,
        )
    )).scalar_one_or_none()

    materials = (await db.execute(
        select(Material).where(Material.lesson_id == lesson_id)
    )).scalars().all()

    return {
        "id": lesson.id, "title": lesson.title, "description": lesson.description,
        "objectives": lesson.objectives, "can_do": lesson.can_do,
        "duration_min": lesson.duration_min,
        "video_url": lesson.video_url, "pdf_url": lesson.pdf_url, "audio_url": lesson.audio_url,
        "module": {"id": module.id, "name": module.name} if module else None,
        "level": {"id": level.id, "code": level.code, "name": level.name} if level else None,
        "course": {"id": course.id, "name": course.name, "color": course.color} if course else None,
        "progress": {
            "is_completed": progress.is_completed if progress else False,
            "progress_pct": progress.progress_pct if progress else 0,
        },
        "materials": [{
            "id": m.id, "title": m.title, "type": m.type.value, "url": m.url,
        } for m in materials],
    }
