"""GATE 6H-A.1 — Cierre de hardening de grabaciones.

[RESTRICCIÓN] una grabación activa POR CLASE (no global): UNIQUE(activa) donde activa =
    session_id mientras ocupa la sala y NULL al terminar. DDL SQLite real y PostgreSQL compilado.
[INTEGRACIÓN] app en proceso + simulador de Egress (de test_gate6ha; NO es LiveKit Cloud):
    · 10 + 10 "iniciar" a la vez en dos clases → 1 Egress en cada una, sin 500;
    · A y B graban a la vez; A detiene y graba otro segmento; B no se entera;
    · errores del proveedor con secretos FICTICIOS → ni en la base, ni en logs, ni en la API;
    · audiencia histórica: congelada al iniciar; quien se inscribe después NO entra sola;
      retirar después no reescribe la foto.
[FRONTEND] sin URL por defecto hacia producción (next.config.js exige NEXT_PUBLIC_API_URL al
    compilar): local sin variable → error claro; producción / preview de Vercel sin variable →
    error claro; producción configurada → esa URL; ningún fallback a dorismon-api.onrender.com.

Uso: python tests/test_gate6ha1.py [BASE_URL] [RUTA_DORISMON_WEB] [RUTA_NODE_MODULES_DE_NEXT]
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_gate6ha as base  # noqa: E402  (simulador de Egress, usuarios, limpieza)

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
# Actualizado en Gate 6H-B: carpeta con node_modules de Next también por variable de entorno
NEXT_DIR = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(os.environ.get("DORISMON_NEXT_DIR") or WEB)
PROD = "dorismon-api.onrender.com"
JWT_FICTICIO = "eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJmaWN0aWNpbyJ9.firmaFicticia_6ha1"
FIRMA_URL = "f1c71c1a0000deadbeef6ha1f1c71c1a0000deadbeef6ha1"


def parte_restriccion(check):
    print("\n[RESTRICCIÓN] Grabación activa por clase")
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import UniqueConstraint
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable
    from app.models import VideoRecording
    uqs = [(c.name, [x.name for x in c.columns]) for c in VideoRecording.__table__.constraints
           if isinstance(c, UniqueConstraint)]
    check(f"restricciones únicas del modelo: {uqs}", ("uq_video_recording_activa", ["activa"]) in uqs)
    check("activa admite NULL (las grabaciones terminadas no ocupan nada)", VideoRecording.__table__.c.activa.nullable)
    check("NO hay unicidad sobre session_id (varias grabaciones por clase en el tiempo)",
          not any(cols == ["session_id"] for _, cols in uqs))
    pg = str(CreateTable(VideoRecording.__table__).compile(dialect=postgresql.dialect()))
    check("PostgreSQL: CONSTRAINT uq_video_recording_activa UNIQUE (activa)",
          "CONSTRAINT uq_video_recording_activa UNIQUE (activa)" in pg)
    sq = base.filas("select sql from sqlite_master where name='video_recordings'")
    check("SQLite real: CONSTRAINT uq_video_recording_activa UNIQUE (activa)",
          bool(sq) and "CONSTRAINT uq_video_recording_activa UNIQUE (activa)" in sq[0][0])


async def parte_integracion(check):
    print("\n[INTEGRACIÓN] App en proceso + simulador de Egress (NO es LiveKit Cloud)")
    L = base._LK
    L.llamadas, L.egresos, L.modo_inicio, L.modo_stop, L.n = [], {}, "ok", "ok", 0
    srv = ThreadingHTTPServer(("127.0.0.1", 0), L)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    key = os.environ.get("LIVEKIT_API_KEY") or "APIprueba"
    secret = os.environ.get("LIVEKIT_API_SECRET") or "secretodepruebaparatest123456789"
    os.environ.update({"LIVEKIT_URL": f"ws://127.0.0.1:{srv.server_address[1]}", "LIVEKIT_API_KEY": key,
                       "LIVEKIT_API_SECRET": secret, "RECORDINGS_R2_BUCKET": base.BUCKET,
                       "R2_ACCOUNT_ID": "cuentaprueba6ha", "R2_ACCESS_KEY": base.ACCESO_R2,
                       "R2_SECRET_KEY": base.SECRETO_R2})
    from app.main import app            # basicConfig(force=True): la captura de logs va DESPUÉS
    from app.core.db import engine
    from app.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    registros = []

    class _Captura(logging.Handler):
        def emit(self, r):
            registros.append(self.format(r))
    cap = _Captura()
    cap.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(cap)
    logging.getLogger().setLevel(logging.DEBUG)

    creadas, respuestas, restaurar = [], [], []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(cred):
            return {"Authorization": f"Bearer {(await c.post('/auth/login', json=cred)).json()['access_token']}"}
        AH, TH, MH, CH = [await login(x) for x in (base.ADMIN, base.TEACHER, base.MARIA, base.CARLOS)]
        ids = {k: (await c.get("/auth/me", headers=h)).json()["id"]
               for k, h in (("prof", TH), ("maria", MH), ("carlos", CH))}
        import datetime as dt
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

        A, B, F, H = [await evento(f"G6HA1 {n}") for n in ("A", "B", "fallos", "historica")]
        check("se crean las clases de prueba", all((A, B, F, H)))
        G = lambda h, sid, acc: c.post(f"/video/sessions/{sid}/grabacion/{acc}", headers=h)
        E = lambda h, sid: c.get(f"/video/sessions/{sid}/grabacion", headers=h)
        eg = lambda sid: [k for k, v in L.egresos.items() if v["roomName"] == f"dorismon-{sid}"]
        estado = lambda eid: base.filas("select status, activa from video_recordings where egress_id=?", eid)[0]
        try:
            try:
                print("\n  · Dos clases a la vez")
                rs = await asyncio.gather(*[G(TH, A, "iniciar") for _ in range(10)], *[G(AH, B, "iniciar") for _ in range(10)])
                respuestas.extend(r.text for r in rs)
                cods = sorted({r.status_code for r in rs})
                check(f"20 'iniciar' simultáneos (10 en A + 10 en B) → todos 200, ningún 500 ({cods})", cods == [200])
                check(f"exactamente 1 Egress en A ({len(eg(A))}) y 1 en B ({len(eg(B))})", len(eg(A)) == 1 and len(eg(B)) == 1)
                ea, eb = eg(A)[0], eg(B)[0]
                for e in (ea, eb):
                    L.egresos[e]["status"] = "EGRESS_ACTIVE"
                ga, gb = (await E(TH, A)).json()["grabacion"]["estado"], (await E(TH, B)).json()["grabacion"]["estado"]
                check(f"A y B graban AL MISMO TIEMPO ({ga}, {gb})", ga == gb == "recording")
                check("en la base: dos filas activas, cada una con SU clase en 'activa'",
                      estado(ea) == ("recording", A) and estado(eb) == ("recording", B))
                rs = await asyncio.gather(*[G(TH, A, "detener") for _ in range(10)], *[G(TH, B, "iniciar") for _ in range(10)])
                respuestas.extend(r.text for r in rs)
                check(f"10 'detener' en A + 10 'iniciar' en B a la vez → ningún 500 ({sorted({r.status_code for r in rs})})",
                      all(r.status_code == 200 for r in rs))
                stops = [x for x in L.llamadas if x["metodo"] == "StopEgress"]
                check(f"UN StopEgress, el de A ({[s['cuerpo']['egressId'] for s in stops]})",
                      len(stops) == 1 and stops[0]["cuerpo"]["egressId"] == ea)
                check("B no se entera: sigue grabando con la misma Egress", estado(eb) == ("recording", B) and len(eg(B)) == 1)
                L.egresos[ea].update({"status": "EGRESS_COMPLETE", "fileResults": [{"size": "1000", "duration": "5000000000"}]})
                await E(TH, A)
                check("A termina (ready) y libera SU clase (activa NULL)", estado(ea) == ("ready", None))
                r = await G(AH, A, "iniciar")   # Dirección: el profesor ya usó 20 peticiones en A (límite 6G-D)
                check("A puede grabar un SEGUNDO segmento (nueva Egress)", r.json().get("resultado") == "iniciada" and len(eg(A)) == 2)
                check("…y B sigue intacto", estado(eb) == ("recording", B))
            except Exception as e:
                check(f"sección «Dos clases a la vez» terminó sin errores ({type(e).__name__}: {e})", False)

            try:
                print("\n  · Errores del proveedor con secretos ficticios")
                sucio = (f"s3 upload failed: AccessDenied PUT https://cuenta.r2.cloudflarestorage.com/{base.BUCKET}/x.mp4"
                         f"?X-Amz-Credential={base.ACCESO_R2}%2F20261011&X-Amz-Signature={FIRMA_URL} "
                         f"Authorization: Bearer {JWT_FICTICIO} secret={base.SECRETO_R2} "
                         f"api_secret={secret} password=hunter2-6ha1 sin-etiqueta:{secret} y {base.SECRETO_R2}")
                prohibidos = [base.SECRETO_R2, base.ACCESO_R2, secret, JWT_FICTICIO, FIRMA_URL, "hunter2-6ha1",
                              "X-Amz-Signature=f1c7"]
                await G(TH, F, "iniciar")
                e1 = eg(F)[-1]
                L.egresos[e1].update({"status": "EGRESS_FAILED", "errorCode": 403, "error": sucio})
                g = (await E(TH, F)).json()["grabacion"]
                respuestas.append(json.dumps(g))
                db1 = base.filas("select error_message, error_code from video_recordings where egress_id=?", e1)[0]
                check("(ListEgress) en la base NO queda ningún secreto, token, JWT ni firma de URL",
                      not [p for p in prohibidos if p in (db1[0] or "")])
                check(f"…pero sí un diagnóstico útil y el código ({db1[1]}): «{(db1[0] or '')[:70]}…»",
                      "s3 upload failed: AccessDenied" in db1[0] and "[REDACTADO]" in db1[0] and db1[1] == 403)
                check("…sin URL firmada completa (la consulta queda redactada)", "?[REDACTADO]" in db1[0] and "X-Amz" not in db1[0])
                check("al profesor: SOLO el mensaje genérico", g["estado"] == "failed"
                      and g["error"] == "El proveedor no pudo completar la grabación (p. ej. no pudo guardar el archivo).")
                # webhook con el mismo tipo de error (otra grabación)
                await G(TH, F, "iniciar")
                e2 = eg(F)[-1]
                L.egresos[e2]["status"] = "EGRESS_ACTIVE"
                await E(TH, F)
                ev = json.dumps({"event": "egress_ended", "egressInfo": {"egressId": e2, "roomName": f"dorismon-{F}",
                                 "status": "EGRESS_FAILED", "errorCode": 500, "error": sucio}}).encode()
                rw = await c.post("/video/livekit/webhook", content=ev,
                                  headers={"Authorization": base.firmar(ev, key, secret), "Content-Type": "application/webhook+json"})
                db2 = base.filas("select error_message, status from video_recordings where egress_id=?", e2)[0]
                check("(webhook) aplicado y en la base NO queda ningún secreto",
                      rw.status_code == 200 and db2[1] == "failed" and not [p for p in prohibidos if p in (db2[0] or "")])
                # error del proveedor que empieza por "LiveKit" (no se confunde con un mensaje propio)
                await G(TH, F, "iniciar")
                e3 = eg(F)[-1]
                L.egresos[e3].update({"status": "EGRESS_FAILED", "error": "LiveKit internal: token=abc123secretish"})
                g3 = (await E(TH, F)).json()["grabacion"]
                respuestas.append(json.dumps(g3))
                check("un error del proveedor que empieza por 'LiveKit' también se muestra genérico",
                      g3["error"].startswith("El proveedor no pudo") and "abc123" not in g3["error"])
                await G(TH, F, "iniciar")
                e4 = eg(F)[-1]
                L.egresos[e4].update({"status": "EGRESS_FAILED", "error": "error de subida " * 200})
                await E(TH, F)
                check("mensaje enorme → recortado a ≤ 300 caracteres",
                      len(base.filas("select error_message from video_recordings where egress_id=?", e4)[0][0]) <= 300)
                logs = "\n".join(registros)
                check(f"ningún log contiene los secretos ({len(registros)} líneas, nivel DEBUG)",
                      len(registros) > 0 and not [p for p in prohibidos if p in logs])
                check("el log de diagnóstico existe y va redactado",
                      any("falló (código 403)" in x and "[REDACTADO]" in x for x in registros))
                check("ninguna respuesta de la API contiene los secretos",
                      not [p for p in prohibidos if p in " ".join(respuestas)])
            except Exception as e:
                check(f"sección «Secretos» terminó sin errores ({type(e).__name__}: {e})", False)

            try:
                print("\n  · Audiencia histórica (autoridad de acceso para 6H-B)")
                # La semilla no tiene grupos: se crea uno SOLO para esta prueba (se borra al final),
                # con una inscripción de María copiada de la suya (nueva fila; la original no se toca).
                import sqlite3
                ruta = str(Path(base.DB_URL.split("///", 1)[-1]))
                cx = sqlite3.connect(ruta, timeout=30)
                cols = [r[1] for r in cx.execute("pragma table_info(enrollments)")]
                fila_orig = dict(zip(cols, cx.execute("select * from enrollments where student_id=? and is_active=1",
                                                      (ids["maria"],)).fetchone()))
                serie = str(uuid.uuid4())
                cx.execute("insert into class_series (id, name, course_id, level_id, teacher_id, days_of_week, start_time_hhmm,"
                           " duration_min, start_date, modality, capacity, is_active) values (?,?,?,?,?,?,?,?,date('now'),?,?,1)",
                           (serie, "G6HA1 grupo de prueba", fila_orig["course_id"], fila_orig["level_id"], ids["prof"],
                            "mon", "10:00", 60, "online", 10))
                restaurar.append(("delete from class_series where id=?", (serie,)))
                fila_m = dict(fila_orig, id=str(uuid.uuid4()), series_id=serie, previous_enrollment_id=None)
                cx.execute(f"insert into enrollments ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                           [fila_m[k] for k in cols])
                restaurar.insert(0, ("delete from enrollments where id=?", (fila_m["id"],)))
                cx.execute("update class_sessions set series_id=?, is_open_event=0, student_id=NULL where id=?", (serie, H))
                cx.commit()
                otros = base.filas("select student_id from enrollments where series_id=? and is_active=1", serie)
                check("grupo de prueba: María dentro, Carlos fuera", {x for (x,) in otros} == {ids["maria"]})
                await G(TH, H, "iniciar")
                rid = base.filas("select id from video_recordings where session_id=? order by requested_at desc", H)[0][0]
                foto = lambda: dict(base.filas("select user_id, reason from video_recording_audience where recording_id=?", rid))
                f0 = foto()
                check(f"María (con derecho al crear la grabación) queda en la foto ({f0.get(ids['maria'])})",
                      f0.get(ids["maria"]) == "derecho" and ids["prof"] not in f0)
                # Carlos se inscribe DESPUÉS (copia de la inscripción de María, nueva fila; se borra al final)
                nueva = dict(fila_m, id=str(uuid.uuid4()), student_id=ids["carlos"], previous_enrollment_id=None)
                cx.execute(f"insert into enrollments ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                           [nueva[k] for k in cols])
                cx.commit()
                restaurar.insert(0, ("delete from enrollments where id=?", (nueva["id"],)))
                e5 = eg(H)[-1]
                L.egresos[e5]["status"] = "EGRESS_ACTIVE"
                await E(TH, H)
                check("inscrito después → NO aparece mientras graba", ids["carlos"] not in foto())
                await G(TH, H, "detener")
                check("inscrito después → tampoco al DETENER (antes 6H-A lo añadía: defecto corregido)",
                      ids["carlos"] not in foto())
                # retirar a María después
                cx.execute("update enrollments set is_active=0 where id=?", (fila_m["id"],))   # fila de la prueba
                cx.commit()
                L.egresos[e5].update({"status": "EGRESS_COMPLETE", "fileResults": [{"size": "10", "duration": "1000000000"}]})
                await E(TH, H)
                check("retirar a María después NO reescribe la foto (sigue con su motivo)", foto().get(ids["maria"]) == "derecho")
                cx.close()
            except Exception as e:
                check(f"sección «Audiencia histórica» terminó sin errores ({type(e).__name__}: {e})", False)
        finally:
            srv.shutdown()
            if restaurar:
                import sqlite3
                cx = sqlite3.connect(str(Path(base.DB_URL.split("///", 1)[-1])), timeout=30)
                for sql, args in restaurar:
                    cx.execute(sql, args)
                cx.commit()
                cx.close()
            n = base.limpiar(creadas)
            print(f"    · limpieza local: {n} sesión(es); filas de prueba borradas: {len(restaurar)}")


def parte_frontend(check):
    print("\n[FRONTEND] Nunca hacia producción por accidente")
    fuentes = {p: p.read_text(encoding="utf-8", errors="ignore") for d in ("lib", "app", "components")
               for p in (WEB / d).rglob("*") if p.suffix in (".ts", ".tsx", ".js")}
    con_prod = [str(p.relative_to(WEB)) for p, t in fuentes.items() if PROD in t]
    check(f"ningún archivo del frontend contiene {PROD} como valor por defecto ({con_prod})", not con_prod)
    cfg = (WEB / "next.config.js").read_text(encoding="utf-8")
    check("next.config.js no contiene ninguna URL de la API", PROD not in cfg and "127.0.0.1" not in cfg.split("Defínela")[0])
    script = r"""
