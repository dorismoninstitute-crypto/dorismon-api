"""V3.9.33 — Grupos, planes que desbloquean de verdad y tipos de tarea."""
import sys
import asyncio
import datetime
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estudiante2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")


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
        stok = (await c.post("/auth/login", json=STUDENT)).json()["access_token"]
        SH = {"Authorization": f"Bearer {stok}"}

        cid = (await c.get("/admin/courses", headers=AH)).json()[0]["id"]
        lv = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        lvls = lv["items"] if isinstance(lv, dict) else lv
        mu = (await c.get("/admin/users?q=maria.estudiante", headers=AH)).json()
        maria = mu["items"][0]["id"]
        perfil = (await c.get(f"/admin/students/{maria}/profile", headers=AH)).json()
        lvl = [l for l in lvls if l["code"] == perfil.get("current_level_code")][0]
        profes = (await c.get("/admin/users?role=teacher", headers=AH)).json()
        profe = [p for p in profes["items"] if p["email"] in SEED_TEACHERS][0]
        ptok = (await c.post("/auth/login", json={"email": profe["email"],
                                                  "password": "Profe2026!"})).json()
        TH = {"Authorization": f"Bearer {ptok['access_token']}"}
        hoy = datetime.date.today().isoformat()

        # ---------- Grupos ----------
        await c.post("/admin/class-series", headers=AH, json={
            "name": "Test Grupo Mañana", "course_id": cid, "level_id": lvl["id"],
            "teacher_id": profe["id"], "days_of_week": "mon,wed",
            "start_time_hhmm": "08:00", "duration_min": 60, "start_date": hoy,
            "num_classes": 4, "modality": "online", "capacity": 6,
        })
        g = await c.get("/admin/groups", headers=AH)
        check("El panel de grupos responde", g.status_code == 200)
        grupos = g.json().get("items", [])
        mio = [x for x in grupos if x["name"] == "Test Grupo Mañana"]
        check("El grupo aparece con sus cupos",
              bool(mio) and "capacity" in mio[0] and "spots_left" in mio[0])

        enr = (await c.get("/admin/enrollments", headers=AH)).json()
        ei = enr.get("items", enr) if isinstance(enr, dict) else enr
        mia = [e for e in ei if e.get("student_id") == maria and e.get("is_active")]
        check("Las inscripciones traen su grupo",
              bool(mia) and "series_id" in mia[0])

        if mia and mio:
            r = await c.post(f"/admin/enrollments/{mia[0]['id']}/assign-group",
                             headers=AH, json={"series_id": mio[0]["id"]})
            check("Se puede asignar un estudiante a un grupo", r.status_code == 200)

            miembros = await c.get(f"/admin/groups/{mio[0]['id']}/students", headers=AH)
            check("El grupo muestra a sus estudiantes",
                  miembros.status_code == 200 and len(miembros.json().get("items", [])) >= 1)

            # Un grupo de otro nivel no debe aceptarse
            otro = [l for l in lvls if l["id"] != lvl["id"]]
            if otro:
                await c.post("/admin/class-series", headers=AH, json={
                    "name": "Test Otro Nivel", "course_id": cid, "level_id": otro[0]["id"],
                    "teacher_id": profe["id"], "days_of_week": "tue",
                    "start_time_hhmm": "10:00", "duration_min": 60,
                    "start_date": hoy, "num_classes": 2, "modality": "online",
                })
                g2 = (await c.get("/admin/groups", headers=AH)).json().get("items", [])
                on = [x for x in g2 if x["name"] == "Test Otro Nivel"]
                if on:
                    bad = await c.post(f"/admin/enrollments/{mia[0]['id']}/assign-group",
                                       headers=AH, json={"series_id": on[0]["id"]})
                    check("Rechaza un grupo de otro nivel", bad.status_code == 400)

            # Sacarlo del grupo
            fuera = await c.post(f"/admin/enrollments/{mia[0]['id']}/assign-group",
                                 headers=AH, json={"series_id": ""})
            check("Se puede sacar del grupo", fuera.status_code == 200)

        no = await c.get("/admin/groups", headers=SH)
        check("Un estudiante NO ve el panel de grupos", no.status_code in (401, 403))

        # ---------- Planes ----------
        fk = await c.get("/admin/feature-keys", headers=AH)
        check("El catálogo de funciones responde", fk.status_code == 200)
        items = fk.json().get("items", [])
        check("Trae las funciones con nombre en español",
              len(items) >= 10 and all(i.get("label") for i in items))
        check("Marca cuáles deben ir en todos los planes",
              any(i.get("recommended_for_all") for i in items))

        planes = (await c.get("/admin/plans", headers=AH)).json()
        pl = planes.get("items", planes) if isinstance(planes, dict) else planes
        if pl:
            pid = pl[0]["id"]
            ok = await c.post(f"/admin/plans/{pid}/features", headers=AH,
                              json={"feature": "Quizzes", "feature_key": "quizzes"})
            check("Se guarda la llave que desbloquea de verdad", ok.status_code == 201)
            mala = await c.post(f"/admin/plans/{pid}/features", headers=AH,
                                json={"feature": "x", "feature_key": "no_existe"})
            check("Rechaza una llave inventada", mala.status_code == 400)

        # ---------- Tipos de tarea ----------
        niv = await c.get("/teacher/my-levels", headers=TH)
        check("El profesor ve sus niveles", niv.status_code == 200)

        for kind in ("audio", "listening", "fill_blanks", "check"):
            body = {"title": f"Test {kind}", "description": "x",
                    "level_id": lvl["id"], "kind": kind}
            if kind == "listening":
                body["media_url"] = "https://youtube.com/watch?v=abc12345678"
            if kind == "fill_blanks":
                body["blanks"] = [{"text": "I ___ to school", "answer": "went"}]
            r = await c.post("/teacher/assignments", headers=TH, json=body)
            check(f"El profesor puede crear una tarea de tipo '{kind}'", r.status_code == 201)

        ta = (await c.get("/student/assignments", headers=SH)).json()
        lst = ta.get("items", ta) if isinstance(ta, dict) else ta
        tipos = {t.get("kind") for t in lst if isinstance(t, dict)}
        check("El estudiante recibe el tipo de cada tarea", len(tipos) >= 2)

        fb = [t for t in lst if isinstance(t, dict) and t.get("kind") == "fill_blanks"]
        if fb:
            filtrada = any("answer" in str(b) for b in (fb[0].get("blanks") or []))
            check("Al estudiante NO se le manda la respuesta correcta", not filtrada)

        # ---------- V3.9.34: no ver clases de OTRO profesor ----------
        otros = [p for p in profes["items"]
                 if p["email"] in SEED_TEACHERS and p["id"] != profe["id"]]
        if mia and otros:
            # Su profesor es "profe"
            await c.patch(f"/admin/enrollments/{mia[0]['id']}", headers=AH,
                          json={"teacher_id": profe["id"]})
            # Clase suya
            await c.post("/admin/class-series", headers=AH, json={
                "name": "Test Mi Grupo", "course_id": cid, "level_id": lvl["id"],
                "teacher_id": profe["id"], "days_of_week": "mon,tue,wed,thu,fri",
                "start_time_hhmm": "09:00", "duration_min": 60,
                "start_date": hoy, "num_classes": 8, "modality": "online",
            })
            # Clase de OTRO estudiante con OTRO profesor, MISMO nivel
            await c.post("/admin/class-series", headers=AH, json={
                "name": "Test Clase Ajena", "course_id": cid, "level_id": lvl["id"],
                "teacher_id": otros[0]["id"], "days_of_week": "mon,tue,wed,thu,fri",
                "start_time_hhmm": "08:00", "duration_min": 60,
                "start_date": hoy, "num_classes": 8, "modality": "online",
            })
            prog = (await c.get("/progress/my-course", headers=SH)).json()
            ns = prog.get("next_session") or {}
            check("Su próxima clase es con SU profesor, no con otro",
                  ns.get("teacher_name") != otros[0]["full_name"])

        # ---------- V3.9.34: quizzes y tareas en TODOS los planes ----------
        qz = (await c.get("/student/quizzes", headers=SH)).json()
        check("Los quizzes NO se bloquean por plan",
              not (isinstance(qz, dict) and qz.get("blocked_by_plan")))
        ta2 = (await c.get("/student/assignments", headers=SH)).json()
        check("Las tareas NO se bloquean por plan",
              not (isinstance(ta2, dict) and ta2.get("blocked_by_plan")))

        # ---------- V3.9.34: aviso al publicar un quiz ----------
        q = await c.post("/teacher/quizzes", headers=TH, json={
            "title": "Test Quiz Aviso", "description": "x", "level_id": lvl["id"],
            "questions": [{"type": "multiple_choice", "statement": "I ___ to school",
                           "options": ["go", "goes", "going", "gone"],
                           "correct_answer": "go", "points": 10}],
        })
        check("El profesor puede crear un quiz", q.status_code == 201)
        if q.status_code == 201:
            qid = q.json().get("id")
            pub = await c.post(f"/teacher/quizzes/{qid}/publish", headers=TH)
            check("Publicar avisa a los estudiantes",
                  pub.status_code == 200 and pub.json().get("notified", 0) >= 1)

            vacio = await c.post("/teacher/quizzes", headers=TH, json={
                "title": "Test Quiz Vacio", "description": "x",
                "level_id": lvl["id"], "questions": [],
            })
            if vacio.status_code == 201:
                p2 = await c.post(f"/teacher/quizzes/{vacio.json().get('id')}/publish",
                                  headers=TH)
                check("No deja publicar un quiz sin preguntas", p2.status_code == 400)

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
