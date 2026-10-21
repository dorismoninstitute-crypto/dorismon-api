"""GATE 6H-A — Grabación de clases (LiveKit Egress RoomComposite), estados y seguridad.

[INTEGRACIÓN] App EN ESTE PROCESO contra un simulador LOCAL de LiveKit (servicio Egress
    con estado + RoomService). El simulador NO es LiveKit Cloud: lo que pasa aquí no
    demuestra el comportamiento real del proveedor.
    · permisos (estudiante / otro profesor no; profesor de la clase y Dirección sí);
    · clase externa, cancelada, finalizada, inexistente, sin almacenamiento privado;
    · 10 "iniciar" a la vez → 1 Egress; 10 "detener" a la vez → 1 StopEgress;
    · estados reales: starting → recording (confirmado por el proveedor) → stopping →
      processing → ready (solo con archivo) / failed;
    · /join avisa "grabando" SOLO cuando el proveedor confirmó;
    · webhook firmado como LiveKit: válido / duplicado / fuera de orden / firma falsa /
      cuerpo alterado / egress de otra sala;
    · fallos: proveedor caído, inicio rechazado, fallo de almacenamiento, terminada sin
      archivo, StopEgress caído (reintento), reinicio del backend (reconciliación);
    · Finalizar con grabación activa: StopEgress ANTES de revocar tokens y DeleteRoom;
    · audiencia congelada; ninguna respuesta ni log con secretos, bucket o clave.
[ESTÁTICO] indicador desde LiveKit (late join / reconexión / 6E), aviso en la pantalla
    previa, controles del profesor, secretos fuera del frontend.

Uso: python tests/test_gate6ha.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import jwt

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}
OTRO_PROFE = {"email": "ana@dorismon.do", "password": "Prof-Test-4822!"}
MARIA = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
CARLOS = {"email": "carlos.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
DB_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dorismon.db")
SECRETO_R2 = "SECRETO-R2-SOLO-PRUEBA-6ha-9f3c"
ACCESO_R2 = "AKPRUEBA6HA"
BUCKET = "dorismon-grabaciones-prueba"


def claims(tok):
    try:
        p = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}


def _a_livekit(x):
    """PREPROD-2 — En el cable, como LiveKit Cloud REAL (Twirp): EgressInfo en snake_case
    (egress_id, room_name, file_results, ended_at…). El modelo interno sigue en camelCase.
    Los webhooks (protojson) sí son camelCase y no pasan por aquí."""
    if isinstance(x, dict):
        return {"".join("_" + c.lower() if c.isupper() else c for c in k): _a_livekit(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_a_livekit(v) for v in x]
    return x


class _LK(BaseHTTPRequestHandler):
    llamadas: list = []
    egresos: dict = {}       # egress_id → EgressInfo (simulado)
    modo_inicio = "ok"       # ok | caido | rechazar
    modo_stop = "ok"         # ok | caido
    n = 0

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        cuerpo = json.loads(self.rfile.read(n) or b"{}")
        servicio, metodo = self.path.split("/twirp/")[-1].split("/")
        v = claims((self.headers.get("Authorization") or "").replace("Bearer ", "")).get("video") or {}
        _LK.llamadas.append({"servicio": servicio, "metodo": metodo, "cuerpo": cuerpo, "video": v})
        if servicio == "livekit.Egress":
            if not v.get("roomRecord"):
                return self._r(401, {"code": "unauthenticated", "msg": "permissions denied"})
            if metodo == "StartRoomCompositeEgress":
                if _LK.modo_inicio == "caido":
                    return self._r(503, {"code": "unavailable"})
                if _LK.modo_inicio == "rechazar":
                    return self._r(400, {"code": "invalid_argument", "msg": "room not found"})
                time.sleep(0.05)
                _LK.n += 1
                eid = f"EG_prueba_{_LK.n}"
                _LK.egresos[eid] = {"egressId": eid, "roomName": cuerpo["roomName"], "status": "EGRESS_STARTING"}
                return self._r(200, _a_livekit(_LK.egresos[eid]))
            if metodo == "StopEgress":
                if _LK.modo_stop == "caido":
                    return self._r(503, {"code": "unavailable"})
                e = _LK.egresos.get(cuerpo.get("egressId"))
                if not e or e["status"] not in ("EGRESS_STARTING", "EGRESS_ACTIVE"):
                    return self._r(412, {"code": "failed_precondition", "msg": "egress not active"})
                time.sleep(0.05)
                e["status"] = "EGRESS_ENDING"
                return self._r(200, _a_livekit(e))
            if metodo == "ListEgress":
                e = _LK.egresos.get(cuerpo.get("egressId"))
                return self._r(200, {"items": [_a_livekit(e)] if e else []})
        if metodo == "ListRooms":
            return self._r(200, {"rooms": [{"name": (cuerpo.get("names") or [""])[0], "metadata": ""}]})
        if metodo in ("ListParticipants",):
            return self._r(200, {"participants": []})
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


def firmar(cuerpo: bytes, key, secret, sha=None):
    """Como el servidor de LiveKit al enviar un webhook (JWT con claim sha256)."""
    now = int(time.time())
    return jwt.encode({"iss": key, "nbf": now - 5, "exp": now + 300,
                       "sha256": sha or base64.b64encode(hashlib.sha256(cuerpo).digest()).decode()},
                      secret, algorithm="HS256")


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
        tablas = {r[0] for r in cx.execute("select name from sqlite_master where type='table'")}
        if "video_recordings" in tablas:
            cx.execute(f"delete from video_recording_audience where recording_id in (select id from video_recordings where session_id in ({marcas}))", ids)
        for t in tablas:
            if t == "class_sessions" or t == "video_recording_audience":
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


def filas(sql, *args):
    import sqlite3
    cx = sqlite3.connect(str(Path(DB_URL.split("///", 1)[-1])), timeout=30)
    try:
        return cx.execute(sql, args).fetchall()
    finally:
        cx.close()


async def parte_integracion(check):
    print("\n[INTEGRACIÓN] App en proceso + simulador de LiveKit Egress (NO es LiveKit Cloud)")
    sys.path.insert(0, str(ROOT))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _LK)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    key = os.environ.get("LIVEKIT_API_KEY") or "APIprueba"
    secret = os.environ.get("LIVEKIT_API_SECRET") or "secretodepruebaparatest123456789"
    os.environ.update({"LIVEKIT_URL": f"ws://127.0.0.1:{srv.server_address[1]}", "LIVEKIT_API_KEY": key,
                       "LIVEKIT_API_SECRET": secret, "RECORDINGS_R2_BUCKET": BUCKET,
                       "R2_ACCOUNT_ID": "cuentaprueba6ha", "R2_ACCESS_KEY": ACCESO_R2, "R2_SECRET_KEY": SECRETO_R2})
    registros = []

    class _Captura(logging.Handler):
        def emit(self, r):
            registros.append(self.format(r))
    try:
        from app.main import app   # main.py hace basicConfig(force=True): la captura va DESPUÉS
        from app.core.db import engine
        from app.models import Base
        try:
            import app.services.grabaciones as gr
        except ImportError:   # código anterior a 6H-A (control negativo): se prueba igual por HTTP
            gr = None
        check("existe app/services/grabaciones.py", gr is not None)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        check(f"la app (con grabación) se importa en proceso ({type(e).__name__}: {e})", False)
        srv.shutdown()
        return
    _cap = _Captura()
    _cap.setLevel(logging.DEBUG)
    _raiz = logging.getLogger()
    _raiz.addHandler(_cap)
    _raiz.setLevel(logging.DEBUG)   # todo lo que la app y httpx registran, incluido DEBUG
    creadas = []
    respuestas = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(cred):
            return {"Authorization": f"Bearer {(await c.post('/auth/login', json=cred)).json()['access_token']}"}
        AH, TH, OH, MH, CH = [await login(x) for x in (ADMIN, TEACHER, OTRO_PROFE, MARIA, CARLOS)]
        ids = {k: (await c.get("/auth/me", headers=h)).json()["id"]
               for k, h in (("admin", AH), ("prof", TH), ("maria", MH), ("carlos", CH))}
        now = dt.datetime.now(dt.timezone.utc)

        async def evento(nombre, **extra):
            body = {"title": nombre, "teacher_id": ids["prof"], "modality": "online", "video_provider": "dorismon",
                    "starts_at_utc": (now - dt.timedelta(minutes=5)).isoformat(),
                    "ends_at_utc": (now + dt.timedelta(minutes=55)).isoformat()}
            body.update(extra)
            r = await c.post("/admin/events", headers=AH, json=body)
            i = r.json().get("id") if r.status_code in (200, 201) else None
            if i:
                creadas.append(i)
            return i

        a, b, fin, fallos = [await evento(f"G6HA {n}") for n in ("A", "B", "finalizar", "fallos")]
        ext = await evento("G6HA externa", video_provider="meet", meeting_url="https://meet.google.com/abc-defg-hij")
        check("se crean las clases de prueba", all((a, b, ext, fin, fallos)))
        try:
            G = lambda h, sid, accion: c.post(f"/video/sessions/{sid}/grabacion/{accion}", headers=h)
            E = lambda h, sid: c.get(f"/video/sessions/{sid}/grabacion", headers=h)
            starts = lambda: [x for x in _LK.llamadas if x["metodo"] == "StartRoomCompositeEgress"]
            for h in (TH, MH, CH):
                await c.post(f"/video/sessions/{a}/join", headers=h)

            print("\n  · Permisos y sesión")
            try:
                check("estudiante NO inicia (403)", (await G(MH, a, "iniciar")).status_code == 403)
                check("estudiante NO detiene (403)", (await G(MH, a, "detener")).status_code == 403)
                check("estudiante NO ve el estado de la grabación del profesor (403)", (await E(MH, a)).status_code == 403)
                check("profesor ajeno NO inicia (403)", (await G(OH, a, "iniciar")).status_code == 403)
                r = await G(TH, ext, "iniciar")
                check(f"clase externa (Meet) rechazada ({r.status_code})", r.status_code == 400 and "Video Dorismon" in r.text)
                check("clase inexistente → 404", (await G(TH, "no-existe", "iniciar")).status_code == 404)
                check("ninguna Egress creada por rechazos", not starts())
                os.environ["RECORDINGS_R2_BUCKET"] = ""
                r = await G(TH, b, "iniciar")
                os.environ["RECORDINGS_R2_BUCKET"] = BUCKET
                check(f"sin almacenamiento privado configurado → 503 y SIN Egress ({r.status_code})", r.status_code == 503 and not starts())
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Permisos y sesión» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · 10 'iniciar' a la vez → UNA Egress")
            try:
                rs = await asyncio.gather(*[G(TH, a, "iniciar") for _ in range(10)])
                respuestas += [r.text for r in rs]
                res = [r.json().get("resultado") for r in rs]
                check(f"10 respuestas 200 ({sorted(set(r.status_code for r in rs))}): 1 iniciada + {res.count('ya_activa')} ya_activa",
                      all(r.status_code == 200 for r in rs) and res.count("iniciada") == 1 and res.count("ya_activa") == 9)
                check(f"UNA sola llamada StartRoomCompositeEgress ({len(starts())})", len(starts()) == 1)
                s0 = starts()[0]
                check("token de servidor con roomRecord (nunca el del navegador)", s0["video"].get("roomRecord") is True and "roomJoin" not in s0["video"])
                fo = s0["cuerpo"]["fileOutputs"][0]
                check("salida MP4 a la sala correcta y al bucket PRIVADO configurado (clave grabaciones/<clase>/<id>.mp4)",
                      s0["cuerpo"]["roomName"] == f"dorismon-{a}" and fo["fileType"] == "MP4" and fo["s3"]["bucket"] == BUCKET
                      and re.fullmatch(rf"grabaciones/{a}/[0-9a-f-]+\.mp4", fo["filepath"]))
                eid = next(iter(_LK.egresos))
                est = (await E(TH, a)).json()
                check(f"estado inicial 'starting' (no 'grabando' por pulsar) → {est['grabacion']['estado']}", est["grabacion"]["estado"] == "starting")
                check("/join de un estudiante: grabando = false mientras el proveedor no confirma",
                      (await c.post(f"/video/sessions/{a}/join", headers=MH)).json().get("grabando") is False)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «10 iniciar a la vez → UNA Egress» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · El proveedor confirma (ACTIVE) → 'recording'")
            try:
                _LK.egresos[eid]["status"] = "EGRESS_ACTIVE"
                _LK.egresos[eid]["startedAt"] = str(int(time.time() * 1e9))
                est = (await E(TH, a)).json()
                check(f"reconciliado con ListEgress → {est['grabacion']['estado']}", est["grabacion"]["estado"] == "recording")
                j = (await c.post(f"/video/sessions/{a}/join", headers=CH)).json()
                check("quien entra tarde: /join dice grabando = true (aviso en la pantalla previa)", j.get("grabando") is True)
                check("Dirección inicia en la misma clase → 'ya_activa', sin otra Egress",
                      (await G(AH, a, "iniciar")).json().get("resultado") == "ya_activa" and len(starts()) == 1)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «El proveedor confirma (ACTIVE) → recording» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · 10 'detener' a la vez → UN StopEgress")
            try:
                rs = await asyncio.gather(*[G(TH if i % 2 else AH, a, "detener") for i in range(10)])
                respuestas += [r.text for r in rs]
                stops = [x for x in _LK.llamadas if x["metodo"] == "StopEgress"]
                check(f"10 respuestas 200 y UN solo StopEgress ({len(stops)})", all(r.status_code == 200 for r in rs) and len(stops) == 1)
                est = (await E(TH, a)).json()
                check(f"tras detener: 'processing' (LiveKit ENDING), NO 'lista' → {est['grabacion']['estado']}",
                      est["grabacion"]["estado"] == "processing")
                check("ya no se avisa 'grabando' a quien entra", (await c.post(f"/video/sessions/{a}/join", headers=MH)).json().get("grabando") is False)
                _LK.egresos[eid].update({"status": "EGRESS_COMPLETE", "endedAt": str(int(time.time() * 1e9)),
                                         "fileResults": [{"filename": fo["filepath"], "size": "7340032", "duration": str(95 * 10**9),
                                                          "location": f"https://{BUCKET}.example/{fo['filepath']}"}]})
                est = (await E(TH, a)).json()
                g = est["grabacion"]
                check(f"COMPLETE con archivo → 'ready' (duración {g.get('duracion_segundos')} s, {g.get('tamano_bytes')} bytes)",
                      g["estado"] == "ready" and g["duracion_segundos"] == 95 and g["tamano_bytes"] == 7340032)
                fila = filas("select storage_bucket, storage_key, status, activa from video_recordings where egress_id=?", eid)[0]
                check("la base guarda bucket + clave (no URL, no 'location'), y libera la sala",
                      fila[0] == BUCKET and fila[1] == fo["filepath"] and fila[2] == "ready" and fila[3] is None)
                check("auditoría: solicitud, grabando, detener, lista",
                      {"video_recording_requested", "video_recording_recording", "video_recording_stop_requested", "video_recording_ready"}
                      <= {x for (x,) in filas("select action from audit_logs where target_id=?", a)})
                aud = dict(filas("select user_id, reason from video_recording_audience where recording_id=(select id from video_recordings where egress_id=?)", eid))
                check(f"audiencia congelada: estudiantes con derecho / que entraron, sin el profesor ({len(aud)})",
                      ids["maria"] in aud and ids["carlos"] in aud and ids["prof"] not in aud)
                r = await G(TH, a, "iniciar")
                check("una vez lista, se puede grabar OTRO segmento (nueva Egress)", r.json().get("resultado") == "iniciada" and len(starts()) == 2)
                eid2 = [k for k in _LK.egresos if k != eid][0]
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «10 detener a la vez → UN StopEgress» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Webhooks firmados (como LiveKit)")
            try:
                W = lambda cuerpo, auth: c.post("/video/livekit/webhook", content=cuerpo, headers={"Authorization": auth, "Content-Type": "application/webhook+json"})
                ev = json.dumps({"event": "egress_updated", "id": "EV1", "egressInfo": {"egressId": eid2, "roomName": f"dorismon-{a}", "status": "EGRESS_ACTIVE"}}).encode()
                check("firma válida → aplica (recording)", (await W(ev, firmar(ev, key, secret))).status_code == 200
                      and filas("select status from video_recordings where egress_id=?", eid2)[0][0] == "recording")
                check("evento DUPLICADO → sin cambios ni errores", (await W(ev, firmar(ev, key, secret))).status_code == 200
                      and filas("select status from video_recordings where egress_id=?", eid2)[0][0] == "recording")
                viejo = json.dumps({"event": "egress_started", "id": "EV0", "egressInfo": {"egressId": eid2, "roomName": f"dorismon-{a}", "status": "EGRESS_STARTING"}}).encode()
                await W(viejo, firmar(viejo, key, secret))
                check("evento FUERA DE ORDEN (starting después de active) → no retrocede",
                      filas("select status from video_recordings where egress_id=?", eid2)[0][0] == "recording")
                falso = json.dumps({"event": "egress_ended", "egressInfo": {"egressId": eid2, "roomName": f"dorismon-{a}", "status": "EGRESS_FAILED"}}).encode()
                check("firma con otro secreto → 401 y nada cambia", (await W(falso, firmar(falso, key, "otro-secreto-cualquiera-123456"))).status_code == 401
                      and filas("select status from video_recordings where egress_id=?", eid2)[0][0] == "recording")
                check("cuerpo alterado (sha256 no coincide) → 401", (await W(falso, firmar(ev, key, secret))).status_code == 401)
                check("sin firma → 401", (await W(falso, "")).status_code == 401)
                ajeno = json.dumps({"event": "egress_ended", "egressInfo": {"egressId": eid2, "roomName": f"dorismon-{b}", "status": "EGRESS_FAILED"}}).encode()
                await W(ajeno, firmar(ajeno, key, secret))
                check("egress_id real con OTRA sala → no modifica nada",
                      filas("select status from video_recordings where egress_id=?", eid2)[0][0] == "recording")
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Webhooks firmados (como LiveKit)» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Finalizar con grabación activa")
            try:
                for h in (TH, MH):
                    await c.post(f"/video/sessions/{fin}/join", headers=h)
                await G(TH, fin, "iniciar")
                eid3 = [k for k in _LK.egresos if k not in (eid, eid2)][0]
                _LK.egresos[eid3]["status"] = "EGRESS_ACTIVE"
                await E(TH, fin)
                _LK.llamadas = []
                r = await c.post(f"/teacher/sessions/{fin}/finalize", headers=TH)
                orden = [x["metodo"] for x in _LK.llamadas]
                check(f"finaliza (200, video_room = {r.json().get('video_room')})", r.status_code == 200)
                check("StopEgress ANTES de revocar tokens y de DeleteRoom",
                      "StopEgress" in orden and orden.index("StopEgress") < orden.index("DeleteRoom")
                      and orden.index("StopEgress") < orden.index("UpdateRoomMetadata"))
                check("la grabación quedó detenida (processing) antes de cerrar la sala",
                      filas("select status from video_recordings where egress_id=?", eid3)[0][0] in ("processing", "ready"))
                check("clase finalizada: no se puede iniciar otra grabación (400)", (await G(TH, fin, "iniciar")).status_code == 400)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Finalizar con grabación activa» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Fallos")
            try:
                await c.post(f"/video/sessions/{fallos}/join", headers=TH)
                _LK.modo_inicio = "caido"
                r = await G(TH, fallos, "iniciar")
                _LK.modo_inicio = "ok"
                check(f"proveedor caído al iniciar → 502 y la grabación queda 'failed' (sala libre) ({r.status_code})",
                      r.status_code == 502 and filas("select count(*) from video_recordings where session_id=? and status='failed' and activa is null", fallos)[0][0] == 1)
                _LK.modo_inicio = "rechazar"
                r = await G(TH, fallos, "iniciar")
                _LK.modo_inicio = "ok"
                check("inicio rechazado por LiveKit (sala inexistente) → 502, failed", r.status_code == 502)
                r = await G(TH, fallos, "iniciar")
                e4 = r.json().get("grabacion", {})
                eid4 = max(_LK.egresos, key=lambda k: int(k.rsplit("_", 1)[1]))
                _LK.egresos[eid4].update({"status": "EGRESS_FAILED", "errorCode": 403,
                                          "error": f"s3 upload failed: AccessDenied https://{BUCKET}.r2.example/clave"})
                g = (await E(TH, fallos)).json()["grabacion"]
                check(f"fallo de ALMACENAMIENTO → 'failed' con mensaje (nunca 'lista') → {g['estado']}",
                      g["estado"] == "failed" and "guardar el archivo" in (g.get("error") or ""))
                check("el texto crudo del proveedor (endpoint / bucket) NO se muestra; queda solo en la base",
                      BUCKET not in (g.get("error") or "")
                      and "AccessDenied" in filas("select error_message from video_recordings where egress_id=?", eid4)[0][0])
                await G(TH, fallos, "iniciar")
                eid5 = max(_LK.egresos, key=lambda k: int(k.rsplit("_", 1)[1]))
                _LK.egresos[eid5].update({"status": "EGRESS_COMPLETE", "fileResults": []})
                g = (await E(TH, fallos)).json()["grabacion"]
                check("COMPLETE SIN archivo → 'failed' (no se informa una grabación que no existe)", g["estado"] == "failed")
                await G(TH, fallos, "iniciar")
                eid6 = max(_LK.egresos, key=lambda k: int(k.rsplit("_", 1)[1]))
                _LK.egresos[eid6]["status"] = "EGRESS_ACTIVE"
                await E(TH, fallos)
                _LK.modo_stop = "caido"
                r = await G(TH, fallos, "detener")
                _LK.modo_stop = "ok"
                st = filas("select status, activa from video_recordings where egress_id=?", eid6)[0]
                check(f"StopEgress caído → 502 honesto; queda 'stopping' y ocupa la sala (no se duplica) ({r.status_code}, {st[0]})",
                      r.status_code == 502 and st[0] == "stopping" and st[1] == fallos)
                check("…otro 'iniciar' mientras tanto → ya_activa (sin otra Egress)",
                      (await G(TH, fallos, "iniciar")).json().get("resultado") == "ya_activa")
                if gr:
                    gr._DETENIENDO.clear()   # "reinicio del backend": lo único en memoria se pierde
                r = await G(TH, fallos, "detener")
                check("reintento tras el reinicio → StopEgress aceptado → processing",
                      r.status_code == 200 and filas("select status from video_recordings where egress_id=?", eid6)[0][0] == "processing")
                _LK.egresos[eid6].update({"status": "EGRESS_COMPLETE", "fileResults": [{"size": "1024", "duration": "1000000000"}]})
                await E(TH, fallos)
                await G(TH, fallos, "iniciar")
                eid7 = max(_LK.egresos, key=lambda k: int(k.rsplit("_", 1)[1]))
                _LK.egresos[eid7].update({"status": "EGRESS_COMPLETE", "fileResults": [{"size": "2048", "duration": "3000000000"}]})
                r = await G(TH, fallos, "detener")   # la sala se cerró / todos salieron: terminó sola
                check(f"detener una grabación que YA terminó sola (sala cerrada) → StopEgress 412 → se consulta → ready ({r.status_code})",
                      r.status_code == 200 and filas("select status, activa from video_recordings where egress_id=?", eid7)[0] == ("ready", None))
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Fallos» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Rate limit 6G-D (protección secundaria)")
            try:
                rs = await asyncio.gather(*[G(AH, b, "iniciar") for _ in range(30)])
                cods = [r.status_code for r in rs]
                eg_b = [x for x in starts() if x["cuerpo"]["roomName"] == f"dorismon-{b}"]
                check(f"30 'iniciar' de golpe → {cods.count(429)}×429 con Retry-After y UNA sola Egress en esa clase ({len(eg_b)})",
                      cods.count(429) >= 1 and all(r.headers.get("retry-after") for r in rs if r.status_code == 429) and len(eg_b) == 1)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Rate limit 6G-D (protección secundaria)» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Secretos")
            try:
                todo = " ".join(respuestas) + " ".join([(await E(TH, a)).text, (await E(TH, fallos)).text])
                check("ninguna respuesta incluye el secreto, la clave de acceso, el bucket ni la clave del objeto",
                      SECRETO_R2 not in todo and ACCESO_R2 not in todo and BUCKET not in todo and "grabaciones/" not in todo)
                check(f"ningún log incluye el secreto ni la clave de acceso ({len(registros)} líneas revisadas, nivel DEBUG)",
                      len(registros) > 0 and not any(SECRETO_R2 in x or ACCESO_R2 in x for x in registros))
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Secretos» terminó sin errores ({type(e).__name__}: {e})", False)

        finally:
            srv.shutdown()
            n = limpiar(creadas)
            print(f"    · limpieza local: {n} sesión(es)" if n is not None else "    · limpieza no aplicable")


def parte_estatica(check):
    print("\n[ESTÁTICO]")
    m = (ROOT / "app/models/placement_booking.py").read_text(encoding="utf-8")
    check("tablas nuevas video_recordings (única grabación activa) y video_recording_audience",
          '__tablename__ = "video_recordings"' in m and 'UniqueConstraint("activa", name="uq_video_recording_activa")' in m
          and '__tablename__ = "video_recording_audience"' in m)
    ruta_gr = ROOT / "app/services/grabaciones.py"
    check("recording_url heredado NO se reutiliza",
          ruta_gr.is_file() and "recording_url" not in ruta_gr.read_text(encoding="utf-8"))
    ix = (WEB / "components/InteraccionClase.tsx").read_text(encoding="utf-8")
    cp = (WEB / "components/CallProvider.tsx").read_text(encoding="utf-8")
    check("indicador desde LiveKit (room.isRecording) y se relee al conectar / reconectar",
          "room.isRecording" in ix and "RoomEvent.RecordingStatusChanged" in ix and ".on(RoomEvent.Reconnected, actualizar)" in ix)
    check("indicador en completa/dividida y en mini; aviso en la pantalla previa",
          "<IndicadorGrabacion />" in cp and "<IndicadorGrabacion compacto />" in cp
          and "Esta clase está siendo grabada." in cp and "datos.grabando === true" in cp)
    check("profesor: confirmación con el texto pedido y un solo botón según el estado",
          "Todos los participantes serán avisados de que esta clase está siendo grabada." in ix and "data-detener-grabacion" in ix)
    web = "".join(p.read_text(encoding="utf-8", errors="ignore") for d in ("components", "app", "lib")
                  for p in (WEB / d).rglob("*") if p.suffix in (".ts", ".tsx"))
    check("el frontend no contiene secretos ni variables del almacenamiento / LiveKit",
          not re.search(r"R2_SECRET|R2_ACCESS|RECORDINGS_R2|LIVEKIT_API_SECRET", web))


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    await parte_integracion(check)
    parte_estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
