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
# V3.9.43 — Ventana de entrada a la sala.
#
# ANTES: 15 min antes / 20 min después del INICIO. Pasado eso, puerta cerrada.
# Eso dejó afuera a estudiantes que llegaron tarde y costó dos matrículas.
#
# AHORA: se puede entrar desde 30 min antes y DURANTE TODA LA CLASE, más un
# margen al final por si se extiende. Llegar tarde no es motivo para quedarse
# afuera: es preferible entrar tarde que perder la clase.
MINUTOS_ANTES = 30
# Margen DESPUÉS DE QUE TERMINA la clase (no del inicio)
MINUTOS_GRACIA_FINAL = 15

# Se conserva el nombre viejo por compatibilidad con código existente
MINUTOS_DESPUES = MINUTOS_GRACIA_FINAL


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
    ttl_seconds: int = 30 * 60,
) -> str:
    """Genera el permiso de entrada a la sala de una clase.

    GATE 6G-B — MÍNIMO PRIVILEGIO: el token del navegador solo sirve para
    PARTICIPAR en la sala de ESA clase (unirse, publicar, recibir, datos). El
    profesor ya NO recibe roomAdmin: silenciar, sacar, permitir volver, finalizar
    y el estado de la clase pasan siempre por el backend, que usa sus propios
    tokens de servidor. El moderador se distingue solo por metadata "teacher"
    (para la interfaz; no da ningún permiso en LiveKit).

    Vida corta (ver ttl_token_participante): LiveKit solo mira el vencimiento al
    CONECTAR; a quien ya está dentro se lo renueva el propio servidor, así que una
    clase larga no se corta. Volver a entrar siempre pasa por /join (token nuevo).

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
        # GATE 6G-C — qué puede publicar (LiveKit lo hace cumplir en el servidor):
        # profesor / Dirección: cámara, micrófono y pantalla (con su audio);
        # estudiante: cámara y micrófono; la pantalla solo si el profesor la permite.
        "canPublishSources": list(FUENTES_MODERADOR if is_moderator else FUENTES_ESTUDIANTE),
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


# ============================================================================
# V3.9.27 — Moderación de la sala (silenciar y sacar participantes)
# ============================================================================

def _admin_token(session_id: str) -> str:
    """Permiso de administración de SERVIDOR para LA SALA de esa clase.

    GATE 6G-A — LiveKit exige que un token roomAdmin nombre la sala sobre la que
    actúa (EnsureAdminPermission: room del token == room pedida). Antes este
    token no la incluía y LiveKit rechazaba ListParticipants, MutePublishedTrack
    y RemoveParticipant: Silenciar y Sacar no funcionaban. Nunca sale del backend.
    """
    api_key = (os.getenv("LIVEKIT_API_KEY") or "").strip()
    api_secret = (os.getenv("LIVEKIT_API_SECRET") or "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("LiveKit no está configurado")
    now = int(time.time())
    payload = {
        "iss": api_key,
        "sub": api_key,
        "nbf": now - 10,
        "exp": now + 600,
        "video": {"room": room_name(session_id), "roomAdmin": True},
    }
    return jwt.encode(payload, api_secret, algorithm="HS256")


def _http_base() -> str:
    """El servidor de LiveKit para llamadas normales (no de video)."""
    url = livekit_url() or ""
    if url.startswith("wss://"):
        return "https://" + url[len("wss://"):]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://"):]
    return url


async def list_participants(session_id: str) -> list[dict]:
    """Quiénes están conectados ahora mismo en la sala de esa clase."""
    import httpx

    token = _admin_token(session_id)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{_http_base()}/twirp/livekit.RoomService/ListParticipants",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"room": room_name(session_id)},
            )
    except Exception as e:  # red, timeout, DNS
        log.warning("ListParticipants sin respuesta para %s: %s", room_name(session_id), e)
        return []
    if r.status_code != 200:
        log.warning("ListParticipants HTTP %s: %s", r.status_code, (r.text or "")[:200])
        return []
    return (r.json() or {}).get("participants", []) or []


async def mute_participant(session_id: str, identity: str, muted: bool = True) -> str:
    """Silencia el micrófono de un participante UNA vez (puede volver a activarlo).

    Devuelve "ok" | "no_esta" (no conectado) | "sin_audio" (no tiene micrófono
    publicado) | "error".
    """
    import httpx

    token = _admin_token(session_id)
    parts = await list_participants(session_id)
    target = next((p for p in parts if p.get("identity") == identity), None)
    if not target:
        log.warning("No se encontró a %s en la sala. Conectados: %s",
                    identity, [p.get("identity") for p in parts])
        return "no_esta"
    audios = [t for t in (target.get("tracks", []) or [])
              # El tipo llega como texto ("AUDIO") o número (0) según la versión
              if t.get("type") in (0, "0", "AUDIO", "audio")]
    if not audios:
        return "sin_audio"
    ok = False
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            for track in audios:
                r = await c.post(
                    f"{_http_base()}/twirp/livekit.RoomService/MutePublishedTrack",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={
                        "room": room_name(session_id),
                        "identity": identity,
                        # LiveKit espera "trackSid" (no "track_sid")
                        "trackSid": track.get("sid"),
                        "muted": muted,
                    },
                )
                if r.status_code != 200:
                    log.warning("MutePublishedTrack HTTP %s: %s", r.status_code, (r.text or "")[:200])
                ok = ok or r.status_code == 200
    except Exception as e:
        log.warning("MutePublishedTrack sin respuesta para %s: %s", room_name(session_id), e)
    return "ok" if ok else "error"


async def remove_participant(session_id: str, identity: str) -> str:
    """Desconecta a alguien de la sala. "ok" | "no_esta" | "error".

    GATE 6G-A — En LiveKit Cloud, RemoveParticipant además revoca el token actual
    de esa persona. Se pide con revoke_token_ts = AHORA para que también quede
    revocado un token emitido hace menos de un minuto (sin ese campo, Cloud usa
    "ahora − 1 min"). Si el servidor no acepta el campo (servidor propio / versión
    sin él), se repite sin él. La autoridad sigue siendo el bloqueo del backend.
    """
    import httpx

    token = _admin_token(session_id)
    url = f"{_http_base()}/twirp/livekit.RoomService/RemoveParticipant"
    cabeceras = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = {"room": room_name(session_id), "identity": identity}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, headers=cabeceras, json={**base, "revokeTokenTs": int(time.time())})
            if r.status_code in (400, 422) and "not_found" not in (r.text or "")[:200]:
                r = await c.post(url, headers=cabeceras, json=base)
    except Exception as e:
        log.warning("RemoveParticipant sin respuesta para %s: %s", room_name(session_id), e)
        return "error"
    if r.status_code == 200:
        return "ok"
    if r.status_code == 404 or '"not_found"' in (r.text or "")[:200]:
        return "no_esta"
    log.warning("RemoveParticipant HTTP %s: %s", r.status_code, (r.text or "")[:200])
    return "error"


# ============================================================================
# GATE 6E — Cerrar la sala al "Finalizar clase para todos"
# ============================================================================

def _token_cerrar_sala() -> str:
    """Permiso de SERVIDOR para borrar una sala (roomCreate). Nunca sale del
    backend: el navegador no puede cerrar salas."""
    api_key = (os.getenv("LIVEKIT_API_KEY") or "").strip()
    api_secret = (os.getenv("LIVEKIT_API_SECRET") or "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("LiveKit no está configurado")
    now = int(time.time())
    payload = {
        "iss": api_key,
        "sub": api_key,
        "nbf": now - 10,
        "exp": now + 600,
        "video": {"roomCreate": True},
    }
    return jwt.encode(payload, api_secret, algorithm="HS256")


async def delete_room(session_id: str, intentos: int = 2) -> str:
    """Cierra la sala de video de la clase (API oficial de LiveKit, RoomService
    DeleteRoom, la misma vía Twirp que ya usan mute/remove/list). LiveKit
    desconecta a todos con el motivo ROOM_DELETED.

    Devuelve:
      "cerrada"    → LiveKit confirmó el cierre;
      "no_existia" → la sala ya no existía (resultado válido: nada que cerrar);
      "error"      → LiveKit no respondió / rechazó. NO se finge el cierre.
    Un único reintento breve ante fallo de red o 5xx (sin colas ni workers).
    """
    import asyncio
    import httpx

    sala = room_name(session_id)
    ultimo = ""
    for intento in range(1, max(1, intentos) + 1):
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.post(
                    f"{_http_base()}/twirp/livekit.RoomService/DeleteRoom",
                    headers={"Authorization": f"Bearer {_token_cerrar_sala()}",
                             "Content-Type": "application/json"},
                    json={"room": sala},
                )
            if r.status_code == 200:
                log.info("Sala %s cerrada en LiveKit", sala)
                return "cerrada"
            cuerpo = (r.text or "")[:200]
            if r.status_code == 404 or '"not_found"' in cuerpo:
                log.info("Sala %s ya no existía en LiveKit (finalización idempotente)", sala)
                return "no_existia"
            ultimo = f"HTTP {r.status_code}: {cuerpo}"
            if r.status_code < 500:
                break  # 401/403/400: reintentar no lo arregla
        except Exception as e:  # red, timeout, DNS
            ultimo = f"{type(e).__name__}: {e}"
        if intento < intentos:
            await asyncio.sleep(0.5)
    log.error("No se pudo cerrar la sala %s en LiveKit (%s). La clase sigue FINALIZADA en "
              "Dorismon: /join y heartbeat quedan bloqueados.", sala, ultimo)
    return "error"


# ============================================================================
# GATE 6F-A — Interacción de la clase (mano, turno, temporizador, foco)
# ============================================================================
# Estado EFÍMERO de la clase guardado en la propia sala de LiveKit, NO en la base
# de datos. LiveKit se lo entrega a todos los conectados y también a quien entra
# tarde (viene con la información de la sala al conectar):
#   · mano levantada → atributo del participante "dorismon.mano" (= hora del
#     servidor en ms, para ordenar por quién la levantó primero);
#   · turno, temporizador y foco → metadatos de la sala (un JSON pequeño).
# Lo escribe SOLO el servidor (los tokens de los participantes no incluyen
# canUpdateOwnMetadata: no se amplían permisos), así que el backend decide quién
# puede hacer qué. Al cerrar la sala (finalizar / vaciarse) desaparece todo.

ATRIBUTO_MANO = "dorismon.mano"


def _token_interaccion(session_id: str) -> str:
    """Permiso de servidor limitado a la sala de ESA clase (leer y actualizar)."""
    api_key = (os.getenv("LIVEKIT_API_KEY") or "").strip()
    api_secret = (os.getenv("LIVEKIT_API_SECRET") or "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("LiveKit no está configurado")
    now = int(time.time())
    payload = {
        "iss": api_key,
        "sub": api_key,
        "nbf": now - 10,
        "exp": now + 600,
        "video": {"room": room_name(session_id), "roomAdmin": True, "roomList": True},
    }
    return jwt.encode(payload, api_secret, algorithm="HS256")


async def _sala(session_id: str, metodo: str, cuerpo: dict):
    """Una llamada a RoomService (Twirp). Devuelve la respuesta o None si no hubo red."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5) as c:
            return await c.post(
                f"{_http_base()}/twirp/livekit.RoomService/{metodo}",
                headers={"Authorization": f"Bearer {_token_interaccion(session_id)}",
                         "Content-Type": "application/json"},
                json=cuerpo,
            )
    except Exception as e:  # red, timeout, DNS
        log.warning("LiveKit %s sin respuesta para %s: %s", metodo, room_name(session_id), e)
        return None


