"""Ayuda para los tests de Gates 6A–6D tras Gate 6E (autorizado después).

Gate 6E toca tres archivos del backend en puntos concretos (cerrar la sala al
finalizar, heartbeat que rechaza clases finalizadas). Para seguir demostrando que
NADA MÁS cambió, se quitan EXACTAMENTE esas inserciones y el resultado debe tener
la misma huella que antes de 6E.
"""
import hashlib
from pathlib import Path

_TEACHER_6E = '''

    # GATE 6E — "Finalizar clase para todos": si la clase usa Video Dorismon,
    # se cierra también la sala de LiveKit (desconecta a quien siga dentro).
    # Va DESPUÉS de guardar "completed" y la auditoría: si LiveKit falla, la
    # clase queda finalizada igual (/join y heartbeat la rechazan) y se informa.
    # Zoom / Meet / Teams / enlace de respaldo: nada cambia ("no_aplica").
    video_room = "no_aplica"
    from app.services.audience import tiene_entrada_online
    from app.services.livekit_service import livekit_ready, delete_room
    if session.video_provider == "dorismon" and tiene_entrada_online(session) and livekit_ready():
        video_room = await delete_room(session_id)
    return {"ok": True, "attendance_taken": att_count > 0, "attendance_count": att_count,
            "video_room": video_room}'''
_TEACHER_ANTES = '''
    return {"ok": True, "attendance_taken": att_count > 0, "attendance_count": att_count}'''

_VIDEO_6E = '''    # GATE 6E — Una clase FINALIZADA ya no suma presencia (antes seguía
    # aceptando avisos hasta el margen final). Lo ya registrado no se toca.
    if s.status == SessionStatus.completed:
        raise HTTPException(400, "Esta clase ya fue finalizada")
'''

_LIVEKIT_6E = "\n\n# ============================================================================\n# GATE 6E"


def sin_gate6e(rel: str, txt: str) -> str:
    """Devuelve el archivo sin las inserciones de Gate 6E (o '' si no están EXACTAS)."""
    if rel.endswith("teacher.py"):
        return txt.replace(_TEACHER_6E, _TEACHER_ANTES, 1) if txt.count(_TEACHER_6E) == 1 else txt
    if rel.endswith("video.py"):
        return txt.replace(_VIDEO_6E, "", 1) if txt.count(_VIDEO_6E) == 1 else txt
    if rel.endswith("livekit_service.py"):
        i = txt.find(_LIVEKIT_6E)
        return txt[:i] if i > 0 else txt
    return txt


# Actualizado en Gate 6F-A (autorizado después): video.py termina ahora con el
# endpoint /interaccion (mano, turno, temporizador, foco). Se quita EXACTAMENTE ese
# bloque final (marcado "GATE 6F-A") y lo anterior debe seguir idéntico.
# (En livekit_service.py el bloque 6F-A va después del de 6E y ya se recorta.)
_FIN_6FA = "\n\n\n# ============================================================================\n# GATE 6F-A"


def sin_gate6fa(rel: str, txt: str) -> str:
    if rel.endswith("video.py"):
        i = txt.find(_FIN_6FA)
        return txt[:i + 1] if i > 0 else txt
    return txt


def huella_sin_gate6e(root: Path, rel: str) -> str:
    txt = (root / rel).read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
    # Actualizado en Gate 6G-A: también se deshacen EXACTAMENTE sus cambios (ver _base_gate6ga.py)
    from _base_gate6ga import sin_gate6ga
    return hashlib.sha256(sin_gate6e(rel, sin_gate6fa(rel, sin_gate6ga(rel, txt))).encode("utf-8")).hexdigest()


def delete_room_solo_en_6e(root: Path) -> bool:
    """DeleteRoom solo aparece en delete_room() de livekit_service (servidor)."""
    for p in (root / "app").rglob("*.py"):
        t = p.read_text(encoding="utf-8")
        if "DeleteRoom" in t:
            if p.name != "livekit_service.py":
                return False
            if t.find("DeleteRoom") < t.find("async def delete_room("):
                return False
    return True


def localstorage_solo_clase_activa(cp_codigo: str) -> bool:
    """En CallProvider, localStorage solo se usa con la clave de la clase activa."""
    import re
    usos = re.findall(r"localStorage\.\w+\(([^)]*)\)", cp_codigo)
    return bool(usos) and all(u.strip().startswith("CLAVE_CLASE_ACTIVA") for u in usos)
