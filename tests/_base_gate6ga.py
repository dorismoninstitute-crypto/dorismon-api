"""Ayuda para los tests de Gates 6A–6F-A.1 tras Gate 6G-A (autorizado después).

Gate 6G-A cambia, con autorización, la moderación:
  · livekit_service.py: token de moderación ligado a la sala; silenciar / sacar;
  · video.py: bloqueo en /join, heartbeat e interacción; /moderate; /bloqueos;
  · ClassRoomExtras.tsx: SOLO el panel de participantes (retirados, Permitir volver).
Para seguir demostrando que NADA MÁS cambió, aquí se deshacen EXACTAMENTE esos
cambios (con el texto anterior) antes de comparar con las huellas de siempre.
Generado a partir de las copias previas a 6G-A.
"""
import hashlib

_FIN_6FA = '\n\n\n# ============================================================================\n# GATE 6F-A'
_FIN_6E = '\n\n\n# ============================================================================\n# GATE 6E'
_FIN_6GA = "\n\n\n# ============================================================================\n# GATE 6G-A"

_MODERATE_ANTES = '@router.post("/sessions/{session_id}/moderate")\nasync def moderate(\n    session_id: str,\n    body: dict,\n    user: Annotated[CurrentUser, Depends(get_current_user)],\n    db: AsyncSession = Depends(get_db),\n):\n    """V3.9.27 — El profesor silencia o saca a un participante.\n\n    Acciones: "mute" (silenciar), "unmute" (dejar hablar), "remove" (sacar).\n    Solo el profesor de la clase o el admin. Nadie puede sacar al profesor.\n    """\n    s = await db.get(ClassSession, session_id)\n    if not s:\n        raise HTTPException(404, "Clase no encontrada")\n    me = await db.get(User, user.user_id)\n    if not (s.teacher_id == user.user_id or (me and me.role == UserRole.super_admin)):\n        raise HTTPException(403, "Solo el profesor de la clase puede moderar")\n    if not livekit_ready():\n        raise HTTPException(503, "El video de Dorismon no está configurado")\n\n    action = (body.get("action") or "").strip()\n    identity = (body.get("identity") or "").strip()\n    if not identity:\n        raise HTTPException(400, "Falta indicar a quién")\n    if identity == s.teacher_id:\n        raise HTTPException(400, "No se puede silenciar ni sacar al profesor de la clase")\n\n    if action == "mute":\n        ok = await mute_participant(session_id, identity, True)\n    elif action == "unmute":\n        ok = await mute_participant(session_id, identity, False)\n    elif action == "remove":\n        ok = await remove_participant(session_id, identity)\n    else:\n        raise HTTPException(400, "Acción no válida")\n\n    if not ok:\n        raise HTTPException(400, "No se pudo aplicar. ¿Sigue esa persona conectada?")\n    return {"ok": True, "action": action}'
_LIVEKIT_MODERACION_ANTES = 'def _admin_token() -> str:\n    """Permiso de administración para hablarle al servidor de LiveKit."""\n    api_key = (os.getenv("LIVEKIT_API_KEY") or "").strip()\n    api_secret = (os.getenv("LIVEKIT_API_SECRET") or "").strip()\n    if not api_key or not api_secret:\n        raise RuntimeError("LiveKit no está configurado")\n    now = int(time.time())\n    payload = {\n        "iss": api_key,\n        "sub": api_key,\n        "nbf": now - 10,\n        "exp": now + 600,\n        "video": {"roomAdmin": True, "roomList": True},\n    }\n    return jwt.encode(payload, api_secret, algorithm="HS256")\n\n\ndef _http_base() -> str:\n    """El servidor de LiveKit para llamadas normales (no de video)."""\n    url = livekit_url() or ""\n    if url.startswith("wss://"):\n        return "https://" + url[len("wss://"):]\n    if url.startswith("ws://"):\n        return "http://" + url[len("ws://"):]\n    return url\n\n\nasync def list_participants(session_id: str) -> list[dict]:\n    """Quiénes están conectados ahora mismo en la sala de esa clase."""\n    import httpx\n\n    token = _admin_token()\n    async with httpx.AsyncClient(timeout=10) as c:\n        r = await c.post(\n            f"{_http_base()}/twirp/livekit.RoomService/ListParticipants",\n            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},\n            json={"room": room_name(session_id)},\n        )\n        if r.status_code != 200:\n            return []\n        return (r.json() or {}).get("participants", []) or []\n\n\nasync def mute_participant(session_id: str, identity: str, muted: bool = True) -> bool:\n    """El profesor silencia (o deja hablar) a un participante."""\n    import httpx\n\n    token = _admin_token()\n    parts = await list_participants(session_id)\n    target = next((p for p in parts if p.get("identity") == identity), None)\n    if not target:\n        log.warning("No se encontró a %s en la sala. Conectados: %s",\n                    identity, [p.get("identity") for p in parts])\n        return False\n    ok = False\n    async with httpx.AsyncClient(timeout=10) as c:\n        for track in target.get("tracks", []) or []:\n            # El tipo llega como texto ("AUDIO") o número (0) según la versión\n            tipo = track.get("type")\n            if tipo not in (0, "0", "AUDIO", "audio"):\n                continue\n            r = await c.post(\n                f"{_http_base()}/twirp/livekit.RoomService/MutePublishedTrack",\n                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},\n                json={\n                    "room": room_name(session_id),\n                    "identity": identity,\n                    # LiveKit espera "trackSid" (no "track_sid"): este era el\n                    # bug por el que silenciar nunca funcionaba.\n                    "trackSid": track.get("sid"),\n                    "muted": muted,\n                },\n            )\n            ok = ok or r.status_code == 200\n    return ok\n\n\nasync def remove_participant(session_id: str, identity: str) -> bool:\n    """El profesor saca a alguien de la clase."""\n    import httpx\n\n    token = _admin_token()\n    async with httpx.AsyncClient(timeout=10) as c:\n        r = await c.post(\n            f"{_http_base()}/twirp/livekit.RoomService/RemoveParticipant",\n            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},\n            json={"room": room_name(session_id), "identity": identity},\n        )\n        return r.status_code == 200'