async def poner_mano(session_id: str, identity: str, arriba: bool) -> str:
    """Sube o baja la mano de un participante. "ok" | "no_esta" | "error"."""
    valor = str(int(time.time() * 1000)) if arriba else ""  # "" borra el atributo
    r = await _sala(session_id, "UpdateParticipant", {
        "room": room_name(session_id), "identity": identity,
        "attributes": {ATRIBUTO_MANO: valor},
    })
    if r is None:
        return "error"
    if r.status_code == 200:
        return "ok"
    if r.status_code == 404 or '"not_found"' in (r.text or "")[:200]:
        return "no_esta"
    log.warning("UpdateParticipant (mano) HTTP %s: %s", r.status_code, (r.text or "")[:200])
    return "error"


async def leer_estado_clase(session_id: str) -> dict | None:
    """Turno / temporizador / foco actuales (metadatos de la sala). None si falla."""
    import json

    r = await _sala(session_id, "ListRooms", {"names": [room_name(session_id)]})
    if r is None or r.status_code != 200:
        return None
    salas = (r.json() or {}).get("rooms", []) or []
    meta = (salas[0].get("metadata") if salas else "") or ""
    try:
        estado = json.loads(meta) if meta else {}
    except ValueError:
        estado = {}
    return estado if isinstance(estado, dict) else {}


