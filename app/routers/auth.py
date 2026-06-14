"""Auth — login, register, refresh, me."""
from datetime import datetime, timedelta, timezone as tz
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, EmailStr, Field

from app.core.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token, decode_token,
    get_current_user, CurrentUser,
)
from app.core.db import get_db
from app.models import User, Student, UserRole
from app.services.audit import log_action

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    full_name: str = Field(min_length=2, max_length=160)
    phone: Optional[str] = None
    gender: Optional[str] = None  # V1.6.4: 'male', 'female', 'other' o None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    phone: Optional[str] = None
    role: str
    avatar_url: Optional[str] = None
    gender: Optional[str] = None  # V1.6.4
    email_verified: Optional[bool] = True  # V2.1
    current_level_id: Optional[int] = None
    placement_done: Optional[bool] = None


@router.post("/register", response_model=TokenResponse, status_code=201)
async def register(body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    existing = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe una cuenta con este email")

    # V2.1: Validar que el email tenga dominio real (MX records)
    from app.services.email_service import validate_email_domain, send_email, tpl_welcome, tpl_welcome_simple, gen_verification_code, is_email_configured
    from app.models import EmailVerification
    valid, err = await validate_email_domain(body.email)
    if not valid:
        raise HTTPException(400, err)

    # V1.6.4: Validar gender
    gender = None
    if body.gender:
        if body.gender not in ("male", "female", "other"):
            raise HTTPException(400, "gender debe ser 'male', 'female' u 'other'")
        gender = body.gender

    # V2.4: UX SIN FRICCIÓN
    # Si el email pasó la validación MX, ya sabemos que es real.
    # Marcamos email_verified=true automáticamente para no bloquear al usuario.
    # El usuario puede usar la plataforma de inmediato.
    # El email de bienvenida se envía en segundo plano (informativo, sin código).
    user = User(
        email=body.email, password_hash=hash_password(body.password),
        full_name=body.full_name, phone=body.phone, role=UserRole.student,
        gender=gender,
        email_verified=True,  # V2.4: sin fricción, validación MX ya fue hecha
    )
    db.add(user)
    await db.flush()
    db.add(Student(user_id=user.id))

    # V2.4: Email de bienvenida INFORMATIVO en segundo plano (no bloquea)
    if is_email_configured():
        try:
            await send_email(
                to=body.email,
                subject="¡Bienvenido a Dorismon Language Institute!",
                html=tpl_welcome_simple(body.full_name),
            )
        except Exception:
            pass  # No rompemos el registro si el email falla

    await log_action(db, user.id, "register", "auth", target_id=user.id)
    await db.commit()
    return TokenResponse(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id),
    )


@router.get("/email-service-status")
async def email_service_status():
    """V2.1.1: Endpoint público para verificar si el servicio de email está configurado.

    Útil para diagnóstico desde el frontend.
    """
    from app.services.email_service import is_email_configured, RESEND_API_KEY, EMAIL_FROM, APP_URL
    return {
        "configured": is_email_configured(),
        "from_address_set": bool(EMAIL_FROM),
        "app_url_set": bool(APP_URL),
        "key_length": len(RESEND_API_KEY) if RESEND_API_KEY else 0,
    }


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Email o contraseña incorrectos")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cuenta desactivada")
    user.last_login_at = datetime.now(tz.utc)
    await log_action(db, user.id, "login", "auth")
    await db.commit()
    return TokenResponse(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id),
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, db: AsyncSession = Depends(get_db)):
    payload = decode_token(body.refresh_token)
    if payload.get("type") != "refresh":
        raise HTTPException(401, "Se requiere refresh token")
    user = await db.get(User, payload["sub"])
    if not user:
        raise HTTPException(401, "Usuario no encontrado")
    return TokenResponse(
        access_token=create_access_token(user.id, user.role.value),
        refresh_token=create_refresh_token(user.id),
    )


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[CurrentUser, Depends(get_current_user)], db: AsyncSession = Depends(get_db)):
    u = await db.get(User, user.user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")
    out = UserOut(id=u.id, email=u.email, full_name=u.full_name, phone=u.phone,
                  role=u.role.value, avatar_url=u.avatar_url, gender=u.gender,
                  email_verified=u.email_verified)
    if u.role == UserRole.student:
        st = await db.get(Student, u.id)
        if st:
            out.current_level_id = st.current_level_id
            out.placement_done = st.placement_done
    return out


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=72)