_INSERCIONES_VIDEO = [
    '\n    # GATE 6G-A — "Sacar de la clase" dura toda la sesión: no hay token nuevo\n    # mientras el bloqueo siga activo (hasta que el profesor permita volver).\n    from app.services.video_bloqueos import bloqueo_activo, MENSAJE_RETIRADO\n    if not is_moderator and await bloqueo_activo(db, session_id, user.user_id):\n        raise HTTPException(403, MENSAJE_RETIRADO)\n',
    '        # GATE 6G-A — quien fue retirado de la clase ya no suma presencia\n        from app.services.video_bloqueos import bloqueo_activo, MENSAJE_RETIRADO\n        if await bloqueo_activo(db, session_id, user.user_id):\n            raise HTTPException(403, MENSAJE_RETIRADO)\n',
    '    # GATE 6G-A — quien fue retirado de la clase no interactúa\n    from app.services.video_bloqueos import bloqueo_activo, MENSAJE_RETIRADO\n    if not es_moderador and await bloqueo_activo(db, session_id, user.user_id):\n        raise HTTPException(403, MENSAJE_RETIRADO)\n',
]


def sin_gate6ga(rel: str, txt: str) -> str:
    """El archivo tal como era antes de 6G-A (si los cambios están EXACTOS)."""
    # Actualizado en Gate 6G-B: primero se deshacen EXACTAMENTE sus cambios
    from _base_gate6gb import sin_gate6gb
    txt = sin_gate6gb(rel, txt)
    if rel.endswith("video.py"):
        for ins in _INSERCIONES_VIDEO:
            if txt.count(ins) == 1:
                txt = txt.replace(ins, "", 1)
        i = txt.find(_FIN_6GA)
        if i > 0:
            txt = txt[:i] + "\n"
        a = txt.find('@router.post("/sessions/{session_id}/moderate")')
        b = txt.find(_FIN_6FA)
        if 0 < a < b:
            txt = txt[:a] + _MODERATE_ANTES + txt[b:]
        return txt
    if rel.endswith("livekit_service.py"):
        a = txt.find("def _admin_token(session_id: str) -> str:")
        b = txt.find(_FIN_6E)
        if 0 < a < b:
            txt = txt[:a] + _LIVEKIT_MODERACION_ANTES + txt[b:]
        return txt
    return txt


_P0 = '// ------------------------------------------------- Panel de participantes'
_P1 = '// -------------------------------------------- Ventana flotante'


def extras_sin_6ga(raw: bytes) -> bool:
    """ClassRoomExtras igual que antes salvo el panel de participantes (6G-A)."""
    t = raw.replace(b"\r\n", b"\n").decode("utf-8")
    # Actualizado en Gate 6G-C: se deshacen EXACTAMENTE sus cambios (permisos) antes
    from _base_gate6gc import sin_gate6gc
    t = sin_gate6gc("components/ClassRoomExtras.tsx", t)
    if _P0 not in t or _P1 not in t:
        return False
    t = t[:t.index(_P0)] + t[t.index(_P1):]
    return hashlib.sha256(t.encode("utf-8")).hexdigest() == '92aef4c9cf7cb5d3821fcf1fd60e6dfc0162f3ae9d3c0185b02d048012c4bfca'
