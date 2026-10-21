"""GATE 6E — Finalizar para todos + recuperación tras recarga.

[API] (contra el servidor local)
  · finalizar: mismos permisos (profesor de la clase o Dirección; estudiante y
    otro profesor → 403); "completed"; /join y heartbeat rechazados después;
    heartbeat permitido ANTES; doble finalizar no rompe nada; respuesta con
    video_room (cerrada / no_existia / error / no_aplica); clase externa
    (Meet/Zoom/Teams) → "no_aplica", sin tocar LiveKit.
[UNIDAD] delete_room contra un stub LOCAL levantado por este mismo test:
    200 → "cerrada"; 404 not_found → "no_existia" (idempotente); 503 → reintento
    breve y "error"; 401 → "error" sin reintentar; servidor caído → "error";
    token de servidor con roomCreate (nunca el del navegador).
[ESTÁTICO] frontend: acción "Finalizar clase para todos" (confirmación, sin
    doble envío, solo moderador); registro dorismon_clase_activa_v1 sin
    tokens, guardado solo al conectar, borrado solo ante hechos definitivos;
    aviso "Estabas en una clase." que NO llama a /join solo; Volver → /join
    nuevo → pantalla previa; split no vuelve en móvil.

Uso: python tests/test_gate6e.py [BASE_URL] [RUTA_DORISMON_WEB]
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
VIDEO_ROOM_VALIDOS = {"cerrada", "no_existia", "error"}


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
    print("\n[API] Finalizar para todos: permisos, completed, /join y heartbeat")
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

        a = await evento("G6E dorismon")
        d = await evento("G6E Dirección")
        ext = await evento("G6E externa", video_provider="meet", meeting_url="https://meet.google.com/abc-defg-hij")
        check("se crean eventos (Video Dorismon y externo)", all((a, d, ext)))
        if not all((a, d, ext)):
            return
        try:
            j_t = await c.post(f"/video/sessions/{a}/join", headers=TH)
            j_s = await c.post(f"/video/sessions/{a}/join", headers=SH)
            check("antes: profesor y estudiante entran (200)", j_t.status_code == 200 and j_s.status_code == 200)
            hb0 = await c.post(f"/video/sessions/{a}/heartbeat", headers=SH)
            check("heartbeat ANTES de finalizar → 200", hb0.status_code == 200)

            r_est = await c.post(f"/teacher/sessions/{a}/finalize", headers=SH)
            check("estudiante NO puede finalizar (403)", r_est.status_code == 403)
            r_otro = await c.post(f"/teacher/sessions/{a}/finalize", headers=OH)
            check("otro profesor (no es su clase) NO puede finalizar (403)", r_otro.status_code == 403)
            check("tras los intentos rechazados la clase sigue abierta (/join 200)",
                  (await c.post(f"/video/sessions/{a}/join", headers=SH)).status_code == 200)

            fin = await c.post(f"/teacher/sessions/{a}/finalize", headers=TH)
            jf = fin.json() if fin.status_code == 200 else {}
            check("profesor de la clase finaliza (200) con los campos de siempre",
                  fin.status_code == 200 and {"ok", "attendance_taken", "attendance_count"} <= set(jf))
            check(f"respuesta informa la sala de video (video_room = {jf.get('video_room')})",
                  jf.get("video_room") in VIDEO_ROOM_VALIDOS)
            rj = await c.post(f"/video/sessions/{a}/join", headers=SH)
            check("después: /join del estudiante rechazado (400 'finalizada')", rj.status_code == 400 and "finalizada" in rj.text)
            rjt = await c.post(f"/video/sessions/{a}/join", headers=TH)
            check("después: /join del profesor rechazado (400)", rjt.status_code == 400)
            hb1 = await c.post(f"/video/sessions/{a}/heartbeat", headers=SH)
            check("heartbeat DESPUÉS de finalizar → rechazado (400 'finalizada')", hb1.status_code == 400 and "finalizada" in hb1.text)
            fin2 = await c.post(f"/teacher/sessions/{a}/finalize", headers=TH)
            check("doble finalizar no rompe nada (200, sigue finalizada)",
                  fin2.status_code == 200 and (await c.post(f"/video/sessions/{a}/join", headers=SH)).status_code == 400)

            fd = await c.post(f"/teacher/sessions/{d}/finalize", headers=AH)
            check("Dirección puede finalizar (permiso existente)", fd.status_code == 200 and fd.json().get("video_room") in VIDEO_ROOM_VALIDOS)

            fe = await c.post(f"/teacher/sessions/{ext}/finalize", headers=TH)
            check("clase externa (Meet): finaliza igual y NO toca LiveKit (video_room = no_aplica)",
                  fe.status_code == 200 and fe.json().get("video_room") == "no_aplica")
        finally:
            n = limpiar_sesiones_locales(creadas)
            print(f"    · limpieza local: {n} sesión(es) de prueba borradas" if n is not None
                  else "    · limpieza local no aplicable")


# ============================================================================ UNIDAD
class _Stub(BaseHTTPRequestHandler):
    modo = "ok"
    llamadas: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        cuerpo = json.loads(self.rfile.read(n) or b"{}")
        auth = (self.headers.get("Authorization") or "").replace("Bearer ", "")
        _Stub.llamadas.append({"ruta": self.path, "sala": cuerpo.get("room"), "auth": auth})
        codigos = {"ok": (200, {}), "notfound": (404, {"code": "not_found", "msg": "room not found"}),
                   "fail": (503, {"code": "unavailable"}), "denied": (401, {"code": "unauthenticated"})}
        code, obj = codigos[_Stub.modo]
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
    print("\n[UNIDAD] delete_room contra un LiveKit simulado local (sin red externa)")
    sys.path.insert(0, str(ROOT))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    puerto = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    previo = {k: os.environ.get(k) for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")}
    os.environ.update({"LIVEKIT_URL": f"ws://127.0.0.1:{puerto}", "LIVEKIT_API_KEY": "APIunidad",
                       "LIVEKIT_API_SECRET": "secreto-de-unidad-0123456789abcdef"})
    try:
        from app.services import livekit_service as lk

        async def probar(modo):
            _Stub.modo = modo
            _Stub.llamadas = []
            return await lk.delete_room("ses-123"), list(_Stub.llamadas)

        r, ll = await probar("ok")
        check("200 → 'cerrada' (DeleteRoom de la sala dorismon-<id>)",
              r == "cerrada" and len(ll) == 1 and ll[0]["ruta"].endswith("/twirp/livekit.RoomService/DeleteRoom")
              and ll[0]["sala"] == "dorismon-ses-123")
        cl = _claims(ll[0]["auth"]) if ll else {}
        check("token de SERVIDOR con roomCreate (no el del navegador: sin roomJoin)",
              cl.get("video", {}).get("roomCreate") is True and "roomJoin" not in cl.get("video", {}) and cl.get("iss") == "APIunidad")
        r, ll = await probar("notfound")
        check("404 not_found → 'no_existia' (idempotente, sin reintentos)", r == "no_existia" and len(ll) == 1)
        r, ll = await probar("fail")
        check("503 → un reintento breve y 'error' (no se finge el cierre)", r == "error" and len(ll) == 2)
        r, ll = await probar("denied")
        check("401 → 'error' sin reintentar", r == "error" and len(ll) == 1)
        srv.shutdown()
        srv.server_close()
        r = await lk.delete_room("ses-123")
        check("LiveKit caído → 'error'", r == "error")
    finally:
        for k, v in previo.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ============================================================================ ESTÁTICO
def parte_estatica(check):
    print("\n[ESTÁTICO] Backend y frontend")
    teacher_py = (ROOT / "app/routers/teacher.py").read_text(encoding="utf-8")
    video_py = (ROOT / "app/routers/video.py").read_text(encoding="utf-8")
    i = teacher_py.find("async def finalize_session")
    fin = teacher_py[i:teacher_py.find("@router.", i + 10)]
    check("finalizar: mismos permisos (require_teacher_or_admin + dueño de la clase)",
          "Depends(require_teacher_or_admin)" in fin and 'if teacher.role == "teacher" and session.teacher_id != teacher.user_id:' in fin)
    check("finalizar: completed + auditoría de Gate 0 ANTES de cerrar la sala",
          fin.find("session.status = SessionStatus.completed") < fin.find('"finalize_session"') < fin.find("await db.commit()")
          < fin.find("await delete_room(session_id)"))
    check("solo Video Dorismon cierra sala; externas → no_aplica",
          'if session.video_provider == "dorismon" and tiene_entrada_online(session) and livekit_ready():' in fin
          and 'video_room = "no_aplica"' in fin)
    hb = video_py[video_py.find("async def heartbeat"):]
    check("heartbeat rechaza clases finalizadas",
          "if s.status == SessionStatus.completed:\n        raise HTTPException(400, \"Esta clase ya fue finalizada\")" in hb)
    check("el cálculo de asistencia del heartbeat no cambió", "if 30 <= delta <= 180:" in hb and "row.minutes = (row.minutes or 0) + int(round(delta / 60))" in hb)

    cp_p = WEB / "components/CallProvider.tsx"
    tp_p = WEB / "app/dashboard/teacher/page.tsx"
    if not (cp_p.exists() and tp_p.exists()):
        check(f"existen los archivos del frontend en {WEB}", False)
        return
    cp = cp_p.read_text(encoding="utf-8").replace("\r\n", "\n")
    cpc = codigo(cp)
    tp = tp_p.read_text(encoding="utf-8").replace("\r\n", "\n")

    print("\n  · Finalizar para todos (UI)")
    check("acción solo para moderador (profesor de la clase / Dirección)",
          "{datos.is_moderator && (\n                    <button" in cp and "Finalizar clase para todos" in cp)
    check("confirmación explícita con el texto pedido",
          "Esto finalizará la clase y desconectará a todos los participantes." in cp and "Sí, finalizar para todos" in cp)
    check("sin doble envío (ref + botón deshabilitado)", "if (enviandoFin.current) return;" in cp and "disabled={finalizando}" in cp)
    check("usa el endpoint de finalizar existente (el backend decide)", "api(`/teacher/sessions/${sessionId}/finalize`, { method: \"POST\", auth: true })" in cp)
    check("si LiveKit no cerró la sala se dice (no se finge)",
          "pero hubo un problema cerrando la sala de video" in cp and "pero hubo un problema cerrando la sala de video" in tp)
    check("Salir sigue separado (solo sale esa persona)", "onClick={colgar}" in cp and "marcarSalida();  // GATE 6B" in cp)
    check("participantes: ROOM_DELETED/ROOM_CLOSED → «La clase ha finalizado.» sin reentrada (6B)",
          re.search(r'tipo: "finalizada", titulo: "La clase ha finalizado\."[^}]*recuperable: false', cp) is not None)

    print("\n  · Clase activa (recuperación)")
    check("clave versionada dorismon_clase_activa_v1 (no reutiliza dorismon_abrir_clase)",
          'const CLAVE_CLASE_ACTIVA = "dorismon_clase_activa_v1";' in cp)
    i_t = cp.find("type ClaseActiva = {")
    tipo = cp[i_t:cp.find("};", i_t)]
    check("sin tokens ni secretos en el registro", not re.search(r"token|jwt|secret|url", tipo, re.I))
    check("se guarda solo al conectar de verdad (RoomEvent.Connected)",
          "room.on(RoomEvent.Connected, guardar);" in cp and "if (room.state === ConnectionState.Connected) guardar();" in cp)
    check("usuario real (identidad de LiveKit = id del usuario)", "const user_id = room.localParticipant?.identity;" in cp)
    check("vence con la ventana de la clase (fin + margen de /join) o 3 h",
          "f.getTime() + VENTANA_MIN_GRACIA * 60_000 : Date.now() + 3 * 60 * 60_000" in cp)
    check("se borra al salir voluntariamente", "const salir = useCallback(() => { borrarClaseActiva(); setLlamada(null); }, []);" in cp)
    check("se borra ante finalizada / retirado / rechazado",
          '["finalizada", "retirado", "rechazado"].includes(terminal.tipo)) borrarClaseActiva(sessionId);' in cp)
    check("se borra si /join rechaza (400/403/404), no por fallo de red",
          "if ([400, 403, 404].includes(e?.status)) borrarClaseActiva(sessionId);" in cp)
    i_b = cp.find("// Avisar antes de cerrar la pestaña si hay una clase en curso")
    efecto_bu = cp[i_b:cp.find("}, [llamada]);", i_b)] if i_b > 0 else ""
    check("NO se borra por recargar / beforeunload",
          'addEventListener("beforeunload"' in efecto_bu and "borrarClaseActiva" not in efecto_bu)
    check("vencida u otro usuario → se borra",
          "if (Date.now() > c.expira) { borrarClaseActiva();" in cp and "me.id !== c.user_id) { borrarClaseActiva();" in cp)
    check("aviso «Estabas en una clase.» con Volver / Descartar", "Estabas en una clase." in cp and "Volver a la clase" in cp and "Descartar" in cp)
    i_r = cp.find("const [recuperable, setRecuperable]")
    efecto = cp[i_r:cp.find("const volverALaClase", i_r)]
    check("el aviso NO llama a /join para comprobar (solo auth.me)", "join" not in efecto and "auth.me()" in efecto)
    check("Volver → entrar (/join NUEVO) → pantalla previa", "entrar(c.session_id, { fin: c.fin ?? null }, c.modo);" in cp
          and ") : !eleccion ? (\n        <PreEntrada" in cp)
    check("modo anterior solo si es válido (split no en móvil)",
          'if (m === "split" && typeof window !== "undefined" && window.innerWidth < 768) return "full";' in cp)
    check("el panel del profesor también borra la clase activa al finalizar", "borrarClaseActiva(sessionId);  // GATE 6E" in tp)

    print("\n  · Fuera de alcance")
    check("6A/6B/6C/6D intactos (opciones, motivos, pantalla previa, mini)",
          "const OPCIONES_SALA: RoomOptions = { adaptiveStream: true, dynacast: true };" in cp
          and "function clasificarDesconexion" in cp and "function encajarMini(" in cp)
    check("sin <RoomAudioRenderer /> propio", "<RoomAudioRenderer" not in cpc)
    # Actualizado en Gate 6G-B: el TTL cambió con autorización (30 min, acotado al fin de la clase)
    check("TTL según Gate 6G-B", "ttl_seconds: int = 30 * 60," in (ROOT / "app/services/livekit_service.py").read_text(encoding="utf-8"))


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
