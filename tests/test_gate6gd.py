"""GATE 6G-D — Protección contra abuso, spam, concurrencia y rate limiting (Video Dorismon).

[UNIDAD] LimitadorEnMemoria con reloj inyectado: ráfaga y recuperación (token bucket),
    claves independientes, ventana que vuelve a permitir, concurrencia con hilos (el
    contador no se salta, sin bloqueos mutuos), memoria acotada (miles de claves viejas
    se borran; tope duro).
[INTEGRACIÓN] App EN ESTE PROCESO (su propio limitador; reloj controlado) contra un
    LiveKit simulado local:
    · uso normal completo SIN 429 (entrar, renovar desde la pantalla previa, heartbeat,
      hora del temporizador, mano / turno / temporizador / foco, moderación, permisos,
      finalizar);
    · abuso → 429 con mensaje en español y Retry-After (join, hora, interacción,
      moderación, permisos); el retirado (6G-A) sigue viendo su motivo real;
    · separación (otra persona, otra clase, misma IP) y ventana que expira;
    · Finalizar: 10 peticiones simultáneas → una sola auditoría, una sola revocación y un
      solo DeleteRoom; respuestas coherentes; reintento solo si el cierre anterior falló.
[ESTÁTICO] orden de las comprobaciones, claves sin IP, frontend discreto con el 429.

Uso: python tests/test_gate6gd.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import base64
import datetime as dt
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
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}
MARIA = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
CARLOS = {"email": "carlos.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
JUANA = {"email": "juana.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
DB_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dorismon.db")
MSG = "Has realizado demasiadas acciones en poco tiempo"


def claims(tok):
    try:
        p = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}


# ============================================================================ UNIDAD
def parte_unidad(check):
    print("\n[UNIDAD] Limitador (token bucket) con reloj inyectado")
    sys.path.insert(0, str(ROOT))
    try:
        from app.services.limites_video import LimitadorEnMemoria, Limite
    except Exception as e:
        check(f"existe app/services/limites_video.py ({type(e).__name__})", False)
        return
    t = [1000.0]
    L = LimitadorEnMemoria(reloj=lambda: t[0])
    lim = Limite("x", 5, 1)          # 5 seguidas, 1 por segundo
    r = [L.consumir("a", lim) for _ in range(6)]
    check(f"ráfaga de 5 permitida, la 6.ª espera ~1 s ({[round(x, 2) for x in r]})", r[:5] == [0] * 5 and 0.9 <= r[5] <= 1.0)
    check("otra clave no se ve afectada", L.consumir("b", lim) == 0)
    t[0] += 1.0
    check("tras la espera indicada vuelve a permitir", L.consumir("a", lim) == 0)
    t[0] += 10
    check("la cubeta se recupera hasta su capacidad (no más)",
          [L.consumir("a", lim) for _ in range(6)].count(0) == 5)

    # concurrencia: 40 hilos x 10 intentos sobre la misma clave con capacidad 100 → exactamente 100
    t2 = [0.0]
    L2 = LimitadorEnMemoria(reloj=lambda: t2[0])
    lim2 = Limite("c", 100, 0.000001)
    permitidos = []
    cerrojo = threading.Lock()

    def golpear():
        n = sum(1 for _ in range(10) if L2.consumir("misma", lim2) == 0)
        with cerrojo:
            permitidos.append(n)
    hilos = [threading.Thread(target=golpear) for _ in range(40)]
    [h.start() for h in hilos]
    [h.join(timeout=10) for h in hilos]
    check(f"400 intentos simultáneos sobre capacidad 100 → exactamente 100 permitidos ({sum(permitidos)})",
          sum(permitidos) == 100 and not any(h.is_alive() for h in hilos))

    # memoria: 20 000 claves viejas → se borran al recuperarse; tope duro
    t3 = [0.0]
    L3 = LimitadorEnMemoria(reloj=lambda: t3[0], max_claves=50_000, purgar_cada=512)
    lim3 = Limite("m", 3, 1)
    for i in range(20_000):
        L3.consumir(f"vieja-{i}", lim3)
    antes = L3.tamanio()
    t3[0] += 10
    L3.purgar()
    check(f"20 000 claves viejas → se eliminan al recuperarse (antes {antes}, después {L3.tamanio()})",
          antes == 20_000 and L3.tamanio() == 0)
    for i in range(3_000):
        L3.consumir(f"nueva-{i}", lim3)
    t3[0] += 61   # pasado el minuto, cada franja se limpia sola en su próximo uso
    L3.consumir("una-mas", lim3)
    for i in range(600):
        L3.consumir(f"otra-{i}", lim3)
    check(f"limpieza perezosa sin llamar a purgar() (quedan {L3.tamanio()} < 3 601)", L3.tamanio() < 3_601)
    L4 = LimitadorEnMemoria(reloj=lambda: 0.0, max_claves=1_024, franjas=16)
    for i in range(10_000):
        L4.consumir(f"k{i}", Limite("t", 3, 0.0001))
    check(f"tope duro: 10 000 claves activas con tope 1 024 → {L4.tamanio()} (acotado)", L4.tamanio() <= 1_024)


# ============================================================================ INTEGRACIÓN
class _LK(BaseHTTPRequestHandler):
    llamadas: list = []
    permisos: dict = {}
    fallar: set = set()

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        cuerpo = json.loads(self.rfile.read(n) or b"{}")
        metodo = self.path.rsplit("/", 1)[-1]
        _LK.llamadas.append({"metodo": metodo, "cuerpo": cuerpo})
        if metodo in _LK.fallar:
            return self._r(503, {"code": "unavailable"})
        clave = (cuerpo.get("room"), cuerpo.get("identity"))
        if metodo == "GetParticipant":
            # Forma REAL de LiveKit (PREPROD-1): snake_case y todos los campos
            return self._r(200, {"permission": _LK.permisos.get(clave) or {
                "can_subscribe": True, "can_publish": True, "can_publish_data": True,
                "can_publish_sources": ["CAMERA", "MICROPHONE"], "hidden": False, "recorder": False,
                "can_update_metadata": False, "agent": False, "can_subscribe_metrics": False,
                "can_manage_agent_session": False}})
        if metodo == "UpdateParticipant":
            if "permission" in cuerpo:
                _LK.permisos[clave] = cuerpo["permission"]
            return self._r(200, {})
        if metodo == "ListParticipants":
            return self._r(200, {"participants": [{"identity": cuerpo.get("room", "")[-6:], "tracks": [{"sid": "T", "type": "AUDIO"}]}]})
        if metodo == "ListRooms":
            return self._r(200, {"rooms": [{"name": (cuerpo.get("names") or [""])[0], "metadata": ""}]})
        if metodo == "RemoveParticipant":
            return self._r(404, {"code": "not_found"})
        return self._r(200, {})

    def _r(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def limpiar(ids):
    import sqlite3
    if not ids or not DB_URL.startswith("sqlite"):
        return None
    ruta = Path(DB_URL.split("///", 1)[-1])
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
        cx.execute(f"delete from audit_logs where module in ('video','teacher') and target_id in ({marcas})", ids)
        n = cx.execute(f"delete from class_sessions where id in ({marcas})", ids).rowcount
        cx.commit()
        return n
    finally:
        cx.close()


def auditorias(session_id, accion):
    import sqlite3
    ruta = Path(DB_URL.split("///", 1)[-1])
    cx = sqlite3.connect(str(ruta), timeout=30)
    try:
        return cx.execute("select count(*) from audit_logs where action=? and target_id=?", (accion, session_id)).fetchone()[0]
    finally:
        cx.close()


async def parte_integracion(check):
    print("\n[INTEGRACIÓN] App en proceso + LiveKit simulado (límites y concurrencia)")
    sys.path.insert(0, str(ROOT))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _LK)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ.update({"LIVEKIT_URL": f"ws://127.0.0.1:{srv.server_address[1]}",
                       "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY") or "APIprueba",
                       "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET") or "secretodepruebaparatest123456789"})
    try:
        from app.main import app
        from app.core.db import engine
        from app.models import Base
        import app.services.limites_video as lv
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        check(f"la app (con límites) se importa en proceso ({type(e).__name__}: {e})", False)
        srv.shutdown()
        return
    reloj = [10_000.0]
    lv.LIMITADOR.reloj = lambda: reloj[0]
    creadas = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(cred):
            return {"Authorization": f"Bearer {(await c.post('/auth/login', json=cred)).json()['access_token']}"}
        AH, TH, MH, CH, JH = [await login(x) for x in (ADMIN, TEACHER, MARIA, CARLOS, JUANA)]
        ids = {k: (await c.get("/auth/me", headers=h)).json()["id"]
               for k, h in (("admin", AH), ("prof", TH), ("maria", MH), ("carlos", CH), ("juana", JH))}
        now = dt.datetime.now(dt.timezone.utc)

        async def evento(nombre):
            r = await c.post("/admin/events", headers=AH, json={
                "title": nombre, "teacher_id": ids["prof"], "modality": "online", "video_provider": "dorismon",
                "starts_at_utc": (now - dt.timedelta(minutes=5)).isoformat(),
                "ends_at_utc": (now + dt.timedelta(minutes=55)).isoformat()})
            i = r.json().get("id") if r.status_code in (200, 201) else None
            if i:
                creadas.append(i)
            return i

        a, b, fin, fin2 = [await evento(f"G6GD {n}") for n in ("A", "B", "finalizar", "finalizar-error")]
        check("se crean las clases de prueba", all((a, b, fin, fin2)))
        try:
            J = lambda h, sid: c.post(f"/video/sessions/{sid}/join", headers=h)
            I = lambda h, sid, **x: c.post(f"/video/sessions/{sid}/interaccion", headers=h, json=x)
            M = lambda h, sid, accion, quien: c.post(f"/video/sessions/{sid}/moderate", headers=h,
                                                     json={"action": accion, "identity": ids[quien]})
            codigos = []

            print("\n  · Uso normal de una clase completa: ningún 429")
            for h in (TH, MH, CH):
                codigos.append((await J(h, a)).status_code)
            codigos.append((await J(MH, a)).status_code)                       # renovación desde la pantalla previa (6G-B)
            for _ in range(3):
                codigos += [(await c.post(f"/video/sessions/{a}/heartbeat", headers=h)).status_code for h in (MH, CH)]
                reloj[0] += 60
            for _ in range(4):                                                  # 4 sincronizaciones del temporizador (6F-A.1)
                codigos += [(await c.get("/video/hora", headers=MH)).status_code for _ in range(3)]
            codigos += [(await I(MH, a, accion="mano", arriba=x)).status_code for x in (True, False, True)]
            codigos += [(await I(TH, a, accion=ac, **kw)).status_code for ac, kw in (
                ("turno", {"identity": ids["maria"]}), ("temporizador", {"op": "iniciar", "segundos": 60}),
                ("foco", {"identity": ids["carlos"]}), ("temporizador", {"op": "pausar"}), ("foco", {"identity": None}),
                ("mano", {"arriba": False, "identity": ids["maria"]}))]
            codigos += [(await M(TH, a, ac, q)).status_code for ac, q in (
                ("mute", "maria"), ("permitir_pantalla", "maria"), ("bloquear_microfono", "carlos"),
                ("permitir_microfono", "carlos"), ("quitar_pantalla", "maria"), ("remove", "carlos"), ("allow", "carlos"))]
            codigos.append((await c.get(f"/video/sessions/{a}/bloqueos", headers=TH)).status_code)
            codigos.append((await J(CH, a)).status_code)                       # vuelve tras "Permitir volver"
            check(f"uso normal sin 429 ({len(codigos)} peticiones; códigos {sorted(set(codigos))})",
                  429 not in codigos and all(x in (200, 400) for x in codigos))
            check("el 400 que aparece es el de 'sacar' con LiveKit sin esa persona (no un límite)",
                  codigos.count(400) <= 1)

            print("\n  · Abuso → 429 claro (Retry-After, sin detalles internos)")
            rs = [await J(JH, a) for _ in range(20)]
            cods = [r.status_code for r in rs]
            r429 = next((r for r in rs if r.status_code == 429), None)
            check(f"/join repetido → 429 tras la ráfaga permitida (ok={cods.count(200)}, 429={cods.count(429)})",
                  cods[:12] == [200] * 12 and r429 is not None)
            check("429 con mensaje en español, Retry-After y sin trazas",
                  r429 is not None and MSG in r429.text and r429.headers.get("retry-after", "").isdigit()
                  and "Traceback" not in r429.text and "token" not in r429.text.lower())
            check("…y ese 429 NO emite token", r429 is not None and "token" not in r429.json())
            cods = [(await c.get("/video/hora", headers=JH)).status_code for _ in range(70)]
            check(f"/video/hora excesivo → 429 (ok={cods.count(200)}, 429={cods.count(429)})", cods[:60] == [200] * 60 and 429 in cods)
            cods = [(await I(JH, a, accion="mano", arriba=bool(i % 2))).status_code for i in range(16)]
            check(f"interacción del estudiante excesiva → 429 (ok={cods.count(200)})", cods[:10] == [200] * 10 and 429 in cods)
            cods = [(await M(AH, b, "mute", "maria")).status_code for _ in range(45)]
            check(f"moderación excesiva → 429 (no-429={45 - cods.count(429)}, 429={cods.count(429)})",
                  cods.count(429) >= 4 and 429 not in cods[:40])
            cods = [(await M(AH, a, "permitir_camara", "maria")).status_code for _ in range(45)]
            check(f"permisos excesivos → 429 (ok={cods.count(200)}, 429={cods.count(429)})",
                  cods[:40] == [200] * 40 and 429 in cods)

            print("\n  · Separación (persona, clase, misma IP) y ventana")
            check("otra persona (misma IP) en la misma clase NO queda limitada",
                  (await J(CH, a)).status_code == 200 and (await c.get("/video/hora", headers=CH)).status_code == 200)
            check("la misma persona en OTRA clase sí puede entrar (clave por clase)", (await J(JH, b)).status_code == 200)
            check("el profesor puede seguir moderando OTRA clase (clave por clase)",
                  (await M(TH, b, "mute", "carlos")).status_code in (200, 400))
            espera = int(r429.headers["retry-after"]) if r429 is not None else 10
            reloj[0] += espera
            check(f"tras Retry-After ({espera} s) vuelve a permitir", (await J(JH, a)).status_code == 200)

            print("\n  · 6G-A: el retirado sigue viendo su motivo real")
            await M(TH, b, "remove", "carlos")
            rs = [await J(CH, b) for _ in range(8)]
            check("retirado → 403 'El profesor te retiró…' (no un 429 que lo esconda)",
                  all(r.status_code == 403 and "retiró" in r.text for r in rs))

            print("\n  · Finalizar: 10 a la vez → UNA sola secuencia")
            await J(MH, fin)
            _LK.llamadas = []
            rs = await asyncio.gather(*[c.post(f"/teacher/sessions/{fin}/finalize", headers=TH) for _ in range(10)])
            vr = [r.json().get("video_room") for r in rs if r.status_code == 200]
            metodos = [x["metodo"] for x in _LK.llamadas]
            check(f"las 10 responden 200 ({sorted(set(r.status_code for r in rs))}) con respuesta coherente {sorted(set(vr))}",
                  all(r.status_code == 200 for r in rs) and set(vr) <= {"cerrada", "ya_finalizada"} and vr.count("cerrada") >= 1)
            check(f"una sola auditoría de finalizar ({auditorias(fin, 'finalize_session')})", auditorias(fin, "finalize_session") == 1)
            check(f"un solo DeleteRoom ({metodos.count('DeleteRoom')}) y una sola marca 'finalizada' ({metodos.count('UpdateRoomMetadata')})",
                  metodos.count("DeleteRoom") == 1 and metodos.count("UpdateRoomMetadata") == 1)
            rem = [x["cuerpo"]["identity"] for x in _LK.llamadas if x["metodo"] == "RemoveParticipant"]
            check(f"revocación una vez por persona ({len(rem)} llamadas, {len(set(rem))} personas)", len(rem) == len(set(rem)))
            r = await c.post(f"/teacher/sessions/{fin}/finalize", headers=TH)
            check("repetir más tarde → 200 coherente sin repetir trabajo",
                  r.status_code == 200 and r.json().get("video_room") == "cerrada" and auditorias(fin, "finalize_session") == 1)
            check("/join tras finalizar sigue rechazado (400)", (await J(MH, fin)).status_code == 400)

            print("\n  · Finalizar con fallo previo de LiveKit: un reintento explícito sí vuelve a cerrar")
            await J(MH, fin2)
            _LK.fallar = {"DeleteRoom"}
            r1 = await c.post(f"/teacher/sessions/{fin2}/finalize", headers=TH)
            _LK.fallar = set()
            _LK.llamadas = []
            r2 = await c.post(f"/teacher/sessions/{fin2}/finalize", headers=TH)
            check(f"1.º: error de LiveKit ({r1.json().get('video_room')}); 2.º: reintenta y cierra ({r2.json().get('video_room')})",
                  r1.json().get("video_room") == "error" and r2.json().get("video_room") == "cerrada"
                  and [x["metodo"] for x in _LK.llamadas].count("DeleteRoom") == 1)
            check("…sin auditar dos veces", auditorias(fin2, "finalize_session") == 1)
        finally:
            srv.shutdown()
            n = limpiar(creadas)
            print(f"    · limpieza local: {n} sesión(es)" if n is not None else "    · limpieza no aplicable")


# ============================================================================ API (servidor compartido)
async def parte_api(check):
    print("\n[API] Servidor local: uso normal sin 429 (no se le provoca abuso: es compartido con la suite)")
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        tok = (await c.post("/auth/login", json=MARIA)).json()["access_token"]
        H = {"Authorization": f"Bearer {tok}"}
        cods = [(await c.get("/video/hora", headers=H)).status_code for _ in range(9)]
        check(f"/video/hora: 3 sincronizaciones seguidas → {sorted(set(cods))}", cods == [200] * 9)


# ============================================================================ ESTÁTICO
def parte_estatica(check):
    print("\n[ESTÁTICO]")
    v = (ROOT / "app/routers/video.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    j = v[v.find("async def join_class_video"):v.find("async def heartbeat")]
    check("/join: límite por persona ANTES de la base de datos",
          0 < j.find('comprobar("join_usuario", user.user_id)') < j.find("s = await db.get(ClassSession"))
    check("/join: límite por clase DESPUÉS de autorizar (retirado, audiencia, horario) y ANTES del token",
          j.find("bloqueo_activo(db, session_id, user.user_id)") < j.find('comprobar("join_clase"')
          < j.find("token = build_token(") and j.find("if ends and now > ends") < j.find('comprobar("join_clase"'))
    lv = (ROOT / "app/services/limites_video.py").read_text(encoding="utf-8") if (ROOT / "app/services/limites_video.py").exists() else ""
    check("claves = acción + usuario autenticado + clase (sin IP)",
          'f"{accion}:{user_id}:{session_id or \'-\'}"' in lv and "client.host" not in lv and "request" not in lv)
    check("limitador por franjas con cerrojo propio (sin cerrojo global) y documentado por instancia",
          "self._cerrojos = [threading.Lock() for _ in range(franjas)]" in lv and "POR PROCESO" in lv)
    t = (ROOT / "app/routers/teacher.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    check("finalizar: transición atómica (UPDATE condicional) antes de auditar y cerrar",
          ".where(ClassSession.id == session_id, ClassSession.status != SessionStatus.completed)" in t
          and t.find("if not gano:") < t.find('"finalize_session"'))
    cp = (WEB / "components/CallProvider.tsx").read_text(encoding="utf-8")
    ex = (WEB / "components/ClassRoomExtras.tsx").read_text(encoding="utf-8")
    check("frontend: 429 al volver a entrar = 'Espera un momento' recuperable (no borra la clase activa 6E)",
          'esperar: e?.status === 429' in cp and "r.esperar ? terminalEspera(r.error)" in cp and 'tipo: "red", titulo: "Espera un momento"' in cp)
    check("frontend: heartbeat sigue silencioso ante errores (sin avisos)",
          "api(`/video/sessions/${sessionId}/heartbeat`, { method: \"POST\", auth: true }).catch(() => {});" in ex)
    check("frontend: 'ya_finalizada' se explica", "La clase ya estaba finalizada." in cp)


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    parte_unidad(check)
    await parte_api(check)
    await parte_integracion(check)
    parte_estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
