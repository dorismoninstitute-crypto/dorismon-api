"""GATE 3 — Experiencia del estudiante (sin cambiar reglas académicas).

Backend (aditivo, sin consultas nuevas):
  · /student/dashboard → cada próxima clase trae `video_provider` y `status`
    (para reutilizar el MISMO botón de entrada) y `stats.attendance_records`
    (para no mostrar "0 %" a quien aún no tiene asistencias).
Regla del test de nivel (frontend): el test sigue siendo obligatorio, SALVO que
el estudiante ya tenga una clase que el backend le autoriza a ver. Este test
comprueba los datos en los que se apoya esa decisión y que nada se marca solo.
Frontend (estático): orden, pendiente, sin repeticiones, ventana compartida,
calendario, WhatsApp y barra inferior solo para estudiante.

Uso: python tests/test_gate3.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import datetime as dt
import os
import re
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
    """Mismo aislamiento que test_gate0/1/2 (solo SQLite local)."""
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


async def parte_http(check):
    print("\n[HTTP] Datos del estudiante y regla del test de nivel")
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        async def login(cred):
            r = await c.post("/auth/login", json=cred)
            return {"Authorization": f"Bearer {r.json()['access_token']}"} if r.status_code == 200 else None

        AH, TH, MH = await login(ADMIN), await login(TEACHER), await login(MARIA)
        check("logins admin / profesor / estudiante con matrícula", all((AH, TH, MH)))
        if not all((AH, TH, MH)):
            return
        prof_id = (await c.get("/auth/me", headers=TH)).json()["id"]
        maria_id = (await c.get("/auth/me", headers=MH)).json()["id"]
        stamp = dt.datetime.now().strftime("%H%M%S%f")[:10]

        async def alumno(nombre):
            email = f"g3{stamp}{nombre}@dorismon.do"
            r = await c.post("/admin/users", headers=AH, json={
                "email": email, "full_name": f"G3 {nombre}", "password": PASS, "role": "student"})
            uid = (r.json().get("id") or r.json().get("user_id")) if r.status_code in (200, 201) else None
            return uid, await login({"email": email, "password": PASS})

        conclase, CH = await alumno("conclase")
        sinclase, SH = await alumno("sinclase")
        check("estudiantes nuevos (sin matrícula ni test) creados", all((conclase, CH, sinclase, SH)))

        cid = (await c.get("/admin/courses", headers=AH)).json()[0]["id"]
        lv = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        nivel = (lv["items"] if isinstance(lv, dict) else lv)[0]["id"]
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

        especial = await crear("G3 especial en curso", -5, student_ids=[maria_id, conclase])
        futura_meet = await crear("G3 futura Meet", 120, student_ids=[maria_id],
                                  video_provider="meet", meeting_url="https://meet.google.com/abc-defg-hij")
        check("clases creadas (especial en curso + futura con enlace externo)", bool(especial and futura_meet))

        d = (await c.get("/student/dashboard", headers=MH)).json()
        st = d.get("stats", {})
        nc = {x["id"]: x for x in d.get("next_classes", [])}
        check("próximas clases traen video_provider y status (aditivo)",
              bool(nc) and all("video_provider" in x and "status" in x for x in nc.values()))
        check("video_provider correcto: dorismon / meet",
              nc.get(especial, {}).get("video_provider") == "dorismon"
              and nc.get(futura_meet, {}).get("video_provider") == "meet")
        check("status 'scheduled' en las próximas clases", all(x["status"] == "scheduled" for x in nc.values()))
        check("claves anteriores de cada clase intactas", all(
            {"id", "title", "modality", "starts_at_utc", "ends_at_utc", "meeting_url",
             "teacher_name", "is_private", "location"} <= set(x) for x in nc.values()))
        check("stats.attendance_records es entero", isinstance(st.get("attendance_records"), int))
        check("pendientes ya llegan: pending_assignments y pending_quizzes",
              isinstance(st.get("pending_assignments"), int) and isinstance(st.get("pending_quizzes"), int))
        check("si hay tareas pendientes, llega next_assignment",
              st.get("pending_assignments", 0) == 0 or bool(d.get("next_assignment")))

        print("  · asistencia: sin registros vs con asistencia real")
        dc = (await c.get("/student/dashboard", headers=CH)).json()["stats"]
        check("sin asistencias: attendance_records = 0 (el panel dirá 'Aún sin clases registradas')",
              dc.get("attendance_records") == 0 and dc.get("attendance_rate") == 0)
        r = await c.post(f"/teacher/sessions/{especial}/attendance", headers=TH,
                         json={"records": [{"student_id": conclase, "state": "present"}]})
        check("el profesor registra asistencia real", r.status_code == 200)
        dc = (await c.get("/student/dashboard", headers=CH)).json()["stats"]
        check("con asistencia real: records = 1 y se conserva el porcentaje (100)",
              dc.get("attendance_records") == 1 and dc.get("attendance_rate") == 100)

        print("  · test de nivel: datos de la decisión y nada se marca solo")
        pc = (await c.get("/placement/status", headers=CH)).json()
        ps = (await c.get("/placement/status", headers=SH)).json()
        check("ambos nuevos tienen el test SIN completar", pc.get("completed") is False and ps.get("completed") is False)
        dcc = (await c.get("/student/dashboard", headers=CH)).json()
        pcc = (await c.get("/progress/my-course", headers=CH)).json()
        check("Gate 1 sin matrícula CON clase: el backend se la autoriza (dashboard o próxima clase)",
              any(x["id"] == especial for x in dcc.get("next_classes", [])) or (pcc.get("next_session") or {}).get("id") == especial)
        dss = (await c.get("/student/dashboard", headers=SH)).json()
        pss = (await c.get("/progress/my-course", headers=SH)).json()
        check("sin matrícula y SIN clase: nada autorizado → el panel mantiene el envío al test",
              not dss.get("next_classes") and not pss.get("next_session"))
        check("con clase puede ENTRAR por las reglas normales (/join 200)",
              (await c.post(f"/video/sessions/{especial}/join", headers=CH)).status_code == 200)
        check("sin clase NO entra a una clase ajena (/join 403)",
              (await c.post(f"/video/sessions/{especial}/join", headers=SH)).status_code == 403)
        pc2 = (await c.get("/placement/status", headers=CH)).json()
        check("tras usar el panel, el test SIGUE sin completar (no se marca solo)", pc2.get("completed") is False)
        check("no se creó matrícula", (await c.get("/student/dashboard", headers=CH)).json().get("enrollments") == [])
        perfil = (await c.get(f"/admin/students/{conclase}/profile", headers=AH)).json()
        check("no se asignó nivel ni se marcó el test en su perfil",
              pcc.get("enrolled") is False and not perfil.get("current_level_code")
              and not (perfil.get("student") or perfil).get("placement_done"))

        print("  · banner '¡Casi listo para empezar!' = sin Enrollment + sin TrialClass + sin próxima clase")

        def banner(dash, prog):  # misma condición que el panel (student/page.tsx)
            return (len(dash.get("enrollments") or []) == 0 and not dash.get("trial_info")
                    and not (prog or {}).get("next_session"))
        check("caso 1: sin matrícula, sin prueba y sin clase → banner SÍ", banner(dss, pss))
        check("caso 2: sin matrícula, sin prueba, con clase de Dirección → banner NO", not banner(dcc, pcc))
        prueba, PH = await alumno("prueba")
        r = await c.post("/payments/trial-class/request", headers=PH, json={})
        dp = (await c.get("/student/dashboard", headers=PH)).json()
        pp = (await c.get("/progress/my-course", headers=PH)).json()
        check("caso 3: con clase de prueba → su flujo de prueba (trial_info), banner NO",
              r.status_code in (200, 201) and bool(dp.get("trial_info")) and not banner(dp, pp))
        dm = (await c.get("/student/dashboard", headers=MH)).json()
        pm = (await c.get("/progress/my-course", headers=MH)).json()
        check("caso 4: con matrícula → banner NO (sin cambios)", bool(dm.get("enrollments")) and not banner(dm, pm))
        check("caso 5: sin test + con clase → sigue entrando (dato que lo decide presente)",
              pc2.get("completed") is False and (pcc.get("next_session") or {}).get("id") == especial)
        check("caso 6: sin test + sin clase → sigue yendo al test (nada autorizado)",
              ps.get("completed") is False and not dss.get("next_classes") and not pss.get("next_session"))

        print("  · limpieza")
        check(f"las {len(creadas)} clases de test_gate3 se retiran de la base local",
              limpiar_sesiones_locales(creadas) == len(creadas))


def parte_estatica(check):
    print("\n[estático] Frontend")
    pg = WEB / "app" / "dashboard" / "student" / "page.tsx"
    cal = WEB / "app" / "dashboard" / "student" / "calendar" / "page.tsx"
    lay = WEB / "app" / "dashboard" / "layout.tsx"
    hlp = WEB / "components" / "ClaseProfesor.tsx"
    tses = WEB / "app" / "dashboard" / "teacher" / "sessions" / "page.tsx"
    if not all(p.exists() for p in (pg, cal, lay, hlp, tses)):
        check(f"existen los archivos del frontend en {WEB}", False)
        return
    p, k, l, h, t = (x.read_text(encoding="utf-8") for x in (pg, cal, lay, hlp, tses))

    i = {n: p.find(m) for n, m in (("proxima", "1º: PRÓXIMA CLASE"), ("pendiente", "2º: PENDIENTE"),
                                   ("clases", "3º: PRÓXIMAS CLASES"), ("ruta", "4º: tu ruta"),
                                   ("eventos", "5º: contenido secundario"))}
    check("E1: orden próxima clase → pendiente → próximas → ruta → eventos",
          all(v > 0 for v in i.values()) and i["proxima"] < i["pendiente"] < i["clases"] < i["ruta"] < i["eventos"])
    check("E2: Pendiente usa pending_assignments / pending_quizzes / next_assignment y enlaza",
          all(x in p for x in ("stats.pending_assignments", "stats.pending_quizzes", "d.next_assignment",
                               'href="/dashboard/student/assignments"', 'href="/dashboard/student/quizzes"')))
    check("E2: caso sin pendientes", "Estás al día" in p)
    check("E3: sin 'Tu progreso' ni 'Bienvenido de vuelta' ni insignia de nivel gigante",
          "Tu progreso" not in p and "Bienvenido de vuelta" not in p and "text-3xl md:text-4xl font-black\">{levelCode}" not in p)
    check("E3: el número 'clases próximas' ya no se repite", "stats.next_classes" not in p)
    check("E3: la lista no repite la clase de la tarjeta grande", "c.id !== ns.id" in p)
    check("punto 4: sin 'Tu profesor' general; cada clase muestra su profesor",
          ">Tu profesor</p>" not in p and "enrollments[0].teacher_name" not in p
          and "c.teacher_name" in p and "ns.teacher_name" in p)
    check("E4: 'Aún sin clases registradas' cuando attendance_records = 0",
          "Aún sin clases registradas" in p and "stats.attendance_records" in p)
    check("E5: tarjetas con inicio–fin, modalidad legible y 🔴 En curso (funciones de Gate 2)",
          "rangoHora(c)" in p and "modalidadLegible(c.modality)" in p and "🔴</span> En curso" in p
          and 'estadoClase(ns, ahora) === "en_curso"' in p)
    check("E5: mismo botón de entrada (JoinClassButton) y misma regla tieneEntradaOnline",
          "<JoinClassButton session={c} />" in p and "<JoinClassButton session={ns} />" in p)
    check("L1: el ÚNICO redirect al test se conserva y solo se omite con clase autorizada",
          p.count('router.replace("/placement")') == 1 and "if (!tieneClaseAutorizada)" in p
          and "safeArray(previos[0]?.next_classes).length > 0 || !!previos[1]?.next_session" in p)
    check("L1: recordatorio visible y SIN marcar el test, asignar nivel ni matricular",
          "Te falta completar tu test de nivel" in p and 'href="/placement"' in p
          and not re.search(r"placement\.(submit|complete|finish)|enroll\(|createEnrollment", p))
    check("banner de venta: sin Enrollment + sin TrialClass + sin próxima clase autorizada (ns)",
          "{enrollments.length === 0 && !trialInfo && !ns && (" in p
          and p.count("¡Casi listo para empezar!") == 1)
    check("L1: 'Esperando coordinador' no contradice a quien ya tiene clase",
          "!progressData?.enrolled && !progressData?.next_session && (" in p)
    check("E9: avisos del teléfono al final, no arriba", p.find("<AvisosTelefono />") > i["eventos"])
    check("E6: WhatsApp en móvil NO flota (botón al final, con texto) → no tapa contenido",
          'className="lg:hidden mb-5 w-full' in p and "¿Dudas? Habla con el coordinador" in p
          and "fixed bottom-5 right-5" not in p.split("lg:hidden mb-5 w-full")[0][-400:])
    check("E6: en escritorio flota solo desde lg, bajo modales (z-40)",
          "hidden lg:flex fixed bottom-5 right-5 z-40" in p and "hidden sm:inline\">Coordinador" not in p)
    check("E7: calendario con título arriba y botones en su propia fila",
          "flex gap-2 flex-wrap mt-3" in k and "break-words" in k and 'className="flex items-center gap-4"' not in k)
    check("L3: calendario y Mis clases usan la MISMA ventana compartida",
          "dentroDeVentanaEntrada(sesion, ahora)" in k and "dentroDeVentanaEntrada(s, ahora)" in t
          and "dentroDeVentanaEntrada(c, ahora)" in p)
    fuera = [f for f in (p, k, l, t) if re.search(r"MIN_ANTES|MIN_GRACIA|-\s*30\s*\*\s*60000|\+\s*15\s*\*\s*60000", f)]
    check("L3: no hay otra copia de '30 antes / 15 después' fuera de ClaseProfesor.tsx",
          not fuera and "VENTANA_MIN_ANTES = 30" in h and "VENTANA_MIN_GRACIA = 15" in h)
    check("E8: barra inferior SOLO para estudiante",
          'const isStudent = role === "student";' in l and "{isStudent && (" in l
          and all(x in l for x in ('label: "Calendario", Icon: Calendar', 'href: "/dashboard/student/assignments", label: "Tareas"')))
    check("E8: barras de Dirección y profesor intactas",
          l.count('aria-label="Navegación rápida"') == 1 and l.count('aria-label="Navegación rápida del profesor"') == 1
          and "{isAdmin && (" in l and "{isTeacher && (" in l)


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    await parte_http(check)
    parte_estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
