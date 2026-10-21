"""GATE 2 — Experiencia del profesor (sin cambiar reglas académicas).

Backend (lo único que cambia):
  L1. /teacher/dashboard: `location` en las clases de hoy, solo presencial/
      híbrida, precargada EN BLOQUE (sin N+1).
  L4. /teacher/sessions: añade `video_provider` y `ends_at_utc` para reutilizar
      el MISMO botón de entrada.
Frontend (estático): clases primero, "En curso"/"Próxima", modalidad legible,
sin duplicados, avisos debajo, barra inferior solo para profesor, niveles
plegados, "Entrar" en Mis clases con JoinClassButton.

Uso: python tests/test_gate2.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import datetime as dt
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}
UTC = dt.timezone.utc
DB_URL_ORIGINAL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dorismon.db")


def limpiar_sesiones_locales(ids):
    """Mismo aislamiento que test_gate0/test_gate1 (solo SQLite local)."""
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


async def parte1(check):
    """En proceso: N consultas NO crece con el número de clases presenciales."""
    print("\n[parte 1] Ubicación en bloque: sin N+1 (conteo real de SQL)")
    db_file = Path(tempfile.gettempdir()) / f"gate2_{uuid.uuid4().hex[:8]}.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import event
    from app.core.db import engine, SessionLocal
    from app.core.security import CurrentUser
    from app.models import (Base, User, Teacher, Course, Level, ClassSession, Branch, Classroom,
                            UserRole, Modality, SessionStatus)
    import app.routers.teacher as tmod

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        prof = User(email="g2prof@test.local", password_hash="x", full_name="Profe G2", role=UserRole.teacher)
        db.add(prof); await db.flush(); db.add(Teacher(user_id=prof.id))
        cur = Course(code="G2", name="Curso G2"); db.add(cur); await db.flush()
        lv = Level(course_id=cur.id, code="B1", name="B1"); db.add(lv); await db.flush()
        sedes = []
        for i in range(5):
            b = Branch(name=f"Sede {i}", address=f"Calle {i}", phone=f"809-000-000{i}"); db.add(b); await db.flush()
            a = Classroom(branch_id=b.id, name=f"Aula {i}", capacity=10); db.add(a); await db.flush()
            sedes.append((b, a))
        await db.commit()
        prof_id, cur_id, lv_id = prof.id, cur.id, lv.id
        sedes = [(b.id, a.id) for b, a in sedes]

    contador = {"n": 0}

    def _cuenta(*_a, **_k):
        contador["n"] += 1
    event.listen(engine.sync_engine, "before_cursor_execute", _cuenta)

    ahora = dt.datetime.now(UTC)

    async def crear(n_presenciales):
        async with SessionLocal() as db:
            await db.execute(ClassSession.__table__.delete())
            for i in range(n_presenciales):
                b, a = sedes[i]
                db.add(ClassSession(course_id=cur_id, level_id=lv_id, teacher_id=prof_id, title=f"P{i}",
                                    modality=Modality.presencial, branch_id=b, classroom_id=a,
                                    status=SessionStatus.scheduled, video_provider="meet",
                                    starts_at_utc=ahora + dt.timedelta(minutes=10 + i),
                                    ends_at_utc=ahora + dt.timedelta(minutes=70 + i)))
            db.add(ClassSession(course_id=cur_id, level_id=lv_id, teacher_id=prof_id, title="Online con sede vieja",
                                modality=Modality.online, branch_id=sedes[0][0], video_provider="dorismon",
                                status=SessionStatus.scheduled,
                                starts_at_utc=ahora + dt.timedelta(minutes=5),
                                ends_at_utc=ahora + dt.timedelta(minutes=65)))
            await db.commit()

    async def panel():
        contador["n"] = 0
        async with SessionLocal() as db:
            d = await tmod.teacher_dashboard(teacher=CurrentUser(user_id=prof_id, role="teacher"), db=db)
        return d, contador["n"]

    try:
        await crear(1); d1, q1 = await panel()
        await crear(5); d5, q5 = await panel()
        check(f"misma cantidad de consultas con 1 y con 5 presenciales ({q1} vs {q5})", q1 == q5)
        hoy = {s["title"]: s for s in d5["today_schedule"]}
        p = hoy.get("P3", {}).get("location") or {}
        check("presencial trae ubicación con sede, aula, dirección y mapa",
              p.get("branch_name") == "Sede 3" and p.get("classroom_name") == "Aula 3"
              and p.get("address") == "Calle 3" and bool(p.get("maps_url")))
        check("online NO trae ubicación aunque conserve una sede vieja",
              hoy.get("Online con sede vieja", {}).get("location") is None)
        check("cada clase de hoy trae la clave 'location'",
              all("location" in s for s in d5["today_schedule"]))
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _cuenta)
        await engine.dispose()
        try:
            db_file.unlink()
        except OSError:
            pass


async def parte2(check):
    print("\n[parte 2] HTTP contra el backend local")
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        async def login(cred):
            r = await c.post("/auth/login", json=cred)
            return {"Authorization": f"Bearer {r.json()['access_token']}"} if r.status_code == 200 else None
        AH, TH = await login(ADMIN), await login(TEACHER)
        check("logins admin / profesor", bool(AH and TH))
        if not (AH and TH):
            return
        prof_id = (await c.get("/auth/me", headers=TH)).json()["id"]
        cid = (await c.get("/admin/courses", headers=AH)).json()[0]["id"]
        lv = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        nivel = (lv["items"] if isinstance(lv, dict) else lv)[0]["id"]
        sedes = (await c.get("/admin/branches", headers=AH)).json()
        sede = (sedes.get("items", sedes) if isinstance(sedes, dict) else sedes)[0]["id"]
        now = dt.datetime.now(UTC)
        creadas = []

        async def crear(titulo, ini, **extra):
            body = {"title": titulo, "teacher_id": prof_id, "course_id": cid, "level_id": nivel,
                    "starts_at_utc": (now + dt.timedelta(minutes=ini)).isoformat(),
                    "ends_at_utc": (now + dt.timedelta(minutes=ini + 60)).isoformat(),
                    "modality": "online", "video_provider": "dorismon", **extra}
            r = await c.post("/admin/sessions", headers=AH, json=body)
            sid = r.json().get("id") if r.status_code in (200, 201) else None
            if sid:
                creadas.append(sid)
            return sid

        dor = await crear("G2 Dorismon en curso", -5)
        meet = await crear("G2 Meet", 10, video_provider="meet", meeting_url="https://meet.google.com/abc-defg-hij")
        pres = await crear("G2 Presencial", 20, modality="presencial", branch_id=sede, video_provider="meet")
        hib = await crear("G2 Híbrida", 25, modality="hibrida", branch_id=sede, video_provider="dorismon")
        canc = await crear("G2 Cancelada", -5)
        fin = await crear("G2 Para finalizar", -5)
        check("se crean clases Dorismon, Meet, presencial, híbrida, cancelada y finalizable",
              all((dor, meet, pres, hib, canc, fin)))
        await c.delete(f"/admin/sessions/{canc}", headers=AH)
        await c.post(f"/teacher/sessions/{fin}/finalize", headers=TH)

        hoy = {s["id"]: s for s in (await c.get("/teacher/dashboard", headers=TH)).json()["today_schedule"]}
        sede_de = lambda sid: ((hoy.get(sid) or {}).get("location") or {}).get("branch_name")
        check("L1: presencial trae su sede en 'hoy'", bool(sede_de(pres)))
        check("L1: híbrida trae su sede en 'hoy'", bool(sede_de(hib)))
        check("L1: online (Dorismon y Meet) sin ubicación",
              hoy.get(dor, {}).get("location") is None and hoy.get(meet, {}).get("location") is None)
        check("'hoy' conserva video_provider y ends_at_utc", all(
            "video_provider" in s and "ends_at_utc" in s for s in hoy.values()))
        check("cancelada y finalizada NO están en 'hoy' (no pueden mostrarse 'en curso')",
              canc not in hoy and fin not in hoy)

        r = await c.get("/teacher/sessions?filter_period=upcoming", headers=TH)
        body = r.json()
        items = {s["id"]: s for s in body.get("items", [])}
        check("L4: /teacher/sessions conserva su forma {items, filter_period}",
              set(body) == {"items", "filter_period"} and body["filter_period"] == "upcoming")
        check("L4: cada clase trae video_provider y ends_at_utc",
              bool(items) and all("video_provider" in s and "ends_at_utc" in s for s in items.values()))
        check("L4: video_provider correcto (dorismon / meet)",
              items.get(dor, {}).get("video_provider") == "dorismon" and items.get(meet, {}).get("video_provider") == "meet")
        check("L4: claves anteriores intactas", all(
            {"id", "title", "modality", "starts_at_utc", "course_name", "level_code",
             "meeting_url", "status", "capacity"} <= set(s) for s in items.values()))
        check("L4: la finalizada sigue como 'completed' (restricción actual intacta)",
              items.get(fin, {}).get("status") in ("completed", None))
        check("entrada desde Mis clases usa el MISMO /join: Dorismon en curso -> 200",
              (await c.post(f"/video/sessions/{dor}/join", headers=TH)).status_code == 200)
        check("presencial sigue sin Video Dorismon (400)",
              (await c.post(f"/video/sessions/{pres}/join", headers=TH)).status_code == 400)
        check("completed sigue rechazada en /join (Gate 0)",
              (await c.post(f"/video/sessions/{fin}/join", headers=TH)).status_code == 400)

        print("  · limpieza")
        check(f"las {len(creadas)} clases de test_gate2 se retiran de la base local",
              limpiar_sesiones_locales(creadas) == len(creadas))


def parte3(check):
    print("\n[parte 3] Frontend (estático)")
    pg = WEB / "app" / "dashboard" / "teacher" / "page.tsx"
    ses = WEB / "app" / "dashboard" / "teacher" / "sessions" / "page.tsx"
    lay = WEB / "app" / "dashboard" / "layout.tsx"
    hlp = WEB / "components" / "ClaseProfesor.tsx"
    if not all(p.exists() for p in (pg, ses, lay, hlp)):
        check(f"existen los archivos del frontend en {WEB}", False)
        return
    p, s, l, h = (x.read_text(encoding="utf-8") for x in (pg, ses, lay, hlp))
    i_clases = p.find("Tus clases de hoy")
    check("V1: 'Tus clases de hoy' va ANTES que avisos y métricas",
          0 < i_clases < p.find("<PanelFinalizaciones") and i_clases < p.find("<EntregasPendientes")
          and i_clases < p.find("<PendingAttendance") and i_clases < p.find("<AvisosTelefono")
          and i_clases < p.find("Estudiantes</span>"))
    check("V2: muestra '🔴 En curso' y '⏭️ Próxima clase'",
          '🔴</span> En curso' in p and "⏭️ Próxima clase" in p)
    check("V2: en curso/próxima solo para status 'scheduled' (completed/cancelled nunca)",
          'if (!s || s.status !== "scheduled") return null;' in h)
    check("V2: timestamps con zona; sin zona se leen como UTC (no ±4h a mano)",
          "`${iso}Z`" in h and not re.search(r"[+-]\s*4\s*\*\s*60|getTimezoneOffset|-04:00", h))
    check("V2: el botón es el MISMO (JoinClassButton) en dashboard y Mis clases",
          "<JoinClassButton session={s} />" in p and "<JoinClassButton session={s} />" in s)
    check("V3: modalidad legible e inicio–fin", "modalidadLegible(s.modality)" in p and "rangoHora(s)" in p
          and all(x in h for x in ("🌐 Online", "🏢 Presencial", "🔀 Híbrida")))
    check("V3: etiquetas existentes conservadas",
          all(x in p for x in ("👤 Privada", "👥 Clase especial ·", "🎉 Evento", "🎯 Clase de prueba")))
    check("L1: ubicación con el componente existente ClassLocation", "<ClassLocation location={s.location} compact />" in p)
    check("V4: 'Tareas por revisar' en un único lugar (panel de entregas)",
          "<EntregasPendientes />" in p and "stats.pending_grading" not in p and "Ir a calificar" not in p)
    check("V4: 'clases hoy' no se repite como métrica", "clases programadas" not in p)
    check("V4: riesgo intacto (panel + 'Asistencia baja', sin unificar)",
          "<EstudiantesEnRiesgo />" in p and "Asistencia baja" in p)
    check("V7: distribución por niveles conservada y plegable", "<details>" in p and "Distribución de niveles" in p)
    check("V6: barra inferior SOLO para profesor (isTeacher)",
          "{isTeacher && (" in l and 'const isTeacher = role === "teacher";' in l)
    check("V6: barra de Dirección intacta", l.count('aria-label="Navegación rápida"') == 1 and "{isAdmin && (" in l)
    check("V6: destinos Inicio · Mis clases · Tareas · Más",
          all(x in l for x in ('label: "Inicio", Icon: Home', 'label: "Mis clases", Icon: Calendar',
                                'label: "Tareas", Icon: FileText')))
    check("V6: queda por debajo de modales (z-50) y Video Dorismon (z-[90])", "z-30" in l)
    check("L4: Mis clases reutiliza tieneEntradaOnline y no crea otro botón de entrada",
          "tieneEntradaOnline(s)" in s and s.count("<button") == 1)  # el único <button> son las pestañas


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    await parte1(check)
    await parte2(check)
    parte3(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
