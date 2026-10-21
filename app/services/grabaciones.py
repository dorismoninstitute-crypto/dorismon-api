"""GATE 6H-A — Grabación de clases de Video Dorismon con LiveKit Egress (RoomComposite).

API oficial (livekit/protocol, livekit_egress.proto — servicio Twirp `livekit.Egress`):
  StartRoomCompositeEgress(RoomCompositeEgressRequest) → EgressInfo
  StopEgress(StopEgressRequest{egress_id})              → EgressInfo
  ListEgress(ListEgressRequest{room_name|egress_id})    → ListEgressResponse{items}
  Permiso: token de servidor con `roomRecord` (no sale nunca del backend).
  EgressStatus: STARTING, ACTIVE, ENDING, COMPLETE, FAILED, ABORTED, LIMIT_REACHED.
  RoomComposite se detiene sola cuando salen todos; NO se confía en eso: al detener o
  finalizar se pide StopEgress explícitamente. Los webhooks (egress_started / updated /
  ended) se verifican como en livekit/protocol webhook/verifier.go (JWT en Authorization
  firmado con la API key/secret + claim sha256 = base64(sha256(cuerpo))). Si un webhook no
  llega, el estado se reconcilia preguntando a LiveKit (ListEgress).

El archivo lo sube LiveKit directamente al almacenamiento PRIVADO configurado
(S3 compatible: Cloudflare R2). En la base solo quedan bucket + clave del objeto.
Sin almacenamiento configurado NO se graba (nunca un bucket público por defecto).
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone as tz

import jwt
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import VideoRecording, VideoRecordingAudience, VideoPresence
from app.services.audit import log_action
from app.services.livekit_service import room_name, _http_base

log = logging.getLogger(__name__)

ACTIVOS = ("starting", "recording", "stopping")          # ocupan la sala
TERMINALES = ("ready", "failed", "deleted")
_RANGO = {"starting": 0, "recording": 1, "stopping": 2, "processing": 3, "ready": 4, "failed": 4, "deleted": 5}
_ESTADO_LIVEKIT = {"EGRESS_STARTING": "starting", "EGRESS_ACTIVE": "recording", "EGRESS_ENDING": "processing",
                   "EGRESS_COMPLETE": "complete", "EGRESS_FAILED": "failed", "EGRESS_ABORTED": "failed",
                   "EGRESS_LIMIT_REACHED": "failed",
                   0: "starting", 1: "recording", 2: "processing", 3: "complete", 4: "failed", 5: "failed", 6: "failed"}
_DETENIENDO: set = set()       # StopEgress en curso por grabación (dentro de este proceso)


def _cfg(nombre: str) -> str:
    from app.core.config import settings
    return (os.getenv(nombre) or getattr(settings, nombre, "") or "").strip()


def almacenamiento() -> dict | None:
    """Destino PRIVADO de las grabaciones, o None si no está configurado.

    Usa las credenciales R2 que Dorismon ya define (R2_ACCOUNT_ID / R2_ACCESS_KEY /
    R2_SECRET_KEY) pero exige un bucket EXPLÍCITO para grabaciones (RECORDINGS_R2_BUCKET):
    no se reutiliza R2_BUCKET="dorismon" sin saber si es privado.
    """
    bucket = _cfg("RECORDINGS_R2_BUCKET")
    clave, secreto = _cfg("R2_ACCESS_KEY"), _cfg("R2_SECRET_KEY")
    endpoint = _cfg("RECORDINGS_S3_ENDPOINT") or (
        f"https://{_cfg('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com" if _cfg("R2_ACCOUNT_ID") else "")
    if not (bucket and clave and secreto and endpoint):
        return None
    return {"provider": "r2", "bucket": bucket, "endpoint": endpoint,
            "region": _cfg("RECORDINGS_S3_REGION") or "auto", "access_key": clave, "secret": secreto}


def _token_grabacion() -> str:
    api_key, api_secret = _cfg("LIVEKIT_API_KEY"), _cfg("LIVEKIT_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError("LiveKit no está configurado")
    now = int(time.time())
    return jwt.encode({"iss": api_key, "sub": api_key, "nbf": now - 10, "exp": now + 600,
                       "video": {"roomRecord": True}}, api_secret, algorithm="HS256")


async def _egress(metodo: str, cuerpo: dict):
    """Una llamada al servicio Egress. Nunca registra el cuerpo (lleva credenciales del almacenamiento)."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            return await c.post(f"{_http_base()}/twirp/livekit.Egress/{metodo}",
                                headers={"Authorization": f"Bearer {_token_grabacion()}",
                                         "Content-Type": "application/json"},
                                json=cuerpo)
    except Exception as e:
        log.warning("LiveKit Egress %s sin respuesta: %s", metodo, type(e).__name__)
        return None


