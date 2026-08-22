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



async def _autorizar_sesion(db: AsyncSession, user: CurrentUser, s):
    """¿Puede ESTE usuario ver los datos de ESTA clase?

    ⚠️ V3.9.67 — HUECO CERRADO. Antes estos endpoints solo comprobaban que
    hubiera sesión iniciada. Cualquier usuario logueado que consiguiera un
    `session_id` podía pedir el .ics o el link de Google Calendar y recibir
    los detalles de una clase ajena, INCLUIDO el `meeting_url`.

    Con las clases de audiencia explícita eso contradecía la regla básica:
    María seleccionada sí, Pedro no seleccionado no. Da igual que el
    calendario "solo" genere un archivo: es el mismo dato.
    """
    if user.role == "super_admin":
        return
    if s.teacher_id == user.user_id:
        return
    if user.role == "student":
        from app.services.audience import puede_acceder_a_clase
        if await puede_acceder_a_clase(db, user.user_id, s):
            return
    raise HTTPException(403, "Esta clase no es tuya.")


def _link_visible(s) -> str | None:
    """El enlace SOLO si la modalidad lo permite.

    V3.9.67 — Una clase presencial conserva su `meeting_url` (para volver a
    virtual sin perder nada), pero no debe ofrecerlo. Antes el .ics y el link
    de Google Calendar lo incluían igualmente, así que la app decía
    "Presencial" y el calendario del estudiante traía la videollamada.
    """
    from app.services.audience import tiene_entrada_online
    return s.meeting_url if (tiene_entrada_online(s) and s.meeting_url) else None


@router.get("/session/{session_id}.ics")
async def session_ics(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Genera archivo .ics descargable para una clase."""
    s = await db.get(ClassSession, session_id)
    if not s: raise HTTPException(404)
    await _autorizar_sesion(db, user, s)
    _link = _link_visible(s)

    teacher_user = await db.get(User, s.teacher_id) if s.teacher_id else None
    teacher_name = teacher_user.full_name if teacher_user else "Profesor"

    location = "Online"
    if s.branch_id:
        b = await db.get(Branch, s.branch_id)
        cr = await db.get(Classroom, s.classroom_id) if s.classroom_id else None
        location = f"{b.name if b else ''} - {cr.name if cr else ''}".strip(" -")
    elif _link:
        location = _link

    description_parts = []
    if s.description: description_parts.append(s.description)
    description_parts.append(f"Profesor: {teacher_name}")
    description_parts.append(f"Modalidad: {s.modality.value}")
    if _link:
        description_parts.append(f"Link de la clase: {_link}")
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
    await _autorizar_sesion(db, user, s)
    _link = _link_visible(s)

    teacher_user = await db.get(User, s.teacher_id) if s.teacher_id else None
    teacher_name = teacher_user.full_name if teacher_user else "Profesor"

    details = []
    if s.description: details.append(s.description)
    details.append(f"Profesor: {teacher_name}")
    if _link:
        details.append(f"Link: {_link}")

    location = ""
    if s.branch_id:
        b = await db.get(Branch, s.branch_id)
        cr = await db.get(Classroom, s.classroom_id) if s.classroom_id else None
        location = f"{b.name if b else ''} - {cr.name if cr else ''}".strip(" -")
    elif _link:
        location = _link

    params = {
        "action": "TEMPLATE",
        "text": s.title,
        "dates": f"{_format_ics_dt(s.starts_at_utc)}/{_format_ics_dt(s.ends_at_utc)}",
        "details": "\n".join(details),
        "location": location,
    }
    url = f"https://calendar.google.com/calendar/render?{urlencode(params)}"
    return {"url": url}
