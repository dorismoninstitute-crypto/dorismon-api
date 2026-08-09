"""V3.9.29 — Notificaciones push al teléfono.

QUÉ RESUELVE: hoy los avisos llegan por correo (que muchos no leen) y dentro
de la plataforma (que hay que abrir). Con esto, al estudiante le suena el
teléfono: "Tu clase empieza en 10 minutos".

CÓMO FUNCIONA EN CRIOLLO:
1. El estudiante instala la plataforma en su pantalla de inicio y acepta
   recibir avisos.
2. Su teléfono le da a Dorismon una "dirección de entrega" (la suscripción).
3. Cuando hay algo que avisar, el servidor manda el mensaje a esa dirección.

CONFIGURACIÓN (variables de entorno en Render):
    VAPID_PUBLIC_KEY    la clave pública (también va en el frontend)
    VAPID_PRIVATE_KEY   la clave privada (es una contraseña)
    VAPID_SUBJECT       mailto:dorismoninstitute@gmail.com

Si no están configuradas, la plataforma funciona igual: simplemente no se
ofrecen los avisos al teléfono. Nada se rompe.

IMPORTANTE SOBRE IPHONE: Apple exige que el estudiante AGREGUE la plataforma
a su pantalla de inicio. Si solo la abre en Safari, no le llegan avisos.
En Android funciona abriendo la web normal.
"""
import os
import json
import logging

log = logging.getLogger(__name__)


def push_ready() -> bool:
    """¿Están las claves configuradas?"""
    return bool(os.getenv("VAPID_PUBLIC_KEY") and os.getenv("VAPID_PRIVATE_KEY"))


def public_key() -> str | None:
    k = (os.getenv("VAPID_PUBLIC_KEY") or "").strip().strip('"').strip("'")
    if k.upper().startswith("VAPID_PUBLIC_KEY="):
        k = k.split("=", 1)[1].strip()
    return k or None


def _private_key() -> str:
    k = (os.getenv("VAPID_PRIVATE_KEY") or "").strip().strip('"').strip("'")
    if k.upper().startswith("VAPID_PRIVATE_KEY="):
        k = k.split("=", 1)[1].strip()
    return k


def _subject() -> str:
    s = (os.getenv("VAPID_SUBJECT") or "").strip()
    if not s:
        return "mailto:dorismoninstitute@gmail.com"
    if not s.startswith(("mailto:", "http")):
        s = "mailto:" + s
    return s


def send_push_sync(subscription: dict, titulo: str, cuerpo: str,
                   url: str = "/dashboard", tag: str | None = None) -> tuple[bool, int | None]:
    """Envía un aviso a un teléfono.

    Devuelve (enviado, codigo_http). Si el código es 404 o 410, esa
    suscripción ya no sirve (desinstaló la app o revocó el permiso) y hay
    que borrarla para no seguir intentando.
    """
    if not push_ready():
        return False, None
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log.warning("pywebpush no está instalado")
        return False, None

    payload = json.dumps({
        "title": titulo,
        "body": cuerpo,
        "url": url,
        "tag": tag or "dorismon",
    })
    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=_private_key(),
            vapid_claims={"sub": _subject()},
            timeout=10,
        )
        return True, 201
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code not in (404, 410):
            log.warning("No se pudo enviar el aviso: %s", e)
        return False, code
    except Exception as e:
        log.warning("Error inesperado enviando aviso: %s", e)
        return False, None


async def notify_user(db, user_id: str, titulo: str, cuerpo: str,
                      url: str = "/dashboard", tag: str | None = None) -> int:
    """Avisa al teléfono de una persona en TODOS sus dispositivos.

    Limpia sola las suscripciones muertas (desinstaló la app, revocó el
    permiso), así la lista no se llena de basura con el tiempo.

    Nunca lanza error: si los avisos fallan, el resto del sistema sigue.
    """
    if not push_ready():
        return 0
    try:
        from sqlalchemy import select
        from starlette.concurrency import run_in_threadpool
        from app.models import PushSubscription

        rows = (await db.execute(
            select(PushSubscription).where(PushSubscription.user_id == user_id)
        )).scalars().all()
        enviados = 0
        for r in rows:
            sub = {"endpoint": r.endpoint, "keys": {"p256dh": r.p256dh, "auth": r.auth}}
            ok, code = await run_in_threadpool(send_push_sync, sub, titulo, cuerpo, url, tag)
            if ok:
                enviados += 1
            elif code in (404, 410):
                await db.delete(r)
        return enviados
    except Exception as e:
        log.warning("Fallo avisando a %s: %s", user_id, e)
        return 0