def _ahora():
    return datetime.now(tz.utc)


# GATE 6H-A.1 — el texto de error del PROVEEDOR puede traer credenciales, tokens o URLs
# firmadas. Antes de guardarlo o registrarlo se redacta, se acorta y se conserva el código.
_REDACTADO = "[REDACTADO]"
_PATRONES_SENSIBLES = [
    re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),            # JWT
    re.compile(r"(?i)\bbearer\s+[^\s,;\"']+"),                                          # Authorization
    re.compile(r"(?i)\b(authorization|x-amz-[a-z-]+|signature|sig|token|access[_-]?token|"
               r"secret|secret[_-]?key|access[_-]?key(?:[_-]?id)?|api[_-]?key|api[_-]?secret|"
               r"password|passwd|credential|credentials|session[_-]?token)"
               r"(\s*[=:]\s*|\s+)[\"']?[^\s,;&\"'}]+"),                                   # clave=valor
    re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{12,}\b"),                                          # id de clave AWS
    re.compile(r"\b[A-Za-z0-9+/_=-]{32,}\b"),                                             # secretos largos (R2: 32/64 hex)
]
_URL_CON_QUERY = re.compile(r"(https?://[^\s?#\"'<>]+)\?[^\s\"'<>]*")                     # URLs firmadas


def sanear_error(texto, limite: int = 300) -> str:
    """Texto de error del proveedor apto para la base de datos y los logs."""
    s = str(texto or "")
    for nombre in ("LIVEKIT_API_SECRET", "LIVEKIT_API_KEY", "R2_SECRET_KEY", "R2_ACCESS_KEY"):
        valor = _cfg(nombre)
        if len(valor) >= 6:
            s = s.replace(valor, _REDACTADO)
    s = _URL_CON_QUERY.sub(lambda m: m.group(1) + "?" + _REDACTADO, s)
    for patron in _PATRONES_SENSIBLES:
        s = patron.sub(_REDACTADO, s)
    s = " ".join(s.split())
    return s[:limite] if len(s) <= limite else s[:limite - 1] + "…"


# PREPROD-2 — Forma REAL comprobada en LiveKit Cloud (Twirp JSON, sin SDK; httpx → dict):
# EgressInfo llega en snake_case (egress_id, room_name, status, file_results, started_at,
# ended_at, error, error_code…), los int64 como TEXTO en nanosegundos ("1792286879032663130";
# "0" o incluso negativo si aún no hay valor) y file_results puede venir vacío con el archivo
# en el campo antiguo `file`. Los webhooks de LiveKit (protojson) usan camelCase (egressInfo,
# egressId…). snake_case es la forma primaria; camelCase, compatibilidad.
def _c(d, snake: str):
    """Campo de un objeto de LiveKit en snake_case (o camelCase); None si no está."""
    if not isinstance(d, dict):
        return None
    if snake in d:
        return d[snake]
    partes = snake.split("_")
    return d.get(partes[0] + "".join(p.title() for p in partes[1:]))


