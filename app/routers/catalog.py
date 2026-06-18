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


# V2.5 — Endpoint público para obtener configuración del instituto (logo, nombre, etc.)
@router.get("/institute-settings")
async def get_institute_settings_public(db: AsyncSession = Depends(get_db)):
    """V2.5: Settings públicos del instituto (logo, nombre, colores, contacto).
    Accesible sin auth — usado en landing, login, register, footer, etc.
    """
    from app.models import InstituteSetting
    s = await db.get(InstituteSetting, 1)
    if not s:
        # Defaults si no hay settings configurados
        return {
            "name": "Dorismon Language Institute",
            "logo_url": None,
            "primary_color": "#4361ee",
            "accent_color": "#f4622a",
            "contact_email": None,
            "contact_phone": None,
            "address": None,
        }
    return {
        "name": s.name,
        "logo_url": s.logo_url,
        "primary_color": s.primary_color,
        "accent_color": s.accent_color,
        "contact_email": s.contact_email,
        "contact_phone": s.contact_phone,
        "address": s.address,
    }


# V2.8 — Servir el logo del instituto como icono PWA dinámico
@router.get("/institute-icon/{size}")
async def get_institute_icon(size: int, db: AsyncSession = Depends(get_db)):
    """V2.8: Devuelve el logo del instituto como PNG cuadrado del tamaño pedido.

    Si admin subió logo en settings → lo usa como icono PWA.
    Si no → devuelve un PNG con la "D" azul (fallback).
    """
    from fastapi import Response
    from app.models import InstituteSetting

    # Validar tamaño
    if size not in (72, 96, 128, 144, 152, 180, 192, 384, 512):
        raise HTTPException(400, "Tamaño no soportado")

    s = await db.get(InstituteSetting, 1)
    logo_url = s.logo_url if s and s.logo_url else None

    if logo_url and logo_url.startswith("data:image"):
        # Logo subido en base64 → procesar con Pillow para hacer icono cuadrado
        try:
            import base64
            from io import BytesIO
            from PIL import Image

            # Extraer base64
            _, b64 = logo_url.split(",", 1)
            img_data = base64.b64decode(b64)
            img = Image.open(BytesIO(img_data)).convert("RGBA")

            # Hacer canvas blanco cuadrado del tamaño pedido
            canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
            inner_size = int(size * 0.86)

            # Escalar logo manteniendo proporción
            ratio = img.size[0] / img.size[1]
            if ratio > 1:
                new_w = inner_size
                new_h = int(inner_size / ratio)
            else:
                new_h = inner_size
                new_w = int(inner_size * ratio)

            resized = img.resize((new_w, new_h), Image.LANCZOS)
            x = (size - new_w) // 2
            y = (size - new_h) // 2
            canvas.paste(resized, (x, y), resized if resized.mode == "RGBA" else None)

            output = BytesIO()
            canvas.save(output, "PNG", optimize=True)
            return Response(content=output.getvalue(), media_type="image/png",
                           headers={"Cache-Control": "public, max-age=3600"})
        except Exception as e:
            pass

    # Fallback: devolver icono genérico (D azul)
    # Generar simple en runtime
    try:
        from io import BytesIO
        from PIL import Image, ImageDraw, ImageFont
        import os

        canvas = Image.new("RGBA", (size, size), (67, 97, 238, 255))  # #4361ee
        draw = ImageDraw.Draw(canvas)

        # Buscar fuente disponible
        font = None
        for fp in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]:
            if os.path.exists(fp):
                try:
                    font = ImageFont.truetype(fp, int(size * 0.55))
                    break
                except Exception:
                    pass
        if not font:
            font = ImageFont.load_default()

        text = "D"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x = (size - text_w) // 2 - bbox[0]
        y = (size - text_h) // 2 - bbox[1]
        draw.text((x, y), text, fill=(255, 255, 255, 255), font=font)

        output = BytesIO()
        canvas.save(output, "PNG", optimize=True)
        return Response(content=output.getvalue(), media_type="image/png",
                       headers={"Cache-Control": "public, max-age=3600"})
    except Exception:
        raise HTTPException(500, "No se pudo generar el icono")
