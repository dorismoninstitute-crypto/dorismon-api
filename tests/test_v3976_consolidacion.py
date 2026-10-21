"""v3.9.76 — Consolidación: D1, D2 y D3 (encontrados con PostgreSQL real en PREPROD-3).

D1  Borrar una serie CON matrículas (future_only=false) → 409, sin tocar nada. En PostgreSQL la FK
    lo rechazaba con 500; en SQLite (sin FK) dejaba matrículas huérfanas. Una serie SIN matrículas
    se sigue pudiendo eliminar como hasta ahora.
D2  Dirección entra a un EVENTO ABIERTO sin inscribirse como estudiante (en PostgreSQL: 500 por FK).
D3  Añadir a la foto de audiencia de una grabación a la vez (varios "detener") no falla ni duplica.
[INTEGRACIÓN] App en este proceso (base SQLite local de la suite). Lo decisivo de D3 —concurrencia
    real— se verifica además en PostgreSQL 18 (PREPROD-3 / consolidación).

Uso: python tests/test_v3976_consolidacion.py [BASE_URL]
"""
import asyncio
import datetime as dt
import json
import os
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}


class _LK(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        metodo = self.path.rsplit("/", 1)[-1]
        cuerpo = {"participants": []} if metodo == "ListParticipants" else {"rooms": []} if metodo == "ListRooms" else {}
        b = json.dumps(cuerpo).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


async def parte_integracion(check):
    print("\n[INTEGRACIÓN] App en proceso")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _LK)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ.update({"LIVEKIT_URL": f"ws://127.0.0.1:{srv.server_address[1]}",
                       "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY") or "APIprueba",
                       "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET") or "secretodepruebaparatest123456789"})
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import select, func
    from app.main import app
    from app.core.db import SessionLocal, engine
    from app.models import (Base, User, Enrollment, ClassSeries, ClassSession, SessionStatus, EventRegistration,
                            VideoRecording, VideoRecordingAudience, VideoPresence)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    ahora = dt.datetime.now(dt.timezone.utc)
    creado = {"series": [], "sesiones": [], "matriculas": [], "grabaciones": []}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        AH = {"Authorization": "Bearer " + (await c.post("/auth/login", json=ADMIN)).json()["access_token"]}
        async with SessionLocal() as db:
            luis = (await db.execute(select(User).where(User.email == TEACHER["email"]))).scalar_one()
            maria = (await db.execute(select(User).where(User.email == "maria.estudiante@dorismon.do"))).scalar_one()
            pl = (await db.execute(select(Enrollment).where(Enrollment.is_active.is_(True)))).scalars().first()
            ids = {}
            for k in ("con", "sin"):
                s = ClassSeries(id=str(uuid.uuid4()), name=f"v3976 D1 serie {k}", course_id=pl.course_id, level_id=pl.level_id,
                                teacher_id=luis.id, days_of_week="mon", start_time_hhmm="10:00", duration_min=60,
                                start_date=ahora.date(), modality="online", capacity=10, is_active=True)
                db.add(s); await db.flush(); ids[k] = s.id; creado["series"].append(s.id)
                for d in (-7, 7):
                    cs = ClassSession(id=str(uuid.uuid4()), teacher_id=luis.id, title=f"v3976 D1 {k} {d}", course_id=pl.course_id,
                                      level_id=pl.level_id, starts_at_utc=ahora + dt.timedelta(days=d),
                                      ends_at_utc=ahora + dt.timedelta(days=d, hours=1), modality="online",
                                      video_provider="dorismon", status=SessionStatus.scheduled, series_id=s.id, is_open_event=False)
                    db.add(cs); creado["sesiones"].append(cs.id)
            for activa in (True, False):
                e = Enrollment(student_id=maria.id, course_id=pl.course_id, level_id=pl.level_id, teacher_id=luis.id,
                               series_id=ids["con"], modality="online", is_active=activa)
                db.add(e); await db.flush(); creado["matriculas"].append(e.id)
            evt = ClassSession(id=str(uuid.uuid4()), teacher_id=luis.id, title="v3976 D2 evento abierto", course_id=pl.course_id,
                               level_id=pl.level_id, starts_at_utc=ahora - dt.timedelta(minutes=5),
                               ends_at_utc=ahora + dt.timedelta(minutes=55), modality="online", video_provider="dorismon",
                               status=SessionStatus.scheduled, is_open_event=True, capacity=30)
            db.add(evt); creado["sesiones"].append(evt.id)
            await db.commit()

        async def cuenta():
            async with SessionLocal() as db:
                q = lambda m, cond: db.execute(select(func.count()).select_from(m).where(cond))
                return {"serie": (await q(ClassSeries, ClassSeries.id == ids["con"])).scalar(),
                        "matriculas": (await q(Enrollment, Enrollment.series_id == ids["con"])).scalar(),
                        "clases": (await q(ClassSession, ClassSession.series_id == ids["con"])).scalar()}

        print("\n  · D1 — serie con matrículas")
        antes = await cuenta()
        r = await c.delete(f"/admin/class-series/{ids['con']}?future_only=false", headers=AH)
        despues = await cuenta()
        check(f"DELETE de una serie CON matrículas → 409 ({r.status_code})", r.status_code == 409)
        check("mensaje claro para el usuario", "matrícula" in (r.json().get("detail") or ""))
        check(f"serie, matrículas (activa e inactiva) y clases intactas ({antes} → {despues})", antes == despues == {"serie": 1, "matriculas": 2, "clases": 2})
        r = await c.delete(f"/admin/class-series/{ids['con']}?future_only=true", headers=AH)
        async with SessionLocal() as db:
            s = await db.get(ClassSeries, ids["con"])
            check(f"con future_only=true sigue el comportamiento actual (200, la serie queda desactivada, no se borra) ({r.status_code})",
                  r.status_code == 200 and s is not None and s.is_active is False)
        check("…y sus matrículas siguen intactas", (await cuenta())["matriculas"] == 2)
        r = await c.delete(f"/admin/class-series/{ids['sin']}?future_only=false", headers=AH)
        async with SessionLocal() as db:
            s = await db.get(ClassSeries, ids["sin"])
            n = (await db.execute(select(func.count()).select_from(ClassSession).where(ClassSession.series_id == ids["sin"]))).scalar()
        check(f"una serie SIN matrículas se sigue eliminando como hasta ahora (200, serie y sus 2 clases) ({r.status_code})",
              r.status_code == 200 and s is None and n == 0)

        print("\n  · D1 — serie con otra información asociada (quiz, tarea, material, asignación de actividad)")
        from sqlalchemy import text
        from app.models import Quiz, Assignment, Material, MaterialType, ActivityAudience
        deps = {}
        async with SessionLocal() as db:
            for k in ("quiz", "assignment", "material", "activity_audience", "varias"):
                s = ClassSeries(id=str(uuid.uuid4()), name=f"v3976 D1 serie {k}", course_id=pl.course_id, level_id=pl.level_id,
                                teacher_id=luis.id, days_of_week="mon", start_time_hhmm="10:00", duration_min=60,
                                start_date=ahora.date(), modality="online", capacity=10, is_active=True)
                db.add(s); await db.flush(); ids[k] = s.id; creado["series"].append(s.id)
                db.add(ClassSession(id=str(uuid.uuid4()), teacher_id=luis.id, title=f"v3976 D1 {k}", course_id=pl.course_id,
                                    level_id=pl.level_id, starts_at_utc=ahora - dt.timedelta(days=7),
                                    ends_at_utc=ahora - dt.timedelta(days=7, hours=-1), modality="online",
                                    video_provider="dorismon", status=SessionStatus.scheduled, series_id=s.id, is_open_event=False))
            fabrica = {
                "quiz": lambda sid: Quiz(teacher_id=luis.id, title="v3976 D1 quiz", series_id=sid, is_published=False),
                "assignment": lambda sid: Assignment(teacher_id=luis.id, title="v3976 D1 tarea", series_id=sid),
                "material": lambda sid: Material(title="v3976 D1 material", type=MaterialType.pdf, url="https://ejemplo.invalid/d1.pdf",
                                                 series_id=sid, audience_kind="series", is_public=False),
                "activity_audience": lambda sid: ActivityAudience(activity_type="quiz", activity_id=999001, series_id=sid),
            }
            for k, f in fabrica.items():
                for sid in (ids[k], ids["varias"]):
                    o = f(sid); db.add(o); await db.flush(); deps.setdefault(k, []).append(o.id)
            e = Enrollment(student_id=maria.id, course_id=pl.course_id, level_id=pl.level_id, teacher_id=luis.id,
                           series_id=ids["varias"], modality="online", is_active=True)
            db.add(e); await db.flush(); creado["matriculas"].append(e.id)
            await db.commit()
        TABLAS = ("class_series", "class_sessions", "enrollments", "quizzes", "assignments", "materials", "activity_audience", "audit_logs")

        async def foto_tablas():
            async with engine.connect() as cx:
                return {t: sorted(map(str, (await cx.execute(text(f"select * from {t}"))).all())) for t in TABLAS}
        for k in ("quiz", "assignment", "material", "activity_audience", "varias"):
            a = await foto_tablas()
            r = await c.delete(f"/admin/class-series/{ids[k]}?future_only=false", headers=AH)
            b = await foto_tablas()
            det = (r.json() or {}).get("detail") or ""
            cambiadas = [t for t in TABLAS if a[t] != b[t]]
            check(f"serie con {k} → 409 ({r.status_code}); ninguna fila cambia ({cambiadas or 'ninguna'})", r.status_code == 409 and not cambiadas)
            if k == "varias":
                check(f"varias a la vez: el mensaje nombra cada tipo ({det[:150]})",
                      all(x in det for x in ("matrícula", "quiz", "tarea", "material", "actividades")))
        async with SessionLocal() as db:   # limpieza SOLO de lo creado aquí (no se tocan datos ajenos)
            from sqlalchemy import delete
            await db.execute(delete(ActivityAudience).where(ActivityAudience.id.in_(deps["activity_audience"])))
            await db.execute(delete(Material).where(Material.id.in_(deps["material"])))
            await db.execute(delete(Assignment).where(Assignment.id.in_(deps["assignment"])))
            await db.execute(delete(Quiz).where(Quiz.id.in_(deps["quiz"])))
            await db.commit()

        print("\n  · D2 — Dirección en un evento abierto")
        r = await c.post(f"/video/sessions/{evt.id}/join", headers=AH)
        async with SessionLocal() as db:
            reg = (await db.execute(select(func.count()).select_from(EventRegistration)
                                    .where(EventRegistration.session_id == evt.id))).scalar()
        check(f"Dirección entra (/join {r.status_code}) sin inscribirse como estudiante (inscripciones = {reg})", r.status_code == 200 and reg == 0)

        print("\n  · D3 — foto de audiencia de una grabación, añadiendo a la vez")
        import app.services.grabaciones as gr
        async with SessionLocal() as db:
            rec = VideoRecording(session_id=evt.id, activa=None, status="processing", requested_by=luis.id, mime_type="video/mp4")
            db.add(rec)
            db.add(VideoPresence(session_id=evt.id, user_id=maria.id, minutes=3))
            await db.commit(); rid = rec.id; creado["grabaciones"].append(rid)

        async def congelar():
            async with SessionLocal() as db:
                s_ = await db.get(ClassSession, evt.id)
                await gr.congelar_audiencia(db, s_, rid, solo_asistencia=True)
        res = await asyncio.gather(*[congelar() for _ in range(5)], return_exceptions=True)
        errores = [type(x).__name__ for x in res if isinstance(x, Exception)]
        async def foto():
            async with SessionLocal() as db:
                filas = (await db.execute(select(func.count(), func.count(func.distinct(VideoRecordingAudience.user_id)))
                                          .where(VideoRecordingAudience.recording_id == rid))).one()
                presentes = (await db.execute(select(func.count(func.distinct(VideoPresence.user_id)))
                                              .where(VideoPresence.session_id == evt.id, VideoPresence.user_id != luis.id))).scalar()
            return tuple(filas), presentes
        (n, nd), presentes = await foto()
        # Presentes: la estudiante + Dirección, que entró en el paso D2
        check(f"5 a la vez: sin errores ({errores}); cada asistente UNA vez ({n} filas = {nd} personas = {presentes} presentes)",
              not errores and n == nd == presentes >= 1)
        await congelar()
        check("repetir es idempotente (mismas filas)", (await foto())[0] == (n, nd))

    srv.shutdown()
    # Limpieza SOLO de lo creado por este test (base local de la suite)
    async with SessionLocal() as db:
        from sqlalchemy import delete
        await db.execute(delete(VideoRecordingAudience).where(VideoRecordingAudience.recording_id.in_(creado["grabaciones"])))
        await db.execute(delete(VideoRecording).where(VideoRecording.id.in_(creado["grabaciones"])))
        await db.execute(delete(VideoPresence).where(VideoPresence.session_id.in_(creado["sesiones"])))
        await db.execute(delete(EventRegistration).where(EventRegistration.session_id.in_(creado["sesiones"])))
        await db.execute(delete(Enrollment).where(Enrollment.id.in_(creado["matriculas"])))
        await db.commit()


def parte_estatica(check):
    print("\n[ESTÁTICO]")
    adm = (ROOT / "app/routers/admin.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    vid = (ROOT / "app/routers/video.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    gra = (ROOT / "app/services/grabaciones.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    check("D1: el 409 se decide ANTES de borrar nada (sin archivado silencioso)",
          adm.index("raise HTTPException(\n                409,") < adm.index("# Eliminar TODAS las clases de la serie"))
    check("D2: Dirección excluida de la autoinscripción", "if s.is_open_event and not is_teacher and not is_admin:" in vid)
    check("D3: inserción idempotente (ON CONFLICT DO NOTHING) en la foto de audiencia",
          'on_conflict_do_nothing(index_elements=["recording_id", "user_id"])' in gra)


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
        check(f"integración sin errores ({type(e).__name__}: {e})", False)
    try:
        parte_estatica(check)
    except Exception as e:
        check(f"estático sin errores ({type(e).__name__}: {e})", False)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