def _entero(valor):
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _ns_a_fecha(valor):
    try:
        n = int(valor or 0)
        return datetime.fromtimestamp(n / 1e9, tz.utc) if n > 0 else None
    except (TypeError, ValueError):
        return None


def aplicar_info(rec: VideoRecording, info: dict) -> bool:
    """Aplica un EgressInfo (respuesta o webhook) al registro. Devuelve True si cambió.

    Solo avanza (eventos repetidos o fuera de orden no hacen retroceder el estado), nunca
    sale de un estado terminal y solo marca "ready" si LiveKit informa un archivo real.
    """
    if rec.status in TERMINALES:
        return False
    nuevo = _ESTADO_LIVEKIT.get(_c(info, "status"), None)
    if nuevo is None:
        return False
    antes = rec.status
    if nuevo == "complete":
        lista, uno = _c(info, "file_results"), _c(info, "file")
        archivos = lista or ([uno] if uno else [])
        f = archivos[0] if archivos else {}
        tam = _entero(_c(f, "size")) if archivos else 0
        if (lista is None and uno is None) or tam is None:
            # Sin datos legibles del archivo no se decide ("ready" ni "failed"): se reconcilia luego
            log.error("Grabación %s: LiveKit informó COMPLETE sin datos de archivo legibles", rec.id)
            return False
        if tam > 0:
            rec.status, rec.ready_at = "ready", _ahora()
            rec.size_bytes = tam
            rec.duration_seconds = int((_entero(_c(f, "duration")) or 0) / 1e9) or None
        else:
            rec.status, rec.failed_at = "failed", _ahora()
            rec.error_message = "La grabación terminó sin archivo en el almacenamiento"
        rec.ended_at = _ns_a_fecha(_c(info, "ended_at")) or _ahora()
    elif nuevo == "failed":
        rec.status, rec.failed_at = "failed", _ahora()
        rec.error_code = _entero(_c(info, "error_code")) or None
        rec.error_message = sanear_error(_c(info, "error") or "Fallo del proveedor de grabación")  # GATE 6H-A.1
        log.warning("Grabación %s falló (código %s): %s", rec.id, rec.error_code, rec.error_message)
        rec.ended_at = _ns_a_fecha(_c(info, "ended_at")) or rec.ended_at
    else:
        if _RANGO[nuevo] < _RANGO[rec.status]:
            return False                      # fuera de orden / repetido
        if rec.status == "stopping" and nuevo == "recording":
            return False                      # aún no aplicó la parada
        rec.status = nuevo
        if nuevo == "recording" and not rec.started_at:
            rec.started_at = _ns_a_fecha(_c(info, "started_at")) or _ahora()
    if not rec.started_at and _c(info, "started_at"):
        rec.started_at = _ns_a_fecha(_c(info, "started_at"))
    rec.activa = rec.session_id if rec.status in ACTIVOS else None
    return rec.status != antes


async def _registrar_cambio(db: AsyncSession, rec: VideoRecording, antes: str, actor: str | None = None):
    if rec.status != antes and rec.status in ("recording", "ready", "failed"):
        await log_action(db, actor, f"video_recording_{rec.status}", "video", target_id=rec.session_id,
                         details=f"recording_id={rec.id}")