@router.post("/change-password")
async def change_password(
    body: ChangePasswordRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    u = await db.get(User, user.user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")
    if not verify_password(body.current_password, u.password_hash):
        raise HTTPException(400, "Contraseña actual incorrecta")
    u.password_hash = hash_password(body.new_password)
    await log_action(db, user.user_id, "change_password", "auth")
    await db.commit()
    return {"ok": True, "message": "Contraseña actualizada"}


class UpdateProfileRequest(BaseModel):
    """V1.6.4: Actualizar perfil propio (cualquier rol)."""
    full_name: str | None = Field(default=None, min_length=2, max_length=100)
    phone: str | None = Field(default=None, max_length=30)
    gender: str | None = Field(default=None)  # V1.6.4: 'male', 'female', 'other' o "" para limpiar
    bio: str | None = Field(default=None, max_length=500)  # solo profes


@router.patch("/me")
async def update_my_profile(
    body: UpdateProfileRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V1.6.4: Actualizar perfil propio. Cualquier campo es opcional."""
    u = await db.get(User, user.user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")

    if body.full_name is not None:
        u.full_name = body.full_name.strip()
    if body.phone is not None:
        u.phone = body.phone.strip() or None
    if body.gender is not None:
        # V1.6.4: Validar gender
        g = body.gender.strip().lower()
        if g and g not in ("male", "female", "other"):
            raise HTTPException(400, "gender debe ser 'male', 'female' u 'other'")
        u.gender = g or None

    # bio solo aplica a profes (Teacher.bio)
    if body.bio is not None and user.role == "teacher":
        from app.models import Teacher
        t = await db.get(Teacher, user.user_id)
        if t:
            t.bio = body.bio.strip() or None

    await log_action(db, user.user_id, "update_profile", "auth")
    await db.commit()
    return {
        "ok": True,
        "user": {
            "id": u.id, "email": u.email, "full_name": u.full_name,
            "phone": u.phone, "avatar_url": u.avatar_url, "gender": u.gender,
            "role": u.role.value,
        },
    }


# ============= V2.1 — VERIFICACIÓN DE EMAIL =============

class VerifyEmailRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


@router.post("/verify-email")
async def verify_email(
    body: VerifyEmailRequest,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.1: Verifica el código de email que recibió el usuario por correo."""
    from app.models import EmailVerification

    u = await db.get(User, user.user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")
    if u.email_verified:
        return {"ok": True, "already_verified": True}

    # Buscar verificación válida más reciente
    ev = (await db.execute(
        select(EmailVerification).where(
            EmailVerification.user_id == u.id,
            EmailVerification.code == body.code,
            EmailVerification.used_at.is_(None),
        ).order_by(EmailVerification.created_at.desc()).limit(1)
    )).scalar_one_or_none()

    if not ev:
        raise HTTPException(400, "Código incorrecto")

    # Validar expiración (timezone-safe)
    expires_aware = ev.expires_at if ev.expires_at.tzinfo else ev.expires_at.replace(tzinfo=tz.utc)
    if expires_aware < datetime.now(tz.utc):
        raise HTTPException(400, "El código expiró. Pide un nuevo código.")

    ev.used_at = datetime.now(tz.utc)
    u.email_verified = True
    await log_action(db, u.id, "verify_email", "auth", target_id=u.id)
    await db.commit()
    return {"ok": True}


@router.post("/resend-verification")
async def resend_verification(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V2.1: Reenvía el código de verificación al email del usuario."""
    from app.models import EmailVerification
    from app.services.email_service import send_email, tpl_welcome, gen_verification_code, is_email_configured

    u = await db.get(User, user.user_id)
    if not u:
        raise HTTPException(404)
    if u.email_verified:
        return {"ok": True, "already_verified": True}
    if not is_email_configured():
        raise HTTPException(503, "Servicio de email no configurado. Contactá al administrador.")

    # Invalidar códigos anteriores
    old_codes = (await db.execute(
        select(EmailVerification).where(
            EmailVerification.user_id == u.id,
            EmailVerification.used_at.is_(None),
        )
    )).scalars().all()
    for old in old_codes:
        old.used_at = datetime.now(tz.utc)

    # Nuevo código
    code = gen_verification_code()
    ev = EmailVerification(
        user_id=u.id,
        code=code,
        expires_at=datetime.now(tz.utc) + timedelta(minutes=30),
    )
    db.add(ev)
    await send_email(
        to=u.email,
        subject="Tu código de verificación — Dorismon",
        html=tpl_welcome(u.full_name, code),
    )
    await db.commit()
    return {"ok": True}


# ============= V2.1 — RECUPERAR CONTRASEÑA =============

class ForgotPasswordRequest(BaseModel):
    email: EmailStr


@router.post("/forgot-password")
async def forgot_password(
    body: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """V2.1: Envía email con link para resetear contraseña.

    V2.1.2: Con logging detallado para diagnóstico.
    Por seguridad SIEMPRE retorna ok=True (no revela si el email existe).
    """
    import logging
    log = logging.getLogger(__name__)
    log.info(f"[FORGOT-PASSWORD] Pedido recibido para email: {body.email}")

    from app.models import PasswordReset
    from app.services.email_service import send_email, tpl_password_reset, gen_reset_token, is_email_configured

    # V2.1.2: log para diagnóstico
    if not is_email_configured():
        log.warning("[FORGOT-PASSWORD] is_email_configured() = False — RESEND_API_KEY no está cargada en runtime")

    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()

    if not user:
        log.warning(f"[FORGOT-PASSWORD] Usuario NO encontrado en DB para email: {body.email}")
    elif not user.is_active:
        log.warning(f"[FORGOT-PASSWORD] Usuario {body.email} está inactivo")
    elif not is_email_configured():
        log.warning(f"[FORGOT-PASSWORD] Usuario {body.email} existe pero servicio email no configurado")
    else:
        log.info(f"[FORGOT-PASSWORD] Usuario {body.email} encontrado y activo — generando token")

        # Invalidar tokens anteriores no usados
        old_tokens = (await db.execute(
            select(PasswordReset).where(
                PasswordReset.user_id == user.id,
                PasswordReset.used_at.is_(None),
            )
        )).scalars().all()
        for old in old_tokens:
            old.used_at = datetime.now(tz.utc)

        token = gen_reset_token()
        pr = PasswordReset(
            user_id=user.id,
            token=token,
            expires_at=datetime.now(tz.utc) + timedelta(hours=2),
        )
        db.add(pr)
        log.info(f"[FORGOT-PASSWORD] Llamando a send_email para {user.email}")
        result = await send_email(
            to=user.email,
            subject="Recuperá tu contraseña — Dorismon",
            html=tpl_password_reset(user.full_name, token),
        )
        log.info(f"[FORGOT-PASSWORD] send_email retornó: {result}")
        await log_action(db, user.id, "forgot_password_request", "auth", target_id=user.id)
        await db.commit()

    # Respuesta uniforme aunque no exista el usuario
    return {"ok": True, "message": "Si el email existe, recibirás un link de recuperación."}


@router.post("/test-email")
async def test_email_endpoint(body: dict):
    """V2.1.2: Endpoint público de diagnóstico — envía un email de prueba.

    USO: POST /auth/test-email con body {"to": "tu@email.com"}
    Sirve para probar Resend SIN depender de usuarios existentes.

    Logs detallados: si falla, los logs de Render te dicen exactamente por qué.
    """
    import logging
    log = logging.getLogger(__name__)
    from app.services.email_service import send_email, is_email_configured, _base_html

    to = body.get("to")
    if not to or "@" not in to:
        raise HTTPException(400, "Falta campo 'to' con un email válido")

    log.info(f"[TEST-EMAIL] Intentando enviar email de prueba a: {to}")

    if not is_email_configured():
        log.error("[TEST-EMAIL] is_email_configured() = False")
        return {
            "ok": False,
            "error": "Servicio de email no configurado (RESEND_API_KEY no cargada)",
            "debug": "Verificar variable RESEND_API_KEY en Render → Environment → forzar redeploy"
        }

    html = _base_html("""
        <h2>✅ Email de prueba</h2>
        <p>Si recibiste este email, Resend está funcionando correctamente.</p>
        <p>El sistema de emails de Dorismon está operativo.</p>
    """)

    try:
        result = await send_email(
            to=to,
            subject="✅ Test de email — Dorismon",
            html=html,
        )
        log.info(f"[TEST-EMAIL] Resultado: {result}")
        return {
            "ok": result,
            "to": to,
            "message": "Email enviado a Resend. Revisa tu inbox + spam en 1-2 min. También revisá Resend Logs."
        }
    except Exception as e:
        log.error(f"[TEST-EMAIL] Excepción: {e}")
        return {"ok": False, "error": str(e)}


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    new_password: str = Field(min_length=8, max_length=72)


@router.post("/reset-password")
async def reset_password(
    body: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """V2.1: Resetear contraseña con el token recibido por email."""
    from app.models import PasswordReset

    pr = (await db.execute(
        select(PasswordReset).where(
            PasswordReset.token == body.token,
            PasswordReset.used_at.is_(None),
        )
    )).scalar_one_or_none()
    if not pr:
        raise HTTPException(400, "Token inválido o ya usado")

    expires_aware = pr.expires_at if pr.expires_at.tzinfo else pr.expires_at.replace(tzinfo=tz.utc)
    if expires_aware < datetime.now(tz.utc):
        raise HTTPException(400, "El link expiró. Pide un nuevo link.")

    u = await db.get(User, pr.user_id)
    if not u:
        raise HTTPException(404, "Usuario no encontrado")

    u.password_hash = hash_password(body.new_password)
    pr.used_at = datetime.now(tz.utc)
    await log_action(db, u.id, "reset_password", "auth", target_id=u.id)
    await db.commit()
    return {"ok": True, "message": "Contraseña actualizada. Ya puedes iniciar sesión."}
