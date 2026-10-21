"""GATE 6F-B — Contenido educativo de una clase de Video Dorismon.

Vocabulario, frases, notas breves y enlaces que el profesor comparte DURANTE la clase.
  · Fuente de verdad: la base (class_session_content). Un paquete de datos de LiveKit solo
    avisa "hay contenido nuevo" para volver a leer; si se pierde, nada desaparece.
  · Autoría: profesor responsable de la clase o Dirección (comprobado en la base).
  · Lectura: moderadores; estudiantes que PERTENECÍAN a la clase según class_session_audience,
    foto ACUMULATIVA (se añade quien tenía derecho o entró; nunca se recalcula ni se borra).
    Durante la clase, quien hoy puede entrar queda registrado al leer; una vez terminada, solo
    la foto: quien se inscribe meses después no ve el contenido de clases pasadas.
  · Texto plano (sin HTML): se guarda tal cual lo escribió el profesor, sin traducir. El
    frontend lo pinta como texto. Enlaces: solo http/https, sin credenciales en la URL.
  · Editar corrige sin cambiar el orden; ocultar retira sin borrar (sin hard delete).
"""
import asyncio
import random
import re
import unicodedata
from datetime import datetime, timedelta, timezone as tz
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    ClassSessionContent, ClassSessionAudience, ClassSession, User, UserRole, SessionStatus, VideoPresence,
)

TIPOS = ("vocabulario", "frase", "nota", "enlace")
# Campos permitidos por tipo y su longitud máxima (None = no se admite en ese tipo)
LIMITES = {
    "vocabulario": {"texto": 120, "traduccion": 200, "ejemplo": 300, "url": None},
    "frase":       {"texto": 300, "traduccion": 300, "ejemplo": None, "url": None},
    "nota":        {"texto": 500, "traduccion": None, "ejemplo": None, "url": None},
    "enlace":      {"texto": 120, "traduccion": None, "ejemplo": None, "url": 1000},
}
OBLIGATORIO = {"vocabulario": "texto", "frase": "texto", "nota": "texto", "enlace": "url"}
MAX_POR_CLASE = 300


def _limpiar(valor, multilinea: bool) -> str | None:
    """Texto plano: sin caracteres de control (salvo saltos de línea en notas)."""
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise HTTPException(400, "Contenido no válido")
    s = unicodedata.normalize("NFC", valor)
    s = "".join(ch for ch in s if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    s = s.replace("\t", " ")
    if multilinea:
        s = "\n".join(l.rstrip() for l in s.split("\n"))
        s = re.sub(r"\n{3,}", "\n\n", s)
    else:
        s = " ".join(s.split())
    s = s.strip()
    return s or None


def validar_url(url: str) -> str:
    if any(c.isspace() for c in url) or len(url) > LIMITES["enlace"]["url"]:
        raise HTTPException(400, "El enlace no es válido")
    p = urlsplit(url)
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "Solo se pueden compartir enlaces http:// o https://")
    if p.username or p.password:
        raise HTTPException(400, "El enlace no puede incluir usuario ni contraseña")
    return url


def validar(tipo: str, datos: dict, parcial: bool = False) -> dict:
    """Valida y limpia los campos según el tipo. parcial=True: solo los que vienen (edición)."""
    if tipo not in TIPOS:
        raise HTTPException(400, "Tipo de contenido no válido")
    lim = LIMITES[tipo]
    out = {}
    for campo in ("texto", "traduccion", "ejemplo", "url"):
        if parcial and campo not in datos:
            continue
        crudo = datos.get(campo)
        if lim[campo] is None:
            if crudo not in (None, ""):
                raise HTTPException(400, f"Este tipo de contenido no admite «{campo}»")
            continue
        if campo == "url" and isinstance(crudo, str):
            # Una URL NO se "limpia": quitar un carácter invisible podría cambiar el destino
            # (p. ej. el dominio). Con cualquier carácter de control o espacio interno → rechazo.
            crudo = crudo.strip()
            if any(unicodedata.category(ch)[0] == "C" or ch.isspace() for ch in crudo):
                raise HTTPException(400, "El enlace no es válido")
        valor = _limpiar(crudo, multilinea=(tipo == "nota" and campo == "texto"))
        if valor and len(valor) > lim[campo]:
            raise HTTPException(400, f"«{campo}» admite como máximo {lim[campo]} caracteres")
        if campo == "url" and valor:
            valor = validar_url(valor)
        out[campo] = valor
    obligatorio = OBLIGATORIO[tipo]
    if (not parcial or obligatorio in out) and not out.get(obligatorio):
        raise HTTPException(400, "Falta el enlace" if obligatorio == "url" else "Falta el texto")
    return out


def _utc(d):
    return d.replace(tzinfo=tz.utc) if d and d.tzinfo is None else d