async def congelar_audiencia(db: AsyncSession, sesion, recording_id: str, solo_asistencia: bool = False):
    """Foto de quién tenía derecho (misma regla que el acceso) + quién entró por /join.

    GATE 6H-A.1 — El derecho se fija UNA vez, al crear la grabación. Después (al detener)
    solo se añade quien de verdad entró a la clase (solo_asistencia=True): quien se inscribe
    más tarde en el grupo NO entra solo en esta audiencia histórica, y nada se borra."""
    from app.services.audience import destinatarios_de_clase
    derecho = set() if solo_asistencia else set(await destinatarios_de_clase(db, sesion))
    asistio = {u for (u,) in (await db.execute(
        select(VideoPresence.user_id).where(VideoPresence.session_id == sesion.id))).all()}
    asistio.discard(sesion.teacher_id)
    ya = {u for (u,) in (await db.execute(select(VideoRecordingAudience.user_id).where(
        VideoRecordingAudience.recording_id == recording_id))).all()}
    faltan = sorted((derecho | asistio) - ya)
    if faltan:
        # PREPROD-3: dos "detener" a la vez pueden añadir a la misma persona; en PostgreSQL el
        # UNIQUE (recording_id, user_id) rechazaba a una (500). ON CONFLICT DO NOTHING: la otra ya
        # la añadió; sin rollback (no caducan los objetos que el llamador sigue usando).
        if db.bind.dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as _insert
        else:
            from sqlalchemy.dialects.sqlite import insert as _insert
        await db.execute(_insert(VideoRecordingAudience).values([
            {"id": str(uuid.uuid4()), "recording_id": recording_id, "user_id": u,
             "reason": "derecho" if u in derecho else "asistio"} for u in faltan
        ]).on_conflict_do_nothing(index_elements=["recording_id", "user_id"]))
    await db.commit()


async def grabacion_activa(db: AsyncSession, session_id: str) -> VideoRecording | None:
    return (await db.execute(select(VideoRecording).where(
        VideoRecording.session_id == session_id, VideoRecording.status.in_(ACTIVOS)))).scalars().first()


async def grabando_confirmado(db: AsyncSession, session_id: str) -> bool:
    """Para avisar en la pantalla previa: solo si el PROVEEDOR confirmó (no al pulsar)."""
    return (await db.execute(select(VideoRecording.id).where(
        VideoRecording.session_id == session_id, VideoRecording.status == "recording"))).first() is not None


async def iniciar(db: AsyncSession, sesion, actor: str):
    """Devuelve (grabación, resultado): "iniciada" | "ya_activa" | "sin_almacenamiento" | "error"."""
    almac = almacenamiento()
    if not almac:
        return None, "sin_almacenamiento"
    session_id, sala = sesion.id, room_name(sesion.id)
    rec = VideoRecording(session_id=session_id, activa=session_id, status="starting", requested_by=actor,
                         storage_provider=almac["provider"], storage_bucket=almac["bucket"], mime_type="video/mp4")
    rec.storage_key = f"grabaciones/{session_id}/{rec.id or ''}"
    db.add(rec)
    try:
        await db.flush()
        rec.storage_key = f"grabaciones/{session_id}/{rec.id}.mp4"
        await log_action(db, actor, "video_recording_requested", "video", target_id=session_id,
                         details=f"recording_id={rec.id}")
        await db.commit()
    except IntegrityError:
        # Otra petición ya la inició (doble clic / 10 a la vez): NO se crea otra Egress
        await db.rollback()
        return await grabacion_activa(db, session_id), "ya_activa"
    await congelar_audiencia(db, sesion, rec.id)
    r = await _egress("StartRoomCompositeEgress", {
        "roomName": sala,
        "layout": "speaker",
        "fileOutputs": [{
            "fileType": "MP4",
            "filepath": rec.storage_key,
            "disableManifest": True,   # PREPROD-2: sin el .json de Egress (Dorismon no lo usa; quedaría huérfano)
            "s3": {"accessKey": almac["access_key"], "secret": almac["secret"], "region": almac["region"],
                   "endpoint": almac["endpoint"], "bucket": almac["bucket"], "forcePathStyle": True},
        }],
    })
    if r is None or r.status_code != 200:
        rec.status, rec.activa, rec.failed_at = "failed", None, _ahora()
        rec.error_message = ("LiveKit no respondió al iniciar la grabación" if r is None
                             else f"LiveKit rechazó iniciar la grabación (HTTP {r.status_code})")
        await log_action(db, actor, "video_recording_failed", "video", target_id=session_id,
                         details=f"recording_id={rec.id}; inicio")
        await db.commit()
        return rec, "error"
    try:
        info = r.json()
    except ValueError:
        info = None
    rec.egress_id = _c(info, "egress_id")
    if not rec.egress_id:
        # Sin identificador no se podría detener ni reconciliar: se libera el slot con un error claro
        log.error("Grabación %s: LiveKit no devolvió egress_id al iniciar", rec.id)
        rec.status, rec.activa, rec.failed_at = "failed", None, _ahora()
        rec.error_message = "LiveKit no devolvió el identificador de la grabación"
        await log_action(db, actor, "video_recording_failed", "video", target_id=session_id,
                         details=f"recording_id={rec.id}; sin egress_id")
        await db.commit()
        return rec, "error"
    antes = rec.status
    aplicar_info(rec, info)
    await _registrar_cambio(db, rec, antes, actor)
    await db.commit()
    return rec, "iniciada"


