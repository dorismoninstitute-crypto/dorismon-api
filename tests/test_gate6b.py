"""GATE 6B — Video Dorismon 2.0: conexión, reconexión y reentrada.

API (servidor local con la configuración LiveKit de pruebas):
  · "Volver a entrar" = un /join NUEVO: se puede pedir de nuevo y vuelve a
    comprobar todo (después de finalizar o cancelar la clase, lo rechaza);
  · heartbeat solo con presencia creada por /join.

Estático (frontend):
  · estado explícito con HECHOS (conectada alguna vez, reconectando, salida
    pedida, error de conexión), sin tiempos ni suposiciones;
  · motivos de LiveKit por su enum (sin números mágicos);
  · reconexión: la sala NO se desmonta, no hay /join ni token nuevo;
  · desconexión definitiva: pantalla final DENTRO de la capa (sin sala, sin
    heartbeat, sin audio) con "Volver a entrar" / "Intentar de nuevo" / "Salir";
  · todas las salidas voluntarias registran el hecho (cabecera, mini, LiveKit);
  · avisos de una sala anterior no afectan a la entrada actual (por token);
  · nada de 6C+ (PreJoin, recarga, localStorage, Finalizar, TTL, enlaces).

Las pruebas de navegador (fallo inicial REAL, reconexión y motivos simulados,
cada botón de salida, mini + navegación, /join rechazado) se describen en el
informe de Gate 6B y en gate6b_check_navegador.js.

Actualizado en Gate 6C: "Salir en la pantalla final" y entrada por pantalla previa.

Actualizado en Gate 6E: backend y localStorage vigilados salvo lo autorizado en 6E.

Uso: python tests/test_gate6b.py [BASE_URL] [RUTA_DORISMON_WEB]
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
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}
DB_URL_ORIGINAL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dorismon.db")


def sha(b: bytes) -> str:
    return hashlib.sha256(b.replace(b"\r\n", b"\n")).hexdigest()


def codigo(txt: str) -> str:
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"\{/\*.*?\*/\}", "", txt, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", txt)


def limpiar_sesiones_locales(ids):
    """Mismo aislamiento que test_gate0/1/6a: borra SOLO lo que este test creó, en la SQLite LOCAL."""
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
    print("\n[API] Volver a entrar = /join nuevo, con todas las comprobaciones")
    creadas = []
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        async def login(cred):
            r = await c.post("/auth/login", json=cred)
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        AH, TH, SH = await login(ADMIN), await login(TEACHER), await login(STUDENT)
        if not (await c.get("/video/status", headers=AH)).json().get("ready"):
            check("LiveKit de pruebas configurado en el servidor local (LIVEKIT_*)", False)
            return
        prof = (await c.get("/auth/me", headers=TH)).json()
        now = dt.datetime.now(dt.timezone.utc)

        async def evento(nombre):
            r = await c.post("/admin/events", headers=AH, json={
                "title": nombre, "teacher_id": prof["id"], "modality": "online", "video_provider": "dorismon",
                "starts_at_utc": (now - dt.timedelta(minutes=5)).isoformat(),
                "ends_at_utc": (now + dt.timedelta(minutes=55)).isoformat()})
            i = (r.json().get("id") or r.json().get("session_id")) if r.status_code in (200, 201) else None
            if i:
                creadas.append(i)
            return i

        a, b, cc = await evento("G6B a"), await evento("G6B b"), await evento("G6B c")
        check("se crean 3 eventos online con Video Dorismon", all((a, b, cc)))
        if not all((a, b, cc)):
            return
        try:
            r1 = await c.post(f"/video/sessions/{a}/join", headers=SH)
            await asyncio.sleep(1.1)  # tokens firmados en segundos distintos
            r2 = await c.post(f"/video/sessions/{a}/join", headers=SH)
            check("volver a entrar: un segundo /join es aceptado (200)", r1.status_code == 200 and r2.status_code == 200)
            check("volver a entrar: entrega un token NUEVO (no se reutiliza el viejo)",
                  r1.status_code == 200 and r2.status_code == 200 and r1.json()["token"] != r2.json()["token"])
            hb = await c.post(f"/video/sessions/{a}/heartbeat", headers=SH)
            check("heartbeat tras entrar → 200", hb.status_code == 200)

            await c.post(f"/video/sessions/{b}/join", headers=TH)
            fin = await c.post(f"/teacher/sessions/{b}/finalize", headers=TH)
            rb = await c.post(f"/video/sessions/{b}/join", headers=TH)
            check("tras FINALIZAR, volver a entrar es rechazado (400 'finalizada')",
                  fin.status_code == 200 and rb.status_code == 400 and "finalizada" in rb.text)

            await c.post(f"/video/sessions/{cc}/join", headers=SH)
            await c.delete(f"/admin/sessions/{cc}", headers=AH)
            rc = await c.post(f"/video/sessions/{cc}/join", headers=SH)
            check("tras CANCELAR, volver a entrar es rechazado (400 'cancelada')",
                  rc.status_code == 400 and "cancelada" in rc.text)
        finally:
            n = limpiar_sesiones_locales(creadas)
            print(f"    · limpieza local: {n} sesión(es) de prueba borradas" if n is not None
                  else "    · limpieza local no aplicable")


def parte_estatica(check):
    rutas = {k: WEB / v for k, v in {
        "cp": "components/CallProvider.tsx", "clase": "app/clase/[id]/page.tsx",
        "ui": "components/ui.tsx", "extras": "components/ClassRoomExtras.tsx"}.items()}
    if not all(p.exists() for p in rutas.values()):
        check(f"existen los archivos del frontend en {WEB}", False)
        return
    raw = {k: p.read_bytes() for k, p in rutas.items()}
    t = {k: b.decode("utf-8").replace("\r\n", "\n") for k, b in raw.items()}
    cp, cpc = t["cp"], codigo(t["cp"])
    ini, fin = cp.find("function clasificarDesconexion"), cp.find("/**\n * V3.9.28")
    clasif = cp[ini:fin] if 0 < ini < fin else ""

    print("\n[1] Estado explícito con hechos (sin tiempos)")
    check("hechos: conectadaAlgunaVez, reconectando, salidaPedida, errorConexion",
          all(f"{h}: boolean;" in cp for h in ("conectadaAlgunaVez", "reconectando", "salidaPedida", "errorConexion")))
    check("los hechos se reinician con cada entrada (hechosNuevos)",
          "hechos.current = hechosNuevos();" in cp and "const hechos = useRef<Hechos>(hechosNuevos());" in cp)
    check("conectada: onConnected de LiveKit", "onConnected={alConectarse}" in cp and "hechos.current.conectadaAlgunaVez = true;" in cp)
    check("reconectando: eventos reales Reconnecting / SignalReconnecting / Reconnected",
          ".on(RoomEvent.Reconnecting, empieza)" in cp and ".on(RoomEvent.SignalReconnecting, empieza)" in cp
          and ".on(RoomEvent.Reconnected, termina)" in cp)
    check("error de conexión: onError solo si es ConnectionError",
          "onError={alFallarConexion}" in cp and "if (!(e instanceof ConnectionError)) return;" in cp)
    check("sin heurísticas de tiempo en la clasificación ni en los hechos",
          bool(clasif) and not re.search(r"setTimeout|Date\.now|performance\.now", clasif)
          and cpc.count("setTimeout(") == 1)  # el único es el de 6A (dar tiempo a LiveKit a cambiar de dispositivo)

    print("\n[2] Motivos de LiveKit por su enum")
    for n in ("PARTICIPANT_REMOVED", "DUPLICATE_IDENTITY", "ROOM_DELETED", "ROOM_CLOSED", "SERVER_SHUTDOWN",
              "MIGRATION", "CONNECTION_TIMEOUT", "SIGNAL_CLOSE"):
        check(f"DisconnectReason.{n}", f"DisconnectReason.{n}" in clasif)
    check("sin números mágicos de motivo", not re.search(r"motivo\s*===\s*\d|case\s+\d+\s*:", codigo(clasif)))

    print("\n[3] Salida voluntaria (todas las rutas)")
    check("salida pedida → se cierra sin mensaje", "if (h.salidaPedida) return null;" in cp
          and "if (!t) { onSalir(); return; }" in cp)
    check("cabecera y mini (colgar) registran la salida antes de desconectar",
          "marcarSalida();  // GATE 6B" in cp and cp.find("marcarSalida();  // GATE 6B") < cp.find("try { room.disconnect(); }"))
    check("botón Leave de LiveKit registra la salida (clic capturado)",
          'closest?.(".lk-disconnect-button")) marcarSalida();' in cp and "onClickCapture=" in cp)
    # (Gate 6C intercala la pantalla previa entre la pantalla final y la sala)
    check("Salir en la pantalla final cierra la capa", "onSalir={onSalir}\n        />\n      ) : " in cp)

    print("\n[4] Reconexión: misma sala, sin /join ni token nuevo")
    i_v, f_v = cp.find("function VigiaReconexion"), cp.find("function PantallaTerminal")
    vigia = cp[i_v:f_v] if 0 < i_v < f_v else ""
    check("al reconectar solo se anotan hechos (no se desmonta, no se pide /join)",
          bool(vigia) and not re.search(r"setTerminal|onReentrar|api\(|join|setLlamada|disconnect", vigia))
    check("textos: «Reconectando… tu clase sigue aquí.» y «La conexión está inestable.»",
          "Reconectando… tu clase sigue aquí." in cp and '"La conexión está inestable."' in cp)

    print("\n[5] Desconexión definitiva: pantalla final dentro de la capa")
    check("con pantalla final NO se monta la sala (ni heartbeat ni audio)",
          "{terminal ? (\n        <PantallaTerminal" in cp and "      ) : (\n      <LiveKitRoom" in cp)
    for m, rec in (("Se perdió la conexión con la clase.", True), ("El profesor te retiró de la clase.", False),
                   ("Esta clase se abrió desde otro dispositivo.", True), ("La clase ha finalizado.", False),
                   ("No pudimos conectar con la clase.", True)):
        check(f"«{m}» (recuperable={rec})", m in cp)
    check("retirado y finalizada NO ofrecen volver a entrar",
          re.search(r'tipo: "retirado".*?recuperable: false', cp, re.S) is not None
          and re.search(r'tipo: "finalizada".*?recuperable: false', cp, re.S) is not None)
    check("lo indistinguible se dice de forma neutra (sin inventar causa)",
          "Se cerró la conexión con la clase." in cp)
    check("fallo inicial: nunca conectada y sin salida pedida",
          "if (!h.conectadaAlgunaVez) return TERMINAL_FALLO_INICIAL;" in cp)
    check("botones: «Volver a entrar» / «Intentar de nuevo» / «Salir»",
          '"Intentar de nuevo" : "Volver a entrar"' in cp and "          Salir\n" in cp)

    print("\n[6] Reentrada = /join nuevo; nada viejo se mezcla")
    check("reentrar llama al /join normal (POST) y guarda los datos NUEVOS",
          "const datos = await api(`/video/sessions/${sessionId}/join`, { method: \"POST\", auth: true });\n"
          "      setLlamada((l) => (l && l.sessionId === sessionId ? { ...l, datos } : l));" in cp)
    check("si /join rechaza, se muestra su mensaje y no se rodea",
          'tipo: "rechazado", titulo: "No pudiste volver a entrar"' in cp and "recuperable: false" in cp)
    check("cada token es una sala nueva (key={datos.token})", "key={datos.token}" in cp)
    check("avisos de una sala anterior se ignoran (por token)", cp.count("if (tokenVigente.current !== token) return;") == 3)
    check("el modo (full/mini/split) vive en la llamada, no se toca al reentrar", "{ ...l, datos }" in cp)

    print("\n[7] Fuera de alcance: intacto")
    check("sin PreJoin", "PreJoin" not in cpc and "usePreviewTracks" not in cpc)
    check("sin conexión manual; localStorage solo para la clase activa (6E)",
          not re.search(r"room\.connect\(", cpc) and localstorage_solo_clase_activa(cpc))
    # Gate 6C (autorizado después): la sala aplica la elección de la pantalla previa
    check("la sala aplica la elección de la pantalla previa (Gate 6C)",
          "        connect\n        video={opcionVideo}\n        audio={opcionAudio}\n" in cp)
    check("/clase/{id} sin cambios", sha(raw["clase"]) == "2f9007d7022de0401e02f4d6b8f5dd5dff3cd14ff5be4eb91a0dbe075c8ec075")
    check("CallProvider sigue abriendo dorismon_abrir_clase igual",
          'const pendiente = sessionStorage.getItem("dorismon_abrir_clase");' in cp and "        entrar(pendiente);" in cp)
    check("heartbeat / mano / moderación (ClassRoomExtras) sin cambios",
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
        check(f"backend sin cambios salvo lo autorizado en 6E: {rel} (token/TTL, /join, heartbeat, Finalizar)",
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