async def guardar_estado_clase(session_id: str, estado: dict) -> bool:
    import json

    r = await _sala(session_id, "UpdateRoomMetadata", {
        "room": room_name(session_id),
        "metadata": json.dumps(estado, separators=(",", ":")),
    })
    if r is None or r.status_code != 200:
        if r is not None:
            log.warning("UpdateRoomMetadata HTTP %s: %s", r.status_code, (r.text or "")[:200])
        return False
    return True


# ============================================================================
# GATE 6G-B — Vida del token del navegador y tokens antiguos al finalizar
# ============================================================================

TTL_MAXIMO_TOKEN = 30 * 60   # segundos desde la emisión


def ttl_token_participante(fin_utc, ahora) -> int:
    """Segundos de vida del token: min(fin de la clase + 15 min, ahora + 30 min).

    Nunca vale más allá del margen final de la clase. /join ya rechaza pedirlo
    después de ese margen, así que el resultado es positivo.
    """
    from datetime import timedelta

    ttl = TTL_MAXIMO_TOKEN
    if fin_utc is not None:
        restante = (fin_utc + timedelta(minutes=MINUTOS_GRACIA_FINAL) - ahora).total_seconds()
        ttl = min(ttl, int(restante))
    return max(1, ttl)


async def revocar_tokens_de_la_clase(session_id: str, identidades) -> dict:
    """Al FINALIZAR: deja inútiles los tokens ya emitidos ANTES de cerrar la sala.

    DeleteRoom desconecta a todos pero (según la documentación de LiveKit) NO
    revoca tokens, y una sala se vuelve a crear sola cuando alguien entra. La vía
    documentada para revocar es RemoveParticipant: en LiveKit Cloud revoca el token
    de esa identidad (revokeTokenTs = ahora), también si ya no está conectada.
    Orden, con la sala todavía abierta:
      1. marcar "finalizada" en los metadatos de la sala (así quien se desconecta
         ve "La clase ha finalizado." y no "el profesor te retiró");
      2. revocar a cada persona que recibió token (presencia de /join) y a quien
         siga conectado;
      3. después, quien llama cierra la sala (DeleteRoom).
    Nunca lanza: si LiveKit falla, la clase sigue finalizada en Dorismon (/join la
    rechaza) y el TTL corto limita el resto.
    """
    import asyncio

    resumen = {"marcada": False, "revocados": 0, "no_estaban": 0, "errores": 0}
    try:
        estado = await leer_estado_clase(session_id)
        if estado is not None:
            estado["finalizada"] = True
            resumen["marcada"] = await guardar_estado_clase(session_id, estado)
        quienes = set(identidades or [])
        quienes |= {p.get("identity") for p in await list_participants(session_id) if p.get("identity")}
        limite = asyncio.Semaphore(8)

        async def una(identidad):
            async with limite:
                return await remove_participant(session_id, identidad)

        for r in await asyncio.gather(*[una(i) for i in sorted(quienes)]):
            resumen["revocados" if r == "ok" else "no_estaban" if r == "no_esta" else "errores"] += 1
    except Exception as e:  # nunca impide finalizar
        log.error("Revocar tokens de %s falló: %s", room_name(session_id), e)
        resumen["errores"] += 1
    if resumen["errores"]:
        log.error("Revocación de tokens incompleta en %s: %s", room_name(session_id), resumen)
    return resumen


