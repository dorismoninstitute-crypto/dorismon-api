"""GATE 6H-B — Biblioteca de grabaciones y reproducción privada.

ACCESO (una sola regla, la misma para listar y para reproducir):
  · Dirección (super_admin, comprobado en la BASE, no en el token) → todas.
  · Profesor RESPONSABLE de la clase (ClassSession.teacher_id) → las de sus clases.
    Ser profesor NO da acceso a clases ajenas.
  · Estudiante → SOLO si está en video_recording_audience de ESA grabación (foto histórica
    de 6H-A / 6H-A.1). Nunca se recalcula con la inscripción actual: quien entra al grupo
    después no ve grabaciones anteriores; quien sale conserva su acceso histórico.
    Además: cuenta activa (is_active) y clase no cancelada. La foto no se borra nunca.
    GATE 6H-B.1 — El bloqueo 6G-A (video_access_blocks) solo impide VOLVER A ENTRAR a la
    videoclase (/join, heartbeat, interacción); NO revoca la grabación: quien está en la foto
    la sigue viendo aunque lo hayan sacado. El bloqueo se conserva para auditoría. Quitar el
    acceso a una grabación sería una función futura, explícita y auditada (no existe).
  · Estudiante: solo ve grabaciones "ready". Profesor / Dirección ven todos los estados.

ALMACENAMIENTO PRIVADO (S3 compatible, Cloudflare R2):
  · Solo con RECORDINGS_R2_BUCKET explícito (nunca R2_BUCKET por defecto). La base guarda
    bucket + clave; la URL firmada se genera al pedirla, vive URL_TTL_SEGUNDOS y no se guarda
    ni se registra. Credenciales de lectura opcionales y separadas
    (RECORDINGS_R2_READ_ACCESS_KEY / RECORDINGS_R2_READ_SECRET_KEY); si faltan, las de R2.
  · SigV4: la URL lleva el ID de la clave de acceso (X-Amz-Credential), NUNCA el secreto. Es
    parte del protocolo; por eso conviene una clave de solo lectura para firmar.
  · Cada petición (incluidas las Range del reproductor) se valida al llegar: una respuesta ya
    en curso continúa, pero una NUEVA petición tras la expiración recibe 403 → el reproductor
    pide otra URL (renovación controlada).
"""
import asyncio
import logging
from datetime import datetime, timezone as tz

from sqlalchemy import select, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    VideoRecording, VideoRecordingAudience, ClassSession, User, UserRole, SessionStatus,
)

log = logging.getLogger(__name__)
# botocore registra en DEBUG la petición canónica (credencial y firma de la URL): nunca a los logs
for _nombre in ("botocore", "boto3", "s3transfer"):
    logging.getLogger(_nombre).setLevel(logging.WARNING)

URL_TTL_SEGUNDOS = 600          # 10 min: R2/S3 admiten hasta 7 días; corto a propósito
POR_PAGINA = 20
ESTADOS = ("starting", "recording", "stopping", "processing", "ready", "failed")
TEXTO_ESTADO = {"starting": "Iniciando", "recording": "Grabando", "stopping": "Deteniendo",
                "processing": "Procesando", "ready": "Lista", "failed": "Fallida"}


# ----------------------------------------------------------------- almacenamiento
def config_lectura() -> dict | None:
    """Configuración para LEER grabaciones (firmar / comprobar). None si no está completa."""
    from app.services.grabaciones import _cfg
    if not _cfg("RECORDINGS_R2_BUCKET"):
        return None            # sin bucket de grabaciones explícito no hay biblioteca
    clave = _cfg("RECORDINGS_R2_READ_ACCESS_KEY") or _cfg("R2_ACCESS_KEY")
    secreto = _cfg("RECORDINGS_R2_READ_SECRET_KEY") or _cfg("R2_SECRET_KEY")
    endpoint = _cfg("RECORDINGS_S3_ENDPOINT") or (
        f"https://{_cfg('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com" if _cfg("R2_ACCOUNT_ID") else "")
    if not (clave and secreto and endpoint):
        return None
    return {"endpoint": endpoint, "region": _cfg("RECORDINGS_S3_REGION") or "auto",
            "access_key": clave, "secret": secreto}


def _cliente(cfg: dict):
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3", endpoint_url=cfg["endpoint"], region_name=cfg["region"],
        aws_access_key_id=cfg["access_key"], aws_secret_access_key=cfg["secret"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                      connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}))


async def objeto_existe(cfg: dict, bucket: str, clave: str) -> str:
    """"si" | "no" | "error" (HEAD del objeto; no descarga nada)."""
    def _head():
        from botocore.exceptions import ClientError
        try:
            _cliente(cfg).head_object(Bucket=bucket, Key=clave)
            return "si"
        except ClientError as e:
            codigo = str((e.response or {}).get("Error", {}).get("Code", ""))
            return "no" if codigo in ("404", "NoSuchKey", "NotFound") else "error"
        except Exception:
            return "error"
    return await asyncio.to_thread(_head)


def firmar_url(cfg: dict, bucket: str, clave: str, segundos: int = URL_TTL_SEGUNDOS) -> str:
    """URL GET temporal (SigV4, cálculo local, sin red). NO se guarda ni se registra."""
    return _cliente(cfg).generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": clave, "ResponseContentType": "video/mp4",
                "ResponseContentDisposition": "inline"},
        ExpiresIn=segundos)


# ----------------------------------------------------------------------- acceso
async def _yo(db: AsyncSession, user_id: str) -> User | None:
    me = await db.get(User, user_id)
    return me if me and me.is_active else None


