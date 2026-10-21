"""GATE 6A — Video Dorismon 2.0: base sana.

API (contra el servidor local, con la configuración LiveKit de pruebas):
  · el profesor entra como moderador; Dirección también; el estudiante no;
  · heartbeat después de entrar → 200;
  · el token conserva TTL (3 h), sala y permisos de siempre.
  (Estudiante no autorizado, presencial, cancelada y completed ya los cubren
   test_gate0 / test_gate1 / test_gate2; heartbeat sin entrar, test_audit_v3974.)

Estático (frontend):
  · adaptiveStream y dynacast activos; códec, resolución y bitrate sin tocar;
  · un solo renderizador de audio (el de <VideoConference />);
  · mensajes de cámara / micrófono y de conexión en español;
  · los errores de medios no sacan de la clase; la salida voluntaria no muestra
    un mensaje falso (CLIENT_INITIATED → sin aviso);
  · nada de Gate 6B ni 6C (PreJoin), /clase/{id} igual, Finalizar / DeleteRoom /
    TTL / enlaces externos intactos.

Las pruebas de comportamiento en navegador (sala real de LiveKit en el cliente,
conteo de renderizadores por modo, navegación con mini, heartbeat) se hicieron
aparte; ver el informe de Gate 6A.

Actualizado en Gate 6B: [4] texto "inestable", [5] y [6] (ver comentarios).
Actualizado en Gate 6C: delimitación del bloque de medios y entrada por pantalla previa.

Actualizado en Gate 6E: backend y localStorage vigilados salvo lo autorizado en 6E.

Uso: python tests/test_gate6a.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import httpx

# Gate 6E (autorizado después) toca puntos concretos del backend y usa
# localStorage para la clase activa: se vigila que NADA MÁS cambie.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Actualizado en Gate 6G-A: el panel de participantes cambia con autorización; el resto de
# ClassRoomExtras (mano, heartbeat, ventana flotante) debe seguir idéntico.
from _base_gate6ga import extras_sin_6ga  # noqa: E402
from _base_gate6e import huella_sin_gate6e, delete_room_solo_en_6e, localstorage_solo_clase_activa  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}
DB_URL_ORIGINAL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dorismon.db")


def sha(b: bytes) -> str:
    return hashlib.sha256(b.replace(b"\r\n", b"\n")).hexdigest()


def codigo(txt: str) -> str:
    """Sin comentarios: se revisa lo que se ejecuta."""
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"\{/\*.*?\*/\}", "", txt, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", txt)


def claims(token: str) -> dict:
    cuerpo = token.split(".")[1]
    cuerpo += "=" * (-len(cuerpo) % 4)
    return json.loads(base64.urlsafe_b64decode(cuerpo))


def limpiar_sesiones_locales(ids):
    """Mismo aislamiento que test_gate0/1: borra SOLO lo que este test creó, en la SQLite LOCAL."""
    import sqlite3
    if not ids or not DB_URL_ORIGINAL.startswith("sqlite"):
        return None
    ruta = Path(DB_URL_ORIGINAL.split("///", 1)[-1])
    if not ruta.is_file():
        return None
    cx = sqlite3.connect(str(ruta), timeout=30)
    try:
        marcas = ",".join("?" * len(ids))
        for (t,) in cx.execute("select name from sqlite_master where type='table'").fetchall():
            if t == "class_sessions":
                continue
            cols = {r[1] for r in cx.execute(f"pragma table_info({t})")}
            for col in ("session_id", "original_session_id", "makeup_session_id"):
                if col in cols:
                    cx.execute(f"delete from {t} where {col} in ({marcas})", ids)
        n = cx.execute(f"delete from class_sessions where id in ({marcas})", ids).rowcount
        cx.commit()
        return n
    finally:
        cx.close()


async def parte_api(check):
    print("\n[API] /join y heartbeat (sin cambios en Gate 6A)")
    creadas = []
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        async def login(cred):
            r = await c.post("/auth/login", json=cred)
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        AH, TH, SH = await login(ADMIN), await login(TEACHER), await login(STUDENT)
        st = (await c.get("/video/status", headers=AH)).json()
        if not st.get("ready"):
            check("LiveKit de pruebas configurado en el servidor local (LIVEKIT_* )", False)
            return
        prof = (await c.get("/auth/me", headers=TH)).json()
        now = dt.datetime.now(dt.timezone.utc)
        r = await c.post("/admin/events", headers=AH, json={
            "title": "G6A evento", "teacher_id": prof["id"], "modality": "online",
            "video_provider": "dorismon",
            "starts_at_utc": (now - dt.timedelta(minutes=5)).isoformat(),
            "ends_at_utc": (now + dt.timedelta(minutes=55)).isoformat()})
        evt = (r.json().get("id") or r.json().get("session_id")) if r.status_code in (200, 201) else None
        if evt:
            creadas.append(evt)
        check("se crea un evento online con Video Dorismon", bool(evt))
        if not evt:
            return
        try:
            rt = await c.post(f"/video/sessions/{evt}/join", headers=TH)
            check("profesor entra (200) como moderador", rt.status_code == 200 and rt.json().get("is_moderator") is True)
            ra = await c.post(f"/video/sessions/{evt}/join", headers=AH)
            check("Dirección entra (200) como moderador", ra.status_code == 200 and ra.json().get("is_moderator") is True)
            rs = await c.post(f"/video/sessions/{evt}/join", headers=SH)
            check("estudiante entra (200) SIN ser moderador", rs.status_code == 200 and rs.json().get("is_moderator") is False)
            hb = await c.post(f"/video/sessions/{evt}/heartbeat", headers=SH)
            check("heartbeat después de entrar → 200", hb.status_code == 200)

            ct, cs = claims(rt.json()["token"]), claims(rs.json()["token"])
            # Actualizado en Gate 6G-B: vida corta min(fin + 15 min, ahora + 30 min)
            check("token: vida corta de Gate 6G-B (≤ 30 min)", 0 < ct["exp"] - ct["nbf"] - 10 <= 30 * 60)
            check("token: sala dorismon-<id> y permisos de siempre",
                  ct["video"]["room"] == f"dorismon-{evt}"
                  and all(ct["video"].get(k) is True for k in ("roomJoin", "canPublish", "canSubscribe", "canPublishData")))
            # Actualizado en Gate 6G-B: NADIE recibe roomAdmin en el navegador (la moderación va por el backend)
            check("token: sin roomAdmin en el navegador (profesor ni estudiante)",
                  "roomAdmin" not in ct["video"] and "roomAdmin" not in cs["video"])
            # Actualizado en Gate 6G-C: fuentes por rol (el estudiante no comparte pantalla sin permiso)
            check("token: canPublishSources por rol (Gate 6G-C)",
                  ct["video"].get("canPublishSources") == ["camera", "microphone", "screen_share", "screen_share_audio"]
                  and cs["video"].get("canPublishSources") == ["camera", "microphone"])
        finally:
            n = limpiar_sesiones_locales(creadas)
            print(f"    · limpieza local: {n} sesión(es) de prueba borradas" if n is not None
                  else "    · limpieza local no aplicable")


def parte_estatica(check):
    rutas = {k: WEB / v for k, v in {
        "cp": "components/CallProvider.tsx", "css": "app/globals.css",
        "clase": "app/clase/[id]/page.tsx", "ui": "components/ui.tsx",
        "extras": "components/ClassRoomExtras.tsx"}.items()}
    if not all(p.exists() for p in rutas.values()):
        check(f"existen los archivos del frontend en {WEB}", False)
        return
    raw = {k: p.read_bytes() for k, p in rutas.items()}
    t = {k: b.decode("utf-8").replace("\r\n", "\n") for k, b in raw.items()}
    cp, cpc = t["cp"], codigo(t["cp"])

    print("\n[1] Calidad: adaptiveStream + dynacast, nada más")
    check("adaptiveStream: true y dynacast: true",
          "const OPCIONES_SALA: RoomOptions = { adaptiveStream: true, dynacast: true };" in cp)
    check("las opciones se pasan a la sala (options={OPCIONES_SALA})", "options={OPCIONES_SALA}" in cp)
    check("códec sin cambios (sin videoCodec / backupCodec)", not re.search(r"videoCodec|backupCodec", cpc))
    check("resolución sin cambios (sin videoCaptureDefaults / VideoPresets / resolution)",
          not re.search(r"videoCaptureDefaults|VideoPresets|resolution", cpc))
    check("bitrate de audio sin cambios (sin publishDefaults / audioPreset / AudioPresets)",
          not re.search(r"publishDefaults|audioPreset|AudioPresets|maxBitrate", cpc))

    print("\n[2] Audio: un solo renderizador")
    check("CallProvider ya no monta <RoomAudioRenderer /> ni lo importa",
          "<RoomAudioRenderer" not in cpc and not re.search(r"import[^;]*RoomAudioRenderer", cpc))
    # Actualizado en Gate 6F-A: el mismo <VideoConference /> recibe el foco del profesor
    # como su SettingsComponent (sin reemplazarlo); sigue habiendo uno en mini y uno en full/split.
    check("<VideoConference /> (que trae el suyo) sigue en mini y en full/split",
          cpc.count("<VideoConference SettingsComponent={AplicarFoco} />") == 2 and cpc.count("<VideoConference") == 2)

    print("\n[3] Mensajes de cámara y micrófono")
    for m in (
        "No pudimos acceder a tu cámara. Puedes continuar sin video o revisar los permisos del navegador.",
        "No encontramos una cámara en este dispositivo. Puedes continuar la clase sin video.",
        "Tu cámara está siendo usada por otra aplicación. Ciérrala y vuelve a activar la cámara.",
        "Se desconectó tu cámara. Puedes continuar sin video o conectar otra.",
    ):
        check(f"cámara: «{m[:48]}…»", m in cp)
    for m in (
        "No pudimos acceder a tu micrófono. Revisa los permisos del navegador para poder hablar en clase.",
        "No encontramos un micrófono. Conecta uno o revisa los ajustes para poder hablar en clase.",
        "Tu micrófono está siendo usado por otra aplicación. Ciérrala y vuelve a activar el micrófono.",
        "Se desconectó tu micrófono. Conecta otro o revisa los ajustes para poder hablar en clase.",
    ):
        check(f"micrófono: «{m[:48]}…»", m in cp)
    check("conectado al evento de LiveKit (onMediaDeviceFailure)", "onMediaDeviceFailure={alFallarDispositivo}" in cp)
    check("dispositivo que desaparece: se escucha MediaDevicesChanged", "room.on(RoomEvent.MediaDevicesChanged, revisar);" in cp)
    # (Gate 6C: el bloque se delimita por su propio final, alFallarDispositivo)
    i, j = cp.find("const avisarMedio"), cp.find("}, [avisarMedio]);")
    bloque_medios = cp[i:j] if 0 < i < j else ""
    check("los errores de medios NO desmontan la sala (solo guardan un aviso)",
          bool(bloque_medios) and not re.search(r"setLlamada|onSalir|onDesconectado|disconnect\(", bloque_medios))

    print("\n[4] Conexión en español (solo presentación)")
    # (Gate 6B, autorizado después, fija el texto final "La conexión está inestable.")
    for m in ("Conectando con la clase…", "Reconectando…", "La conexión está inestable"):
        check(f"«{m}»", m in cp)
    check("usa el estado real de LiveKit (useConnectionState / calidad del participante)",
          "useConnectionState()" in cp and "useConnectionQualityIndicator({ participant: room.localParticipant })" in cp)
    check("el aviso en inglés de LiveKit queda oculto",
          ".ds-lk-stage .lk-toast-connection-state { display: none; }" in t["css"])

    # Gate 6B (autorizado después) reemplaza el diálogo de 6A por la pantalla
    # final dentro de la capa y la reentrada; eso lo verifica test_gate6b.py.
    # Aquí se conserva lo que 6A garantiza: salida voluntaria SIN mensaje y
    # motivos explicados en español.
    print("\n[5] Desconexión: salida voluntaria sin mensaje; motivos explicados")
    check("salida voluntaria → sin mensaje (hecho: la persona pulsó salir)",
          "if (h.salidaPedida) return null;" in cp)
    for m in ("El profesor te retiró de la clase.",
              "Esta clase se abrió desde otro dispositivo.",
              "La sala de esta clase se cerró.",
              "Se perdió la conexión con la clase."):
        check(f"motivo: «{m[:45]}…»", m in cp)
    check("Salir sigue igual: desconecta la sala y cierra",
          "try { room.disconnect(); } catch { /* ya desconectado */ }\n    onSalir();" in cp)

    print("\n[6] Fuera de alcance: intacto")
    check("sin conexión a mano; localStorage solo para la clase activa (6E)",
          not re.search(r"room\.connect\(", cpc) and localstorage_solo_clase_activa(cpc))
    # Gate 6C (autorizado después) añade la pantalla previa propia: la sala ya
    # no enciende cámara/micrófono sola, aplica lo elegido (test_gate6c.py).
    check("sin el <PreJoin> oficial de LiveKit en la sala", "PreJoin" not in cpc and "usePreviewTracks" not in cpc)
    check("la sala aplica la elección de la pantalla previa (Gate 6C)",
          "        connect\n        video={opcionVideo}\n        audio={opcionAudio}\n" in cp)
    check("/clase/{id} sin cambios",
          sha(raw["clase"]) == "2f9007d7022de0401e02f4d6b8f5dd5dff3cd14ff5be4eb91a0dbe075c8ec075")
    check("CallProvider sigue abriendo dorismon_abrir_clase igual",
          'const pendiente = sessionStorage.getItem("dorismon_abrir_clase");' in cp
          and "        entrar(pendiente);" in cp)
    check("ClassRoomExtras (mano, participantes, moderación, heartbeat) sin cambios",
          extras_sin_6ga(raw["extras"]))
    ui = t["ui"]
    a, b = ui.find("export function JoinClassButton"), ui.find("\nexport function", ui.find("export function JoinClassButton") + 10)
    check("enlaces externos (Zoom / Meet / Teams / fallback) sin cambios",
          sha(ui[a:b].encode("utf-8")) == "ea3b2523bdb49512bf7297da569f139a164eaee8fd0df0eb6ae84d342bfc5e51"
          and cp.count("datos.fallback_url") == 4)

    back = {
        "app/services/livekit_service.py": "6ad4bc43fb596fd27fd13f9d47264da693a4c9f1a474b07f42de7b8830355985",
        "app/routers/video.py": "6aaa7d192751c9c8c85b20ac17187e26e615c2b5b457af158cca564c68fdc6e0",
        "app/routers/teacher.py": "d91197f1d609aaeef415d5984c30b938fa6b320d588f7ee36103508a0f19f00d",
    }
    for rel, h in back.items():
        check(f"backend sin cambios salvo lo autorizado en 6E: {rel} (token/TTL, /join, Finalizar)",
              huella_sin_gate6e(ROOT, rel) == h)
    check("DeleteRoom solo en el cierre de sala de 6E (servidor)", delete_room_solo_en_6e(ROOT))


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    await parte_api(check)
    parte_estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
