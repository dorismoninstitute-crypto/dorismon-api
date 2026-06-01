"""Verificación pública de certificados."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models import Certificate, User, Course, Level

router = APIRouter(tags=["certificates"])


@router.get("/certificate/verify/{code}")
async def verify_certificate(code: str, db: AsyncSession = Depends(get_db)):
    cert = (await db.execute(select(Certificate).where(Certificate.code == code))).scalar_one_or_none()
    if not cert or cert.revoked:
        raise HTTPException(404, "Certificado no encontrado o revocado")
    student = await db.get(User, cert.student_id)
    course = await db.get(Course, cert.course_id)
    level = await db.get(Level, cert.level_id)
    return {
        "valid": True, "code": cert.code,
        "student_name": student.full_name if student else "—",
        "course_name": course.name if course else "—",
        "level_code": level.code if level else "—",
        "level_name": level.name if level else "—",
        "hours": cert.hours,
        "final_grade": float(cert.final_grade) if cert.final_grade else None,
        "issued_at": cert.issued_at.isoformat(),
    }
