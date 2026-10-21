"""GATE 6C — Video Dorismon 2.0: pantalla previa y privacidad de entrada.

API (servidor local, configuración LiveKit de pruebas):
  · /join sigue siendo la autoridad y su respuesta no cambió (mismo contrato).

Estático (frontend):
  · Entrar → /join → pantalla previa → "Entrar a la clase" → sala;
  · cámara y micrófono APAGADOS por defecto, sin recordar la vez anterior,
    sin pedir permisos hasta que la persona enciende algo;
  · vista previa local (nada se publica), medidor de micrófono sin reproducirlo,
    altavoz solo si el navegador permite elegirlo;
  · al entrar se liberan las pistas de la vista previa y la sala aplica
    exactamente lo elegido; cancelar no monta la sala;
  · "Volver a entrar" (6B) y /clase/{id} pasan por la pantalla previa;
  · se mantienen 6A (adaptiveStream/dynacast, un audio) y 6B (reconexión).

Las pruebas de navegador (permisos REALES denegados; dispositivos SIMULADOS
para los caminos con cámara/micrófono encendidos) se describen en el informe
de Gate 6C y en gate6c_check_navegador.js.

Actualizado en Gate 6D: el useMemo de la elección se comprueba por nombre.

Actualizado en Gate 6E: /join con modo anterior, backend y localStorage salvo lo autorizado en 6E.

Uso: python tests/test_gate6c.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import datetime as dt
import hashlib
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
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}
DB_URL_ORIGINAL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dorismon.db")


def sha(b: bytes) -> str:
    return hashlib.sha256(b.replace(b"\r\n", b"\n")).hexdigest()


def codigo(txt: str) -> str:
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"\{/\*.*?\*/\}", "", txt, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", txt)


def funcion(txt: str, inicio: str) -> str:
    """Cuerpo aproximado de una función: desde su declaración hasta la siguiente."""
    i = txt.find(inicio)
    if i < 0:
        return ""
    j = min([k for k in (txt.find("\n  const ", i + len(inicio)), txt.find("\n  return (", i + len(inicio))) if k > 0] or [len(txt)])
    return txt[i:j]


def limpiar_sesiones_locales(ids):
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
    print("\n[API] /join sigue siendo la autoridad (contrato sin cambios)")
    creadas = []
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        async def login(cred):
            r = await c.post("/auth/login", json=cred)
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        AH, TH = await login(ADMIN), await login(TEACHER)
        if not (await c.get("/video/status", headers=AH)).json().get("ready"):
            check("LiveKit de pruebas configurado en el servidor local (LIVEKIT_*)", False)
            return
        prof = (await c.get("/auth/me", headers=TH)).json()
        now = dt.datetime.now(dt.timezone.utc)
        r = await c.post("/admin/events", headers=AH, json={
            "title": "G6C evento", "teacher_id": prof["id"], "modality": "online", "video_provider": "dorismon",
            "starts_at_utc": (now - dt.timedelta(minutes=5)).isoformat(),
            "ends_at_utc": (now + dt.timedelta(minutes=55)).isoformat()})
        evt = (r.json().get("id") or r.json().get("session_id")) if r.status_code in (200, 201) else None
        if evt:
            creadas.append(evt)
        try:
            rj = await c.post(f"/video/sessions/{evt}/join", headers=TH)
            check("/join del profesor → 200", rj.status_code == 200)
            check("respuesta de /join con los MISMOS campos de siempre (no se cambió el contrato)",
                  rj.status_code == 200 and set(rj.json()) == {
                      "late", "token", "url", "room", "is_moderator", "title", "display_name", "fallback_url",
                      "expires_in",   # Actualizado en Gate 6G-B: único campo nuevo (vida del token)
                      "grabando"})    # Actualizado en Gate 6H-A: campo aditivo (aviso de grabación)
        finally:
            n = limpiar_sesiones_locales(creadas)
            print(f"    · limpieza local: {n} sesión(es) de prueba borradas" if n is not None
                  else "    · limpieza local no aplicable")


def parte_estatica(check):
    rutas = {k: WEB / v for k, v in {
        "pe": "components/PreEntrada.tsx", "cp": "components/CallProvider.tsx",
        "ui": "components/ui.tsx", "clase": "app/clase/[id]/page.tsx",
        "extras": "components/ClassRoomExtras.tsx"}.items()}
    if not all(p.exists() for p in rutas.values()):
        check(f"existen los archivos del frontend (incluida la pantalla previa) en {WEB}", False)
        return
    raw = {k: p.read_bytes() for k, p in rutas.items()}
    t = {k: b.decode("utf-8").replace("\r\n", "\n") for k, b in raw.items()}
    pe, pec, cp, cpc = t["pe"], codigo(t["pe"]), t["cp"], codigo(t["cp"])

    print("\n[1] Flujo: /join → pantalla previa → sala")
    check("sin elección, la capa muestra la pantalla previa (no la sala)",
          ") : !eleccion ? (\n        <PreEntrada" in cp
          # Actualizado en Gate 6G-B: al elegir se pide token nuevo si el anterior está por vencer
          and "onEntrar={entrarDesdePrevia}" in cp and "    setEleccion(e);\n  }, [datos.expires_in, onReentrar, sessionId]);" in cp)
    check("la sala solo se monta después de elegir (rama siguiente al !eleccion)",
          cp.find("<PreEntrada") < cp.find("<LiveKitRoom"))
    # (6E: entre /join y setLlamada hay un comentario y se restaura el modo anterior)
    i_j = cp.find("const datos = await api(`/video/sessions/${sessionId}/join`, { method: \"POST\", auth: true });")
    check("/join sigue siendo la entrada (entrar → api /join)",
          0 < i_j < cp.find("      setLlamada({ sessionId, datos, modo: modoValido(modoInicial), info });", i_j)
          < cp.find("} catch (e: any) {", i_j))
    check("cancelar cierra la capa sin montar la sala", "onCancelar={onSalir}" in cp)
    check("la pantalla previa se ve completa; al entrar vuelve el modo que tenía",
          "const vista: ModoVista = enPrevia ? \"full\" : modo;" in cp)

    print("\n[2] Privacidad: todo apagado, sin permisos ni memoria")
    check("cámara y micrófono empiezan apagados (sin pista)",
          "useState<LocalVideoTrack | null>(null)" in pe and "useState<LocalAudioTrack | null>(null)" in pe)
    check("las pistas solo se crean al pulsar encender (no al montar)",
          pec.count("createLocalVideoTrack(") == 1 and pec.count("createLocalAudioTrack(") == 1
          and "createLocalVideoTrack(" in funcion(pe, "const encenderCamara")
          and "createLocalAudioTrack(" in funcion(pe, "const encenderMicrofono"))
    check("sin getUserMedia directo (solo a través de LiveKit al encender)", "getUserMedia" not in pec)
    check("la lista de dispositivos se lee SIN pedir permiso (enumerateDevices)", "enumerateDevices()" in pe)
    check("no se recuerda la vez anterior (sin localStorage / sessionStorage / persistencia)",
          not re.search(r"localStorage|sessionStorage|usePersistentUserChoices|saveUserChoices", pec))
    check("no se usa el <PreJoin> oficial (pide nombre, inglés, guarda elecciones)",
          "<PreJoin" not in pec and "usePreviewTracks" not in pec)
    check("reentrada (6B) vuelve a la pantalla previa con todo apagado",
          "setEleccion(null);   // GATE 6C" in cp and cp.find("setEleccion(null);") < cp.find("setTerminal(null);", cp.find("setEleccion(null);")))

    print("\n[3] Vista previa, medidor y altavoz")
    check("vista previa local de la cámara (attach a <video> silenciado)", "cam.attach(el);" in pe and "muted playsInline" in pe)
    check("medidor del micrófono con createAudioAnalyser (sin reproducirlo)",
          "createAudioAnalyser(mic, { cloneTrack: true })" in pe and "mic.attach" not in pe and "<audio" not in pe)
    check("altavoz solo si el navegador lo permite (supportsAudioOutputSelection)",
          "supportsAudioOutputSelection()" in pe and "{salidaSoportada && lista.audiooutput.length > 0 && (" in pe)
    check("«Probar sonido» solo dentro de ese bloque", pe.find("data-probar-sonido") > pe.find("{salidaSoportada && lista.audiooutput.length > 0 && ("))
    check("selectores de cámara y micrófono", 'data-selector="camara"' in pe and 'data-selector="microfono"' in pe)
    check("cambiar dispositivo usa setDeviceId (no abre una segunda pista)",
          "await cam.setDeviceId(id)" in pe and "await mic.setDeviceId(id)" in pe)
    check("profesor y hora con la misma función que la tarjeta (rangoHora)",
          'import { rangoHora } from "@/components/ClaseProfesor";' in pe and "Profesor: ${info.profesor}" in pe)
    check("el botón de entrar pasa profesor y hora (sin cambiar /join)",
          "profesor: session.teacher_name || null," in t["ui"] and "inicio: session.starts_at_utc || null," in t["ui"])

    print("\n[4] Permisos y dispositivos ausentes (mensajes)")
    for m in ("No pudimos acceder a tu cámara. Puedes entrar sin video.",
              "No pudimos acceder a tu micrófono. Revisa el permiso antes de entrar si quieres hablar.",
              "No encontramos una cámara en este dispositivo. Puedes entrar sin video.",
              "No encontramos un micrófono. Podrás escuchar la clase, pero no hablar.",
              "Este dispositivo no tiene cámara. Puedes entrar sin video."):
        check(f"«{m[:50]}…»", m in pe)
    check("motivo del fallo con MediaDeviceFailure.getFailure (enum oficial)", pe.count("MediaDeviceFailure.getFailure(e)") == 2)
    check("los avisos no bloquean: «Entrar a la clase» sigue disponible",
          'disabled={abriendo !== ""}\n                  className="flex-[2]' in pe)

    print("\n[5] Entrar: se libera la vista previa y se aplica exactamente lo elegido")
    entrar = funcion(pe, "const entrar = () =>")
    check("al entrar se detienen las pistas de la vista previa ANTES de pasar la elección",
          "cam?.stop(); mic?.stop();" in entrar and entrar.find("cam?.stop()") < entrar.find("onEntrar(eleccion)"))
    check("cancelar y desmontar también las liberan",
          "cam?.stop(); mic?.stop();" in funcion(pe, "const cancelar = () =>")
          and "pistas.current.cam?.stop();" in pe and "pistas.current.mic?.stop();" in pe)
    check("la sala recibe la elección: apagado = false (no se captura)",
          "(eleccion?.camara ? (eleccion.camaraId ? { deviceId: eleccion.camaraId } : true) : false)" in cp
          and "(eleccion?.microfono ? (eleccion.microfonoId ? { deviceId: eleccion.microfonoId } : true) : false)" in cp
          and "video={opcionVideo}\n        audio={opcionAudio}" in cp)
    # (Gate 6D añade otro useMemo para el arrastre: se comprueban los dos de la elección)
    check("la elección es estable durante la clase (useMemo)",
          "const opcionVideo = useMemo(" in cp and "const opcionAudio = useMemo(" in cp)
    check("altavoz elegido: switchActiveDevice('audiooutput'), opciones de la sala intactas",
          'room.switchActiveDevice("audiooutput", deviceId)' in cp
          and "const OPCIONES_SALA: RoomOptions = { adaptiveStream: true, dynacast: true };" in cp
          and "options={OPCIONES_SALA}" in cp)

    print("\n[6] Se mantiene 6A / 6B y lo de fuera de alcance")
    check("un solo renderizador de audio (sin <RoomAudioRenderer /> propio)", "<RoomAudioRenderer" not in cpc)
    check("reconexión 6B intacta (hechos, pantalla final, reentrada por /join)",
          "function clasificarDesconexion" in cp and "<VigiaReconexion hechos={hechos} />" in cp and "onReentrar={volverAEntrar}" in cp)
    check("/clase/{id} sin cambios (ahora llega a la pantalla previa vía entrar)",
          sha(raw["clase"]) == "2f9007d7022de0401e02f4d6b8f5dd5dff3cd14ff5be4eb91a0dbe075c8ec075"
          and "        entrar(pendiente);" in cp)
    check("localStorage en la sala solo para la clase activa (6E)", localstorage_solo_clase_activa(cpc))
    check("heartbeat / mano / moderación sin cambios",
          extras_sin_6ga(raw["extras"]))
    ui = t["ui"]
    a = ui.find("export function JoinClassButton")
    b = ui.find("\nexport function", a + 10)
    check("enlaces externos (Zoom / Meet / Teams / fallback) sin cambios",
          sha(ui[a:b].encode("utf-8")) == "ea3b2523bdb49512bf7297da569f139a164eaee8fd0df0eb6ae84d342bfc5e51"
          and cp.count("datos.fallback_url") == 4)
    for rel, h in {
        "app/services/livekit_service.py": "6ad4bc43fb596fd27fd13f9d47264da693a4c9f1a474b07f42de7b8830355985",
        "app/routers/video.py": "6aaa7d192751c9c8c85b20ac17187e26e615c2b5b457af158cca564c68fdc6e0",
        "app/routers/teacher.py": "d91197f1d609aaeef415d5984c30b938fa6b320d588f7ee36103508a0f19f00d",
    }.items():
        check(f"backend sin cambios salvo lo autorizado en 6E: {rel}", huella_sin_gate6e(ROOT, rel) == h)


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
