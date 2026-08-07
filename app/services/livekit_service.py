"""V3.9.26 — Videollamadas dentro de Dorismon (LiveKit).

QUÉ HACE: crea el permiso de entrada ("token") para que un estudiante o un
profesor entre a la sala de video de SU clase, dentro de dorismon.com.

POR QUÉ LIVEKIT: su servidor es de código abierto. Hoy usamos su nube por
comodidad, pero el día que Dorismon quiera su propio servidor, se muda sin
rehacer la integración. Con un servicio cerrado habría que empezar de cero.

CONFIGURACIÓN (variables de entorno en Render):
    LIVEKIT_URL         wss://<algo>.livekit.cloud
    LIVEKIT_API_KEY     la clave
    LIVEKIT_API_SECRET  el secreto (es una contraseña)

Si no está configurado, la plataforma sigue funcionando igual: las clases
usan Google Meet o Zoom como siempre. Nada se rompe.

NOTA TÉCNICA: el token es un JWT firmado. Se genera con pyjwt (que ya estaba
en el proyecto) en vez de sumar otra librería, para no arriesgar el despliegue.
"""
import os
import time
import logging

import jwt

log = logging.getLogger(__name__)

# Cuánto antes de la clase se puede entrar, y cuánto después sigue abierta.
MINUTOS_ANTES = 15
MINUTOS_DESPUES = 20


def livekit_ready() -> bool:
    """¿Están puestas las credenciales?"""
    return bool(
        os.getenv("LIVEKIT_URL")
        and os.getenv("LIVEKIT_API_KEY")
        and os.getenv("LIVEKIT_API_SECRET")
    )


def livekit_url() -> str | None:
    """URL del servidor, limpiando errores comunes al pegarla en Render."""
    raw = (os.getenv("LIVEKIT_URL") or "").strip().strip('"').strip("'")
    if not raw:
        return None
    # Si pegaron la línea completa "LIVEKIT_URL=wss://..." la limpiamos
    if raw.upper().startswith("LIVEKIT_URL="):
        raw = raw.split("=", 1)[1].strip()
    # Aceptar https:// y convertirlo a wss:// (error habitual)
    if raw.startswith("https://"):
        raw = "wss://" + raw[len("https://"):]
    elif raw.startswith("http://"):
        raw = "wss://" + raw[len("http://"):]
    return raw.rstrip("/")


def room_name(session_id: str) -> str:
    """Cada clase tiene su propia sala, derivada de su identificador."""
    return f"dorismon-{session_id}"


def build_token(
    session_id: str,
    user_id: str,
    display_name: str,
    is_moderator: bool,
    ttl_seconds: int = 3 * 60 * 60,
) -> str:
    """Genera el permiso de entrada a la sala de una clase.

    El profesor entra como moderador: puede silenciar, sacar a alguien y
    cerrar la sala. El estudiante entra como participante normal.

    IMPORTANTE: quién puede pedir este token se decide ANTES, en el router
    (solo los inscritos en esa clase). Aquí solo se firma el permiso.
    """
    api_key = (os.getenv("LIVEKIT_API_KEY") or "").strip()
    api_secret = (os.getenv("LIVEKIT_API_SECRET") or "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("LiveKit no está configurado")

    now = int(time.time())
    grant = {
        "room": room_name(session_id),
        "roomJoin": True,
        "canPublish": True,
        "canSubscribe": True,
        "canPublishData": True,
        # Solo el profesor administra la sala
        "roomAdmin": bool(is_moderator),
    }
    payload = {
        "iss": api_key,
        "sub": user_id,
        "nbf": now - 10,
        "exp": now + ttl_seconds,
        "name": display_name,
        "video": grant,
        "metadata": "teacher" if is_moderator else "student",
    }
    return jwt.encode(payload, api_secret, algorithm="HS256")
