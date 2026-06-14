"""V2.0 — Sistema de mensajes y tickets de soporte.

Endpoints comunes a todos los roles para enviar/recibir mensajes.
- Cualquier usuario puede enviar mensaje a otro
- Estudiante puede enviar ticket a admin
- Admin puede gestionar tickets
"""
from typing import Annotated
from datetime import datetime, timezone as tz

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field

from app.core.security import get_current_user, require_admin, CurrentUser
from app.core.db import get_db
from app.services.audit import log_action
from app.models import (
    User, UserRole, Message, MessageCategory, MessagePriority, MessageStatus,
    Notification, NotificationType, Student, Enrollment, Teacher,
)

router = APIRouter(prefix="/messages", tags=["messages"])


class SendMessageRequest(BaseModel):
    to_user_id: str | None = None  # None = al admin (cualquiera)
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)
    is_ticket: bool = False
    category: str = "general"  # general/urgent/consultation/bug/request
    priority: str = "normal"  # low/normal/high
    reply_to_id: str | None = None


@router.post("", status_code=201)
async def send_message(
    body: SendMessageRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Enviar un mensaje a otro usuario o crear un ticket de soporte (to_user_id=None)."""
    # Validaciones
    try:
        cat = MessageCategory(body.category)
    except Exception:
        raise HTTPException(400, "Categoría inválida")
    try:
        prio = MessagePriority(body.priority)
    except Exception:
        raise HTTPException(400, "Prioridad inválida")

    # Si tiene to_user_id, validar que exista
    if body.to_user_id:
        target = await db.get(User, body.to_user_id)
        if not target:
            raise HTTPException(404, "Usuario destinatario no encontrado")
        if not target.is_active:
            raise HTTPException(400, "El destinatario está inactivo")

    # Validar permisos:
    # - Estudiante puede mensajear a su profe asignado o crear ticket (to_user_id=None)
    # - Estudiante NO puede mensajear a otros estudiantes
    if user.role == "student":
        if body.to_user_id:
            # Sólo a un profe asignado a él
            target = await db.get(User, body.to_user_id)
            if target.role.value not in ("teacher", "super_admin"):
                raise HTTPException(403, "Solo puedes mensajear a tu profesor o al admin")
            if target.role.value == "teacher":
                # Verificar que esté inscripto con este profe
                enr = (await db.execute(
                    select(Enrollment).where(
                        Enrollment.student_id == user.user_id,
                        Enrollment.teacher_id == body.to_user_id,
                        Enrollment.is_active.is_(True),
                    )
                )).scalar_one_or_none()
                if not enr:
                    raise HTTPException(403, "Solo puedes mensajear a profesores asignados")

    elif user.role == "teacher":
        # Profe puede mensajear a sus estudiantes o al admin
        if body.to_user_id:
            target = await db.get(User, body.to_user_id)
            if target.role.value not in ("student", "super_admin"):
                raise HTTPException(403, "Solo puedes mensajear a tus estudiantes o al admin")
            if target.role.value == "student":
                enr = (await db.execute(
                    select(Enrollment).where(
                        Enrollment.student_id == body.to_user_id,
                        Enrollment.teacher_id == user.user_id,
                        Enrollment.is_active.is_(True),
                    )
                )).scalar_one_or_none()
                if not enr:
                    raise HTTPException(403, "Solo puedes mensajear a tus estudiantes")

    # Admin puede mensajear a cualquiera

    # Crear mensaje
    # Si no tiene to_user_id, asignamos al primer admin disponible
    to_user_id = body.to_user_id
    if not to_user_id:
        # Buscar primer admin
        admin = (await db.execute(
            select(User).where(User.role == UserRole.super_admin, User.is_active.is_(True)).limit(1)
        )).scalar_one_or_none()
        if not admin:
            raise HTTPException(500, "No hay admin disponible para recibir tickets")
        to_user_id = admin.id

    msg = Message(
        from_user_id=user.user_id,
        to_user_id=to_user_id,
        subject=body.subject.strip(),
        body=body.body.strip(),
        is_ticket=body.is_ticket,
        category=cat,
        priority=prio,
        reply_to_id=body.reply_to_id,
        status=MessageStatus.open if body.is_ticket else MessageStatus.open,
    )
    db.add(msg)
    await db.flush()

    # Notificación al destinatario
    sender = await db.get(User, user.user_id)
    sender_name = sender.full_name if sender else "Usuario"
    notif_title = "🆘 Nuevo ticket de soporte" if body.is_ticket else "💬 Nuevo mensaje"
    if body.is_ticket and prio == MessagePriority.high:
        notif_title = "🚨 Ticket URGENTE"

    db.add(Notification(
        user_id=to_user_id,
        type=NotificationType.info,
        title=notif_title,
        body=f"De {sender_name}: {body.subject[:80]}",
        link="/dashboard/messages",
    ))

    await log_action(db, user.user_id, "send_message", "messages",
                     target_id=msg.id,
                     details=f"to={to_user_id}, ticket={body.is_ticket}, cat={cat.value}")
    await db.commit()
    return {"ok": True, "message_id": msg.id}


@router.get("/inbox")
async def inbox(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.0: Mensajes recibidos por el usuario."""
    messages = (await db.execute(
        select(Message).where(Message.to_user_id == user.user_id)
        .order_by(Message.created_at.desc()).limit(100)
    )).scalars().all()

    out = []
    for m in messages:
        sender = await db.get(User, m.from_user_id)
        out.append({
            "id": m.id,
            "from_user_id": m.from_user_id,
            "from_name": sender.full_name if sender else "—",
            "from_role": sender.role.value if sender else None,
            "from_gender": sender.gender if sender else None,
            "subject": m.subject,
            "body": m.body,
            "is_ticket": m.is_ticket,
            "category": m.category.value,
            "priority": m.priority.value,
            "status": m.status.value,
            "reply_to_id": m.reply_to_id,
            "read_at": m.read_at.isoformat() if m.read_at else None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        })
    return out


@router.get("/sent")
async def sent(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.0: Mensajes enviados por el usuario."""
    messages = (await db.execute(
        select(Message).where(Message.from_user_id == user.user_id)
        .order_by(Message.created_at.desc()).limit(100)
    )).scalars().all()
    out = []
    for m in messages:
        recipient = await db.get(User, m.to_user_id) if m.to_user_id else None
        out.append({
            "id": m.id,
            "to_user_id": m.to_user_id,
            "to_name": recipient.full_name if recipient else "—",
            "to_role": recipient.role.value if recipient else None,
            "subject": m.subject,
            "body": m.body,
            "is_ticket": m.is_ticket,
            "category": m.category.value,
            "priority": m.priority.value,
            "status": m.status.value,
            "read_at": m.read_at.isoformat() if m.read_at else None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        })
    return out


@router.post("/{message_id}/read")
async def mark_read(
    message_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.0: Marcar mensaje como leído."""
    m = await db.get(Message, message_id)
    if not m:
        raise HTTPException(404, "Mensaje no encontrado")
    if m.to_user_id != user.user_id:
        raise HTTPException(403, "No es para vos")
    if not m.read_at:
        m.read_at = datetime.now(tz.utc)
        await db.commit()
    return {"ok": True}


@router.get("/contacts")
async def list_contacts(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.0: Lista los contactos con los que el usuario puede mensajear.

    - Estudiante: ve su profesor asignado + opción "Coordinador" (admin)
    - Profesor: ve sus estudiantes + admin
    - Admin: ve todos
    """
    out = []

    if user.role == "student":
        # Profes asignados
        enrs = (await db.execute(
            select(Enrollment, User)
            .join(User, Enrollment.teacher_id == User.id)
            .where(
                Enrollment.student_id == user.user_id,
                Enrollment.is_active.is_(True),
                Enrollment.teacher_id.is_not(None),
            )
        )).all()
        seen = set()
        for e, t in enrs:
            if t.id not in seen:
                seen.add(t.id)
                out.append({
                    "user_id": t.id, "full_name": t.full_name, "role": "teacher",
                    "gender": t.gender, "label": "Tu profesor",
                })
        # Admin (label "Coordinador" para estudiante)
        admins = (await db.execute(
            select(User).where(User.role == UserRole.super_admin, User.is_active.is_(True))
        )).scalars().all()
        for a in admins:
            out.append({
                "user_id": a.id, "full_name": a.full_name, "role": "super_admin",
                "gender": a.gender, "label": "Coordinador (Admin)",
            })

    elif user.role == "teacher":
        # Estudiantes inscritos con este profe
        enrs = (await db.execute(
            select(Enrollment, User)
            .join(User, Enrollment.student_id == User.id)
            .where(
                Enrollment.teacher_id == user.user_id,
                Enrollment.is_active.is_(True),
            )
        )).all()
        seen = set()
        for e, s in enrs:
            if s.id not in seen:
                seen.add(s.id)
                out.append({
                    "user_id": s.id, "full_name": s.full_name, "role": "student",
                    "gender": s.gender, "label": "Estudiante",
                })
        # Admin
        admins = (await db.execute(
            select(User).where(User.role == UserRole.super_admin, User.is_active.is_(True))
        )).scalars().all()
        for a in admins:
            out.append({
                "user_id": a.id, "full_name": a.full_name, "role": "super_admin",
                "gender": a.gender, "label": "Administrador",
            })

    else:
        # Admin ve todos los usuarios activos
        users = (await db.execute(
            select(User).where(User.is_active.is_(True), User.id != user.user_id)
            .order_by(User.role, User.full_name).limit(500)
        )).scalars().all()
        for u in users:
            label = {"teacher": "Profesor", "student": "Estudiante", "super_admin": "Admin"}.get(u.role.value, "")
            out.append({
                "user_id": u.id, "full_name": u.full_name, "role": u.role.value,
                "gender": u.gender, "label": label,
            })

    return out


@router.get("/unread-count")
async def unread_count(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.0: Cuántos mensajes no leídos tiene el usuario."""
    n = (await db.execute(
        select(func.count()).select_from(Message).where(
            Message.to_user_id == user.user_id,
            Message.read_at.is_(None),
        )
    )).scalar() or 0
    return {"unread": n}


# === ADMIN: gestión de tickets ===
@router.get("/admin/tickets")
async def admin_list_tickets(
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
    status: str | None = None,
):
    """V2.0 admin: lista tickets (mensajes con is_ticket=True)."""
    stmt = select(Message).where(Message.is_ticket.is_(True))
    if status:
        try:
            st = MessageStatus(status)
            stmt = stmt.where(Message.status == st)
        except Exception:
            pass
    stmt = stmt.order_by(
        # urgentes primero
        Message.priority.desc(),
        Message.created_at.desc(),
    )
    tickets = (await db.execute(stmt)).scalars().all()
    out = []
    for t in tickets:
        sender = await db.get(User, t.from_user_id)
        out.append({
            "id": t.id,
            "from_user_id": t.from_user_id,
            "from_name": sender.full_name if sender else "—",
            "from_role": sender.role.value if sender else None,
            "from_gender": sender.gender if sender else None,
            "subject": t.subject,
            "body": t.body,
            "category": t.category.value,
            "priority": t.priority.value,
            "status": t.status.value,
            "created_at": t.created_at.isoformat() if t.created_at else None,
        })
    return out


@router.patch("/admin/tickets/{ticket_id}")
async def admin_update_ticket_status(
    ticket_id: str, body: dict,
    admin: Annotated[CurrentUser, Depends(require_admin)],
    db: AsyncSession = Depends(get_db),
):
    """V2.0 admin: cambiar estado de un ticket."""
    m = await db.get(Message, ticket_id)
    if not m or not m.is_ticket:
        raise HTTPException(404, "Ticket no encontrado")
    new_status = body.get("status")
    if new_status:
        try:
            m.status = MessageStatus(new_status)
        except Exception:
            raise HTTPException(400, "Estado inválido")
    await log_action(db, admin.user_id, "update_ticket_status", "messages",
                     target_id=ticket_id, details=f"status={new_status}")
    await db.commit()
    return {"ok": True}
