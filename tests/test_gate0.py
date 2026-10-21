"""GATE 0 — El profesor no pierde la entrada a su clase.

Causas corregidas:
  A. "Hoy" del panel del profesor se calculaba con la medianoche UTC (= 8:00 PM
     en RD): una clase de 7:50 PM desaparecía a las 8:00 PM y una de 8:00 PM no
     se veía antes de empezar.
  B. "Finalizar clase" se ejecutaba de un toque, sin confirmación ni auditoría.
  D. /video/.../join no rechazaba una clase ya FINALIZADA (status completed).

Parte 1 (en proceso, reloj fijado): matriz de horarios de RD contra el código
REAL de `teacher_dashboard`, sobre una base SQLite temporal propia.
Parte 2 (HTTP contra el backend local): entrada, audiencia, finalizar,
auditoría y que nada se marque `completed` solo.
Parte 3 (estática): el frontend exige confirmación y bloquea doble envío.

Uso: python tests/test_gate0.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import datetime as dt
import os
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
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
OUTSIDER = {"email": "carlos.estudiante@dorismon.do", "password": "Estu-Test-4823!"}

RD = dt.timezone(dt.timedelta(hours=-4))   # solo para FABRICAR horas de prueba
UTC = dt.timezone.utc
DIA = (2026, 10, 14)                        # miércoles cualquiera
OFFSETS = [("-30", -30), ("-5", -5), ("inicio", 0), ("+5", 5), ("+10", 10), ("+30", 30),
           ("fin-5", 55), ("fin", 60), ("gracia+15", 75)]
HORAS = [(9, 0), (16, 0), (18, 0), (19, 0), (19, 30), (19, 50), (20, 0)]


async def parte1(check):
    """Matriz de horarios contra el teacher_dashboard REAL con reloj fijado."""
    db_file = Path(tempfile.gettempdir()) / f"gate0_{uuid.uuid4().hex[:8]}.db"
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    sys.path.insert(0, str(ROOT))
    from app.core.db import engine, SessionLocal
    from app.core.security import CurrentUser
    from app.models import Base, User, Teacher, Course, Level, ClassSession, UserRole, Modality, SessionStatus
    import app.routers.teacher as tmod

    class Reloj(dt.datetime):
        fijo = None

        @classmethod
        def now(cls, tz=None):
            return cls.fijo.astimezone(tz) if tz else cls.fijo.replace(tzinfo=None)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as db:
        prof = User(email="g0prof@test.local", password_hash="x", full_name="Profe G0", role=UserRole.teacher)
        db.add(prof); await db.flush()
        db.add(Teacher(user_id=prof.id))
        cur = Course(code="G0", name="Curso G0"); db.add(cur); await db.flush()
        lv = Level(course_id=cur.id, code="A2", name="A2"); db.add(lv); await db.flush()

        def clase(titulo, inicio_rd, mins=60, status=SessionStatus.scheduled):
            s = ClassSession(course_id=cur.id, level_id=lv.id, teacher_id=prof.id, title=titulo,
                             modality=Modality.online, video_provider="dorismon", status=status,
                             starts_at_utc=inicio_rd.astimezone(UTC),
                             ends_at_utc=(inicio_rd + dt.timedelta(minutes=mins)).astimezone(UTC))
            db.add(s)
            return s

        ses = {f"{h:02d}:{m:02d}": clase(f"G0 {h:02d}:{m:02d}", dt.datetime(*DIA, h, m, tzinfo=RD))
               for h, m in HORAS}
        cruza = clase("G0 23:30 cruza medianoche", dt.datetime(*DIA, 23, 30, tzinfo=RD))
        manana = clase("G0 mañana 10:00", dt.datetime(*DIA, 10, 0, tzinfo=RD) + dt.timedelta(days=1))
        madrugada = clase("G0 00:10 mañana", dt.datetime(*DIA, 0, 10, tzinfo=RD) + dt.timedelta(days=1))
        cancelada = clase("G0 cancelada 18:00", dt.datetime(*DIA, 18, 0, tzinfo=RD), status=SessionStatus.cancelled)
        completada = clase("G0 completada 18:00", dt.datetime(*DIA, 18, 0, tzinfo=RD), status=SessionStatus.completed)
        await db.commit()
        ids = {k: s.id for k, s in ses.items()}
        ids_extra = {"cruza": cruza.id, "manana": manana.id, "madrugada": madrugada.id,
                     "cancelada": cancelada.id, "completada": completada.id}

    original = tmod.datetime
    tmod.datetime = Reloj

    async def panel(ahora_rd):
        Reloj.fijo = ahora_rd.astimezone(UTC)
        async with SessionLocal() as db:
            d = await tmod.teacher_dashboard(teacher=CurrentUser(user_id=prof.id, role="teacher"), db=db)
        return {s["id"] for s in d["today_schedule"]}, {s["id"] for s in d["week_schedule"]}

    try:
        print("\n[parte 1] Matriz RD: la clase debe estar en 'hoy' en todos los momentos")
        for hhmm, sid in ids.items():
            h, m = map(int, hhmm.split(":"))
            inicio = dt.datetime(*DIA, h, m, tzinfo=RD)
            fallos = []
            for nombre, off in OFFSETS:
                hoy, semana = await panel(inicio + dt.timedelta(minutes=off))
                if sid not in hoy:
                    fallos.append(nombre)
                if sid in hoy and sid in semana:
                    fallos.append(nombre + "(duplicada)")
            check(f"clase {hhmm} RD visible en -30/-5/inicio/+5/+10/+30/fin-5/fin/gracia"
                  + (f"  FALLA EN: {fallos}" if fallos else ""), not fallos)

        hoy, _ = await panel(dt.datetime(*DIA, 20, 0, tzinfo=RD))
        check("caso clave: la clase de 7:50 PM NO desaparece a las 8:00 PM", ids["19:50"] in hoy)
        hoy, _ = await panel(dt.datetime(*DIA, 19, 30, tzinfo=RD))
        check("caso clave: la clase de 8:00 PM se ve ANTES de empezar (7:30 PM)", ids["20:00"] in hoy)
        hoy, _ = await panel(dt.datetime(*DIA, 23, 50, tzinfo=RD) + dt.timedelta(minutes=25))
        check("clase en curso que cruza la medianoche de RD sigue en 'hoy' (00:15)", ids_extra["cruza"] in hoy)
        hoy, semana = await panel(dt.datetime(*DIA, 15, 0, tzinfo=RD))
        check("clase de mañana NO está en 'hoy'", ids_extra["manana"] not in hoy)
        check("clase de mañana SÍ está en 'próximas de la semana'", ids_extra["manana"] in semana)
        hoy, semana = await panel(dt.datetime(*DIA, 23, 50, tzinfo=RD))
        check("clase de 00:10 con sala ya abierta (23:50) está en 'hoy'", ids_extra["madrugada"] in hoy)
        check("…y no se repite en 'próximas de la semana'", ids_extra["madrugada"] not in semana)
        hoy, _ = await panel(dt.datetime(*DIA, 19, 20, tzinfo=RD))
        check("una clase ya terminada HOY sigue listada en 'hoy' (comportamiento previo conservado)",
              ids["18:00"] in hoy)
        check("clase CANCELADA no aparece en 'hoy'", ids_extra["cancelada"] not in hoy)
        check("clase COMPLETED no aparece en 'hoy'", ids_extra["completada"] not in hoy)
        hoy, _ = await panel(dt.datetime(*DIA, 2, 0, tzinfo=RD) + dt.timedelta(days=1))
        check("al día siguiente (02:00) las clases de ayer ya no están en 'hoy'",
              not (set(ids.values()) & hoy))
    finally:
        tmod.datetime = original
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
        AH, TH, SH, OH = [await login(x) for x in (ADMIN, TEACHER, STUDENT, OUTSIDER)]
        check("logins admin/profesor/estudiante/no-autorizado", all((AH, TH, SH, OH)))
        if not all((AH, TH, SH, OH)):
            return
        prof_id = (await c.get("/auth/me", headers=TH)).json()["id"]
        est_id = (await c.get("/auth/me", headers=SH)).json()["id"]
        cid = (await c.get("/admin/courses", headers=AH)).json()[0]["id"]
        lv = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        nivel = (lv["items"] if isinstance(lv, dict) else lv)[0]["id"]
        sedes = (await c.get("/admin/branches", headers=AH)).json()
        sede = (sedes.get("items", sedes) if isinstance(sedes, dict) else sedes)[0]["id"]
        now = dt.datetime.now(UTC)
        creadas = []

        async def crear(titulo, ini_min, dur=60, **extra):
            body = {"title": titulo, "teacher_id": prof_id, "course_id": cid, "level_id": nivel,
                    "starts_at_utc": (now + dt.timedelta(minutes=ini_min)).isoformat(),
                    "ends_at_utc": (now + dt.timedelta(minutes=ini_min + dur)).isoformat(),
                    "modality": "online", "video_provider": "dorismon", "student_ids": [est_id], **extra}
            r = await c.post("/admin/sessions", headers=AH, json=body)
            sid = r.json().get("id") if r.status_code in (200, 201) else None
            if sid:
                creadas.append(sid)
            return sid

        async def join(h, sid):
            return (await c.post(f"/video/sessions/{sid}/join", headers=h)).status_code

        async def estado(sid):
            for page in range(1, 30):
                r = await c.get(f"/admin/sessions?filter_period=all&limit=200&page={page}", headers=AH)
                items = r.json().get("items", []) if r.status_code == 200 else []
                for x in items:
                    if x["id"] == sid:
                        return x.get("status")
                if not items:
                    break
            return None

        curso = await crear("G0 en curso", -10)
        check("se crea clase Dorismon en curso con audiencia explícita", bool(curso))
        print("  · ventana de entrada (misma regla que /join)")
        for etiqueta, ini, dur, esperado in (("empieza en 25 min", 25, 60, 200), ("empieza en 40 min", 40, 60, 400),
                                             ("empezó hace 5 min", -5, 60, 200), ("empezó hace 30 min", -30, 60, 200),
                                             ("termina en 5 min", -55, 60, 200), ("terminó hace 10 min (gracia)", -70, 60, 200),
                                             ("terminó hace 20 min", -80, 60, 400)):
            sid = await crear(f"G0 ventana {etiqueta}", ini, dur)
            tj, sj = await join(TH, sid), await join(SH, sid)
            check(f"{etiqueta}: profesor {esperado} / estudiante autorizado {esperado}", tj == esperado and sj == esperado)
        check("estudiante NO autorizado sigue rechazado (403)", await join(OH, curso) == 403)

        pres = await crear("G0 presencial", -10, modality="presencial", branch_id=sede, video_provider="meet")
        check("presencial: sin Video Dorismon para el profesor", bool(pres) and await join(TH, pres) == 400)
        check("presencial: sin Video Dorismon para el estudiante", bool(pres) and await join(SH, pres) == 400)

        canc = await crear("G0 cancelada", -10)
        await c.delete(f"/admin/sessions/{canc}", headers=AH)
        check("cancelada: profesor rechazado (400)", await join(TH, canc) == 400)
        check("cancelada: estudiante rechazado (400)", await join(SH, canc) == 400)

        hoy = {s["id"] for s in (await c.get("/teacher/dashboard", headers=TH)).json()["today_schedule"]}
        check("la clase en curso aparece en 'hoy' del profesor (reloj real)", curso in hoy)

        print("  · nada se marca completed solo")
        for _ in range(3):
            await c.get("/teacher/dashboard", headers=TH)
            await join(TH, curso); await join(SH, curso)
        check("tras dashboards y entradas, la clase sigue 'scheduled'", await estado(curso) == "scheduled")

        print("  · finalizar: auditoría y cierre de entrada")
        r = await c.post(f"/teacher/sessions/{curso}/finalize", headers=TH)
        check("profesor finaliza su clase -> 200", r.status_code == 200)
        check("estado pasa a 'completed'", await estado(curso) == "completed")
        logs = (await c.get("/admin/audit-logs?limit=200", headers=AH)).json()
        logs = logs.get("items", logs) if isinstance(logs, dict) else logs
        fin = [x for x in logs if x.get("action") == "finalize_session" and x.get("target_id") == curso]
        check("finalizar crea audit log (acción finalize_session, sesión correcta)", len(fin) == 1)
        check("audit log registra al usuario que finalizó", bool(fin) and fin[0].get("user_id") == prof_id)
        check("audit log tiene timestamp", bool(fin) and bool(fin[0].get("created_at")))
        check("completed: profesor rechazado en /join", await join(TH, curso) == 400)
        check("completed: estudiante rechazado en /join", await join(SH, curso) == 400)
        r = await c.post(f"/video/sessions/{curso}/join", headers=TH)
        check("mensaje claro: 'ya fue finalizada'", "finalizada" in r.text)
        hoy = {s["id"] for s in (await c.get("/teacher/dashboard", headers=TH)).json()["today_schedule"]}
        check("la clase finalizada ya no aparece en 'hoy'", curso not in hoy)

        print("  · limpieza: no dejar clases que desplacen datos de tests posteriores")
        borradas = limpiar_sesiones_locales(creadas)
        if borradas is None:
            # Base no-SQLite: no se borra nada; al menos no quedan en curso.
            for sid in creadas:
                if sid != curso:
                    await c.delete(f"/admin/sessions/{sid}", headers=AH)
            check("limpieza local no aplicable (base no SQLite): clases solo canceladas", False)
        else:
            quedan = [sid for sid in creadas if await estado(sid) is not None]
            check(f"limpieza: las {len(creadas)} clases de test_gate0 ya no están en el listado",
                  not quedan and borradas == len(creadas))


# GATE 0 — Aislamiento: mismo mecanismo que test_repeat_level/test_p3_progression
# (sqlite3 sobre la base LOCAL de la suite). Solo borra las clases que este test
# creó y las filas que las referencian. Nunca actúa sobre una base que no sea un
# archivo SQLite local presente en el directorio de ejecución.
DB_URL_ORIGINAL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dorismon.db")


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
        tablas = [r[0] for r in cx.execute("select name from sqlite_master where type='table'")]
        for t in tablas:
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


def parte3(check):
    print("\n[parte 3] Frontend (estático): confirmación y doble envío")
    page = WEB / "app" / "dashboard" / "teacher" / "page.tsx"
    if not page.exists():
        check(f"existe {page}", False)
        return
    src = page.read_text(encoding="utf-8")
    check("usa el ConfirmModal existente de Dorismon", "<ConfirmModal" in src and "ConfirmModal }" in src)
    check("el botón abre la confirmación, NO finaliza directo",
          "onClick={() => setConfirmarFinalizar(s)}" in src and "onClick={() => finalizeClass(s.id)}" not in src)
    check("texto de confirmación acordado",
          "¿Seguro que deseas finalizar esta clase? Una vez finalizada dejará de aparecer como clase en curso "
          "y ya no se podrá entrar normalmente." in src)
    check("botones Cancelar / 'Sí, finalizar clase'", 'confirmLabel="Sí, finalizar clase"' in src)
    check("bloqueo de doble envío (ref + botón deshabilitado)",
          "if (finalizandoRef.current) return;" in src and "disabled={finalizandoId !== null}" in src)
    check("separado visualmente del botón Entrar (bloque propio con divisoria)",
          'mt-4 pt-3 border-t border-slate-100 flex justify-end' in src)


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