# ============================================================================
# GATE 6G-C — Permisos dinámicos: cámara, micrófono y compartir pantalla
# ============================================================================
# En el token van como texto en minúsculas ("camera"…, auth/grants.go de LiveKit);
# en la API de servidor (ParticipantPermission.can_publish_sources) como el enum
# TrackSource: CAMERA=1, MICROPHONE=2, SCREEN_SHARE=3, SCREEN_SHARE_AUDIO=4.
# Si a alguien se le quita una fuente que está publicando, el servidor de LiveKit
# DESPUBLICA esa pista y rechaza volver a publicarla (NOT_ALLOWED).

FUENTES_MODERADOR = ("camera", "microphone", "screen_share", "screen_share_audio")
FUENTES_ESTUDIANTE = ("camera", "microphone")
_ENUM_FUENTE = {"camera": "CAMERA", "microphone": "MICROPHONE",
                "screen_share": "SCREEN_SHARE", "screen_share_audio": "SCREEN_SHARE_AUDIO"}
_FUENTE_DE_ENUM = {v: k for k, v in _ENUM_FUENTE.items()}
_FUENTE_DE_ENUM.update({1: "camera", 2: "microphone", 3: "screen_share", 4: "screen_share_audio"})
# Un cambio a la vez por (sala, persona): profesor y Dirección a la vez no se pisan
# (lectura + escritura del permiso completo). Vale dentro de un proceso del backend.
_CERROJOS: dict = {}


# PREPROD-1 (hotfix 6G-C) — Forma REAL comprobada en LiveKit Cloud: aquí no hay SDK; la API
# de servidor es Twirp JSON y httpx la devuelve como dict, con los nombres del proto en
# snake_case, TODOS los campos presentes (también los false) y las fuentes como texto:
#   {"can_subscribe": true, "can_publish": true, "can_publish_data": true,
#    "can_publish_sources": ["CAMERA", "MICROPHONE"], "hidden": false, "recorder": false,
#    "can_update_metadata": false, "agent": false, "can_subscribe_metrics": false, ...}
# snake_case es la fuente primaria; camelCase (nombre JSON del proto, que LiveKit también
# acepta al recibir) solo por compatibilidad. Lo que no se puede leer NO se rellena con
# False / []: eso retiraría permisos legítimos (era el defecto).
_PERMISO_ESENCIAL = ("can_publish", "can_subscribe", "can_publish_data", "can_publish_sources")