def en_ventana(s: ClassSession, ahora: datetime | None = None) -> bool:
    """La misma ventana que /join: desde MINUTOS_ANTES antes hasta MINUTOS_GRACIA_FINAL después."""
    from app.services.livekit_service import MINUTOS_ANTES, MINUTOS_GRACIA_FINAL
    ahora = ahora or datetime.now(tz.utc)
    ini, fin = _utc(s.starts_at_utc), _utc(s.ends_at_utc)
    return not ((ini and ahora < ini - timedelta(minutes=MINUTOS_ANTES))
                or (fin and ahora > fin + timedelta(minutes=MINUTOS_GRACIA_FINAL)))


async def yo_activo(db: AsyncSession, user_id: str) -> User:
    me = await db.get(User, user_id)
    if not me or not me.is_active:
        raise HTTPException(403, "Cuenta desactivada")
    return me


def es_moderador(me: User, s: ClassSession) -> bool:
    return me.role == UserRole.super_admin or s.teacher_id == me.id


async def congelar(db: AsyncSession, s: ClassSession):
    """Añade a la foto quien tiene derecho AHORA y quien entró (nunca quita a nadie)."""
    from app.services.audience import destinatarios_de_clase
    derecho = set(await destinatarios_de_clase(db, s))
    asistio = {u for (u,) in (await db.execute(
        select(VideoPresence.user_id).where(VideoPresence.session_id == s.id))).all()}
    ya = {u for (u,) in (await db.execute(
        select(ClassSessionAudience.user_id).where(ClassSessionAudience.session_id == s.id))).all()}
    for u in sorted((derecho | asistio) - ya - {s.teacher_id}):
        db.add(ClassSessionAudience(session_id=s.id, user_id=u, reason="derecho" if u in derecho else "asistio"))


async def puede_leer(db: AsyncSession, me: User, s: ClassSession) -> bool:
    if es_moderador(me, s):
        return True
    if me.role != UserRole.student or s.status == SessionStatus.cancelled:
        return False
    en_foto = (await db.execute(select(ClassSessionAudience.id).where(
        ClassSessionAudience.session_id == s.id, ClassSessionAudience.user_id == me.id))).first() is not None
    if en_foto:
        return True
    # Durante la clase: quien HOY puede entrar (incluye quien entra tarde) queda en la foto
    if s.status == SessionStatus.scheduled and en_ventana(s):
        from app.services.audience import puede_acceder_a_clase
        if await puede_acceder_a_clase(db, me.id, s):
            db.add(ClassSessionAudience(session_id=s.id, user_id=me.id, reason="derecho"))
            try:
                await db.commit()
            except IntegrityError:      # otra petición suya lo registró a la vez
                await db.rollback()
                await db.refresh(s)
                await db.refresh(me)
            return True
    return False


def resumen(c: ClassSessionContent, moderador: bool) -> dict:
    d = {"id": c.id, "tipo": c.kind, "texto": c.texto, "traduccion": c.traduccion, "ejemplo": c.ejemplo,
         "url": c.url, "posicion": c.posicion,
         "creado": c.created_at.isoformat() if c.created_at else None,
         "editado": bool(c.updated_at)}
    if moderador:
        d["oculto"] = c.hidden_at is not None
    return d


async def listar(db: AsyncSession, s: ClassSession, moderador: bool) -> list[dict]:
    q = select(ClassSessionContent).where(ClassSessionContent.session_id == s.id)
    if not moderador:
        q = q.where(ClassSessionContent.hidden_at.is_(None))
    filas = (await db.execute(q.order_by(ClassSessionContent.posicion))).scalars().all()
    return [resumen(c, moderador) for c in filas]


async def crear(db: AsyncSession, s: ClassSession, me: User, tipo: str, datos: dict) -> ClassSessionContent:
    limpio = validar(tipo, datos)
    total = (await db.execute(select(func.count()).select_from(ClassSessionContent)
                              .where(ClassSessionContent.session_id == s.id))).scalar() or 0
    if total >= MAX_POR_CLASE:
        raise HTTPException(400, f"Esta clase ya tiene el máximo de {MAX_POR_CLASE} contenidos")
    sid, uid = s.id, me.id      # tras un rollback los objetos caducan: se usan los ids
    await congelar(db, s)
    try:
        await db.commit()
    except IntegrityError:      # otra publicación simultánea ya añadió a esas personas
        await db.rollback()
    for intento in range(20):   # posición única por clase (publicaciones simultáneas: se reintenta)
        pos = ((await db.execute(select(func.max(ClassSessionContent.posicion))
                                 .where(ClassSessionContent.session_id == sid))).scalar() or 0) + 1
        c = ClassSessionContent(session_id=sid, kind=tipo, posicion=pos, created_by=uid,
                                created_at=datetime.now(tz.utc), **limpio)
        db.add(c)
        try:
            await db.commit()
            return c
        except IntegrityError:
            await db.rollback()
            await asyncio.sleep(random.uniform(0.005, 0.03) * (1 + intento))   # se separan los reintentos
    raise HTTPException(409, "No se pudo guardar. Inténtalo de nuevo.")
