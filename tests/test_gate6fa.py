"""GATE 6F-A — Interacción para clases de idiomas (mano, reacciones, turno,
temporizador y foco).

[API] (contra el servidor local; todo se rechaza ANTES de hablar con LiveKit)
  · permisos: el estudiante solo sube/baja SU mano; no da turno, ni temporizador,
    ni foco; nadie levanta la mano de otro; otro profesor → 403; sin sesión → 401.
  · validación: acción, 'arriba', duración (10 s – 30 min), operación, identidad.
  · clase externa (Meet) / finalizada → 400. /join sigue igual.
[UNIDAD] servicio contra un LiveKit simulado LOCAL levantado por este test:
    mano = atributo "dorismon.mano" (hora del servidor; "" la borra); 404 → no_esta;
    5xx / caído → error; estado de la clase = metadatos de la sala (lectura y
    escritura); token de servidor limitado a ESA sala (sin roomJoin ni roomCreate).
[ESTÁTICO] estado actual por atributos/metadatos (lo recibe quien entra tarde);
    reacciones por el canal de datos con tema propio y nombre del remitente de
    LiveKit; turno sin tocar micrófonos; temporizador sin paquetes por segundo;
    foco dentro de <VideoConference /> sin reemplazarlo; nada en base de datos;
    6B / 6C / 6D / 6E intactos; fuera de alcance sin tocar.

Uso: python tests/test_gate6fa.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}
OTRO_PROFE = {"email": "ana@dorismon.do", "password": "Prof-Test-4822!"}
DB_URL_ORIGINAL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dorismon.db")


def sha(b: bytes) -> str:
    return hashlib.sha256(b.replace(b"\r\n", b"\n")).hexdigest()


def codigo(txt: str) -> str:
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"\{/\*.*?\*/\}", "", txt, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", txt)


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


# ============================================================================ API
async def parte_api(check):
    print("\n[API] /video/sessions/{id}/interaccion: permisos y validación (sin llegar a LiveKit)")
    creadas = []
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        async def login(cred):
            r = await c.post("/auth/login", json=cred)
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        AH, TH, SH, OH = await login(ADMIN), await login(TEACHER), await login(STUDENT), await login(OTRO_PROFE)
        if not (await c.get("/video/status", headers=AH)).json().get("ready"):
            check("LiveKit de pruebas configurado en el servidor local (LIVEKIT_*)", False)
            return
        prof = (await c.get("/auth/me", headers=TH)).json()
        alumna = (await c.get("/auth/me", headers=SH)).json()
        now = dt.datetime.now(dt.timezone.utc)

        async def evento(nombre, **extra):
            body = {"title": nombre, "teacher_id": prof["id"], "modality": "online", "video_provider": "dorismon",
                    "starts_at_utc": (now - dt.timedelta(minutes=5)).isoformat(),
                    "ends_at_utc": (now + dt.timedelta(minutes=55)).isoformat()}
            body.update(extra)
            r = await c.post("/admin/events", headers=AH, json=body)
            i = (r.json().get("id") or r.json().get("session_id")) if r.status_code in (200, 201) else None
            if i:
                creadas.append(i)
            return i

        a = await evento("G6FA dorismon")
        ext = await evento("G6FA externa", video_provider="meet", meeting_url="https://meet.google.com/abc-defg-hij")
        fin = await evento("G6FA finalizada")
        check("se crean eventos (Video Dorismon, externo y uno para finalizar)", all((a, ext, fin)))
        if not all((a, ext, fin)):
            return
        try:
            I = lambda h, sid, **b: c.post(f"/video/sessions/{sid}/interaccion", headers=h, json=b)
            check("/join sigue igual: profesor y estudiante entran (200)",
                  (await c.post(f"/video/sessions/{a}/join", headers=TH)).status_code == 200
                  and (await c.post(f"/video/sessions/{a}/join", headers=SH)).status_code == 200)
            r = await c.post(f"/video/sessions/{a}/interaccion", json={"accion": "mano", "arriba": True})
            check(f"sin sesión → rechazado ({r.status_code})", r.status_code in (401, 403))

            print("\n  · Estudiante")
            for acc, extra in (("turno", {"identity": alumna["id"]}), ("foco", {"identity": alumna["id"]}),
                               ("temporizador", {"op": "iniciar", "segundos": 60})):
                r = await I(SH, a, accion=acc, **extra)
                check(f"estudiante NO puede '{acc}' (403)", r.status_code == 403)
            check("estudiante NO puede levantar la mano de otro (403)",
                  (await I(SH, a, accion="mano", arriba=True, identity=prof["id"])).status_code == 403)
            check("estudiante NO puede bajar la mano de otro (403)",
                  (await I(SH, a, accion="mano", arriba=False, identity=prof["id"])).status_code == 403)

            print("\n  · Profesor / otros")
            check("profesor NO puede LEVANTAR la mano de otra persona (403)",
                  (await I(TH, a, accion="mano", arriba=True, identity=alumna["id"])).status_code == 403)
            check("otro profesor (no es su clase) → 403",
                  (await I(OH, a, accion="turno", identity=alumna["id"])).status_code == 403)

            print("\n  · Validación")
            check("acción desconocida → 400", (await I(TH, a, accion="grabar")).status_code == 400)
            check("'arriba' debe ser booleano → 400", (await I(SH, a, accion="mano", arriba="si")).status_code == 400)
            for v in (5, 1801, True, "60", None):
                check(f"duración no válida {v!r} → 400",
                      (await I(TH, a, accion="temporizador", op="iniciar", segundos=v)).status_code == 400)
            check("operación de temporizador desconocida → 400",
                  (await I(TH, a, accion="temporizador", op="explotar")).status_code == 400)
            check("identidad demasiado larga → 400", (await I(TH, a, accion="foco", identity="x" * 200)).status_code == 400)
            r = await I(TH, ext, accion="turno", identity=None)
            check("clase externa (Meet) → 400, no toca LiveKit", r.status_code == 400 and "Video Dorismon" in r.text)
            await c.post(f"/teacher/sessions/{fin}/finalize", headers=TH)
            r = await I(SH, fin, accion="mano", arriba=True)
            check("clase finalizada → 400", r.status_code == 400 and "finalizada" in r.text)
        finally:
            n = limpiar_sesiones_locales(creadas)
            print(f"    · limpieza local: {n} sesión(es) de prueba borradas" if n is not None
                  else "    · limpieza local no aplicable")


# ============================================================================ UNIDAD
class _Stub(BaseHTTPRequestHandler):
    modo = "ok"
    llamadas: list = []
    salas: dict = {}

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        cuerpo = json.loads(self.rfile.read(n) or b"{}")
        auth = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        metodo = self.path.rsplit("/", 1)[-1]
        _Stub.llamadas.append({"metodo": metodo, "cuerpo": cuerpo, "auth": auth})
        if _Stub.modo == "fail":
            return self._r(503, {"code": "unavailable"})
        sala = cuerpo.get("room") or (cuerpo.get("names") or [None])[0]
        s = _Stub.salas.setdefault(sala, {"metadata": "", "atributos": {}})
        if metodo == "UpdateParticipant":
            if _Stub.modo == "notfound":
                return self._r(404, {"code": "not_found", "msg": "participant not found"})
            at = s["atributos"].setdefault(cuerpo["identity"], {})
            for k, v in cuerpo.get("attributes", {}).items():
                at.pop(k, None) if v == "" else at.__setitem__(k, v)
            return self._r(200, {"identity": cuerpo["identity"]})
        if metodo == "ListRooms":
            return self._r(200, {"rooms": [{"name": sala, "metadata": s["metadata"]}]})
        if metodo == "UpdateRoomMetadata":
            s["metadata"] = cuerpo.get("metadata", "")
            return self._r(200, {"name": sala})
        return self._r(400, {"code": "bad_route"})

    def _r(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _claims(tok):
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


async def parte_unidad(check):
    print("\n[UNIDAD] servicio de interacción contra un LiveKit simulado local (sin red externa)")
    sys.path.insert(0, str(ROOT))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    puerto = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    previo = {k: os.environ.get(k) for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")}
    os.environ.update({"LIVEKIT_URL": f"ws://127.0.0.1:{puerto}", "LIVEKIT_API_KEY": "APIunidad",
                       "LIVEKIT_API_SECRET": "secreto-de-unidad-0123456789abcdef"})
    try:
        from app.services import livekit_service as lk
        if not hasattr(lk, "poner_mano"):
            check("existe el servicio de interacción (poner_mano / leer_estado_clase / guardar_estado_clase)", False)
            return
        _Stub.modo, _Stub.llamadas, _Stub.salas = "ok", [], {}
        r = await lk.poner_mano("ses-1", "u-maria", True)
        at = _Stub.salas.get("dorismon-ses-1", {}).get("atributos", {}).get("u-maria", {})
        check("subir mano → UpdateParticipant con atributo dorismon.mano (hora del servidor)",
              r == "ok" and _Stub.llamadas[-1]["metodo"] == "UpdateParticipant" and at.get("dorismon.mano", "").isdigit())
        r = await lk.poner_mano("ses-1", "u-maria", False)
        check("bajar mano → el atributo se borra (valor vacío)",
              r == "ok" and _Stub.llamadas[-1]["cuerpo"]["attributes"] == {"dorismon.mano": ""}
              and "dorismon.mano" not in _Stub.salas["dorismon-ses-1"]["atributos"]["u-maria"])
        cl = _claims(_Stub.llamadas[-1]["auth"])
        v = cl.get("video", {})
        check("token de servidor limitado a ESA sala (room, roomAdmin, sin roomJoin ni roomCreate)",
              v.get("room") == "dorismon-ses-1" and v.get("roomAdmin") is True and "roomJoin" not in v
              and "roomCreate" not in v and cl.get("iss") == "APIunidad")
        _Stub.modo = "notfound"
        check("persona no conectada → 'no_esta'", await lk.poner_mano("ses-1", "u-x", True) == "no_esta")
        _Stub.modo = "ok"
        check("sala sin metadatos → estado vacío {}", await lk.leer_estado_clase("ses-2") == {})
        ok = await lk.guardar_estado_clase("ses-2", {"v": 1, "turno": "u-maria", "foco": None, "temporizador": None})
        check("guardar estado → UpdateRoomMetadata (JSON)", ok and json.loads(_Stub.salas["dorismon-ses-2"]["metadata"])["turno"] == "u-maria")
        check("leer estado → lo guardado", (await lk.leer_estado_clase("ses-2")).get("turno") == "u-maria")
        _Stub.salas["dorismon-ses-3"] = {"metadata": "{no es json", "atributos": {}}
        check("metadatos corruptos → estado vacío (no rompe)", await lk.leer_estado_clase("ses-3") == {})
        _Stub.modo = "fail"
        check("LiveKit 503 → mano 'error', leer None, guardar False",
              await lk.poner_mano("ses-1", "u-maria", True) == "error"
              and await lk.leer_estado_clase("ses-1") is None
              and await lk.guardar_estado_clase("ses-1", {}) is False)
        srv.shutdown()
        srv.server_close()
        check("LiveKit caído → 'error' sin excepción", await lk.poner_mano("ses-1", "u-maria", True) == "error")
    finally:
        for k, val in previo.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val


# ============================================================================ ESTÁTICO
def parte_estatica(check):
    # Actualizado en Gate 6G-A: el panel de participantes cambia con autorización; el resto de
    # ClassRoomExtras (mano, heartbeat, ventana flotante) debe seguir idéntico.
    from _base_gate6ga import extras_sin_6ga
    print("\n[ESTÁTICO] Backend")
    video_py = (ROOT / "app/routers/video.py").read_text(encoding="utf-8")
    # Actualizado en Gate 6H-A: se deshacen EXACTAMENTE sus bloques (grabación, autorizada después)
    from _base_gate6ha import sin_gate6ha
    video_py = sin_gate6ha("app/routers/video.py", video_py)
    lk_py = (ROOT / "app/services/livekit_service.py").read_text(encoding="utf-8")
    i = video_py.find("async def interaccion_clase")
    ep = video_py[i:]
    check("endpoint POST /video/sessions/{id}/interaccion", '@router.post("/sessions/{session_id}/interaccion")' in video_py and i > 0)
    check("mismos permisos que el resto de la sala (moderador = profesor de la clase o Dirección; estudiante = puede_acceder_a_clase)",
          "es_moderador = (s.teacher_id == user.user_id) or (me.role == UserRole.super_admin)" in ep
          and "puede_acceder_a_clase(db, user.user_id, s)" in ep)
    check("rechaza clases canceladas / finalizadas / presenciales / externas",
          all(x in ep for x in ("SessionStatus.cancelled", "SessionStatus.completed", "tiene_entrada_online(s)",
                                 's.video_provider != "dorismon"')))
    check("valida todo ANTES de hablar con LiveKit",
          ep.find("Duración no válida") < ep.find("await leer_estado_clase(") and ep.find("Operación de temporizador no válida") < ep.find("await leer_estado_clase("))
    check("nada en base de datos (sin add/commit/delete en el endpoint)",
          not re.search(r"db\.(add|commit|delete|execute|flush)", ep))
    check("sin notificaciones ni asistencia en el endpoint", not re.search(r"notif|VideoPresence|Attendance", ep))
    # Actualizado en Gate 6G-C: la sección 6F-A termina donde empieza la siguiente (6G-B / 6G-C)
    _i6fa = lk_py.find("# GATE 6F-A")
    _sig = [j for j in (lk_py.find("# GATE 6G-B", _i6fa), lk_py.find("# GATE 6G-C", _i6fa)) if j > 0]
    sec = lk_py[_i6fa:min(_sig) if _sig else None]
    check("servicio 6F-A solo usa UpdateParticipant / ListRooms / UpdateRoomMetadata",
          set(re.findall(r'"(\w+)", \{', sec)) | set(re.findall(r'_sala\(session_id, "(\w+)"', sec))
          <= {"UpdateParticipant", "ListRooms", "UpdateRoomMetadata"} and "_sala(session_id," in sec)
    check("no se amplían permisos LiveKit (sin canUpdateOwnMetadata; token del navegador igual)",
          '"canUpdateOwnMetadata"' not in lk_py and '"canPublishData": True,' in lk_py
          # Actualizado en Gate 6G-B: sin roomAdmin en el navegador y TTL corto
          and "roomAdmin\": bool(is_moderator)" not in lk_py and "ttl_seconds: int = 30 * 60," in lk_py)
    i6e = lk_py.find("# GATE 6E")
    check("bloque de 6E (DeleteRoom) intacto antes del de 6F-A",
          0 < i6e < lk_py.find("# GATE 6F-A") and "async def delete_room(session_id: str, intentos: int = 2) -> str:" in lk_py)

    print("\n[ESTÁTICO] Frontend")
    cp_p, ix_p = WEB / "components/CallProvider.tsx", WEB / "components/InteraccionClase.tsx"
    if not (cp_p.exists() and ix_p.exists()):
        check(f"existen CallProvider.tsx e InteraccionClase.tsx en {WEB}", False)
        return
    cp = cp_p.read_text(encoding="utf-8").replace("\r\n", "\n")
    cpc = codigo(cp)
    ix = ix_p.read_text(encoding="utf-8").replace("\r\n", "\n")
    ixc = codigo(ix)
    css = (WEB / "app/globals.css").read_text(encoding="utf-8")
    extras = (WEB / "components/ClassRoomExtras.tsx").read_bytes()

    print("\n  · Mano")
    check("mano = atributo 'dorismon.mano' que escribe el servidor (no mensajes de datos)",
          'export const ATRIBUTO_MANO = "dorismon.mano";' in ix and 'accion: "mano", arriba' in ixc
          and "useRaisedHands" not in cpc)
    check("al entrar / reconectar se lee lo que trae la sala (quien entra tarde)",
          ".on(RoomEvent.Connected, releer)" in ix and ".on(RoomEvent.Reconnected, releer)" in ix
          and ".on(RoomEvent.ParticipantAttributesChanged, refrescar)" in ix)
    check("orden por momento en que se levantó (valor = hora del servidor)",
          "m[p.identity] = Number(v) || 1;" in ix)
    check("el profesor baja la mano de otro por el servidor (también en la pantalla del estudiante)",
          'void enviar({ accion: "mano", arriba: false, identity });' in ix)
    check("ClassRoomExtras sin cambios (el panel de participantes reutiliza bajarMano)",
          extras_sin_6ga(extras))

    print("\n  · Reacciones")
    check("5 reacciones simples", 'export const REACCIONES = ["👍", "👏", "❤️", "😂", "🎉"] as const;' in ix)
    check("canal de datos de LiveKit con tema propio", 'export const TEMA_REACCION = "dorismon-reaccion";' in ix
          and "{ reliable: true, topic: TEMA_REACCION }" in ix and "if (tema !== TEMA_REACCION || !participante) return;" in ix)
    check("nombre del REMITENTE que certifica LiveKit (no del contenido) y solo emojis permitidos",
          "mostrar(e, participante.name || \"Participante\", participante.identity);" in ix
          and "if (!(REACCIONES as readonly string[]).includes(e)) return;" in ix)
    check("desaparecen solas y con límite (3 por persona cada 3 s, máx. en pantalla)",
          "DURACION_REACCION_MS = 4000" in ix and "if (previas.length >= 3) return;" in ix and "MAX_REACCIONES_EN_PANTALLA" in ix)
    r0, r1 = ix.find("export function useReacciones"), ix.find("/* ------------------------------------------------------------ temporizador")
    reac = ix[r0:r1]
    check("reacciones sin backend, sin almacenamiento y sin notificaciones",
          "api(" not in reac and "Storage" not in reac and "notif" not in reac.lower())

    print("\n  · Turno / temporizador / foco")
    check("turno, temporizador y foco = metadatos de la sala (estado actual)",
          ".on(RoomEvent.RoomMetadataChanged, alCambiarSala)" in ix and "leerEstadoClase(room.metadata)" in ix)
    check("«Tu turno para hablar» sin tocar micrófono ni permisos",
          "Tu turno para hablar" in ix and not re.search(r"setMicrophoneEnabled|setPermissions|getUserMedia", ixc))
    check("temporizador: inicio + duración, cada cliente calcula (sin paquetes por segundo)",
          "export function restanteMs(" in ix and ixc.count("publishData") == 1
          and "setInterval(() => tic((x) => x + 1), 250)" in ix)
    check("no se corrige el reloj con metadatos viejos", "desfase" not in ixc)
    check("foco del profesor DENTRO de <VideoConference /> (SettingsComponent), sin reemplazarlo",
          cpc.count("<VideoConference SettingsComponent={AplicarFoco} />") == 2 and cpc.count("<VideoConference") == 2
          and "<FocoContexto.Provider value={ix.foco}>" in cp and 'layout.pin.dispatch?.({ msg: "set_pin", trackReference: ref });' in ix)
    check("foco: pantalla compartida primero; fijar local respetado; liberar suelta solo lo suyo",
          "if (hayPantalla || !ref) { aplicado.current = null; return; }" in ix
          and "if (aplicado.current === foco) return;" in ix and 'layout.pin.dispatch?.({ msg: "clear_pin" });' in ix)
    check("botón 'Settings' de LiveKit oculto", ".ds-lk-stage .lk-settings-toggle { display: none !important; }" in css)
    check("herramientas (temporizador, turno, foco) solo para el moderador, en el menú",
          "{datos.is_moderator && (\n                    <button\n                      onClick={() => { setMenu(false); setPanel(false); setHerramientas(true); }}" in cp
          and "{herramientas && datos.is_moderator && (" in cp)

    print("\n  · Móvil / mini (6D)")
    m0 = cp.find("if (minimizada) {")
    mini = cp[m0:cp.find("// -------------------- Pantalla completa", m0)]
    check("mini: temporizador y turno compactos, SIN reacciones", "<BarraClase ix={ix} compacto />" in mini and "CapaReacciones" not in mini)
    check("reacciones no captan toques (no tapan controles)", 'className="pointer-events-none absolute' in ix)
    check("menú de reacciones siempre dentro de la pantalla", "Math.min(Math.max(8, r.right - ANCHO_MENU), window.innerWidth - ANCHO_MENU - 8)" in ix)
    check("aviso de manos dentro de la zona de video (ya no tapa la cabecera)",
          'className="absolute top-2 left-1/2 -translate-x-1/2 z-20' in cp and "top-16" not in cp)

    print("\n  · Gates anteriores intactos")
    check("6B: motivos de desconexión y salida voluntaria", "function clasificarDesconexion" in cp and "marcarSalida();  // GATE 6B" in cp)
    check("6C: la sala aplica la elección de la pantalla previa",
          "        connect\n        video={opcionVideo}\n        audio={opcionAudio}\n" in cp)
    check("6D: mini encajada", "function encajarMini(" in cp)
    check("6E: finalizar para todos y clase activa",
          "Esto finalizará la clase y desconectará a todos los participantes." in cp
          and 'const CLAVE_CLASE_ACTIVA = "dorismon_clase_activa_v1";' in cp)
    check("un solo renderizador de audio (el de VideoConference)", "<RoomAudioRenderer" not in cpc)
    check("sin almacenamiento nuevo (localStorage solo la clase activa)",
          "localStorage" not in ix and "sessionStorage" not in ix)

    print("\n  · Fuera de alcance")
    # Actualizado en Gate 6H-A: la grabación se autorizó después; el resto sigue fuera de alcance
    from _base_gate6ha import sin_gate6ha
    todo = sin_gate6ha("components/CallProvider.tsx", cp) + sin_gate6ha("components/InteraccionClase.tsx", ix)
    check("sin grabación, transcripción, subtítulos, pizarra ni breakouts",
          not re.search(r"Egress|egress|grabaci|transcrip|subt[ií]tul|pizarra|whiteboard|breakout", codigo(todo), re.I))
    check("enlaces externos sin cambios", cp.count("datos.fallback_url") == 4)


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    await parte_api(check)
    await parte_unidad(check)
    parte_estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
