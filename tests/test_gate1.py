"""GATE 1 — Clase para estudiantes seleccionados: visible y fácil de usar.

No es un sistema nuevo: la clase se crea por el flujo de siempre
(POST /admin/sessions con student_ids → SessionAudience). Gate 1 solo:
  A. añade el atajo "👥 Clase para estudiantes seleccionados" en el menú;
  B. expone `audience_count` en /admin/sessions (consulta agrupada);
  C. expone `audience_count` en el dashboard del profesor (hoy y próximas);
  D. no ofrece ni acepta estudiantes DESACTIVADOS al crear una clase nueva.

Uso: python tests/test_gate1.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}
MARIA = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
PASS = "Estu-Test-4823!"
UTC = dt.timezone.utc
DB_URL_ORIGINAL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dorismon.db")


def limpiar_sesiones_locales(ids):
    """Mismo aislamiento que test_gate0 / test_repeat_level: borra SOLO las clases
    que este test creó (y lo que las referencia) en la base SQLite LOCAL."""
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


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        async def login(cred):
            r = await c.post("/auth/login", json=cred)
            return {"Authorization": f"Bearer {r.json()['access_token']}"} if r.status_code == 200 else None

        AH, TH, MH = await login(ADMIN), await login(TEACHER), await login(MARIA)
        check("logins admin / profesor / estudiante con matrícula", all((AH, TH, MH)))
        if not all((AH, TH, MH)):
            print(f"\n{passed}/{total} tests pasaron"); return False
        prof_id = (await c.get("/auth/me", headers=TH)).json()["id"]
        maria_id = (await c.get("/auth/me", headers=MH)).json()["id"]
        cid = (await c.get("/admin/courses", headers=AH)).json()[0]["id"]
        lv = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        nivel = (lv["items"] if isinstance(lv, dict) else lv)[0]["id"]
        stamp = dt.datetime.now().strftime("%H%M%S%f")[:10]
        now = dt.datetime.now(UTC)
        creadas = []

        async def alumno(nombre):
            email = f"g1{stamp}{nombre}@dorismon.do"
            r = await c.post("/admin/users", headers=AH, json={
                "email": email, "full_name": f"G1 {nombre}", "password": PASS, "role": "student"})
            uid = (r.json().get("id") or r.json().get("user_id")) if r.status_code in (200, 201) else None
            return uid, await login({"email": email, "password": PASS}), email

        alumnos = {n: await alumno(n) for n in ("a", "b", "c", "d", "fuera", "baja")}
        check("se crean 6 estudiantes nuevos SIN grupo ni matrícula",
              all(u and h for u, h, _ in alumnos.values()))
        A, B, Cc, D, E, F = (alumnos[n] for n in ("a", "b", "c", "d", "fuera", "baja"))

        async def crear(titulo, ini_min, **extra):
            body = {"title": titulo, "teacher_id": prof_id, "course_id": cid, "level_id": nivel,
                    "starts_at_utc": (now + dt.timedelta(minutes=ini_min)).isoformat(),
                    "ends_at_utc": (now + dt.timedelta(minutes=ini_min + 60)).isoformat(),
                    "modality": "online", "video_provider": "dorismon", **extra}
            r = await c.post("/admin/sessions", headers=AH, json=body)
            sid = r.json().get("id") if r.status_code in (200, 201) else None
            if sid:
                creadas.append(sid)
            return r, sid

        async def admin_item(sid):
            for page in range(1, 30):
                r = await c.get(f"/admin/sessions?filter_period=all&limit=200&page={page}", headers=AH)
                items = r.json().get("items", []) if r.status_code == 200 else []
                for x in items:
                    if x["id"] == sid:
                        return x
                if not items:
                    break
            return None

        async def ve(H, sid):
            cal = (await c.get("/student/calendar", headers=H)).json()
            ev = cal.get("events", cal) if isinstance(cal, dict) else cal
            en_cal = any(e.get("id") == sid for e in ev if e.get("type") == "class")
            dash = (await c.get("/student/dashboard", headers=H)).json()
            en_dash = any(x.get("id") == sid for x in dash.get("next_classes", []))
            return en_cal, en_dash

        async def join(H, sid):
            return (await c.post(f"/video/sessions/{sid}/join", headers=H)).status_code

        print("\n[1-2] Dirección selecciona 1 o varios estudiantes")
        r1, uno = await crear("G1 refuerzo 1 alumno", 24 * 60, student_ids=[A[0]])
        check("1 estudiante: se crea (201) y reporta audiencia 1",
              r1.status_code == 201 and r1.json().get("audience") == 1)
        r4, cuatro = await crear("G1 especial 4 alumnos", -5, student_ids=[A[0], B[0], Cc[0], D[0]])
        check("varios estudiantes: se crea (201) y reporta audiencia 4",
              r4.status_code == 201 and r4.json().get("audience") == 4)

        print("\n[3-7] Visibilidad y entrada (estudiantes SIN matrícula)")
        for nombre, al in (("A", A), ("B", B), ("C", Cc), ("D", D)):
            en_cal, en_dash = await ve(al[1], cuatro)
            check(f"seleccionado {nombre} (sin Enrollment) la ve en calendario y dashboard", en_cal and en_dash)
        en_cal, en_dash = await ve(E[1], cuatro)
        check("NO seleccionado (sin Enrollment) NO la ve", not en_cal and not en_dash)
        en_cal, en_dash = await ve(MH, cuatro)
        check("NO seleccionado con matrícula del mismo profesor NO la ve", not en_cal and not en_dash)
        check("seleccionado A entra a Video Dorismon (200)", await join(A[1], cuatro) == 200)
        check("seleccionado D entra a Video Dorismon (200)", await join(D[1], cuatro) == 200)
        check("NO seleccionado recibe 403", await join(E[1], cuatro) == 403)
        check("NO seleccionado con matrícula recibe 403", await join(MH, cuatro) == 403)

        print("\n[8-9, 11] Profesor")
        t = (await c.get("/teacher/dashboard", headers=TH)).json()
        hoy = {s["id"]: s for s in t.get("today_schedule", [])}
        sem = {s["id"]: s for s in t.get("week_schedule", [])}
        check("el profesor ve la clase especial en 'hoy'", cuatro in hoy)
        check("profesor: audience_count = 4 en 'hoy'", hoy.get(cuatro, {}).get("audience_count") == 4)
        check("profesor: la clase de 1 alumno (mañana) en 'próximas' con audience_count = 1",
              sem.get(uno, {}).get("audience_count") == 1)
        check("profesor: el resto de clases trae audience_count numérico",
              all(isinstance(s.get("audience_count"), int) for s in list(hoy.values()) + list(sem.values())))
        rr = await c.get(f"/teacher/sessions/{cuatro}/attendance", headers=TH)
        data = rr.json() if rr.status_code == 200 else {}
        items = data.get("students", data.get("items", data)) if isinstance(data, dict) else data
        ids = {x.get("student_id") or x.get("id") for x in items} if isinstance(items, list) else set()
        check("asistencia contiene EXACTAMENTE a los 4 seleccionados",
              ids == {A[0], B[0], Cc[0], D[0]})

        print("\n[10] Dirección")
        it4, it1 = await admin_item(cuatro), await admin_item(uno)
        check("Dirección: audience_count = 4", bool(it4) and it4.get("audience_count") == 4)
        check("Dirección: audience_count = 1", bool(it1) and it1.get("audience_count") == 1)
        check("Dirección: kind NO cambia (sigue 'single')",
              bool(it4) and it4.get("kind") == "single" and it1.get("kind") == "single")

        print("\n[12-13] Estudiantes desactivados")
        r = await c.patch(f"/admin/users/{F[0]}", headers=AH, json={"is_active": False})
        check("se desactiva un estudiante de prueba", r.status_code == 200)
        u = (await c.get(f"/admin/users?role=student&q={F[2]}", headers=AH)).json()
        fila = [x for x in u.get("items", []) if x["id"] == F[0]]
        check("el buscador recibe is_active=false para el desactivado (el selector lo filtra)",
              bool(fila) and fila[0].get("is_active") is False)
        r, sid = await crear("G1 con desactivado", 120, student_ids=[A[0], F[0]])
        check("backend RECHAZA crear una clase con un estudiante desactivado (400)", r.status_code == 400)
        check("…con mensaje claro", "desactivad" in r.text.lower())
        check("…y no se creó ninguna clase", sid is None)
        r, sid = await crear("G1 solo activos", 120, student_ids=[A[0], B[0]])
        check("con estudiantes activos se sigue creando normal (201)", r.status_code == 201)

        print("\n[14-16] Clases normales, privadas y eventos siguen igual")
        dash_m = (await c.get("/student/dashboard", headers=MH)).json()
        enr = [e for e in dash_m.get("enrollments", []) if e.get("teacher_id")]
        if enr:
            e0 = enr[0]
            body = {"title": "G1 clase normal", "teacher_id": e0["teacher_id"], "course_id": e0["course_id"],
                    "level_id": e0["level_id"], "modality": "online", "video_provider": "dorismon",
                    "starts_at_utc": (now + dt.timedelta(hours=3)).isoformat(),
                    "ends_at_utc": (now + dt.timedelta(hours=4)).isoformat()}
            r = await c.post("/admin/sessions", headers=AH, json=body)
            normal = r.json().get("id") if r.status_code in (200, 201) else None
            if normal:
                creadas.append(normal)
            itn = await admin_item(normal) if normal else None
            check("normal: se crea sin student_ids (respuesta audience = null, como antes)",
                  r.status_code == 201 and r.json().get("audience") is None)
            check("normal: audience_count = 0 y kind 'single'",
                  bool(itn) and itn.get("audience_count") == 0 and itn.get("kind") == "single")
            check("normal: el alumno con esa matrícula la ve", (await ve(MH, normal))[0])
            check("normal: un alumno sin matrícula NO la ve", not (await ve(E[1], normal))[0])
        else:
            check("normal: María tiene una matrícula con profesor para probar", False)

        r = await c.post("/admin/private-classes", headers=AH, json={
            "student_id": A[0], "teacher_id": prof_id, "course_id": cid, "level_id": nivel,
            "title": "G1 privada", "starts_at_utc": (now + dt.timedelta(hours=5)).isoformat(),
            "duration_min": 60, "modality": "online", "video_provider": "dorismon"})
        priv = (r.json().get("id") or r.json().get("session_id")) if r.status_code in (200, 201) else None
        if priv:
            creadas.append(priv)
        itp = await admin_item(priv) if priv else None
        check("privada: se crea como antes", bool(priv))
        check("privada: kind 'private' y audience_count = 0",
              bool(itp) and itp.get("kind") == "private" and itp.get("audience_count") == 0)
        check("privada: su alumno la ve; otro alumno no",
              (await ve(A[1], priv))[0] and not (await ve(B[1], priv))[0] if priv else False)

        r = await c.post("/admin/events", headers=AH, json={
            "title": "G1 evento", "teacher_id": prof_id, "modality": "online", "video_provider": "dorismon",
            "starts_at_utc": (now - dt.timedelta(minutes=5)).isoformat(),
            "ends_at_utc": (now + dt.timedelta(minutes=55)).isoformat()})
        evt = (r.json().get("id") or r.json().get("session_id")) if r.status_code in (200, 201) else None
        if evt:
            creadas.append(evt)
        ite = await admin_item(evt) if evt else None
        check("evento: se crea como antes", bool(evt))
        check("evento: kind 'event' y audience_count = 0",
              bool(ite) and ite.get("kind") == "event" and ite.get("audience_count") == 0)
        check("evento: cualquier estudiante activo puede entrar (200)", bool(evt) and await join(E[1], evt) == 200)

        print("\n[limpieza]")
        borradas = limpiar_sesiones_locales(creadas)
        check(f"las {len(creadas)} clases de test_gate1 se retiran de la base local",
              borradas == len(creadas))

    print("\n[A, C y D en frontend] (estático)")
    adm = (WEB / "app" / "dashboard" / "admin" / "sessions" / "page.tsx")
    prf = (WEB / "app" / "dashboard" / "teacher" / "page.tsx")
    if adm.exists() and prf.exists():
        a, p = adm.read_text(encoding="utf-8"), prf.read_text(encoding="utf-8")
        check("A: opción 'Clase para estudiantes seleccionados' en el menú", "Clase para estudiantes seleccionados" in a)
        check("A: abre el MISMO formulario con 'Estudiantes específicos' ya elegido",
              'setAudienceMode("students"); openModal();' in a)
        check("A: el envío sigue siendo el de siempre (student_ids)",
              "body.student_ids = pickedStudents.map((x) => x.id);" in a)
        check("B: Dirección muestra '👥 N estudiantes' solo si audience_count > 0",
              "s.audience_count > 0 && <Badge" in a)
        check("C: profesor muestra '👥 Clase especial · N estudiantes'",
              p.count("👥 Clase especial ·") == 2 and p.count("s.audience_count > 0 &&") == 2)
        check("D: el selector no ofrece estudiantes desactivados",
              ".filter((u: any) => u?.is_active !== false)" in a)
    else:
        check(f"existen las páginas del frontend en {WEB}", False)

    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