const path = require('path');
const cfgPath = path.resolve(process.argv[1]);
const {PHASE_PRODUCTION_BUILD:B, PHASE_DEVELOPMENT_SERVER:D, PHASE_PRODUCTION_SERVER:P} = require('next/constants');
const caso = (env, fase) => {
  for (const k of ['NEXT_PUBLIC_API_URL','VERCEL','VERCEL_ENV']) delete process.env[k];
  Object.assign(process.env, env); delete require.cache[cfgPath];
  try { const c = require(cfgPath)(fase); return {ok: true, env: c.env}; } catch (e) { return {ok: false, msg: e.message}; }
};
console.log(JSON.stringify({
  local_build: caso({}, B), local_dev: caso({}, D), start: caso({}, P),
  vercel_prod: caso({VERCEL:'1', VERCEL_ENV:'production'}, B), vercel_preview: caso({VERCEL:'1', VERCEL_ENV:'preview'}, B),
  prod_ok: caso({VERCEL:'1', VERCEL_ENV:'production', NEXT_PUBLIC_API_URL:'https://api-prueba.dorismon.test'}, B),
  preview_ok: caso({VERCEL:'1', VERCEL_ENV:'preview', NEXT_PUBLIC_API_URL:'https://api-prueba.dorismon.test'}, B),
  prod_http: caso({VERCEL:'1', VERCEL_ENV:'production', NEXT_PUBLIC_API_URL:'http://api-prueba.dorismon.test'}, B),
  local_ok: caso({NEXT_PUBLIC_API_URL:'http://127.0.0.1:8000'}, B),
}));
"""
    try:
        out = subprocess.run(["node", "-e", script, str(WEB / "next.config.js")], cwd=NEXT_DIR,
                             env=dict(os.environ, NODE_PATH=str(NEXT_DIR / "node_modules")),
                             capture_output=True, text=True, timeout=60)
        r = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as e:
        check(f"next.config.js se evalúa con Node ({type(e).__name__}: {e})", False)
        return
    falta = lambda x: (not x["ok"]) and "Falta NEXT_PUBLIC_API_URL" in x["msg"] and "No existe URL por defecto" in x["msg"]
    check("build LOCAL sin la variable → error claro (no hay URL por defecto)", falta(r["local_build"]))
    check("next dev LOCAL sin la variable → error claro", falta(r["local_dev"]))
    check("producción de Vercel sin la variable → error claro (fail closed)", falta(r["vercel_prod"]))
    check("preview de Vercel sin la variable → error claro (fail closed)", falta(r["vercel_preview"]))
    check("producción configurada (https) → compila", r["prod_ok"]["ok"])
    check("producción con http:// → error", not r["prod_http"]["ok"] and "https" in r["prod_http"]["msg"])
    check("local configurado → compila; next start no se ve afectado", r["local_ok"]["ok"] and r["start"]["ok"])
    check("Gate 5B intacto: SW solo en producción de Vercel; preview sin SW; solo 2 variables expuestas",
          r["prod_ok"].get("env") == {"NEXT_PUBLIC_SW_ENABLED": "1", "NEXT_PUBLIC_APP_VERSION": "local"}
          and (r["preview_ok"].get("env") or {}).get("NEXT_PUBLIC_SW_ENABLED") == "0"
          and (r["local_ok"].get("env") or {}).get("NEXT_PUBLIC_SW_ENABLED") == "0")


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    try:
        parte_restriccion(check)
    except Exception as e:
        check(f"parte de restricción sin errores ({type(e).__name__}: {e})", False)
    try:
        await parte_integracion(check)
    except Exception as e:
        check(f"parte de integración sin errores ({type(e).__name__}: {e})", False)
    parte_frontend(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