def _snake(clave: str) -> str:
    return "".join("_" + c.lower() if c.isupper() else c for c in clave)


def normalizar_permiso(permiso) -> dict | None:
    """ParticipantPermission leído de LiveKit → dict en snake_case con TODOS sus campos.

    None (no se puede conocer el estado actual) si no es un objeto, falta un campo
    esencial, un tipo no es el esperado, hay una fuente desconocida o la misma clave
    llega en las dos formas con valores distintos.
    """
    if not isinstance(permiso, dict):
        return None
    out: dict = {}
    for k, v in permiso.items():
        s = _snake(k)
        if s in out and out[s] != v:
            return None
        out[s] = v
    if any(k not in out for k in _PERMISO_ESENCIAL):
        return None
    if not all(type(out[k]) is bool for k in ("can_publish", "can_subscribe", "can_publish_data")):
        return None
    lista = out["can_publish_sources"]
    if not isinstance(lista, list) or not all(type(x) in (str, int) and x in _FUENTE_DE_ENUM for x in lista):
        return None
    return out


def fuentes_permitidas(permiso: dict) -> set | None:
    """Fuentes que puede publicar según un ParticipantPermission (JSON de LiveKit).

    Regla de LiveKit: sin can_publish no publica nada; con can_publish y lista VACÍA,
    todo (por eso nunca se guarda una lista vacía con can_publish = true).
    None si el permiso no se puede leer (ver normalizar_permiso).
    """
    p = normalizar_permiso(permiso)
    if p is None:
        return None
    if not p["can_publish"]:
        return set()
    if not p["can_publish_sources"]:
        return set(_ENUM_FUENTE)
    return {_FUENTE_DE_ENUM[x] for x in p["can_publish_sources"]}


async def cambiar_fuentes(session_id: str, identity: str, agregar=(), quitar=()):
    """Cambia SOLO las fuentes pedidas, conservando todo lo demás del permiso vigente.

    1) GetParticipant → permiso ACTUAL completo; 2) se agrega / quita lo pedido;
    3) UpdateParticipant con el permiso COMPLETO (LiveKit lo reemplaza entero).
    Devuelve (resultado, fuentes_finales): "ok" | "no_esta" | "error".
    Nunca enciende nada: un permiso no publica; la persona decide si activa.
    """
    import asyncio

    clave = (session_id, identity)
    cerrojo = _CERROJOS.setdefault(clave, asyncio.Lock())
    async with cerrojo:
        r = await _sala(session_id, "GetParticipant", {"room": room_name(session_id), "identity": identity})
        if r is None:
            return "error", None
        if r.status_code == 404 or '"not_found"' in (r.text or "")[:200]:
            return "no_esta", None
        if r.status_code != 200:
            log.warning("GetParticipant HTTP %s: %s", r.status_code, (r.text or "")[:200])
            return "error", None
        try:
            cuerpo = r.json()
        except ValueError:
            cuerpo = None
        actual = normalizar_permiso(cuerpo.get("permission") if isinstance(cuerpo, dict) else None)
        if actual is None:
            # Sin el permiso vigente no se escribe nada: un permiso a ciegas lo borraría todo
            log.error("GetParticipant: permiso ilegible en %s; no se cambia nada", room_name(session_id))
            return "error", None
        fuentes = (fuentes_permitidas(actual) | set(agregar)) - set(quitar)
        orden = [f for f in _ENUM_FUENTE if f in fuentes]
        # Lo demás (suscripción, datos, hidden, recorder, agent…) se conserva tal como estaba
        permiso = dict(actual)
        # Sin ninguna fuente: can_publish = false (una lista vacía significaría "todo")
        permiso["can_publish"] = bool(orden)
        permiso["can_publish_sources"] = [_ENUM_FUENTE[f] for f in orden]
        r = await _sala(session_id, "UpdateParticipant", {
            "room": room_name(session_id), "identity": identity, "permission": permiso})
        if r is None:
            return "error", None
        if r.status_code == 404 or '"not_found"' in (r.text or "")[:200]:
            return "no_esta", None
        if r.status_code != 200:
            log.warning("UpdateParticipant (permisos) HTTP %s: %s", r.status_code, (r.text or "")[:200])
            return "error", None
        return "ok", orden
