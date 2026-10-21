"""V3.9.26 — Entrar a la sala de video de una clase (dentro de Dorismon)."""
from typing import Annotated
from datetime import datetime, timedelta, timezone as tz

from fastapi import APIRouter, Depends, HTTPException, Request  # GATE 6H-A: Request (webhook)
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, CurrentUser
from app.core.db import get_db
from app.models import (
    ClassSession, Enrollment, User, UserRole, SessionStatus,
    EventRegistration, VideoPresence,
)
from app.services.livekit_service import (
    livekit_ready, livekit_url, build_token, room_name,
    mute_participant, remove_participant, list_participants,
    MINUTOS_ANTES, MINUTOS_DESPUES, MINUTOS_GRACIA_FINAL,
)

# Mínimo de permanencia para sugerir "presente" en la asistencia
MINUTOS_PARA_PRESENTE = 10

router = APIRouter(prefix="/video", tags=["video"])


@router.get("/status")
async def video_status(user: Annotated[CurrentUser, Depends(get_current_user)]):
    """¿Está disponible el video propio? Lo usa el admin para saber si puede
    ofrecer la opción 'Video de Dorismon' al crear una clase."""
    return {"ready": livekit_ready(), "url_set": bool(livekit_url())}


@router.post("/sessions/{session_id}/join")
async def join_class_video(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Da permiso de entrada a la sala de video de una clase.

    CONTROL DE ACCESO (lo que un enlace de Meet no puede hacer):
    - El profesor de la clase entra como moderador.
    - El admin puede entrar (para supervisar o resolver un problema).
    - El estudiante entra solo si esa clase es suya: es su clase privada o de
      prueba, está inscrito en el nivel, o está registrado si es un evento.
    - Cualquier otro recibe 403, aunque tenga el enlace.

    VENTANA DE TIEMPO: se puede entrar desde 15 minutos antes y hasta 20
    minutos después de terminar. Fuera de eso, no.
    """
    # GATE 6G-D — nadie pide cientos de permisos de entrada: límite por persona
    from app.services.limites_video import comprobar
    comprobar("join_usuario", user.user_id)
    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    if s.status == SessionStatus.cancelled:
        raise HTTPException(400, "Esta clase fue cancelada")
    # GATE 0 — Una clase FINALIZADA no admite entrada normal, tampoco desde
    # la API: el backend es la fuente de verdad, no solo el botón oculto.
    if s.status == SessionStatus.completed:
        raise HTTPException(400, "Esta clase ya fue finalizada")

    # ⚠️ V3.9.67 — LA MODALIDAD SE MIRA ANTES QUE LIVEKIT, A PROPÓSITO.
    #
    # Una clase PRESENCIAL no tiene sala de video, ni siquiera para el
    # profesor, y eso es cierto esté LiveKit configurado o no. Si se
    # comprobara después, la respuesta sería "el video no está configurado",
    # que es un motivo equivocado: sugiere que con LiveKit activo sí se
    # podría entrar.
    #
    # La sesión conserva su `meeting_url` heredado por si vuelve a ser
    # virtual, pero mientras sea presencial la clase ocurre en el aula.
    from app.services.audience import tiene_entrada_online
    if not tiene_entrada_online(s):
        raise HTTPException(
            400,
            "Esta clase es presencial. Nos vemos en el aula.",
        )

    if not livekit_ready():
        raise HTTPException(
            503,
            "El video de Dorismon no está configurado todavía. Usa el enlace de la clase.",
        )

    me = await db.get(User, user.user_id)
    if not me:
        raise HTTPException(404, "Usuario no encontrado")
    if not me.is_active:
        raise HTTPException(403, "Cuenta desactivada")

    is_teacher = s.teacher_id == user.user_id
    is_admin = me.role == UserRole.super_admin
    is_moderator = is_teacher or is_admin

    # ¿Puede entrar este estudiante?
    if not is_moderator:
        # V3.9.43 — UNA SOLA REGLA para todo. Antes este archivo tenía su
        # propia copia de la lógica (por eso el video dejaba entrar a quien no
        # debía, y el evento bloqueaba a quien no se había anotado).
        from app.services.audience import puede_acceder_a_clase
        allowed = await puede_acceder_a_clase(db, user.user_id, s)

        if not allowed:
            raise HTTPException(
                403,
                "Esta clase no es de tu grupo. Si crees que es un error, "
                "escríbenos y lo revisamos.",
            )

    # GATE 6G-A — "Sacar de la clase" dura toda la sesión: no hay token nuevo
    # mientras el bloqueo siga activo (hasta que el profesor permita volver).
    from app.services.video_bloqueos import bloqueo_activo, MENSAJE_RETIRADO
    if not is_moderator and await bloqueo_activo(db, session_id, user.user_id):
        raise HTTPException(403, MENSAJE_RETIRADO)

    # Ventana de entrada
    now = datetime.now(tz.utc)
    starts = s.starts_at_utc
    ends = s.ends_at_utc
    if starts and starts.tzinfo is None:
        starts = starts.replace(tzinfo=tz.utc)
    if ends and ends.tzinfo is None:
        ends = ends.replace(tzinfo=tz.utc)

    # V3.9.43 — Se puede entrar desde 30 min antes y DURANTE TODA LA CLASE.
    # Antes se cerraba 20 minutos después del inicio y quien llegaba tarde
    # quedaba afuera. Llegar tarde no debe significar perder la clase.
    if starts and now < starts - timedelta(minutes=MINUTOS_ANTES):
        faltan = int((starts - now).total_seconds() // 60)
        raise HTTPException(
            400,
            f"La sala abre {MINUTOS_ANTES} minutos antes. "
            f"Faltan {faltan} minutos para tu clase.",
        )
    if ends and now > ends + timedelta(minutes=MINUTOS_GRACIA_FINAL):
        raise HTTPException(400, "Esta clase ya terminó.")

    # Si llega tarde, entra igual — solo se deja constancia
    llega_tarde = bool(starts and now > starts + timedelta(minutes=10))

    # V3.9.43 — AUTO-REGISTRO EN EVENTOS: si es un evento abierto y no se
    # había anotado, se anota al entrar (si hay cupo). Antes quedaba afuera
    # aunque llegara a tiempo y hubiera lugar.
    # v3.9.76 (D2): Dirección no es estudiante; en PostgreSQL la FK a students rechazaba su inscripción (500)
    if s.is_open_event and not is_teacher and not is_admin:
        ya = (await db.execute(
            select(EventRegistration).where(
                EventRegistration.session_id == session_id,
                EventRegistration.student_id == user.user_id,
                EventRegistration.cancelled_at.is_(None),
            )
        )).scalar_one_or_none()
        if not ya:
            inscritos = (await db.execute(
                select(func.count()).select_from(EventRegistration).where(
                    EventRegistration.session_id == session_id,
                    EventRegistration.cancelled_at.is_(None),
                )
            )).scalar() or 0
            cupo = s.capacity or 0
            if cupo and inscritos >= cupo:
                raise HTTPException(
                    400,
                    f"Este evento ya está lleno ({inscritos} de {cupo} lugares). "
                    "Escríbenos por si se libera un cupo.",
                )
            db.add(EventRegistration(session_id=session_id, student_id=user.user_id))
            await db.commit()

    # V3.9.27: dejar constancia de que esta persona entró a la clase.
    # Sirve para sugerir la asistencia después (el profesor confirma).
    try:
        prev = (await db.execute(
            select(VideoPresence).where(
                VideoPresence.session_id == session_id,
                VideoPresence.user_id == user.user_id,
            )
        )).scalar_one_or_none()
        if prev:
            prev.last_seen_at = now
        else:
            db.add(VideoPresence(session_id=session_id, user_id=user.user_id))
        await db.commit()
    except Exception:
        pass  # nunca impedir entrar a clase por un problema de registro

    comprobar("join_clase", user.user_id, session_id)   # GATE 6G-D
    # GATE 6G-B — vida corta: min(fin + 15 min, ahora + 30 min)
    from app.services.livekit_service import ttl_token_participante
    ttl = ttl_token_participante(ends, now)
    token = build_token(
        session_id=session_id,
        user_id=user.user_id,
        display_name=me.full_name or "Participante",
        is_moderator=is_moderator,
        ttl_seconds=ttl,
    )
    from app.services.grabaciones import grabando_confirmado   # GATE 6H-A
    grabando = await grabando_confirmado(db, session_id)
    return {
        "late": llega_tarde,
        "token": token,
        "expires_in": ttl,   # GATE 6G-B — segundos de vida del token (campo nuevo)
        "url": livekit_url(),
        "room": room_name(session_id),
        "is_moderator": is_moderator,
        "title": s.title,
        "display_name": me.full_name,
        # Plan B: si algo falla en vivo, el enlace de siempre sigue disponible
        "fallback_url": s.meeting_url,
        # GATE 6H-A — campo nuevo: LiveKit confirmó que la clase se está grabando
        # (la pantalla previa avisa "Esta clase está siendo grabada.")
        "grabando": grabando,
    }


@router.post("/sessions/{session_id}/heartbeat")
async def heartbeat(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.74 — Confirma que una persona autorizada SIGUE en la sala.

    El heartbeat ya no crea presencia por sí solo. La primera presencia solo se
    crea al pasar correctamente por ``/join``. Así nadie puede fabricar minutos
    de asistencia llamando este endpoint con el id de una clase ajena.
    """
    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    if s.status == SessionStatus.cancelled:
        raise HTTPException(400, "Esta clase fue cancelada")
    # GATE 6E — Una clase FINALIZADA ya no suma presencia (antes seguía
    # aceptando avisos hasta el margen final). Lo ya registrado no se toca.
    if s.status == SessionStatus.completed:
        raise HTTPException(400, "Esta clase ya fue finalizada")

    from app.services.audience import tiene_entrada_online, puede_acceder_a_clase
    if not tiene_entrada_online(s):
        raise HTTPException(400, "Esta clase es presencial. No tiene sala de video.")

    me = await db.get(User, user.user_id)
    if not me:
        raise HTTPException(404, "Usuario no encontrado")
    if not me.is_active:
        raise HTTPException(403, "Cuenta desactivada")

    is_moderator = (s.teacher_id == user.user_id) or (me.role == UserRole.super_admin)
    if not is_moderator:
        if not await puede_acceder_a_clase(db, user.user_id, s):
            raise HTTPException(403, "Esta clase no corresponde a tu cuenta")
        # GATE 6G-A — quien fue retirado de la clase ya no suma presencia
        from app.services.video_bloqueos import bloqueo_activo, MENSAJE_RETIRADO
        if await bloqueo_activo(db, session_id, user.user_id):
            raise HTTPException(403, MENSAJE_RETIRADO)

    now = datetime.now(tz.utc)
    starts = s.starts_at_utc
    ends = s.ends_at_utc
    if starts and starts.tzinfo is None:
        starts = starts.replace(tzinfo=tz.utc)
    if ends and ends.tzinfo is None:
        ends = ends.replace(tzinfo=tz.utc)
    if starts and now < starts - timedelta(minutes=MINUTOS_ANTES):
        raise HTTPException(400, "La sala todavía no está abierta")
    if ends and now > ends + timedelta(minutes=MINUTOS_GRACIA_FINAL):
        raise HTTPException(400, "Esta clase ya terminó")

    # GATE 6G-D — margen amplio (normal: 1 por minuto); silencioso en el frontend
    from app.services.limites_video import comprobar
    comprobar("heartbeat", user.user_id, session_id)
    row = (await db.execute(
        select(VideoPresence).where(
            VideoPresence.session_id == session_id,
            VideoPresence.user_id == user.user_id,
        )
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(409, "Primero debes entrar a la sala de esta clase")

    last = row.last_seen_at
    if last and last.tzinfo is None:
        last = last.replace(tzinfo=tz.utc)
    # Solo se cuenta el tiempo REAL transcurrido. El aviso llega cada minuto:
    # - menos de 30 segundos → no suma (evita inflar con avisos repetidos)
    # - más de 3 minutos → no suma (la persona se fue y volvió, o quedó una
    #   pestaña abierta; no queremos regalar tiempo que no estuvo)
    if last:
        delta = (now - last).total_seconds()
        if 30 <= delta <= 180:
            row.minutes = (row.minutes or 0) + int(round(delta / 60))
    row.last_seen_at = now
    await db.commit()
    return {"ok": True, "minutes": row.minutes}


@router.get("/sessions/{session_id}/participants")
async def who_is_connected(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Quiénes están conectados ahora. Solo para el profesor o el admin."""
    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    me = await db.get(User, user.user_id)
    if not (s.teacher_id == user.user_id or (me and me.role == UserRole.super_admin)):
        raise HTTPException(403, "Solo el profesor de la clase puede ver esto")
    if not livekit_ready():
        return {"items": []}

    from app.services.limites_video import comprobar
    comprobar("participantes", user.user_id, session_id)   # GATE 6G-D
    parts = await list_participants(session_id)
    items = []
    for p in parts:
        uid = p.get("identity")
        u = await db.get(User, uid) if uid else None
        items.append({
            "identity": uid,
            "name": (u.full_name if u else None) or p.get("name") or "Participante",
            "is_teacher": uid == s.teacher_id,
        })
    return {"items": items}


@router.post("/sessions/{session_id}/moderate")
async def moderate(
    session_id: str,
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """V3.9.27 / GATE 6G-A — El profesor de la clase o Dirección modera a un participante.

    Acciones:
      "mute"   → silencia su micrófono UNA vez (puede volver a activarlo);
      "unmute" → pide reactivarlo (LiveKit lo impide por defecto, por privacidad);
      "remove" → lo BLOQUEA en esta clase (persiste) y DESPUÉS lo desconecta;
      "allow"  → "Permitir volver": quita el bloqueo (no emite token ni lo conecta).
    Nadie puede moderar al profesor de la clase ni a Dirección (super_admin).
    """
    from app.services.audience import tiene_entrada_online
    from app.services.audit import log_action
    from app.services.video_bloqueos import bloquear, permitir_volver

    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    me = await db.get(User, user.user_id)
    if not me or not me.is_active:
        raise HTTPException(403, "Cuenta desactivada")
    if not (s.teacher_id == user.user_id or me.role == UserRole.super_admin):
        raise HTTPException(403, "Solo el profesor de la clase puede moderar")
    # Solo hay sala que moderar en clases con Video Dorismon (misma regla que Finalizar)
    if not tiene_entrada_online(s) or s.video_provider != "dorismon":
        raise HTTPException(400, "Esta clase no usa Video Dorismon")
    if not livekit_ready():
        raise HTTPException(503, "El video de Dorismon no está configurado")

    action = str(body.get("action") or "").strip()
    identity = str(body.get("identity") or "").strip()
    if not identity:
        raise HTTPException(400, "Falta indicar a quién")
    # GATE 6G-C — permisos dinámicos (no encienden nada; solo permiten / impiden)
    permisos = {
        "permitir_pantalla": ({"screen_share", "screen_share_audio"}, set()),
        "quitar_pantalla": (set(), {"screen_share", "screen_share_audio"}),
        "bloquear_microfono": (set(), {"microphone"}),
        "permitir_microfono": ({"microphone"}, set()),
        "bloquear_camara": (set(), {"camera"}),
        "permitir_camara": ({"camera"}, set()),
    }
    if action not in ("mute", "unmute", "remove", "allow") and action not in permisos:
        raise HTTPException(400, "Acción no válida")
    if identity == s.teacher_id:
        raise HTTPException(400, "No se puede silenciar ni sacar al profesor de la clase")
    if identity == user.user_id:
        raise HTTPException(400, "No puedes moderarte a ti mismo")
    objetivo = await db.get(User, identity)
    if objetivo and objetivo.role == UserRole.super_admin:
        raise HTTPException(400, "No se puede silenciar ni sacar a Dirección")
    actor = user.user_id
    # GATE 6G-D — una clase grande se gestiona de corrido; un script no
    from app.services.limites_video import comprobar
    comprobar("permisos" if action in permisos else "moderacion", actor, session_id)

    if action in ("mute", "unmute"):
        r = await mute_participant(session_id, identity, action == "mute")
        if r == "no_esta":
            raise HTTPException(400, "Esa persona ya no está conectada a la clase")
        if r == "sin_audio":
            raise HTTPException(400, "Esa persona no tiene el micrófono activado")
        if r != "ok":
            raise HTTPException(502, "No se pudo silenciar. Inténtalo de nuevo.")
        await log_action(db, actor, f"video_{action}", "video", target_id=session_id,
                         details=f"user_id={identity}", commit=True)
        return {"ok": True, "action": action}

    if action == "allow":
        estaba = await permitir_volver(db, session_id, identity, actor)
        return {"ok": True, "action": "allow", "estaba_bloqueado": estaba}

    if action in permisos:
        # GATE 6G-C — cambia SOLO esa fuente en ESA sala; si LiveKit falla, nada cambia
        from app.services.livekit_service import cambiar_fuentes
        agregar, quitar = permisos[action]
        r, fuentes = await cambiar_fuentes(session_id, identity, agregar, quitar)
        if r == "no_esta":
            raise HTTPException(400, "Esa persona ya no está conectada a la clase")
        if r != "ok":
            raise HTTPException(502, "No se pudo cambiar el permiso. Inténtalo de nuevo.")
        await log_action(db, actor, f"video_{action}", "video", target_id=session_id,
                         details=f"user_id={identity}; fuentes={','.join(fuentes)}", commit=True)
        return {"ok": True, "action": action, "fuentes": fuentes}

    # "remove" — ORDEN SEGURO: 1) bloqueo persistente, 2) desconectar en LiveKit.
    # Si LiveKit falla, el bloqueo SE QUEDA (fail-closed): /join ya no da token.
    if not objetivo:
        raise HTTPException(404, "Participante no encontrado")
    motivo = body.get("motivo")
    motivo = motivo.strip()[:300] if isinstance(motivo, str) and motivo.strip() else None
    bloqueo = await bloquear(db, session_id, identity, actor, motivo)
    r = await remove_participant(session_id, identity)
    await log_action(db, actor, "video_remove", "video", target_id=session_id,
                     details=f"user_id={identity}; livekit={r}", commit=True)
    if r == "error":
        raise HTTPException(
            502,
            "El acceso del estudiante quedó bloqueado, pero no pudimos desconectarlo "
            "de la sala. Intenta nuevamente.",
        )
    return {"ok": True, "action": "remove", "bloqueado": True,
            "desconectado": r == "ok", "bloqueo": bloqueo}


# ============================================================================
# GATE 6F-A — Interacción de la clase: mano, turno, temporizador y foco
# ============================================================================
# Estado EFÍMERO: vive en la sala de LiveKit (atributos / metadatos), no en la
# base de datos, no toca asistencia ni envía notificaciones. Este endpoint solo
# VALIDA quién puede hacer qué y se lo pide a LiveKit desde el servidor.
# Las reacciones no pasan por aquí: son pasajeras (canal de datos de LiveKit).

SEGUNDOS_TEMPORIZADOR = (10, 30 * 60)   # mínimo / máximo de un temporizador
_LARGO_IDENTIDAD = 128


@router.post("/sessions/{session_id}/interaccion")
async def interaccion_clase(
    session_id: str,
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Estudiante: subir/bajar SU mano. Profesor de la clase o Dirección: además
    bajar la mano de otro, dar turno, temporizador y foco para todos."""
    import time as _time
    from app.services.audience import tiene_entrada_online, puede_acceder_a_clase
    from app.services.livekit_service import poner_mano, leer_estado_clase, guardar_estado_clase

    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    if s.status == SessionStatus.cancelled:
        raise HTTPException(400, "Esta clase fue cancelada")
    if s.status == SessionStatus.completed:
        raise HTTPException(400, "Esta clase ya fue finalizada")
    if not tiene_entrada_online(s):
        raise HTTPException(400, "Esta clase es presencial. No tiene sala de video.")
    if s.video_provider != "dorismon":   # misma condición que "Finalizar" (Gate 6E)
        raise HTTPException(400, "Esta clase no usa Video Dorismon")
    if not livekit_ready():
        raise HTTPException(503, "El video de Dorismon no está configurado")

    me = await db.get(User, user.user_id)
    if not me or not me.is_active:
        raise HTTPException(403, "Cuenta desactivada")
    es_moderador = (s.teacher_id == user.user_id) or (me.role == UserRole.super_admin)
    if not es_moderador and not await puede_acceder_a_clase(db, user.user_id, s):
        raise HTTPException(403, "Esta clase no corresponde a tu cuenta")
    # GATE 6G-A — quien fue retirado de la clase no interactúa
    from app.services.video_bloqueos import bloqueo_activo, MENSAJE_RETIRADO
    if not es_moderador and await bloqueo_activo(db, session_id, user.user_id):
        raise HTTPException(403, MENSAJE_RETIRADO)

    accion = str(body.get("accion") or "").strip()
    # GATE 6G-D — el estudiante (su mano) y el profesor (turno, foco, temporizador) tienen márgenes distintos
    from app.services.limites_video import comprobar
    comprobar("interaccion_moderador" if es_moderador else "interaccion_estudiante", user.user_id, session_id)

    def identidad(valor, obligatoria: bool):
        if valor is None or valor == "":
            if obligatoria:
                raise HTTPException(400, "Falta indicar a quién")
            return None
        if not isinstance(valor, str) or len(valor) > _LARGO_IDENTIDAD:
            raise HTTPException(400, "Participante no válido")
        return valor

    # ---------------------------------------------------------------- mano
    if accion == "mano":
        arriba = body.get("arriba")
        if not isinstance(arriba, bool):
            raise HTTPException(400, "Falta indicar si la mano sube o baja")
        quien = identidad(body.get("identity"), False) or user.user_id
        if quien != user.user_id:
            # Solo el moderador toca la mano de otro, y solo para BAJARLA.
            if not es_moderador:
                raise HTTPException(403, "Solo puedes subir o bajar tu propia mano")
            if arriba:
                raise HTTPException(403, "No se puede levantar la mano de otra persona")
        r = await poner_mano(session_id, quien, arriba)
        if r == "no_esta":
            raise HTTPException(409, "Esa persona ya no está conectada a la clase")
        if r != "ok":
            raise HTTPException(502, "No se pudo actualizar la mano. Inténtalo de nuevo.")
        return {"ok": True, "identity": quien, "arriba": arriba}

    # --------------------------------------- turno / temporizador / foco
    if accion not in ("turno", "foco", "temporizador"):
        raise HTTPException(400, "Acción no válida")
    if not es_moderador:
        raise HTTPException(403, "Solo el profesor de la clase puede hacerlo")

    # Todo se valida ANTES de hablar con LiveKit
    quien = op = seg = None
    if accion in ("turno", "foco"):
        quien = identidad(body.get("identity"), False)
    else:
        op = str(body.get("op") or "")
        if op not in ("iniciar", "pausar", "reanudar", "cancelar"):
            raise HTTPException(400, "Operación de temporizador no válida")
        if op == "iniciar":   # también "reiniciar": vuelve a empezar desde la duración
            seg = body.get("segundos")
            if not isinstance(seg, int) or isinstance(seg, bool) \
                    or not SEGUNDOS_TEMPORIZADOR[0] <= seg <= SEGUNDOS_TEMPORIZADOR[1]:
                raise HTTPException(400, "Duración no válida (de 10 segundos a 30 minutos)")

    estado = await leer_estado_clase(session_id)
    if estado is None:
        raise HTTPException(502, "No se pudo leer el estado de la clase. Inténtalo de nuevo.")
    ahora = int(_time.time() * 1000)

    if accion in ("turno", "foco"):
        # Turno: señal educativa (NO enciende micrófonos ni cambia permisos).
        # Foco: a quién muestran todos en grande (NO da permisos nuevos).
        estado[accion] = quien
    else:
        t = estado.get("temporizador") if isinstance(estado.get("temporizador"), dict) else None
        if op == "iniciar":
            estado["temporizador"] = {"duracion": seg * 1000, "restante": seg * 1000,
                                      "inicio": ahora, "pausado": False}
        elif op == "pausar" and t and not t.get("pausado"):
            pasado = ahora - int(t.get("inicio") or ahora)
            t.update(restante=max(0, int(t.get("restante") or 0) - pasado), inicio=ahora, pausado=True)
        elif op == "reanudar" and t and t.get("pausado"):
            t.update(inicio=ahora, pausado=False)
        elif op == "cancelar":
            estado["temporizador"] = None

    estado = {"v": 1, "turno": estado.get("turno"), "foco": estado.get("foco"),
              "temporizador": estado.get("temporizador"), "ahora": ahora}
    if not await guardar_estado_clase(session_id, estado):
        raise HTTPException(502, "No se pudo actualizar la clase. Inténtalo de nuevo.")
    return {"ok": True, "estado": estado}



# ============================================================================
# GATE 6F-A.1 — Hora de referencia para el temporizador de la clase
# ============================================================================
# El temporizador guarda su inicio con la hora de ESTE servidor. Para que dos
# dispositivos con relojes distintos vean el mismo tiempo, cada cliente pide la
# hora aquí (unas pocas veces, nunca cada segundo), compensa la ida y vuelta y
# avanza con su reloj monotónico. Solo lectura: no guarda nada.

@router.get("/hora")
async def hora_del_servidor(user: Annotated[CurrentUser, Depends(get_current_user)]):
    import time as _time
    from app.services.limites_video import comprobar
    comprobar("hora", user.user_id)   # GATE 6G-D — muy por encima de 6F-A.1
    return {"ahora": int(_time.time() * 1000)}


# ============================================================================
# GATE 6G-A — Retirados de la clase (para "Permitir volver")
# ============================================================================

@router.get("/sessions/{session_id}/bloqueos")
async def retirados_de_la_clase(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Quiénes tienen bloqueado el regreso a ESTA clase. Solo el profesor de la
    clase o Dirección (los mismos que pueden moderar)."""
    from app.services.video_bloqueos import bloqueados

    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    me = await db.get(User, user.user_id)
    if not me or not me.is_active:
        raise HTTPException(403, "Cuenta desactivada")
    if not (s.teacher_id == user.user_id or me.role == UserRole.super_admin):
        raise HTTPException(403, "Solo el profesor de la clase puede ver esto")
    from app.services.limites_video import comprobar
    comprobar("bloqueos", user.user_id, session_id)   # GATE 6G-D
    items = []
    for b in await bloqueados(db, session_id):
        u = await db.get(User, b.user_id)
        items.append({"user_id": b.user_id, "name": (u.full_name if u else None) or "Participante",
                      "blocked_at": b.blocked_at.isoformat() if b.blocked_at else None,
                      "times_blocked": b.times_blocked})
    return {"items": items}


# ============================================================================
# GATE 6H-A — Grabación de la clase (LiveKit Egress RoomComposite)
# ============================================================================
# Solo el profesor de la clase o Dirección, solo clases de Video Dorismon, nunca
# automática. Idempotente: una sola grabación activa por clase (restricción única).

async def _clase_para_grabar(db: AsyncSession, session_id: str, user: CurrentUser):
    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    me = await db.get(User, user.user_id)
    if not me or not me.is_active:
        raise HTTPException(403, "Cuenta desactivada")
    if not (s.teacher_id == user.user_id or me.role == UserRole.super_admin):
        raise HTTPException(403, "Solo el profesor de la clase o Dirección pueden grabar")
    from app.services.audience import tiene_entrada_online
    if not tiene_entrada_online(s) or s.video_provider != "dorismon":
        raise HTTPException(400, "Solo se pueden grabar clases de Video Dorismon")
    return s


@router.post("/sessions/{session_id}/grabacion/iniciar")
async def iniciar_grabacion(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    from app.services.grabaciones import iniciar, resumen
    from app.services.limites_video import comprobar

    s = await _clase_para_grabar(db, session_id, user)
    if s.status == SessionStatus.cancelled:
        raise HTTPException(400, "Esta clase fue cancelada")
    if s.status == SessionStatus.completed:
        raise HTTPException(400, "Esta clase ya fue finalizada")
    if not livekit_ready():
        raise HTTPException(503, "El video de Dorismon no está configurado")
    now = datetime.now(tz.utc)
    starts, ends = s.starts_at_utc, s.ends_at_utc
    if starts and starts.tzinfo is None:
        starts = starts.replace(tzinfo=tz.utc)
    if ends and ends.tzinfo is None:
        ends = ends.replace(tzinfo=tz.utc)
    if (starts and now < starts - timedelta(minutes=MINUTOS_ANTES)) or \
            (ends and now > ends + timedelta(minutes=MINUTOS_GRACIA_FINAL)):
        raise HTTPException(400, "Solo se puede grabar durante la clase")
    comprobar("grabacion", user.user_id, session_id)
    rec, resultado = await iniciar(db, s, user.user_id)
    if resultado == "sin_almacenamiento":
        raise HTTPException(503, "El almacenamiento privado de grabaciones no está configurado todavía.")
    if resultado == "error":
        raise HTTPException(502, "No se pudo iniciar la grabación. Inténtalo de nuevo.")
    return {"ok": True, "resultado": resultado, "grabacion": resumen(rec)}


@router.post("/sessions/{session_id}/grabacion/detener")
async def detener_grabacion(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    from app.services.grabaciones import detener, resumen, congelar_audiencia
    from app.services.limites_video import comprobar

    s = await _clase_para_grabar(db, session_id, user)
    comprobar("grabacion", user.user_id, session_id)
    rec, resultado = await detener(db, session_id, user.user_id)
    if resultado == "iniciando":
        raise HTTPException(409, "La grabación todavía está iniciando. Inténtalo en unos segundos.")
    if resultado == "error":
        raise HTTPException(502, "Se pidió detener la grabación, pero LiveKit no respondió. Inténtalo de nuevo.")
    if rec is not None:
        s = await db.get(ClassSession, session_id)
        # GATE 6H-A.1: solo quien ENTRÓ durante la grabación; el derecho quedó fijado al iniciarla
        await congelar_audiencia(db, s, rec.id, solo_asistencia=True)
    return {"ok": True, "resultado": resultado, "grabacion": resumen(rec)}


@router.get("/sessions/{session_id}/grabacion")
async def estado_grabacion(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    """Estado para el profesor / Dirección (el indicador de TODOS viene de LiveKit)."""
    from app.models import VideoRecording
    from app.services.grabaciones import grabacion_activa, sincronizar, resumen, almacenamiento
    from app.services.limites_video import comprobar

    await _clase_para_grabar(db, session_id, user)
    comprobar("grabacion_estado", user.user_id, session_id)
    recientes = (await db.execute(select(VideoRecording).where(VideoRecording.session_id == session_id)
                                  .order_by(VideoRecording.requested_at.desc()).limit(5))).scalars().all()
    actual = await grabacion_activa(db, session_id) or (recientes[0] if recientes else None)
    if actual is not None and actual.status not in ("ready", "failed", "deleted"):
        actual = await sincronizar(db, actual)
    return {"grabacion": resumen(actual), "almacenamiento_listo": almacenamiento() is not None,
            "historial": [resumen(r) for r in recientes]}


@router.post("/livekit/webhook")
async def webhook_livekit(request: Request, db: AsyncSession = Depends(get_db)):
    """Eventos de LiveKit (egress_*). Se acepta SOLO si la firma es válida."""
    from app.services.grabaciones import verificar_webhook, recibir_evento

    cuerpo = await request.body()
    evento = verificar_webhook(cuerpo, request.headers.get("Authorization") or "")
    if evento is None:
        raise HTTPException(401, "Firma no válida")
    await recibir_evento(db, evento)
    return {"ok": True}


# ============================================================================
# GATE 6H-B — Biblioteca de grabaciones y reproducción privada
# ============================================================================
# Regla de acceso ÚNICA en app/services/biblioteca_grabaciones.puede_ver. Una grabación
# ajena, inexistente o de otra clase responde SIEMPRE 404 (sin distinguir: no se revela
# si existe). La URL firmada no se guarda ni se registra.

import logging as _logging

log = _logging.getLogger(__name__)

@router.get("/grabaciones")
async def biblioteca_grabaciones(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    pagina: int = 1,
    estado: str | None = None,
    profesor: str | None = None,
    desde: str | None = None,
    hasta: str | None = None,
    q: str | None = None,
):
    from app.services.biblioteca_grabaciones import _yo, listar
    from app.services.limites_video import comprobar

    comprobar("biblioteca", user.user_id)
    me = await _yo(db, user.user_id)
    if not me:
        raise HTTPException(403, "Cuenta desactivada")
    return await listar(db, me, pagina=pagina, estado=estado, profesor=profesor, desde=desde, hasta=hasta, q=q)


@router.post("/sessions/{session_id}/grabaciones/{recording_id}/reproducir")
async def reproducir_grabacion(
    session_id: str,
    recording_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import JSONResponse
    from app.models import VideoRecording
    from app.services.biblioteca_grabaciones import (
        _yo, puede_ver, config_lectura, objeto_existe, firmar_url, URL_TTL_SEGUNDOS)
    from app.services.limites_video import comprobar

    comprobar("reproducir", user.user_id)          # antes de la base: también frena el tanteo de ids
    me = await _yo(db, user.user_id)
    if not me:
        raise HTTPException(403, "Cuenta desactivada")
    no_disponible = HTTPException(404, "Grabación no disponible")
    rec = await db.get(VideoRecording, recording_id)
    s = await db.get(ClassSession, session_id) if rec else None
    if not rec or not s or rec.session_id != session_id or not await puede_ver(db, me, rec, s):
        log.info("reproducir: rechazada recording=%s user=%s", recording_id[:64], user.user_id)
        raise no_disponible
    if rec.status != "ready":
        raise HTTPException(409, "La grabación aún no está lista")
    cfg = config_lectura()
    if not cfg or not rec.storage_bucket or not rec.storage_key:
        raise HTTPException(503, "El almacenamiento de grabaciones no está disponible")
    existe = await objeto_existe(cfg, rec.storage_bucket, rec.storage_key)
    if existe == "no":
        log.warning("reproducir: archivo inexistente recording=%s", rec.id)
        raise HTTPException(404, "El archivo de esta grabación no está disponible")
    if existe != "si":
        log.warning("reproducir: almacenamiento sin respuesta recording=%s", rec.id)
        raise HTTPException(503, "No se pudo acceder a la grabación. Inténtalo de nuevo.")
    url = firmar_url(cfg, rec.storage_bucket, rec.storage_key)
    log.info("reproducir: ok recording=%s user=%s", rec.id, user.user_id)   # nunca la URL
    return JSONResponse({"url": url, "expira_en_segundos": URL_TTL_SEGUNDOS, "tipo": rec.mime_type or "video/mp4"},
                        headers={"Cache-Control": "no-store, private"})


# ============================================================================
# GATE 6F-B — Contenido educativo de la clase (vocabulario, frase, nota, enlace)
# ============================================================================
# Lógica y reglas en app/services/contenido_clase.py. Sin notificaciones (es interacción
# de clase); el aviso inmediato es un paquete de datos del navegador del profesor.

async def _clase_contenido(db: AsyncSession, session_id: str, user: CurrentUser):
    from app.services.contenido_clase import yo_activo
    s = await db.get(ClassSession, session_id)
    if not s:
        raise HTTPException(404, "Clase no encontrada")
    me = await yo_activo(db, user.user_id)
    return s, me


@router.get("/sessions/{session_id}/contenido")
async def contenido_de_la_clase(
    session_id: str,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    from app.services.contenido_clase import puede_leer, es_moderador, listar, en_ventana
    from app.services.limites_video import comprobar

    comprobar("contenido_lectura", user.user_id, session_id)
    s, me = await _clase_contenido(db, session_id, user)
    if not await puede_leer(db, me, s):
        raise HTTPException(403, "No tienes acceso al contenido de esta clase")
    mod = es_moderador(me, s)
    puede_publicar = mod and s.status == SessionStatus.scheduled and en_ventana(s)
    return {"items": await listar(db, s, mod), "puede_publicar": puede_publicar,
            "puede_editar": mod and s.status != SessionStatus.cancelled}


@router.post("/sessions/{session_id}/contenido")
async def publicar_contenido(
    session_id: str,
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    from app.services.contenido_clase import es_moderador, en_ventana, crear, resumen
    from app.services.audit import log_action
    from app.services.audience import tiene_entrada_online
    from app.services.limites_video import comprobar

    s, me = await _clase_contenido(db, session_id, user)
    if not es_moderador(me, s):
        raise HTTPException(403, "Solo el profesor de la clase o Dirección pueden compartir contenido")
    if not tiene_entrada_online(s) or s.video_provider != "dorismon":
        raise HTTPException(400, "El contenido de clase es para clases de Video Dorismon")
    if s.status == SessionStatus.cancelled:
        raise HTTPException(400, "Esta clase fue cancelada")
    if s.status == SessionStatus.completed:
        raise HTTPException(400, "Esta clase ya fue finalizada")
    if not en_ventana(s):
        raise HTTPException(400, "Solo se puede compartir contenido durante la clase")
    comprobar("contenido", user.user_id, session_id)
    c = await crear(db, s, me, str(body.get("tipo") or ""), body)
    await log_action(db, user.user_id, "class_content_create", "video", target_id=session_id,
                     details=f"content_id={c.id}; tipo={c.kind}", commit=True)
    return {"ok": True, "item": resumen(c, True)}


@router.patch("/sessions/{session_id}/contenido/{content_id}")
async def editar_contenido(
    session_id: str,
    content_id: str,
    body: dict,
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
):
    from app.models import ClassSessionContent
    from app.services.contenido_clase import es_moderador, validar, resumen
    from app.services.audit import log_action
    from app.services.limites_video import comprobar

    s, me = await _clase_contenido(db, session_id, user)
    if not es_moderador(me, s):
        raise HTTPException(403, "Solo el profesor de la clase o Dirección pueden editar el contenido")
    c = await db.get(ClassSessionContent, content_id)
    if not c or c.session_id != session_id:
        raise HTTPException(404, "Contenido no encontrado")
    if s.status == SessionStatus.cancelled:
        raise HTTPException(400, "Esta clase fue cancelada")
    comprobar("contenido", user.user_id, session_id)
    ahora = datetime.now(tz.utc)
    cambios = validar(c.kind, {k: v for k, v in body.items() if k in ("texto", "traduccion", "ejemplo", "url")}, parcial=True)
    accion = None
    if cambios:
        for k, v in cambios.items():
            setattr(c, k, v)
        c.updated_by, c.updated_at, accion = me.id, ahora, "class_content_edit"
    if "oculto" in body:
        if bool(body["oculto"]) and c.hidden_at is None:
            c.hidden_at, c.hidden_by, accion = ahora, me.id, "class_content_hide"
        elif not bool(body["oculto"]) and c.hidden_at is not None:
            c.hidden_at, c.hidden_by, accion = None, None, "class_content_show"
    if accion:
        await log_action(db, me.id, accion, "video", target_id=session_id, details=f"content_id={c.id}")
    await db.commit()
    return {"ok": True, "item": resumen(c, True)}


@router.get("/contenido-clases")
async def clases_con_contenido(
    user: Annotated[CurrentUser, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
    pagina: int = 1,
):
    """Después de la clase: las clases con contenido que puedo consultar (más recientes primero)."""
    from app.models import ClassSessionContent, ClassSessionAudience
    from app.services.contenido_clase import yo_activo
    from app.services.limites_video import comprobar

    comprobar("biblioteca", user.user_id)
    me = await yo_activo(db, user.user_id)
    pagina = max(1, int(pagina or 1))
    visibles = select(ClassSessionContent.session_id, func.count().label("n")).where(
        ClassSessionContent.hidden_at.is_(None)).group_by(ClassSessionContent.session_id).subquery()
    q = select(ClassSession, visibles.c.n).join(visibles, visibles.c.session_id == ClassSession.id)
    if me.role == UserRole.student:
        q = q.join(ClassSessionAudience, (ClassSessionAudience.session_id == ClassSession.id)
                   & (ClassSessionAudience.user_id == me.id)).where(ClassSession.status != SessionStatus.cancelled)
    elif me.role != UserRole.super_admin:
        q = q.where(ClassSession.teacher_id == me.id)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar() or 0
    filas = (await db.execute(q.order_by(ClassSession.starts_at_utc.desc()).offset((pagina - 1) * 20).limit(20))).all()
    items = []
    for s, n in filas:
        prof = await db.get(User, s.teacher_id)
        ini = s.starts_at_utc.replace(tzinfo=tz.utc) if s.starts_at_utc and s.starts_at_utc.tzinfo is None else s.starts_at_utc
        items.append({"session_id": s.id, "titulo": s.title, "profesor": prof.full_name if prof else None,
                      "clase_inicio": ini.isoformat() if ini else None, "contenidos": n})
    return {"items": items, "total": total, "pagina": pagina, "paginas": max(1, -(-total // 20))}