async def sincronizar(db: AsyncSession, rec: VideoRecording) -> VideoRecording:
    """Pregunta a LiveKit por esta grabación (si un webhook se perdió o se retrasó)."""
    if not rec or not rec.egress_id or rec.status in TERMINALES:
        return rec
    r = await _egress("ListEgress", {"egressId": rec.egress_id})
    if r is None or r.status_code != 200:
        return rec
    try:
        items = _c(r.json(), "items") or []
    except ValueError:
        items = []
    info = next((i for i in items if _c(i, "egress_id") == rec.egress_id), None)
    if info:
        antes = rec.status
        if aplicar_info(rec, info):
            await _registrar_cambio(db, rec, antes)
            await db.commit()
    return rec


async def detener(db: AsyncSession, session_id: str, actor: str | None):
    """Devuelve (grabación, resultado): "detenida" | "ya_detenida" | "sin_grabacion" |
    "iniciando" (aún sin id del proveedor) | "error" (LiveKit no respondió; se puede reintentar)."""
    rec = await grabacion_activa(db, session_id)
    if not rec:
        return None, "sin_grabacion"
    if not rec.egress_id:
        return rec, "iniciando"
    if rec.status in ("starting", "recording"):
        cambio = await db.execute(
            update(VideoRecording)
            .where(VideoRecording.id == rec.id, VideoRecording.status.in_(("starting", "recording")))
            .values(status="stopping", stop_requested_by=actor, stop_requested_at=_ahora())
            .execution_options(synchronize_session=False))
        if cambio.rowcount != 1:
            # Otra petición ya la está deteniendo. commit (no rollback: no expira objetos)
            await db.commit()
            await db.refresh(rec)
            return rec, "ya_detenida"
        await log_action(db, actor, "video_recording_stop_requested", "video", target_id=session_id,
                         details=f"recording_id={rec.id}")
        await db.commit()
        await db.refresh(rec)
    if rec.id in _DETENIENDO:
        return rec, "ya_detenida"
    _DETENIENDO.add(rec.id)
    try:
        r = await _egress("StopEgress", {"egressId": rec.egress_id})
        if r is not None and r.status_code == 200:
            antes = rec.status
            aplicar_info(rec, r.json() or {})
            await _registrar_cambio(db, rec, antes, actor)
            await db.commit()
            return rec, "detenida"
        if r is not None and r.status_code < 500:
            # p. ej. ya no estaba activa (terminó sola): se pregunta su estado real
            await sincronizar(db, rec)
            return rec, "detenida"
        return rec, "error"
    finally:
        _DETENIENDO.discard(rec.id)


