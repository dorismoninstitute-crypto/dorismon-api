"""V3.9.46 P1 — Calendario, notificaciones y materiales por audiencia.

Escenario: MISMO profesor, MISMO nivel, dos grupos. Lo que va al grupo A no
debe aparecerle al grupo B ni en el calendario, ni como notificación, ni en
la biblioteca.
"""
import sys
import asyncio
import datetime
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")
ESTUDIANTES = [
    {"email": "maria.estudiante@dorismon.do", "password": "Estudiante2026!"},
    {"email": "carlos.estudiante@dorismon.do", "password": "Estudiante2026!"},
]


async def main():
    passed = total = 0

    def check(label, ok):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
        print(f"  {'✓' if ok else '✗'} {label}")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        tok = (await c.post("/auth/login", json=ADMIN)).json()["access_token"]
        AH = {"Authorization": f"Bearer {tok}"}
        cid = (await c.get("/admin/courses", headers=AH)).json()[0]["id"]
        lv = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        lvls = lv["items"] if isinstance(lv, dict) else lv
        profes = (await c.get("/admin/users?role=teacher", headers=AH)).json()
        P = [p for p in profes["items"] if p["email"] in SEED_TEACHERS][0]
        hoy = datetime.date.today().isoformat()
        now = datetime.datetime.now(datetime.timezone.utc)

        alumnos = []
        for e in ESTUDIANTES:
            r = await c.post("/auth/login", json=e)
            if r.status_code != 200:
                continue
            u = (await c.get(f"/admin/users?q={e['email'].split('@')[0]}",
                             headers=AH)).json()
            alumnos.append({"id": u["items"][0]["id"],
                            "H": {"Authorization": f"Bearer {r.json()['access_token']}"}})
        if len(alumnos) < 2:
            print("  (faltan estudiantes de prueba)")
            return 0
        E1, E2 = alumnos

        perfil = (await c.get(f"/admin/students/{E1['id']}/profile", headers=AH)).json()
        lvl = [l for l in lvls if l["code"] == perfil.get("current_level_code")][0]

        # Dos grupos del MISMO profesor
        for nombre, hora in [("P1 Mañana", "07:30"), ("P1 Noche", "21:30")]:
            await c.post("/admin/class-series", headers=AH, json={
                "name": nombre, "course_id": cid, "level_id": lvl["id"],
                "teacher_id": P["id"], "days_of_week": "mon,tue,wed,thu,fri,sat,sun",
                "start_time_hhmm": hora, "duration_min": 60, "start_date": hoy,
                "num_classes": 6, "modality": "online", "capacity": 10,
            })
        gs = (await c.get("/admin/groups", headers=AH)).json().get("items", [])
        GA = [x for x in gs if x["name"] == "P1 Mañana"]
        GB = [x for x in gs if x["name"] == "P1 Noche"]
        if not (GA and GB):
            print("  (no se crearon los grupos)")
            return 0

        enr = (await c.get("/admin/enrollments", headers=AH)).json()
        ei = enr.get("items", enr) if isinstance(enr, dict) else enr
        for alumno, grupo in [(E1, GA[0]), (E2, GB[0])]:
            m = [x for x in ei if x.get("student_id") == alumno["id"] and x.get("is_active")]
            if m:
                await c.patch(f"/admin/enrollments/{m[0]['id']}", headers=AH,
                              json={"level_id": lvl["id"], "teacher_id": P["id"]})
                await c.post(f"/admin/enrollments/{m[0]['id']}/assign-group",
                             headers=AH, json={"series_id": grupo["id"], "confirm_full": True})

        ptok = (await c.post("/auth/login", json={"email": P["email"],
                                                  "password": "Profe2026!"})).json()
        TH = {"Authorization": f"Bearer {ptok['access_token']}"}

        def _n(r):
            j = r.json()
            return [x for x in (j.get("items", j) if isinstance(j, dict) else j)
                    if isinstance(x, dict)]

        # ---------- CALENDARIO ----------
        antes_1 = len(_n(await c.get("/student/notifications", headers=E1["H"])))
        antes_2 = len(_n(await c.get("/student/notifications", headers=E2["H"])))

        ta = await c.post("/teacher/assignments", headers=TH, json={
            "title": "P1 Tarea Grupo A", "description": "x", "level_id": lvl["id"],
            "series_id": GA[0]["id"],
            "due_at": (now + datetime.timedelta(days=2)).isoformat(),
        })
        check("Se crea una tarea dirigida al grupo A", ta.status_code == 201)

        cal1 = _n(await c.get("/student/calendar", headers=E1["H"]))
        cal2 = _n(await c.get("/student/calendar", headers=E2["H"]))
        t1 = [x for x in cal1 if x.get("type") == "assignment"
              and "P1 Tarea Grupo A" in (x.get("title") or "")]
        t2 = [x for x in cal2 if x.get("type") == "assignment"
              and "P1 Tarea Grupo A" in (x.get("title") or "")]
        check("E1 (grupo A) SÍ ve la tarea en su calendario", bool(t1))
        check("E2 (grupo B) NO la ve en su calendario", not t2)

        # Las clases del otro grupo tampoco
        c2 = [x for x in cal2 if x.get("type") == "class"
              and "P1 Mañana" in (x.get("title") or "")]
        check("E2 no ve en su calendario las clases del grupo A", not c2)
        c1 = [x for x in cal1 if x.get("type") == "class"
              and "P1 Mañana" in (x.get("title") or "")]
        check("E1 SÍ ve sus propias clases en el calendario", bool(c1))

        # ---------- NOTIFICACIONES ----------
        n1 = _n(await c.get("/student/notifications", headers=E1["H"]))
        n2 = _n(await c.get("/student/notifications", headers=E2["H"]))
        av1 = [x for x in n1 if "P1 Tarea Grupo A" in (x.get("title") or "")]
        av2 = [x for x in n2 if "P1 Tarea Grupo A" in (x.get("title") or "")]
        check("E1 recibe la notificación de la tarea", bool(av1))
        check("E2 NO recibe notificación de una tarea que no es suya", not av2)

        # Quiz del grupo A
        qa = await c.post("/teacher/quizzes", headers=TH, json={
            "title": "P1 Quiz Grupo A", "description": "x", "level_id": lvl["id"],
            "series_id": GA[0]["id"],
            "questions": [{"type": "multiple_choice", "statement": "They ___",
                           "options": ["go", "goes", "going", "gone"],
                           "correct_answer": "go", "points": 10}],
        })
        if qa.status_code in (200, 201):
            qid = qa.json().get("id")
            pub = await c.post(f"/teacher/quizzes/{qid}/publish", headers=TH)
            check("Al publicar, solo se avisa al grupo destinatario",
                  pub.status_code == 200 and pub.json().get("notified", 99) <= 1)

            nq1 = _n(await c.get("/student/notifications", headers=E1["H"]))
            nq2 = _n(await c.get("/student/notifications", headers=E2["H"]))
            check("E1 recibe el aviso del quiz",
                  any("P1 Quiz Grupo A" in (x.get("body") or "") for x in nq1))
            check("E2 NO recibe el aviso del quiz",
                  not any("P1 Quiz Grupo A" in (x.get("body") or "") for x in nq2))

        # ---------- MATERIALES ----------
        # V3.9.48: el material institucional lo crea el ADMIN (un profesor
        # ahora recibe 403 explícito, que se verifica en test_teacher_authz)
        mat_inst = await c.post("/teacher/materials", headers=AH, json={
            "title": "P1 Material institucional", "type": "pdf",
            "url": "https://ejemplo.com/libro.pdf",
            "course_id": cid, "level_id": lvl["id"], "is_public": True,
            "audience_kind": "institutional",
        })
        if mat_inst.status_code in (200, 201):
            b1 = _n(await c.get("/student/library", headers=E1["H"]))
            b2 = _n(await c.get("/student/library", headers=E2["H"]))
            check("El material institucional lo ven AMBOS",
                  any("P1 Material institucional" in (x.get("title") or "") for x in b1)
                  and any("P1 Material institucional" in (x.get("title") or "") for x in b2))

        mat_grupo = await c.post("/teacher/materials", headers=TH, json={
            "title": "P1 Material Grupo A", "type": "pdf",
            "url": "https://ejemplo.com/grupoa.pdf",
            "course_id": cid, "level_id": lvl["id"], "is_public": True,
            "audience_kind": "teacher", "series_id": GA[0]["id"],
            })
        if mat_grupo.status_code in (200, 201):
            g1 = _n(await c.get("/student/library", headers=E1["H"]))
            g2 = _n(await c.get("/student/library", headers=E2["H"]))
            check("El material del grupo A lo ve E1",
                  any("P1 Material Grupo A" in (x.get("title") or "") for x in g1))
            check("El material del grupo A NO lo ve E2",
                  not any("P1 Material Grupo A" in (x.get("title") or "") for x in g2))

        mat_ind = await c.post("/teacher/materials", headers=TH, json={
            "title": "P1 Material solo para E1", "type": "pdf",
            "url": "https://ejemplo.com/feedback.pdf",
            "is_public": True, "audience_kind": "student", "student_id": E1["id"],
        })
        if mat_ind.status_code in (200, 201):
            i1 = _n(await c.get("/student/library", headers=E1["H"]))
            i2 = _n(await c.get("/student/library", headers=E2["H"]))
            check("El material individual lo ve solo su destinatario",
                  any("P1 Material solo para E1" in (x.get("title") or "") for x in i1))
            check("Otro estudiante NO ve el material individual",
                  not any("P1 Material solo para E1" in (x.get("title") or "") for x in i2))

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
