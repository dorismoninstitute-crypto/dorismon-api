"""V3.9.76 — Performance Gate A sin cambio de contratos.

Comprueba que las optimizaciones (JOIN en mensajes, consultas agrupadas en
los dashboards) no cambian la forma de las respuestas ni mezclan usuarios.

La parte estática verifica lo que por HTTP no se puede provocar: la API ya no
crea mensajes con `to_user_id` NULL, pero pueden existir en datos históricos,
así que `/messages/sent` debe usar OUTER JOIN para no perderlos.
"""
import asyncio
import re
import sys
import uuid
from pathlib import Path
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
OTHER_STUDENT = {"email": "carlos.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}

INBOX_KEYS = {"id", "from_user_id", "from_name", "from_role", "from_gender", "subject",
              "body", "is_ticket", "category", "priority", "status", "reply_to_id",
              "read_at", "created_at"}
SENT_KEYS = {"id", "to_user_id", "to_name", "to_role", "subject", "body", "is_ticket",
             "category", "priority", "status", "read_at", "created_at"}
STUDENT_DASH_KEYS = {"user", "stats", "enrollments", "next_classes", "next_assignment",
                     "last_grade", "recent_cancelled", "my_absence_session_ids",
                     "missed_class_alerts", "trial_info"}
STUDENT_STATS_KEYS = {"enrolled_courses", "next_classes", "pending_assignments",
                      "pending_quizzes", "certificates", "attendance_rate",
                      "attendance_records"}  # GATE 3: clave AÑADIDA (no se quitó ninguna)
TEACHER_DASH_KEYS = {"user", "stats", "today_schedule", "week_schedule",
                     "levels_distribution", "students_at_risk"}


def _funcion(src: str, nombre: str) -> str:
    """Cuerpo de `async def nombre(...)` hasta el siguiente decorador."""
    m = re.search(rf"async def {nombre}\(.*?(?=\n@router|\Z)", src, re.S)
    return m.group(0) if m else ""


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    root = Path(__file__).resolve().parents[1]
    msgs = (root / "app" / "routers" / "messages.py").read_text(encoding="utf-8")
    stud = (root / "app" / "routers" / "student.py").read_text(encoding="utf-8")
    teach = (root / "app" / "routers" / "teacher.py").read_text(encoding="utf-8")

    print("\n[estático] N+1 eliminados sin perder filas")
    inbox_src, sent_src = _funcion(msgs, "inbox"), _funcion(msgs, "sent")
    check("inbox: remitente por OUTER JOIN", ".outerjoin(User, User.id == Message.from_user_id)" in inbox_src)
    check("inbox: sin db.get(User) por mensaje", "db.get(User" not in inbox_src)
    check("sent: destinatario por OUTER JOIN (to_user_id puede ser NULL)",
          ".outerjoin(User, User.id == Message.to_user_id)" in sent_src)
    check("sent: sin db.get(User) por mensaje", "db.get(User" not in sent_src)
    dash_est = _funcion(stud, "student_dashboard")
    check("dashboard estudiante: contexto académico calculado una sola vez",
          dash_est.count("await _ctx_fn(") == 1 and "_ctx_dash(" not in dash_est)
    check("dashboard estudiante: sin consulta de entrega por tarea",
          "AssignmentSubmission.assignment_id == a.id" not in dash_est)
    check("dashboard estudiante: sin COUNT de intentos por quiz",
          "QuizAttempt.quiz_id == q.id" not in dash_est)
    check("dashboard estudiante: _build_location intacto (fuera de alcance)",
          "location = await _build_location(db, s)" in dash_est)
    dash_prof = _funcion(teach, "teacher_dashboard")
    check("dashboard profesor: asistencia agregada en una consulta",
          "group_by(SessionAttendance.student_id)" in dash_prof)
    check("dashboard profesor: 'presente' sigue siendo solo present (late no cuenta)",
          "func.count().filter(SessionAttendance.state == AttendanceState.present)" in dash_prof)
    # GATE 0 cambió deliberadamente el "hoy" del profesor: ya no es la
    # medianoche UTC (8:00 PM en RD) sino el día real de Santo Domingo.
    check("dashboard profesor: 'hoy' NO usa la medianoche UTC",
          "today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)" not in dash_prof)
    check("dashboard profesor: 'hoy' se calcula con America/Santo_Domingo",
          'ZoneInfo("America/Santo_Domingo")' in dash_prof
          and "today_start = inicio_dia_rd.astimezone(tz.utc)" in dash_prof)
    check("/teacher/sessions: sin db.get(Course/Level) por sesión",
          "db.get(Course" not in _funcion(teach, "my_sessions")
          and "db.get(Level" not in _funcion(teach, "my_sessions"))

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        logins = {}
        for nombre, cred in (("admin", ADMIN), ("maria", STUDENT),
                             ("carlos", OTHER_STUDENT), ("luis", TEACHER)):
            r = await c.post("/auth/login", json=cred)
            check(f"login {nombre}", r.status_code == 200)
            if r.status_code == 200:
                logins[nombre] = {"Authorization": f"Bearer {r.json()['access_token']}"}
        if len(logins) < 4:
            print(f"\n{passed}/{total} tests pasaron")
            return False
        AH, MH, CH, LH = logins["admin"], logins["maria"], logins["carlos"], logins["luis"]
        me_admin = (await c.get("/auth/me", headers=AH)).json()
        me_maria = (await c.get("/auth/me", headers=MH)).json()

        print("\n[HTTP] Mensajes: mismo contrato, usuario correcto, sin mezcla")
        asunto = f"v3976-{uuid.uuid4().hex[:8]}"
        r = await c.post("/messages", headers=MH, json={
            "subject": asunto, "body": "Prueba de contrato v3.9.76", "is_ticket": True})
        check("estudiante crea ticket -> 201", r.status_code == 201)

        inbox = (await c.get("/messages/inbox", headers=AH)).json()
        mio = [m for m in inbox if m.get("subject") == asunto]
        check("admin recibe el ticket en inbox", len(mio) == 1)
        if mio:
            m = mio[0]
            check("inbox: claves idénticas al contrato", set(m) == INBOX_KEYS)
            check("inbox: remitente correcto (id)", m["from_user_id"] == me_maria["id"])
            check("inbox: nombre del remitente correcto", m["from_name"] == me_maria["full_name"])
            check("inbox: rol del remitente correcto", m["from_role"] == "student")
        check("inbox: todas las filas con contrato completo",
              all(set(x) == INBOX_KEYS for x in inbox))
        check("inbox: máximo 100", len(inbox) <= 100)

        sent = (await c.get("/messages/sent", headers=MH)).json()
        mio = [m for m in sent if m.get("subject") == asunto]
        check("estudiante ve el ticket en enviados", len(mio) == 1)
        if mio:
            m = mio[0]
            check("sent: claves idénticas al contrato", set(m) == SENT_KEYS)
            check("sent: destinatario es un admin real",
                  m["to_role"] == "super_admin" and m["to_name"] not in (None, "—"))
        check("sent: solo mensajes propios",
              all(x["to_user_id"] != me_maria["id"] for x in sent))

        otro_inbox = (await c.get("/messages/inbox", headers=CH)).json()
        otro_sent = (await c.get("/messages/sent", headers=CH)).json()
        check("otro estudiante NO ve el ticket (inbox)",
              all(x.get("subject") != asunto for x in otro_inbox))
        check("otro estudiante NO ve el ticket (sent)",
              all(x.get("subject") != asunto for x in otro_sent))

        print("\n[HTTP] Dashboard estudiante: mismo contrato")
        r = await c.get("/student/dashboard", headers=MH)
        check("dashboard estudiante -> 200", r.status_code == 200)
        d = r.json() if r.status_code == 200 else {}
        check("dashboard estudiante: claves idénticas", set(d) == STUDENT_DASH_KEYS)
        check("dashboard estudiante: stats idénticas", set(d.get("stats", {})) == STUDENT_STATS_KEYS)
        check("dashboard estudiante: identidad propia", d.get("user", {}).get("id") == me_maria["id"])
        st = d.get("stats", {})
        check("contadores enteros >= 0", all(isinstance(st.get(k), int) and st.get(k) >= 0
                                             for k in ("pending_assignments", "pending_quizzes")))
        check("matrículas con teacher_id/teacher_name",
              all("teacher_id" in e and "teacher_name" in e for e in d.get("enrollments", [])))
        check("próximas clases con teacher_name y location",
              all("teacher_name" in s and "location" in s for s in d.get("next_classes", [])))
        lg = d.get("last_grade")
        check("last_grade conserva forma", lg is None or set(lg) == {"assignment_title", "score", "max_score"})

        print("\n[HTTP] Dashboard profesor y /teacher/sessions")
        r = await c.get("/teacher/dashboard", headers=LH)
        check("dashboard profesor -> 200", r.status_code == 200)
        t = r.json() if r.status_code == 200 else {}
        check("dashboard profesor: claves idénticas", set(t) == TEACHER_DASH_KEYS)
        check("hoy: con teacher_name y level_code",
              all("teacher_name" in s and "level_code" in s for s in t.get("today_schedule", [])))
        check("semana: con level_code", all("level_code" in s for s in t.get("week_schedule", [])))
        check("en riesgo: forma y umbrales",
              all(set(s) == {"student_id", "student_name", "gender", "level_code",
                             "attendance_pct", "total_classes"}
                  and s["total_classes"] >= 3 and s["attendance_pct"] < 70
                  for s in t.get("students_at_risk", [])))
        for periodo in ("upcoming", "past", "all"):
            r = await c.get(f"/teacher/sessions?filter_period={periodo}", headers=LH)
            body = r.json() if r.status_code == 200 else {}
            ok = isinstance(body, dict) and isinstance(body.get("items"), list) \
                and body.get("filter_period") == periodo
            check(f"/teacher/sessions?{periodo} -> {{items, filter_period}}", ok)
            if ok:
                check(f"/teacher/sessions?{periodo}: course_name/level_code/status presentes",
                      all("course_name" in s and "level_code" in s and "status" in s
                          for s in body["items"]))
                check(f"/teacher/sessions?{periodo}: sin canceladas",
                      all(s["status"] != "cancelled" for s in body["items"]))

        print("\n[HTTP] Dashboard Dirección")
        r = await c.get("/admin/dashboard", headers=AH)
        check("admin dashboard -> 200", r.status_code == 200)
        check("identidad admin desde /auth/me", me_admin.get("role") == "super_admin")
        r = await c.get("/student/dashboard", headers=LH)
        check("profesor NO accede al dashboard de estudiante", r.status_code == 403)

    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
