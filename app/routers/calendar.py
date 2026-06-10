"""Generación de archivos .ics (calendario) y links de Google Calendar."""
from typing import Annotated
from datetime import datetime, timezone as tz
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, CurrentUser
from app.core.db import get_db
from app.models import ClassSession, User, Branch, Classroom

router = APIRouter(prefix="/calendar", tags=["calendar"])


def _format_ics_dt(dt: datetime) -> str:
    """Formato UTC para ICS: 20260615T140000Z"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz.utc)
    return dt.astimezone(tz.utc).strftime("%Y%m%dT%H%M%SZ")


def _ics_escape(s: str) -> str:
    """Escape de caracteres especiales en ICS."""
    if not s: return ""
    return s.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


@router.get("/session/{session_id}.ics")
async def session_ics(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Genera archivo .ics descargable para una clase."""
    s = await db.get(ClassSession, session_id)
    if not s: raise HTTPException(404)

    teacher_user = await db.get(User, s.teacher_id) if s.teacher_id else None
    teacher_name = teacher_user.full_name if teacher_user else "Profesor"

    location = "Online"
    if s.branch_id:
        b = await db.get(Branch, s.branch_id)
        cr = await db.get(Classroom, s.classroom_id) if s.classroom_id else None
        location = f"{b.name if b else ''} - {cr.name if cr else ''}".strip(" -")
    elif s.meeting_url:
        location = s.meeting_url

    description_parts = []
    if s.description: description_parts.append(s.description)
    description_parts.append(f"Profesor: {teacher_name}")
    description_parts.append(f"Modalidad: {s.modality.value}")
    if s.meeting_url:
        description_parts.append(f"Link de la clase: {s.meeting_url}")
    description = "\\n".join(description_parts)

    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Dorismon Language Institute//ES
CALSCALE:GREGORIAN
METHOD:PUBLISH
BEGIN:VEVENT
UID:dorismon-{s.id}@dorismon.do
DTSTAMP:{_format_ics_dt(datetime.now(tz.utc))}
DTSTART:{_format_ics_dt(s.starts_at_utc)}
DTEND:{_format_ics_dt(s.ends_at_utc)}
SUMMARY:{_ics_escape(s.title)}
DESCRIPTION:{_ics_escape(description)}
LOCATION:{_ics_escape(location)}
STATUS:CONFIRMED
SEQUENCE:0
BEGIN:VALARM
TRIGGER:-PT15M
ACTION:DISPLAY
DESCRIPTION:Recordatorio: {_ics_escape(s.title)} en 15 minutos
END:VALARM
END:VEVENT
END:VCALENDAR"""

    return Response(
        content=ics,
        media_type="text/calendar",
        headers={
            "Content-Disposition": f'attachment; filename="dorismon-clase-{s.id[:8]}.ics"',
        },
    )


@router.get("/session/{session_id}/google-link")
async def google_calendar_link(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Devuelve un link a Google Calendar pre-cargado con el evento."""
    from urllib.parse import urlencode
    s = await db.get(ClassSession, session_id)
    if not s: raise HTTPException(404)

    teacher_user = await db.get(User, s.teacher_id) if s.teacher_id else None
    teacher_name = teacher_user.full_name if teacher_user else "Profesor"

    details = []
    if s.description: details.append(s.description)
    details.append(f"Profesor: {teacher_name}")
    if s.meeting_url:
        details.append(f"Link: {s.meeting_url}")

    location = ""
    if s.branch_id:
        b = await db.get(Branch, s.branch_id)
        cr = await db.get(Classroom, s.classroom_id) if s.classroom_id else None
        location = f"{b.name if b else ''} - {cr.name if cr else ''}".strip(" -")
    elif s.meeting_url:
        location = s.meeting_url

    params = {
        "action": "TEMPLATE",
        "text": s.title,
        "dates": f"{_format_ics_dt(s.starts_at_utc)}/{_format_ics_dt(s.ends_at_utc)}",
        "details": "\n".join(details),
        "location": location,
    }
    url = f"https://calendar.google.com/calendar/render?{urlencode(params)}"
    return {"url": url}
