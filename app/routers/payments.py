"""V2.6 — Sistema de pagos por transferencia + clases de prueba.

Endpoints del lado del estudiante para:
- Ver cuentas bancarias activas
- Subir prueba de pago
- Ver historial de pagos
- Reservar clase de prueba gratis
"""
from typing import Annotated
from datetime import datetime, date, timezone as tz
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, CurrentUser
from app.core.db import get_db
from app.services.audit import log_action
from app.models import (
    User, Student, BankAccount, PaymentProof, PaymentProofStatus, PaymentMethod,
    TrialClass, Plan, Modality, Notification, NotificationType,
)

router = APIRouter(prefix="/payments", tags=["payments"])


@router.get("/bank-accounts")
async def get_active_bank_accounts(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Cuentas bancarias activas (visibles al estudiante en checkout)."""
    accounts = (await db.execute(
        select(BankAccount).where(BankAccount.is_active.is_(True))
        .order_by(BankAccount.bank_name)
    )).scalars().all()
    return [
        {
            "id": a.id,
            "bank_name": a.bank_name,
            "account_type": a.account_type.value if a.account_type else "savings",
            "account_type_label": "Ahorros" if (a.account_type and a.account_type.value == "savings") else "Corriente",
            "account_number": a.account_number,
            "holder_name": a.holder_name,
            "holder_document": a.holder_document,
            "notes": a.notes,
        }
        for a in accounts
    ]


@router.post("/submit-proof", status_code=201)
async def submit_payment_proof(
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Estudiante sube prueba de pago.

    Body requerido:
    - plan_id: int
    - amount: float
    - payment_date: str (YYYY-MM-DD)
    - reference_number: str
    - voucher_url: str (base64 de la imagen, max 1MB)

    Body opcional:
    - bank_origin: str
    - method: str (bank_transfer/yappy/tpago/pingdigital/cash/other)
    - student_notes: str
    - modality: str (online/presencial/hibrida)
    - level_id: int
    - course_id: int
    """
    if user.role != "student":
        raise HTTPException(403, "Solo estudiantes pueden subir pruebas de pago")

    # Validaciones
    for f in ("plan_id", "amount", "payment_date", "reference_number", "voucher_url"):
        if not body.get(f):
            raise HTTPException(400, f"{f} es requerido")

    # Validar voucher (base64, max 1MB)
    voucher = body["voucher_url"]
    if not isinstance(voucher, str):
        raise HTTPException(400, "voucher_url debe ser string")
    if voucher.startswith("data:"):
        if not (voucher.startswith("data:image/png") or
                voucher.startswith("data:image/jpeg") or
                voucher.startswith("data:image/jpg") or
                voucher.startswith("data:image/webp")):
            raise HTTPException(400, "El comprobante debe ser una imagen (PNG/JPG/WebP)")
        # ~1MB max
        if len(voucher) * 0.75 > 1024 * 1024:
            raise HTTPException(400, "El comprobante es muy pesado (max 1MB). Comprime la imagen.")

    # Validar plan existe
    plan = await db.get(Plan, body["plan_id"])
    if not plan:
        raise HTTPException(404, "Plan no encontrado")

    # Validar fecha
    try:
        pay_date = date.fromisoformat(body["payment_date"])
    except Exception:
        raise HTTPException(400, "payment_date inválido (YYYY-MM-DD)")

    # Validar amount
    try:
        amount = float(body["amount"])
        if amount <= 0:
            raise HTTPException(400, "El monto debe ser mayor a 0")
    except (TypeError, ValueError):
        raise HTTPException(400, "amount inválido")

    # Validar método
    method_str = body.get("method", "bank_transfer")
    try:
        method = PaymentMethod(method_str)
    except ValueError:
        method = PaymentMethod.bank_transfer

    # Validar modalidad
    modality_str = body.get("modality", "online")
    try:
        modality = Modality(modality_str)
    except ValueError:
        modality = Modality.online

    # Crear PaymentProof
    proof = PaymentProof(
        student_id=user.user_id,
        plan_id=body["plan_id"],
        course_id=body.get("course_id"),
        level_id=body.get("level_id"),
        modality=modality,
        amount=amount,
        currency=body.get("currency", "DOP"),
        method=method,
        bank_origin=body.get("bank_origin"),
        payment_date=pay_date,
        reference_number=body["reference_number"].strip(),
        voucher_url=voucher,
        student_notes=body.get("student_notes"),
        status=PaymentProofStatus.pending,
    )
    db.add(proof)
    await db.flush()

    # Notificar a TODOS los admins (super_admin)
    admins = (await db.execute(
        select(User).where(User.role == "super_admin", User.is_active.is_(True))
    )).scalars().all()
    student_user = await db.get(User, user.user_id)
    for adm in admins:
        db.add(Notification(
            user_id=adm.id,
            type=NotificationType.info,
            title="💰 Nueva prueba de pago para verificar",
            body=f"{student_user.full_name if student_user else 'Estudiante'} subió un pago de RD${amount:,.2f} para el plan {plan.name}.",
            link="/dashboard/admin/payment-proofs",
        ))

        # Email al admin
        try:
            from app.services.email_service import send_email, is_email_configured, _base_html
            if is_email_configured() and adm.email:
                html = _base_html(f"""
                    <h2>Nueva prueba de pago pendiente</h2>
                    <p><strong>Estudiante:</strong> {student_user.full_name if student_user else '?'} ({student_user.email if student_user else ''})</p>
                    <p><strong>Plan:</strong> {plan.name}</p>
                    <p><strong>Monto:</strong> RD${amount:,.2f}</p>
                    <p><strong>Fecha del pago:</strong> {pay_date.isoformat()}</p>
                    <p><strong>Referencia:</strong> {body['reference_number']}</p>
                    <p><strong>Método:</strong> {method.value}</p>
                    <p style="text-align: center; margin-top: 24px;">
                        <a href="https://dorismon.com/dashboard/admin/payment-proofs" class="button">Revisar pago</a>
                    </p>
                """)
                await send_email(
                    to=adm.email,
                    subject=f"💰 Nueva prueba de pago: {student_user.full_name if student_user else 'Estudiante'} | Dorismon",
                    html=html,
                )
        except Exception:
            pass

    # Notificación al estudiante
    db.add(Notification(
        user_id=user.user_id,
        type=NotificationType.info,
        title="✉️ Prueba de pago enviada",
        body=f"Tu pago de RD${amount:,.2f} está esperando verificación. Te avisamos en máximo 24h.",
        link="/dashboard/student/payments",
    ))

    await log_action(db, user.user_id, "submit_payment_proof", "payments", target_id=proof.id)
    await db.commit()
    return {"id": proof.id, "status": "pending"}


@router.get("/my-proofs")
async def my_payment_proofs(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Historial de pruebas de pago del estudiante."""
    if user.role != "student":
        raise HTTPException(403)

    proofs = (await db.execute(
        select(PaymentProof, Plan)
        .outerjoin(Plan, PaymentProof.plan_id == Plan.id)
        .where(PaymentProof.student_id == user.user_id)
        .order_by(PaymentProof.created_at.desc())
    )).all()

    return [
        {
            "id": p.id,
            "plan_id": p.plan_id,
            "plan_name": plan.name if plan else "?",
            "amount": float(p.amount),
            "currency": p.currency,
            "method": p.method.value if p.method else "bank_transfer",
            "payment_date": p.payment_date.isoformat() if p.payment_date else None,
            "reference_number": p.reference_number,
            "status": p.status.value if p.status else "pending",
            "admin_notes": p.admin_notes if p.status == PaymentProofStatus.rejected else None,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p, plan in proofs
    ]


# ============= CLASES DE PRUEBA GRATIS =============

@router.get("/trial-class/status")
async def my_trial_status(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.6: ¿El estudiante ya tiene clase de prueba? Estado de la misma."""
    if user.role != "student":
        raise HTTPException(403)

    tc = (await db.execute(
        select(TrialClass).where(TrialClass.student_id == user.user_id)
    )).scalar_one_or_none()

    if not tc:
        return {"has_trial": False, "can_request": True}

    teacher = await db.get(User, tc.teacher_id) if tc.teacher_id else None

    return {
        "has_trial": True,
        "can_request": False,
        "id": tc.id,
        "modality": tc.modality.value if tc.modality else "online",
        "status": tc.status,
        "preferred_date": tc.preferred_date.isoformat() if tc.preferred_date else None,
        "preferred_time": tc.preferred_time,
        "preferred_level": tc.preferred_level,
        "scheduled_at": tc.scheduled_at.isoformat() if tc.scheduled_at else None,
        "teacher_name": teacher.full_name if teacher else None,
        "completed_at": tc.completed_at.isoformat() if tc.completed_at else None,
        # V3.4: campos de reagenda (para la página de clase de prueba)
        "can_reschedule": (tc.status == "no_show" and (tc.reschedule_count or 0) < 1 and not tc.reschedule_requested),
        "reschedule_requested": bool(tc.reschedule_requested),
    }


@router.post("/trial-class/request", status_code=201)
async def request_trial_class(
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.6: Estudiante solicita su clase de prueba gratis.

    Body:
    - modality: online/presencial/hibrida
    - preferred_level: A1/A2/B1/etc (opcional)
    - preferred_date: YYYY-MM-DD (opcional)
    - preferred_time: morning/afternoon/evening (opcional)
    - notes: str (opcional)
    """
    if user.role != "student":
        raise HTTPException(403, "Solo estudiantes pueden reservar clase de prueba")

    # Verificar si ya tiene una
    existing = (await db.execute(
        select(TrialClass).where(TrialClass.student_id == user.user_id)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(400, "Ya tienes una clase de prueba reservada. Solo se permite UNA por estudiante.")

    # Validar modalidad
    modality_str = body.get("modality", "online")
    try:
        modality = Modality(modality_str)
    except ValueError:
        modality = Modality.online

    preferred_date = None
    if body.get("preferred_date"):
        try:
            preferred_date = date.fromisoformat(body["preferred_date"])
        except Exception:
            pass

    tc = TrialClass(
        student_id=user.user_id,
        modality=modality,
        preferred_level=body.get("preferred_level"),
        preferred_date=preferred_date,
        preferred_time=body.get("preferred_time"),
        notes=body.get("notes"),
        status="requested",
    )
    db.add(tc)
    await db.flush()

    # Notificar admins
    admins = (await db.execute(
        select(User).where(User.role == "super_admin", User.is_active.is_(True))
    )).scalars().all()
    student_user = await db.get(User, user.user_id)
    for adm in admins:
        db.add(Notification(
            user_id=adm.id,
            type=NotificationType.info,
            title="🎁 Nueva solicitud de clase de prueba",
            body=f"{student_user.full_name if student_user else 'Estudiante'} solicitó una clase de prueba ({modality.value}).",
            link="/dashboard/admin/trial-classes",
        ))

    # Notificación al estudiante
    db.add(Notification(
        user_id=user.user_id,
        type=NotificationType.info,
        title="🎁 Solicitud de clase de prueba enviada",
        body="Te asignaremos un profesor pronto. Te avisamos por email.",
    ))

    await log_action(db, user.user_id, "request_trial_class", "payments", target_id=tc.id)
    await db.commit()

    # V3.6: Avisar al dueño por email de la solicitud de clase de prueba
    try:
        from app.services.email_service import send_admin_trial_request_email, is_email_configured
        if is_email_configured() and student_user:
            await send_admin_trial_request_email(
                student_name=student_user.full_name,
                student_email=student_user.email,
                modality=modality.value,
                preferred_level=body.get("preferred_level"),
            )
    except Exception:
        pass

    return {"id": tc.id, "status": "requested"}
