"""GATE 6H-B.1 — El bloqueo 6G-A no revoca la grabación.

[INTEGRACIÓN] App en proceso + simulador Egress (test_gate6ha) + simulador S3/R2 (test_gate6hb).
  Secuencia real: estudiante en la foto → el profesor la SACA durante la clase (/moderate
  remove, bloqueo 6G-A activo) → no puede volver a entrar (/join 403, sin cambios) → la clase
  se finaliza → la grabación llega a "ready" → la estudiante SIGUE pudiendo verla. El bloqueo
  queda intacto (activo, sin "Permitir volver") para auditoría.
  Además: fuera de la foto → 404; añadido después → sin acceso a la anterior; cuenta
  desactivada → 403; profesor ajeno → 404; Dirección → acceso; estudiante solo ve "ready".
PENDIENTE DE VERIFICACIÓN CON R2 REAL y LiveKit Cloud (simuladores locales).

Uso: python tests/test_gate6hb1.py [BASE_URL]
"""
import asyncio
import datetime as dt
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import test_gate6ha as base  # noqa: E402
import test_gate6hb as b6  # noqa: E402


async def integracion(check):
    print("\n[INTEGRACIÓN] Bloqueo 6G-A ≠ revocación de la grabación (simuladores locales)")
    L, S3 = base._LK, b6.S3Stub
    L.llamadas, L.egresos, L.modo_inicio, L.modo_stop, L.n = [], {}, "ok", "ok", 0
    lk, s3 = ThreadingHTTPServer(("127.0.0.1", 0), L), ThreadingHTTPServer(("127.0.0.1", 0), S3)
    for srv in (lk, s3):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    S3.acceso = {b6.LECTURA_ID: b6.LECTURA_SECRETO, base.ACCESO_R2: base.SECRETO_R2}
    S3.objetos, S3.peticiones, S3.desfase, S3.modo = {}, [], 0, "ok"
    ep = f"http://127.0.0.1:{s3.server_address[1]}"
    key = os.environ.get("LIVEKIT_API_KEY") or "APIprueba"
    secret = os.environ.get("LIVEKIT_API_SECRET") or "secretodepruebaparatest123456789"
    os.environ.update({"LIVEKIT_URL": f"ws://127.0.0.1:{lk.server_address[1]}", "LIVEKIT_API_KEY": key,
                       "LIVEKIT_API_SECRET": secret, "RECORDINGS_R2_BUCKET": base.BUCKET,
                       "R2_ACCOUNT_ID": "cuentaprueba6ha", "R2_ACCESS_KEY": base.ACCESO_R2, "R2_SECRET_KEY": base.SECRETO_R2,
                       "RECORDINGS_S3_ENDPOINT": ep, "RECORDINGS_R2_READ_ACCESS_KEY": b6.LECTURA_ID,
                       "RECORDINGS_R2_READ_SECRET_KEY": b6.LECTURA_SECRETO})
    from app.main import app
    from app.core.db import engine
    from app.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    cx = sqlite3.connect(str(Path(base.DB_URL.split("///", 1)[-1])), timeout=30)
    creadas, borrar = [], []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(cred):
            return {"Authorization": f"Bearer {(await c.post('/auth/login', json=cred)).json()['access_token']}"}
        AH, TH, OH, MH, JH, CH = [await login(x) for x in (base.ADMIN, base.TEACHER, base.OTRO_PROFE, base.MARIA, b6.JUANA, base.CARLOS)]
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

        H, Y = await evento("G6HB1 clase con expulsión", ids["prof"]), await evento("G6HB1 clase ajena", ids["otro"])
        G = lambda h, sid, acc: c.post(f"/video/sessions/{sid}/grabacion/{acc}", headers=h)
        E = lambda h, sid: c.get(f"/video/sessions/{sid}/grabacion", headers=h)
        LIB = lambda h: c.get("/video/grabaciones", headers=h)
        REP = lambda h, sid, rid: c.post(f"/video/sessions/{sid}/grabaciones/{rid}/reproducir", headers=h)
        JOIN = lambda h, sid: c.post(f"/video/sessions/{sid}/join", headers=h)
        eg = lambda sid: [k for k, v in L.egresos.items() if v["roomName"] == f"dorismon-{sid}"]
        ids_lib = lambda h: c.get("/video/grabaciones", headers=h)
        try:
            cols = [r[1] for r in cx.execute("pragma table_info(enrollments)")]
            orig = {k: dict(zip(cols, cx.execute("select * from enrollments where student_id=? and is_active=1",
                                                 (ids[k],)).fetchone())) for k in ("maria", "juana", "carlos")}
            serie = str(uuid.uuid4())
            cx.execute("insert into class_series (id, name, course_id, level_id, teacher_id, days_of_week, start_time_hhmm,"
                       " duration_min, start_date, modality, capacity, is_active) values (?,?,?,?,?,?,?,?,date('now'),?,?,1)",
                       (serie, "G6HB1 grupo de prueba", orig["maria"]["course_id"], orig["maria"]["level_id"], ids["prof"],
                        "mon", "10:00", 60, "online", 10))
            borrar.append(("class_series", serie))

            def inscribir(k):
                fila = dict(orig[k], id=str(uuid.uuid4()), series_id=serie, previous_enrollment_id=None)
                cx.execute(f"insert into enrollments ({','.join(cols)}) values ({','.join('?' * len(cols))})", [fila[x] for x in cols])
                cx.commit()
                borrar.insert(0, ("enrollments", fila["id"]))
            inscribir("maria")
            inscribir("juana")
            cx.execute("update class_sessions set series_id=?, is_open_event=0, student_id=NULL where id=?", (serie, H))
            cx.commit()
            check("se preparan la clase y el grupo (María y Juana)", bool(H and Y))

            print("\n  · Durante la clase")
            for h in (TH, MH, JH):
                await JOIN(h, H)
            await G(TH, H, "iniciar")
            e1 = eg(H)[-1]
            L.egresos[e1].update(status="EGRESS_ACTIVE", startedAt=str(int(time.time() * 1e9)))
            await E(TH, H)
            rid = base.filas("select id from video_recordings where egress_id=?", e1)[0][0]
            foto = {u for (u,) in base.filas("select user_id from video_recording_audience where recording_id=?", rid)}
            check("1. María está en la foto de la grabación", ids["maria"] in foto)
            check("   mientras graba, la estudiante NO la ve en su biblioteca (solo 'ready')",
                  rid not in {i["id"] for i in (await LIB(MH)).json()["items"]})
            r = await REP(MH, H, rid)
            check(f"   …ni recibe URL ({r.status_code})", r.status_code != 200 and "url" not in r.text)
            r = await c.post(f"/video/sessions/{H}/moderate", headers=TH, json={"action": "remove", "identity": ids["maria"]})
            check(f"2. el profesor la SACA durante la clase (/moderate remove → {r.status_code})",
                  r.status_code == 200 and r.json().get("bloqueado") is True)
            blq = base.filas("select active, unblocked_at from video_access_blocks where session_id=? and user_id=?", H, ids["maria"])
            check("3. el bloqueo 6G-A queda ACTIVO", blq == [(1, None)])
            rj = await JOIN(MH, H)
            check(f"   /join sin cambios: la bloqueada NO puede volver a la videoclase ({rj.status_code})",
                  rj.status_code == 403 and "token" not in rj.text)
            inscribir("carlos")                     # se añade DESPUÉS de empezar la grabación

            print("\n  · Fin de clase y grabación lista")
            r = await c.post(f"/teacher/sessions/{H}/finalize", headers=TH)
            check(f"4. la clase termina (Finalizar → {r.status_code})", r.status_code == 200)
            check("   StopEgress pedido al finalizar", any(x["metodo"] == "StopEgress" for x in L.llamadas))
            L.egresos[e1].update(status="EGRESS_COMPLETE", endedAt=str(int(time.time() * 1e9)),
                                 fileResults=[{"size": str(len(b6.CONTENIDO)), "duration": str(900 * 10**9)}])
            await E(TH, H)
            check("5. la grabación llega a 'ready'", base.filas("select status from video_recordings where id=?", rid)[0][0] == "ready")
            bk, k = base.filas("select storage_bucket, storage_key from video_recordings where id=?", rid)[0]
            httpx.put(f"{ep}/_subir/{bk}/{k}", content=b6.CONTENIDO, headers={"Content-Type": "video/mp4"})

            print("\n  · Acceso a la grabación")
            check("6. la estudiante sacada de la clase LA VE en su biblioteca",
                  rid in {i["id"] for i in (await LIB(MH)).json()["items"]})
            r = await REP(MH, H, rid)
            ok = r.status_code == 200 and httpx.get(r.json()["url"], headers={"Range": "bytes=0-99"}).status_code == 206
            check(f"6. …y la REPRODUCE ({r.status_code}, URL firmada válida)", ok)
            blq = base.filas("select active, unblocked_at, unblocked_by from video_access_blocks where session_id=? and user_id=?", H, ids["maria"])
            check("   el bloqueo se CONSERVA (activo, sin 'Permitir volver' automático) para auditoría", blq == [(1, None, None)])
            check("   y la auditoría de la expulsión sigue ahí",
                  bool(base.filas("select 1 from audit_logs where action='video_remove' and target_id=?", H)))
            check("Juana (en la foto, no sacada) también la ve", (await REP(JH, H, rid)).status_code == 200)
            rc = await REP(CH, H, rid)
            check(f"Carlos (añadido DESPUÉS) sigue sin acceso a la anterior ({rc.status_code})",
                  rc.status_code == 404 and rid not in {i["id"] for i in (await LIB(CH)).json()["items"]})
            # grabación de otra clase donde María NO está en la foto
            for h in (OH,):
                await JOIN(h, Y)
            await G(AH, Y, "iniciar")
            e2 = eg(Y)[-1]
            L.egresos[e2].update(status="EGRESS_ACTIVE")
            await E(AH, Y)
            await G(AH, Y, "detener")
            L.egresos[e2].update(status="EGRESS_COMPLETE", fileResults=[{"size": "100", "duration": "1000000000"}])
            await E(AH, Y)
            ry = base.filas("select id from video_recordings where egress_id=?", e2)[0][0]
            r = await REP(MH, Y, ry)
            check(f"estudiante FUERA de la foto → {r.status_code} y fuera de su biblioteca",
                  r.status_code == 404 and ry not in {i["id"] for i in (await LIB(MH)).json()["items"]})
            ro = await REP(OH, H, rid)
            check(f"profesor ajeno → {ro.status_code}", ro.status_code == 404)
            check("Dirección → acceso", (await REP(AH, H, rid)).status_code == 200)
            check("profesor responsable → acceso", (await REP(TH, H, rid)).status_code == 200)
            cx.execute("update users set is_active=0 where id=?", (ids["maria"],))
            cx.commit()
            try:
                r1, r2 = await LIB(MH), await REP(MH, H, rid)
                check(f"cuenta desactivada → 403 ({r1.status_code}, {r2.status_code}) sin URL",
                      r1.status_code == 403 and r2.status_code == 403 and "url" not in r2.text)
            finally:
                cx.execute("update users set is_active=1 where id=?", (ids["maria"],))
                cx.commit()
            check("reactivada → vuelve a verla (la foto no se tocó)", (await REP(MH, H, rid)).status_code == 200)
        finally:
            for srv in (lk, s3):
                srv.shutdown()
            for tabla, i in borrar:
                cx.execute(f"delete from {tabla} where id=?", (i,))
            cx.commit()
            cx.close()
            n = base.limpiar(creadas)
            print(f"    · limpieza local: {n} sesión(es), {len(borrar)} filas de prueba")


def estatica(check):
    print("\n[ESTÁTICO]")
    t = (ROOT / "app/services/biblioteca_grabaciones.py").read_text(encoding="utf-8")
    codigo = "\n".join(l for l in t.splitlines() if not l.strip().startswith("#"))
    check("la autorización de grabaciones ya no consulta video_access_blocks / bloqueo_activo",
          "bloqueo_activo" not in codigo and "VideoAccessBlock" not in codigo)
    v = (ROOT / "app/routers/video.py").read_text(encoding="utf-8")
    check("/join sigue comprobando el bloqueo 6G-A", "bloqueo_activo" in v and "MENSAJE_RETIRADO" in v)


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    try:
        await integracion(check)
    except Exception as e:
        check(f"integración sin errores ({type(e).__name__}: {e})", False)
    estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