async def puede_ver(db: AsyncSession, me: User, rec: VideoRecording, sesion: ClassSession) -> bool:
    """La regla ÚNICA de acceso (ver arriba). No mira el estado: eso lo decide quien llama."""
    if rec.deleted_at is not None or rec.status == "deleted":
        return False
    if me.role == UserRole.super_admin:
        return True
    if sesion.teacher_id == me.id:
        return True
    if me.role != UserRole.student:
        return False                       # otro profesor: NO por ser profesor
    if sesion.status == SessionStatus.cancelled:
        return False
    en_foto = (await db.execute(select(VideoRecordingAudience.id).where(
        VideoRecordingAudience.recording_id == rec.id,
        VideoRecordingAudience.user_id == me.id))).first() is not None
    return en_foto            # GATE 6H-B.1: sin mirar el bloqueo 6G-A


def _iso(d):
    if not d:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=tz.utc)
    return d.isoformat()


async def listar(db: AsyncSession, me: User, pagina: int = 1, estado: str | None = None,
                 profesor: str | None = None, desde: str | None = None, hasta: str | None = None,
                 q: str | None = None) -> dict:
    """Biblioteca según el rol (paginada; recientes primero)."""
    from app.services.grabaciones import _error_visible, sincronizar
    pagina = max(1, int(pagina or 1))
    cond = [VideoRecording.deleted_at.is_(None), VideoRecording.status != "deleted"]
    base = select(VideoRecording, ClassSession).join(ClassSession, ClassSession.id == VideoRecording.session_id)
    rol = "direccion" if me.role == UserRole.super_admin else (
        "estudiante" if me.role == UserRole.student else "profesor")
    if rol == "estudiante":
        base = base.join(VideoRecordingAudience, and_(VideoRecordingAudience.recording_id == VideoRecording.id,
                                                      VideoRecordingAudience.user_id == me.id))
        cond += [VideoRecording.status == "ready", ClassSession.status != SessionStatus.cancelled]
        # GATE 6H-B.1: el bloqueo 6G-A no filtra la biblioteca (misma regla que puede_ver)
    elif rol == "profesor":
        cond.append(ClassSession.teacher_id == me.id)
    if estado in ESTADOS and rol != "estudiante":
        cond.append(VideoRecording.status == estado)
    if profesor and rol == "direccion":
        cond.append(ClassSession.teacher_id == profesor)
    if q and rol == "direccion":
        cond.append(ClassSession.title.ilike(f"%{q.strip()[:80]}%"))
    for valor, op in ((desde, "desde"), (hasta, "hasta")):
        if valor and rol != "estudiante":
            try:
                d = datetime.fromisoformat(valor[:10])
            except ValueError:
                continue
            cond.append(ClassSession.starts_at_utc >= d if op == "desde"
                        else ClassSession.starts_at_utc < d.replace(hour=23, minute=59, second=59))
    consulta = base.where(*cond)
    total = (await db.execute(select(func.count()).select_from(consulta.subquery()))).scalar() or 0
    filas = (await db.execute(consulta.order_by(VideoRecording.requested_at.desc(), VideoRecording.started_at.desc(), VideoRecording.id)
                              .offset((pagina - 1) * POR_PAGINA).limit(POR_PAGINA))).all()

    # Reconciliación de 6H-A (ListEgress) para lo que sigue en curso: la MISMA fuente de verdad.
    if rol != "estudiante":
        for rec, _ in [f for f in filas if f[0].status in ("starting", "recording", "stopping", "processing")][:5]:
            await sincronizar(db, rec)

    # Número de segmento dentro de su clase (entre los visibles para este rol)
    sesiones = {s.id for _, s in filas}
    partes: dict[str, list[str]] = {}
    if sesiones:
        cs = [VideoRecording.session_id.in_(sesiones), VideoRecording.deleted_at.is_(None),
              VideoRecording.status != "deleted"]
        if rol == "estudiante":
            cs.append(VideoRecording.status == "ready")
        for rid, sid in (await db.execute(select(VideoRecording.id, VideoRecording.session_id).where(*cs)
                                          .order_by(VideoRecording.requested_at, VideoRecording.started_at, VideoRecording.id))).all():
            partes.setdefault(sid, []).append(rid)
    nombres = {}
    for _, s in filas:
        if s.teacher_id not in nombres:
            u = await db.get(User, s.teacher_id)
            nombres[s.teacher_id] = u.full_name if u else "—"
    items = []
    for rec, s in filas:
        lista = partes.get(s.id, [rec.id])
        items.append({
            "id": rec.id, "session_id": s.id, "titulo": s.title or "Clase",
            "profesor": nombres.get(s.teacher_id), "clase_inicio": _iso(s.starts_at_utc),
            "grabacion_inicio": _iso(rec.started_at), "duracion_segundos": rec.duration_seconds,
            "estado": rec.status, "estado_texto": TEXTO_ESTADO.get(rec.status, rec.status),
            "parte": (lista.index(rec.id) + 1) if rec.id in lista else 1, "partes": len(lista),
            "reproducible": rec.status == "ready",
            "disponible_hasta": _iso(rec.retention_until),
            **({"error": _error_visible(rec)} if rec.status == "failed" and rol != "estudiante" else {}),
        })
    out = {"rol": rol, "items": items, "total": total, "pagina": pagina, "por_pagina": POR_PAGINA,
           "paginas": max(1, -(-total // POR_PAGINA))}
    if rol == "direccion":
        prof = (await db.execute(select(User.id, User.full_name).join(ClassSession, ClassSession.teacher_id == User.id)
                                 .join(VideoRecording, VideoRecording.session_id == ClassSession.id)
                                 .where(VideoRecording.deleted_at.is_(None)).distinct()
                                 .order_by(User.full_name))).all()
        out["profesores"] = [{"id": i, "nombre": n} for i, n in prof]
    return out