async def detener_para_finalizar(db: AsyncSession, session_id: str, actor: str | None, espera_max: float = 8.0):
    """Al FINALIZAR: detener la grabación activa ANTES de revocar tokens y cerrar la sala,
    y esperar (poco) a que LiveKit deje de grabar. Nunca lanza ni bloquea el cierre: si
    falla, la clase se finaliza igual y la sala cerrada termina la grabación (riesgo
    documentado: archivo posiblemente incompleto; el estado se reconcilia luego)."""
    try:
        rec, res = await detener(db, session_id, actor)
        if not rec:
            return "sin_grabacion"
        limite = time.monotonic() + espera_max
        while rec.status in ACTIVOS and time.monotonic() < limite:
            await asyncio.sleep(1)
            await sincronizar(db, rec)
        return res if rec.status not in ACTIVOS else "sigue_activa"
    except Exception as e:  # nunca impide finalizar
        log.error("No se pudo detener la grabación de %s al finalizar: %s", room_name(session_id), type(e).__name__)
        return "error"


def verificar_webhook(cuerpo: bytes, autorizacion: str) -> dict | None:
    """Como livekit/protocol webhook.Receive: JWT (API key/secret) + sha256 del cuerpo."""
    api_key, api_secret = _cfg("LIVEKIT_API_KEY"), _cfg("LIVEKIT_API_SECRET")
    token = (autorizacion or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not (token and api_key and api_secret):
        return None
    try:
        claims = jwt.decode(token, api_secret, algorithms=["HS256"], options={"verify_aud": False})
    except jwt.PyJWTError:
        return None
    if claims.get("iss") != api_key:
        return None
    esperado = base64.b64encode(hashlib.sha256(cuerpo).digest()).decode()
    if not hmac.compare_digest(str(claims.get("sha256") or ""), esperado):
        return None
    try:
        return json.loads(cuerpo)
    except ValueError:
        return None


async def recibir_evento(db: AsyncSession, evento: dict) -> str:
    """Aplica un evento egress_* YA VERIFICADO. Solo toca una grabación nuestra cuyo
    egress_id y sala coinciden (un egress_id ajeno no modifica nada)."""
    if not str(evento.get("event") or "").startswith("egress_"):
        return "ignorado"
    info = _c(evento, "egress_info") or {}
    eid = _c(info, "egress_id")
    rec = (await db.execute(select(VideoRecording).where(
        VideoRecording.egress_id == eid))).scalars().first() if eid else None
    if not rec or _c(info, "room_name") != room_name(rec.session_id):
        return "desconocido"
    antes = rec.status
    if aplicar_info(rec, info):
        await _registrar_cambio(db, rec, antes)
        await db.commit()
        return "aplicado"
    return "sin_cambios"


def resumen(rec: VideoRecording | None) -> dict | None:
    """Lo que se muestra al moderador. Sin bucket, clave, URLs ni secretos."""
    if not rec:
        return None
    return {"id": rec.id, "estado": rec.status,
            "solicitada": rec.requested_at.isoformat() if rec.requested_at else None,
            "inicio": rec.started_at.isoformat() if rec.started_at else None,
            "fin": rec.ended_at.isoformat() if rec.ended_at else None,
            "duracion_segundos": rec.duration_seconds, "tamano_bytes": rec.size_bytes,
            "error": _error_visible(rec) if rec.status == "failed" else None}


def _error_visible(rec: VideoRecording) -> str:
    """Los mensajes propios se muestran tal cual; el texto crudo del proveedor (puede nombrar
    el endpoint o el bucket) queda solo en la base de datos."""
    m = rec.error_message or ""
    # GATE 6H-A.1 — solo los mensajes PROPIOS exactos (un error del proveedor podría empezar por "LiveKit")
    if m in _MENSAJES_PROPIOS or m.startswith("LiveKit rechazó iniciar la grabación (HTTP "):
        return m
    return "El proveedor no pudo completar la grabación (p. ej. no pudo guardar el archivo)."


_MENSAJES_PROPIOS = {"LiveKit no respondió al iniciar la grabación",
                     "LiveKit no devolvió el identificador de la grabación",
                     "La grabación terminó sin archivo en el almacenamiento"}
