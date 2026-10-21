"""GATE 6H-B — Biblioteca de grabaciones y reproducción privada.

[INTEGRACIÓN] App EN ESTE PROCESO + simulador de LiveKit Egress (de test_gate6ha) + simulador
    LOCAL de almacenamiento S3/R2 (bucket privado). Ninguno es el servicio real:
    PENDIENTE DE VERIFICACIÓN CON R2 REAL y con LiveKit Cloud.
    El simulador S3 verifica DE VERDAD la firma SigV4 de la URL (petición canónica recalculada
    con el secreto), su expiración, Range (206) y que sin firma el bucket es privado (403).
    · acceso histórico: A y B en el grupo al empezar, C entra durante la grabación → A/B la
      ven, C no; A sale del grupo después → conserva el acceso; cuenta desactivada → no;
      sacado de la clase (6G-A) → no;
    · profesor responsable sí, profesor ajeno no, Dirección sí (filtros y páginas);
    · estados: el estudiante solo ve "ready"; processing / failed solo para profesor y
      Dirección (error genérico); no-ready → 409 sin URL;
    · IDOR: recording_id ajeno, session_id cambiado o inexistente → 404 sin URL;
    · URL firmada corta (600 s), renovable, no guardada ni registrada; dos segmentos;
      almacenamiento caído → 503; objeto inexistente → 404; sin configuración → 503;
    · rate limit; la biblioteca no crea Egress; secretos fuera de respuestas y logs.
[ESTÁTICO] biblioteca en el menú de los tres roles, reproductor nativo, renovación con
    límite, estado vacío, carga de Gate 5A, service worker sin caché de otros orígenes.

Uso: python tests/test_gate6hb.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl, quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_gate6ha as base  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
JUANA = {"email": "juana.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
LECTURA_ID = "AKLECTURA6HB"
LECTURA_SECRETO = "SECRETO-LECTURA-6hb-ficticio-7c1e"
CONTENIDO = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 64     # bytes de prueba (no es un vídeo)


# --------------------------------------------------------------- simulador S3 / R2
class S3Stub(BaseHTTPRequestHandler):
    """Bucket PRIVADO S3-compatible (solo pruebas). Valida SigV4 por consulta (URL firmada)."""
    objetos: dict = {}            # (bucket, clave) → (bytes, content-type)
    acceso: dict = {}             # access key id → secret
    desfase = 0                   # segundos añadidos al reloj (simular expiración)
    modo = "ok"                   # ok | caido
    peticiones: list = []

    def log_message(self, *a):
        pass

    def _xml(self, code, codigo, msg=""):
        cuerpo = f"<?xml version='1.0'?><Error><Code>{codigo}</Code><Message>{msg}</Message></Error>".encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(cuerpo)

    def _destino(self):
        partes = urlsplit(self.path)
        ruta = partes.path.lstrip("/")
        bucket, _, clave = ruta.partition("/")
        from urllib.parse import unquote
        return partes, bucket, unquote(clave)

    def _firma_query_valida(self, partes):
        q = dict(parse_qsl(partes.query, keep_blank_values=True))
        if q.get("X-Amz-Algorithm") != "AWS4-HMAC-SHA256" or "X-Amz-Signature" not in q:
            return "AccessDenied"
        cred = q.get("X-Amz-Credential", "").split("/")
        if len(cred) != 5 or cred[0] not in self.acceso:
            return "InvalidAccessKeyId"
        fecha = dt.datetime.strptime(q["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        ahora = time.time() + self.desfase
        if ahora > fecha.timestamp() + int(q.get("X-Amz-Expires", "0")):
            return "AccessDenied:Request has expired"
        pares = sorted((k, v) for k, v in parse_qsl(partes.query, keep_blank_values=True) if k != "X-Amz-Signature")
        cq = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in pares)
        canonica = "\n".join(["GET", partes.path, cq, f"host:{self.headers['Host']}\n", "host", "UNSIGNED-PAYLOAD"])
        alcance = "/".join(cred[1:])
        a_firmar = "\n".join(["AWS4-HMAC-SHA256", q["X-Amz-Date"], alcance, hashlib.sha256(canonica.encode()).hexdigest()])
        k = ("AWS4" + self.acceso[cred[0]]).encode()
        for parte in cred[1:]:
            k = hmac.new(k, parte.encode(), hashlib.sha256).digest()
        esperada = hmac.new(k, a_firmar.encode(), hashlib.sha256).hexdigest()
        return None if hmac.compare_digest(esperada, q["X-Amz-Signature"]) else "SignatureDoesNotMatch"

    def do_PUT(self):            # solo para sembrar objetos en las pruebas (ruta /_subir/...)
        partes, _, _ = self._destino()
        if not partes.path.startswith("/_subir/"):
            return self._xml(403, "AccessDenied")
        bucket, _, clave = partes.path[len("/_subir/"):].partition("/")
        n = int(self.headers.get("Content-Length") or 0)
        self.objetos[(bucket, clave)] = (self.rfile.read(n), self.headers.get("Content-Type") or "video/mp4")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self._servir(cuerpo=False)

    def do_GET(self):
        self._servir(cuerpo=True)

    def _servir(self, cuerpo):
        partes, bucket, clave = self._destino()
        S3Stub.peticiones.append({"metodo": self.command, "ruta": partes.path, "range": self.headers.get("Range")})
        if self.modo == "caido":
            return self._xml(500, "InternalError")
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("AWS4-HMAC-SHA256"):      # HEAD del backend (firma en cabecera)
            m = re.search(r"Credential=([^/]+)/", auth)
            if not m or m.group(1) not in self.acceso:
                return self._xml(403, "InvalidAccessKeyId")
        else:
            error = self._firma_query_valida(partes)
            if error:
                codigo, _, msg = error.partition(":")
                return self._xml(403, codigo, msg)
        obj = self.objetos.get((bucket, clave))
        if not obj:
            return self._xml(404, "NoSuchKey")
        datos, tipo = obj
        rango = self.headers.get("Range")
        if rango and cuerpo:
            m = re.match(r"bytes=(\d+)-(\d*)", rango)
            ini = int(m.group(1))
            fin = int(m.group(2)) if m.group(2) else len(datos) - 1
            trozo = datos[ini:fin + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {ini}-{fin}/{len(datos)}")
        else:
            trozo = datos
            self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(trozo) if cuerpo else len(datos)))
        self.end_headers()
        if cuerpo:
            self.wfile.write(trozo)


# ----------------------------------------------------------------------- pruebas
async def parte_integracion(check):
    print("\n[INTEGRACIÓN] App en proceso + simulador Egress + simulador S3/R2 privado (NO son los reales)")
    L = base._LK
    L.llamadas, L.egresos, L.modo_inicio, L.modo_stop, L.n = [], {}, "ok", "ok", 0
    lk = ThreadingHTTPServer(("127.0.0.1", 0), L)
    s3 = ThreadingHTTPServer(("127.0.0.1", 0), S3Stub)
    for srv in (lk, s3):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    S3Stub.acceso = {LECTURA_ID: LECTURA_SECRETO, base.ACCESO_R2: base.SECRETO_R2}
    S3Stub.objetos, S3Stub.peticiones, S3Stub.desfase, S3Stub.modo = {}, [], 0, "ok"
    endpoint_s3 = f"http://127.0.0.1:{s3.server_address[1]}"
    key = os.environ.get("LIVEKIT_API_KEY") or "APIprueba"
    secret = os.environ.get("LIVEKIT_API_SECRET") or "secretodepruebaparatest123456789"
    os.environ.update({"LIVEKIT_URL": f"ws://127.0.0.1:{lk.server_address[1]}", "LIVEKIT_API_KEY": key,
                       "LIVEKIT_API_SECRET": secret, "RECORDINGS_R2_BUCKET": base.BUCKET,
                       "R2_ACCOUNT_ID": "cuentaprueba6ha", "R2_ACCESS_KEY": base.ACCESO_R2, "R2_SECRET_KEY": base.SECRETO_R2,
                       "RECORDINGS_S3_ENDPOINT": endpoint_s3, "RECORDINGS_R2_READ_ACCESS_KEY": LECTURA_ID,
                       "RECORDINGS_R2_READ_SECRET_KEY": LECTURA_SECRETO})
    try:
        from app.main import app
        from app.core.db import engine
        from app.models import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        check(f"la app con la biblioteca se importa ({type(e).__name__}: {e})", False)
        for srv in (lk, s3):
            srv.shutdown()
        return
    try:
        import app.services.biblioteca_grabaciones as _bib  # noqa: F401
        check("existe app/services/biblioteca_grabaciones.py", True)
    except ImportError:          # código anterior (control negativo): se prueba igual por HTTP
        check("existe app/services/biblioteca_grabaciones.py", False)
    registros = []

    class _Captura(logging.Handler):
        def emit(self, r):
            # Las GET de ESTA prueba a la URL firmada (simulan al navegador) no son logs de la app
            if r.name == "httpx" and endpoint_s3 in r.getMessage():
                return
            registros.append(self.format(r))
    cap = _Captura()
    cap.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(cap)
    logging.getLogger().setLevel(logging.DEBUG)

    import sqlite3
    ruta_db = str(Path(base.DB_URL.split("///", 1)[-1]))
    creadas, borrar, respuestas = [], [], []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(cred):
            return {"Authorization": f"Bearer {(await c.post('/auth/login', json=cred)).json()['access_token']}"}
        AH, TH, OH, MH, JH, CH = [await login(x) for x in (base.ADMIN, base.TEACHER, base.OTRO_PROFE, base.MARIA, JUANA, base.CARLOS)]
        ids = {k: (await c.get("/auth/me", headers=h)).json()["id"]
               for k, h in (("prof", TH), ("otro", OH), ("maria", MH), ("juana", JH), ("carlos", CH))}
        now = dt.datetime.now(dt.timezone.utc)

        async def evento(nombre, profe):
            r = await c.post("/admin/events", headers=AH, json={
                "title": nombre, "teacher_id": profe, "modality": "online", "video_provider": "dorismon",
                "starts_at_utc": (now - dt.timedelta(minutes=5)).isoformat(),
                "ends_at_utc": (now + dt.timedelta(minutes=55)).isoformat()})
            i = r.json().get("id") if r.status_code in (200, 201) else None
            if i:
                creadas.append(i)
            return i

        H, P, F, Y = (await evento("G6HB Historia del arte", ids["prof"]), await evento("G6HB procesando", ids["prof"]),
                      await evento("G6HB fallida", ids["prof"]), await evento("G6HB clase ajena", ids["otro"]))
        check("se crean las clases de prueba", all((H, P, F, Y)))
        G = lambda h, sid, acc: c.post(f"/video/sessions/{sid}/grabacion/{acc}", headers=h)
        E = lambda h, sid: c.get(f"/video/sessions/{sid}/grabacion", headers=h)
        LIB = lambda h, **p: c.get("/video/grabaciones", headers=h, params=p)
        REP = lambda h, sid, rid: c.post(f"/video/sessions/{sid}/grabaciones/{rid}/reproducir", headers=h)
        eg = lambda sid: [k for k, v in L.egresos.items() if v["roomName"] == f"dorismon-{sid}"]
        rid_de = lambda e: base.filas("select id from video_recordings where egress_id=?", e)[0][0]

        async def subir(rid):
            b, k = base.filas("select storage_bucket, storage_key from video_recordings where id=?", rid)[0]
            httpx.put(f"{endpoint_s3}/_subir/{b}/{k}", content=CONTENIDO, headers={"Content-Type": "video/mp4"})

        async def grabar(sid, actor, hasta="ready", antes_de_parar=None):
            await asyncio.sleep(1.1)   # SQLite guarda requested_at con resolución de 1 s (PostgreSQL: µs)
            await G(actor, sid, "iniciar")
            e = eg(sid)[-1]
            L.egresos[e].update(status="EGRESS_ACTIVE", startedAt=str(int(time.time() * 1e9)))
            await E(actor, sid)
            if antes_de_parar:
                antes_de_parar()
            if hasta == "failed":
                L.egresos[e].update(status="EGRESS_FAILED", error="s3 upload failed: AccessDenied secret=x9")
                await E(actor, sid)
                return rid_de(e)
            await G(actor, sid, "detener")
            if hasta == "ready":
                L.egresos[e].update(status="EGRESS_COMPLETE", endedAt=str(int(time.time() * 1e9)),
                                    fileResults=[{"size": str(len(CONTENIDO)), "duration": str(1500 * 10**9)}])
                await E(actor, sid)
            return rid_de(e)

        cx = sqlite3.connect(ruta_db, timeout=30)
        try:
            # ---------- grupo de prueba: A (María) y B (Juana) dentro al empezar; C (Carlos) entra después
            cols = [r[1] for r in cx.execute("pragma table_info(enrollments)")]
            orig = {k: dict(zip(cols, cx.execute("select * from enrollments where student_id=? and is_active=1",
                                                 (ids[k],)).fetchone())) for k in ("maria", "juana", "carlos")}
            serie = str(uuid.uuid4())
            cx.execute("insert into class_series (id, name, course_id, level_id, teacher_id, days_of_week, start_time_hhmm,"
                       " duration_min, start_date, modality, capacity, is_active) values (?,?,?,?,?,?,?,?,date('now'),?,?,1)",
                       (serie, "G6HB grupo de prueba", orig["maria"]["course_id"], orig["maria"]["level_id"], ids["prof"],
                        "mon", "10:00", 60, "online", 10))
            borrar.append(("class_series", serie))
            insc = {}

            def inscribir(k):
                fila = dict(orig[k], id=str(uuid.uuid4()), series_id=serie, previous_enrollment_id=None)
                cx.execute(f"insert into enrollments ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                           [fila[x] for x in cols])
                cx.commit()
                insc[k] = fila["id"]
                borrar.insert(0, ("enrollments", fila["id"]))
            inscribir("maria")
            inscribir("juana")
            for sid in (H, P, F):
                cx.execute("update class_sessions set series_id=?, is_open_event=0, student_id=NULL where id=?", (serie, sid))
            cx.commit()

            print("\n  · Preparación: grabaciones reales por el flujo de 6H-A")
            try:
                r1 = await grabar(H, TH, antes_de_parar=lambda: inscribir("carlos"))     # C entra DURANTE la grabación
                r2 = await grabar(H, TH)                                                  # segundo segmento
                await subir(r1)
                await subir(r2)
                rp = await grabar(P, TH, hasta="processing")
                rf = await grabar(F, TH, hasta="failed")
                ry = await grabar(Y, AH)                                                  # clase de otro profesor
                await subir(ry)
                starts_antes = sum(1 for x in L.llamadas if x["metodo"] == "StartRoomCompositeEgress")
                estados = dict(base.filas("select id, status from video_recordings where id in (?,?,?,?,?)", r1, r2, rp, rf, ry))
                check(f"estados de partida: {sorted(estados.values())}",
                      [estados[r] for r in (r1, r2, rp, rf, ry)] == ["ready", "ready", "processing", "failed", "ready"])
                foto = {u for (u,) in base.filas("select user_id from video_recording_audience where recording_id=?", r1)}
                check("foto de la 1.ª grabación: A y B sí, C (entró durante la grabación) no",
                      ids["maria"] in foto and ids["juana"] in foto and ids["carlos"] not in foto)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Preparación: grabaciones reales por el flujo de 6H-A» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Estudiante (acceso histórico)")
            try:
                lm = (await LIB(MH)).json()
                respuestas.append(json.dumps(lm))
                mis = [i["id"] for i in lm["items"]]
                check(f"A ve SOLO las grabaciones listas de su clase ({len(mis)}): ambos segmentos",
                      set(mis) == {r1, r2} and lm["rol"] == "estudiante")
                check("recientes primero (segmento 2 antes que el 1)", mis == [r2, r1])
                it = {i["id"]: i for i in lm["items"]}
                check("segmentos comprensibles: «1 de 2» y «2 de 2»",
                      (it[r1]["parte"], it[r1]["partes"], it[r2]["parte"], it[r2]["partes"]) == (1, 2, 2, 2))
                check("cada fila trae título, profesor, fecha de la clase, duración y estado",
                      it[r1]["titulo"] == "G6HB Historia del arte" and it[r1]["profesor"] and it[r1]["clase_inicio"]
                      and it[r1]["duracion_segundos"] == 1500 and it[r1]["estado"] == "ready" and it[r1]["reproducible"])
                check("al estudiante NO se le muestran processing / failed / ajenas, ni bucket, clave, egress ni error",
                      not ({rp, rf, ry} & set(mis)) and not re.search(r"storage|bucket|egress|grabaciones/|error", json.dumps(lm)))
                lj = {i["id"] for i in (await LIB(JH)).json()["items"]}
                check("B también las ve", lj == {r1, r2})
                lc = (await LIB(CH)).json()
                check("C (entró al grupo DURANTE la 1.ª) NO ve la 1.ª; sí la 2.ª, que empezó cuando ya estaba",
                      {i["id"] for i in lc["items"]} == {r2})
                rc = await REP(CH, H, r1)
                check(f"C pide la URL directamente → {rc.status_code}, sin URL", rc.status_code == 404 and "url" not in rc.text)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Estudiante (acceso histórico)» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · URL firmada")
            try:
                r = await REP(MH, H, r1)
                respuestas.append(r.text)
                d = r.json()
                u = urlsplit(d.get("url", ""))
                q = dict(parse_qsl(u.query))
                check(f"A recibe una URL temporal ({r.status_code}, expira en {d.get('expira_en_segundos')} s)",
                      r.status_code == 200 and d.get("expira_en_segundos") == 600 and q.get("X-Amz-Expires") == "600")
                check("respuesta no cacheable (Cache-Control: no-store)", "no-store" in r.headers.get("cache-control", ""))
                check("firmada con la clave de LECTURA (no la de escritura de LiveKit), sin el secreto",
                      q.get("X-Amz-Credential", "").startswith(LECTURA_ID + "/") and base.ACCESO_R2 not in d["url"]
                      and LECTURA_SECRETO not in d["url"] and base.SECRETO_R2 not in d["url"])
                g = httpx.get(d["url"])
                check(f"el navegador descarga DIRECTO del almacenamiento con esa URL → {g.status_code}",
                      g.status_code == 200 and g.content == CONTENIDO)
                g = httpx.get(d["url"], headers={"Range": "bytes=100-299"})
                check(f"Range (como el reproductor) → {g.status_code} {g.headers.get('content-range')}",
                      g.status_code == 206 and g.content == CONTENIDO[100:300])
                sin_firma = httpx.get(f"{u.scheme}://{u.netloc}{u.path}")
                check(f"sin firma el bucket es privado → {sin_firma.status_code}", sin_firma.status_code == 403)
                otra = d["url"].replace(Path(u.path).name, rid_de(eg(Y)[-1]) + ".mp4")
                check("misma firma con otra clave de objeto → 403 (la firma no sirve para otro archivo)",
                      httpx.get(otra).status_code == 403)
                S3Stub.desfase = 601
                vencida = httpx.get(d["url"], headers={"Range": "bytes=0-99"})
                check(f"pasados 600 s una NUEVA petición Range con esa URL → {vencida.status_code} (Request has expired)",
                      vencida.status_code == 403 and b"expired" in vencida.content)
                S3Stub.desfase = 0          # la nueva URL se firma con la hora actual
                r = await REP(MH, H, r1)
                check("renovación: Dorismon da otra URL válida si sigue autorizado",
                      r.status_code == 200 and httpx.get(r.json()["url"], headers={"Range": "bytes=0-9"}).status_code == 206)
                S3Stub.desfase = 0
                todo_db = json.dumps(base.filas("select * from video_recordings"), default=str)
                check("la base NO guarda ninguna URL firmada ni firma", "X-Amz" not in todo_db and "Signature" not in todo_db
                      and "http" not in todo_db)
                r = await REP(MH, H, r2)
                check("el segundo segmento también se reproduce", r.status_code == 200)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «URL firmada» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · IDOR / session id cambiado")
            try:
                for etiqueta, sid, rid in (("recording_id de otra clase (existe)", Y, ry), ("rid propio con session_id de otra clase", Y, r1),
                                           ("rid ajeno con session_id propio", H, ry), ("session_id inexistente", "no-existe", r1),
                                           ("recording_id inexistente", H, "no-existe")):
                    rr = await REP(MH, sid, rid)
                    check(f"{etiqueta} → {rr.status_code} y sin URL", rr.status_code == 404 and "url" not in rr.json())
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «IDOR / session id cambiado» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Profesor y Dirección")
            try:
                lt = (await LIB(TH)).json()
                tid = {i["id"]: i for i in lt["items"]}
                check("profesor responsable: ve sus 4 grabaciones (también processing y failed), no la ajena",
                      set(tid) == {r1, r2, rp, rf} and lt["rol"] == "profesor")
                check(f"failed con error GENÉRICO para el profesor: «{tid[rf].get('error')}»",
                      tid[rf]["estado"] == "failed" and tid[rf]["error"].startswith("El proveedor no pudo") and "secret" not in tid[rf]["error"])
                check("processing: 'Procesando' y no reproducible", tid[rp]["estado_texto"] == "Procesando" and not tid[rp]["reproducible"])
                r = await REP(TH, P, rp)
                check(f"processing → {r.status_code}, sin URL (aunque la clave del objeto exista)", r.status_code == 409 and "url" not in r.text)
                r = await REP(TH, F, rf)
                check(f"failed → {r.status_code}, sin URL", r.status_code == 409 and "url" not in r.text)
                check("profesor responsable reproduce", (await REP(TH, H, r1)).status_code == 200)
                ro = await REP(OH, H, r1)
                check(f"profesor AJENO (solo por ser profesor) → {ro.status_code}", ro.status_code == 404)
                lo = (await LIB(OH)).json()
                check("profesor ajeno solo ve las de SUS clases", {i["id"] for i in lo["items"]} == {ry})
                la = (await LIB(AH)).json()
                check(f"Dirección ve todas ({la['total']}) con paginación (página {la['pagina']} de {la['paginas']}, {la['por_pagina']} por página)",
                      {r1, r2, rp, rf, ry} <= {i["id"] for i in la["items"]} and la["rol"] == "direccion" and la["por_pagina"] == 20)
                check("Dirección: filtro por estado", {i["estado"] for i in (await LIB(AH, estado="failed")).json()["items"]} == {"failed"})
                check("Dirección: filtro por profesor", {i["id"] for i in (await LIB(AH, profesor=ids["otro"])).json()["items"]} == {ry})
                check("Dirección: búsqueda por título", {i["id"] for i in (await LIB(AH, q="Historia del arte")).json()["items"]} == {r1, r2})
                check("Dirección: lista de profesores para el filtro", {p["id"] for p in la["profesores"]} >= {ids["prof"], ids["otro"]})
                check("Dirección reproduce", (await REP(AH, Y, ry)).status_code == 200)
                check("filtros no amplían al estudiante (estado=failed sigue dando solo ready)",
                      {i["estado"] for i in (await LIB(MH, estado="failed")).json()["items"]} == {"ready"})
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Profesor y Dirección» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Cambios posteriores: salir del grupo, cuenta, 6G-A")
            try:
                cx.execute("update enrollments set is_active=0 where id=?", (insc["maria"],))
                cx.commit()
                check("A sale del grupo DESPUÉS: conserva el acceso histórico (foto intacta)",
                      {i["id"] for i in (await LIB(MH)).json()["items"]} == {r1, r2} and (await REP(MH, H, r1)).status_code == 200)
                cx.execute("update users set is_active=0 where id=?", (ids["maria"],))
                cx.commit()
                try:
                    l403, r403 = await LIB(MH), await REP(MH, H, r1)
                    check(f"cuenta DESACTIVADA → sin biblioteca ({l403.status_code}) ni URL nueva ({r403.status_code})",
                          l403.status_code == 403 and r403.status_code == 403 and "url" not in r403.text)
                finally:
                    cx.execute("update users set is_active=1 where id=?", (ids["maria"],))
                    cx.commit()
                check("…la foto NO se borró", ids["maria"] in {u for (u,) in base.filas(
                    "select user_id from video_recording_audience where recording_id=?", r1)})
                bid = str(uuid.uuid4())
                cx.execute("insert into video_access_blocks (id, session_id, user_id, active, blocked_by, times_blocked) values (?,?,?,1,?,1)",
                           (bid, H, ids["juana"], ids["prof"]))
                cx.commit()
                borrar.insert(0, ("video_access_blocks", bid))
                # Actualizado en Gate 6H-B.1: el bloqueo 6G-A NO revoca la grabación (decisión de Dirección)
                check("sacada de la clase (bloqueo 6G-A activo) → SIGUE viéndola y reproduciéndola",
                      r1 in {i["id"] for i in (await LIB(JH)).json()["items"]} and (await REP(JH, H, r1)).status_code == 200)
                cx.execute("update video_access_blocks set active=0 where id=?", (bid,))
                cx.commit()
                check("…y con el bloqueo levantado, igual", (await REP(JH, H, r1)).status_code == 200)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Cambios posteriores: salir del grupo, cuenta, 6G-A» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Almacenamiento")
            try:
                S3Stub.modo = "caido"
                r = await REP(MH, H, r1)
                S3Stub.modo = "ok"
                check(f"almacenamiento caído → {r.status_code} con mensaje propio, sin URL", r.status_code == 503 and "url" not in r.json())
                k2 = base.filas("select storage_key from video_recordings where id=?", r2)[0][0]
                obj = S3Stub.objetos.pop((base.BUCKET, k2))
                r = await REP(MH, H, r2)
                S3Stub.objetos[(base.BUCKET, k2)] = obj
                check(f"objeto inexistente → {r.status_code} «{r.json().get('detail')}», sin URL", r.status_code == 404 and "url" not in r.json())
                os.environ["RECORDINGS_R2_BUCKET"] = ""
                r = await REP(TH, H, r1)
                os.environ["RECORDINGS_R2_BUCKET"] = base.BUCKET
                check(f"sin RECORDINGS_R2_BUCKET → {r.status_code}, nunca otro bucket", r.status_code == 503)
                check("ninguna grabación se firmó para el bucket general R2_BUCKET",
                      not any(p["ruta"].startswith("/dorismon/") for p in S3Stub.peticiones))
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Almacenamiento» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Rate limit y costes")
            try:
                rs = [await REP(CH, H, r1) for _ in range(30)]
                cods = [x.status_code for x in rs]
                check(f"30 peticiones de URL seguidas → {cods.count(429)}×429 con Retry-After",
                      cods.count(429) >= 5 and all(x.headers.get("retry-after") for x in rs if x.status_code == 429))
                check("abrir la biblioteca y reproducir NO crea Egress",
                      sum(1 for x in L.llamadas if x["metodo"] == "StartRoomCompositeEgress") == starts_antes)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Rate limit y costes» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Secretos")
            try:
                todo = " ".join(respuestas) + json.dumps(la) + json.dumps(lt)
                logs = "\n".join(registros)
                check("respuestas sin secretos (R2, lectura, LiveKit) ni clave de escritura",
                      not [s for s in (base.SECRETO_R2, LECTURA_SECRETO, secret, base.ACCESO_R2) if s in todo])
                check(f"logs ({len(registros)} líneas DEBUG) sin URL firmada, firma, credencial ni secretos",
                      len(registros) > 0 and not [s for s in ("X-Amz-Signature", "X-Amz-Credential", base.SECRETO_R2,
                                                              LECTURA_SECRETO, secret) if s in logs])
                check("el log de reproducción registra id de grabación y usuario", "reproducir: ok recording=" in logs)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Secretos» terminó sin errores ({type(e).__name__}: {e})", False)

        finally:
            for srv in (lk, s3):
                srv.shutdown()
            for tabla, i in borrar:
                cx.execute(f"delete from {tabla} where id=?", (i,))
            cx.commit()
            cx.close()
            n = base.limpiar(creadas)
            print(f"    · limpieza local: {n} sesión(es), {len(borrar)} filas de prueba")


def parte_estatica(check):
    print("\n[ESTÁTICO] Frontend")
    comp = WEB / "components/BibliotecaGrabaciones.tsx"
    if not comp.is_file():
        check("existe components/BibliotecaGrabaciones.tsx", False)
        return
    b = comp.read_text(encoding="utf-8")
    lay = (WEB / "app/dashboard/layout.tsx").read_text(encoding="utf-8")
    check("en el menú de los tres roles (estudiante, profesor, Dirección)",
          all(f'href: "/dashboard/{r}/grabaciones"' in lay for r in ("student", "teacher", "admin")))
    check("páginas de los tres roles usan el mismo componente",
          all('<BibliotecaGrabaciones' in (WEB / f"app/dashboard/{r}/grabaciones/page.tsx").read_text(encoding="utf-8")
              for r in ("student", "teacher", "admin")))
    check("carga compartida de Gate 5A (LoadingScreen), sin spinner propio", "<LoadingScreen" in b and "animate-spin" not in b)
    check("estado vacío del estudiante", "Aún no tienes grabaciones disponibles." in b)
    check("reproductor NATIVO (<video controls playsInline>) y velocidad", "<video" in b and "controls" in b
          and "playsInline" in b and "playbackRate" in b)
    codigo = re.sub(r"/\*.*?\*/|//[^\n]*", "", b, flags=re.S)       # sin comentarios
    check("sin botón / enlace de descarga y sin prometer protección anti-descarga",
          not re.search(r">\s*[^<]*Descargar", codigo) and " download" not in codigo and 'download=' not in codigo
          and not re.search(r"imposible de descargar|anti-?descarga|protegid[oa] contra", codigo, re.I))
    check("URL vencida: mensaje y renovación con límite (sin bucles)",
          "La sesión de reproducción venció. Estamos renovando el acceso." in b and "MAX_RENOVACIONES" in b)
    check("la URL firmada solo vive en memoria (no localStorage / sessionStorage)",
          "localStorage" not in b and "sessionStorage" not in b)
    check("polling lento SOLO mientras hay grabaciones en curso (≥ 15 s)",
          all(int(a + c) >= 15000 for a, c in re.findall(r"setInterval\(.*?,\s*(\d+)_?(\d{3})\)", b, re.S))
          and re.search(r"setInterval\(.*?,\s*\d+_?\d{3}\)", b, re.S) is not None and "if (!enCurso) return;" in b)
    sw = (WEB / "public/sw.js").read_text(encoding="utf-8")
    check("service worker (5B): otros orígenes (R2 / API) nunca se cachean",
          "if (url.origin !== self.location.origin) return;" in sw)


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    try:
        await parte_integracion(check)
    except Exception as e:
        check(f"parte de integración sin errores ({type(e).__name__}: {e})", False)
    parte_estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
