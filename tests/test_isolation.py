"""V3.9.43 — PRUEBA DE AISLAMIENTO ENTRE PROFESORES Y GRUPOS.

Este es el test más importante del sistema. Monta el escenario que causó los
problemas reales y verifica que NADIE vea ni pueda tocar lo que no es suyo.

    Profesor A → B1 → Grupo Mañana → Estudiante 1
    Profesor B → B1 → Grupo Noche  → Estudiante 2

Mismo nivel, distinto profesor y distinto grupo. El estudiante 1 no debe ver
NADA del profesor B: ni clases, ni tareas, ni quizzes, ni el video. Y tampoco
debe poder acceder conociendo el ID.
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
        seed = [p for p in profes["items"] if p["email"] in SEED_TEACHERS]
        if len(seed) < 2:
            print("  (faltan profesores de prueba)")
            return 0
        PA, PB = seed[0], seed[1]
        hoy = datetime.date.today().isoformat()
        now = datetime.datetime.now(datetime.timezone.utc)

        # Los dos estudiantes, con sus sesiones
        alumnos = []
        for e in ESTUDIANTES:
            r = await c.post("/auth/login", json=e)
            if r.status_code != 200:
                continue
            u = (await c.get(f"/admin/users?q={e['email'].split('@')[0]}", headers=AH)).json()
            alumnos.append({
                "id": u["items"][0]["id"],
                "H": {"Authorization": f"Bearer {r.json()['access_token']}"},
                "email": e["email"],
            })
        if len(alumnos) < 2:
            print("  (faltan estudiantes de prueba)")
            return 0
        E1, E2 = alumnos[0], alumnos[1]

        # Ponerlos en el MISMO nivel, con profesor y grupo distintos
        perfil = (await c.get(f"/admin/students/{E1['id']}/profile", headers=AH)).json()
        lvl = [l for l in lvls if l["code"] == perfil.get("current_level_code")][0]

        enr = (await c.get("/admin/enrollments", headers=AH)).json()
        ei = enr.get("items", enr) if isinstance(enr, dict) else enr

        grupos = {}
        for nombre, hora, profe in [("ISO Mañana", "08:00", PA), ("ISO Noche", "20:00", PB)]:
            await c.post("/admin/class-series", headers=AH, json={
                "name": nombre, "course_id": cid, "level_id": lvl["id"],
                "teacher_id": profe["id"], "days_of_week": "mon,tue,wed,thu,fri,sat,sun",
                "start_time_hhmm": hora, "duration_min": 60,
                "start_date": hoy, "num_classes": 10, "modality": "online", "video_provider": "dorismon",
                "capacity": 10,
            })
        gs = (await c.get("/admin/groups", headers=AH)).json().get("items", [])
        for g in gs:
            if g["name"] in ("ISO Mañana", "ISO Noche"):
                grupos[g["name"]] = g

        for alumno, gname, profe in [(E1, "ISO Mañana", PA), (E2, "ISO Noche", PB)]:
            mia = [x for x in ei if x.get("student_id") == alumno["id"] and x.get("is_active")]
            if mia and gname in grupos:
                # Mismo nivel para los dos
                await c.patch(f"/admin/enrollments/{mia[0]['id']}", headers=AH,
                              json={"level_id": lvl["id"], "teacher_id": profe["id"]})
                await c.post(f"/admin/enrollments/{mia[0]['id']}/assign-group",
                             headers=AH, json={"series_id": grupos[gname]["id"],
                                               "confirm_full": True})
                alumno["grupo"] = grupos[gname]
                alumno["profe"] = profe

        print("Escenario: dos profesores, mismo nivel, grupos distintos\n")

        # ---------- CLASES ----------
        ses = (await c.get("/admin/sessions?filter_period=all&limit=100", headers=AH)).json()
        del_B = [s for s in ses["items"]
                 if s.get("series_id") == grupos.get("ISO Noche", {}).get("id")]

        prog = (await c.get("/progress/my-course", headers=E1["H"])).json()
        ns = prog.get("next_session") or {}
        check("E1 NO ve la próxima clase del grupo de B",
              "ISO Noche" not in (ns.get("title") or ""))

        dash = (await c.get("/student/dashboard", headers=E1["H"])).json()
        prox = dash.get("next_classes", []) if isinstance(dash, dict) else []
        check("E1 NO tiene clases de B en su listado",
              not any("ISO Noche" in (x.get("title") or "") for x in prox))

        # ---------- VIDEO: lo más crítico ----------
        if del_B:
            v = await c.post(f"/video/sessions/{del_B[0]['id']}/join", headers=E1["H"])
            check("E1 NO puede entrar al video de la clase de B", v.status_code == 403)

        # ---------- TAREAS ----------
        tokB = (await c.post("/auth/login", json={"email": PB["email"],
                                                   "password": "Profe2026!"})).json()
        TB = {"Authorization": f"Bearer {tokB['access_token']}"}
        ta = await c.post("/teacher/assignments", headers=TB, json={
            "title": "ISO Tarea de B", "description": "x", "level_id": lvl["id"]})
        if ta.status_code == 201:
            tid = ta.json().get("id")
            lst = (await c.get("/student/assignments", headers=E1["H"])).json()
            items = lst.get("items", lst) if isinstance(lst, dict) else lst
            check("E1 NO ve la tarea del profesor B",
                  not any(x.get("id") == tid for x in items))

            env = await c.post(f"/student/assignments/{tid}/submit",
                               headers=E1["H"], json={"content": "intento"})
            check("E1 NO puede entregar la tarea de B (conociendo el ID)",
                  env.status_code == 404)

        # ---------- QUIZZES ----------
        qz = await c.post("/teacher/quizzes", headers=TB, json={
            "title": "ISO Quiz de B", "description": "x", "level_id": lvl["id"],
            "questions": [{"type": "multiple_choice", "statement": "I ___",
                           "options": ["go", "goes", "going", "gone"],
                           "correct_answer": "go", "points": 10}]})
        if qz.status_code in (200, 201):
            qid = qz.json().get("id")
            await c.post(f"/teacher/quizzes/{qid}/publish", headers=TB)

            lq = (await c.get("/student/quizzes", headers=E1["H"])).json()
            iq = lq.get("items", lq) if isinstance(lq, dict) else lq
            check("E1 NO ve el quiz del profesor B",
                  not any(x.get("id") == qid for x in iq))

            abrir = await c.get(f"/student/quizzes/{qid}", headers=E1["H"])
            check("E1 NO puede abrir el quiz de B (conociendo el ID)",
                  abrir.status_code == 404)

            resp = await c.post(f"/student/quizzes/{qid}/submit",
                                headers=E1["H"], json={"answers": []})
            check("E1 NO puede responder el quiz de B", resp.status_code == 404)

            # Y el suyo sí funciona
            tokA = (await c.post("/auth/login", json={"email": PA["email"],
                                                       "password": "Profe2026!"})).json()
            TA = {"Authorization": f"Bearer {tokA['access_token']}"}
            qa = await c.post("/teacher/quizzes", headers=TA, json={
                "title": "ISO Quiz de A", "description": "x", "level_id": lvl["id"],
                "questions": [{"type": "multiple_choice", "statement": "She ___",
                               "options": ["go", "goes", "going", "gone"],
                               "correct_answer": "goes", "points": 10}]})
            if qa.status_code in (200, 201):
                qaid = qa.json().get("id")
                await c.post(f"/teacher/quizzes/{qaid}/publish", headers=TA)
                mio = await c.get(f"/student/quizzes/{qaid}", headers=E1["H"])
                check("E1 SÍ puede abrir el quiz de SU profesor", mio.status_code == 200)

                # ---------- max_attempts ----------
                permitidos = 3
                for i in range(permitidos + 2):
                    r = await c.post(f"/student/quizzes/{qaid}/submit",
                                     headers=E1["H"], json={"answers": []})
                    if r.status_code == 400 and i >= permitidos:
                        break
                ultimo = await c.post(f"/student/quizzes/{qaid}/submit",
                                      headers=E1["H"], json={"answers": []})
                check("max_attempts se aplica de verdad", ultimo.status_code == 400)

        # ---------- Llegar tarde ----------
        tarde = (await c.post("/admin/sessions", headers=AH, json={
            "title": "ISO Clase en curso",
            "starts_at_utc": (now - datetime.timedelta(minutes=35)).isoformat(),
            "ends_at_utc": (now + datetime.timedelta(minutes=25)).isoformat(),
            "modality": "online", "video_provider": "dorismon", "teacher_id": E1.get("profe", PA)["id"],
            "course_id": cid, "level_id": lvl["id"],
            "series_id": E1.get("grupo", {}).get("id"),
            "video_provider": "dorismon",
        })).json()
        if tarde.get("id"):
            r = await c.post(f"/video/sessions/{tarde['id']}/join", headers=E1["H"])
            check("Llegando 35 min tarde, SÍ puede entrar (clase en curso)",
                  r.status_code == 200)
            if r.status_code == 200:
                check("El sistema sabe que llegó tarde", r.json().get("late") is True)

        # ---------- Eventos: auto-registro y cupo ----------
        def _ev(titulo, cupo):
            return {
                "title": titulo, "modality": "online", "teacher_id": PA["id"],
                "course_id": cid,
                "starts_at_utc": (now - datetime.timedelta(minutes=5)).isoformat(),
                "ends_at_utc": (now + datetime.timedelta(minutes=55)).isoformat(),
                "capacity": cupo, "video_provider": "dorismon",
            }

        ev = await c.post("/admin/events", headers=AH, json=_ev("ISO Evento", 30))
        check("Se puede crear un evento con el video de Dorismon (sin link)",
              ev.status_code == 201)
        if ev.status_code == 201:
            eid = ev.json().get("id")
            r = await c.post(f"/video/sessions/{eid}/join", headers=E2["H"])
            check("Sin anotarse, puede entrar al evento (auto-registro)",
                  r.status_code == 200)
            mis = (await c.get("/events/my-events", headers=E2["H"])).json()
            it = mis.get("items", mis) if isinstance(mis, dict) else mis
            check("Al entrar queda inscrito automáticamente", bool(it))

        lleno = await c.post("/admin/events", headers=AH, json=_ev("ISO Lleno", 1))
        if lleno.status_code == 201:
            lid = lleno.json().get("id")
            await c.post(f"/video/sessions/{lid}/join", headers=E1["H"])
            r2 = await c.post(f"/video/sessions/{lid}/join", headers=E2["H"])
            check("Con el cupo lleno, no deja entrar", r2.status_code == 400)

        # ══════════════════════════════════════════════════════════════════
        # ESCENARIO B — MISMO profesor, MISMO nivel, grupos DIFERENTES
        #
        # El escenario A separa a dos profesores. Este separa a dos grupos del
        # MISMO profesor: Carlos con B1 mañana y B1 noche. Antes, una tarea
        # para uno le llegaba a los dos, porque la audiencia era solo
        # teacher_id + level_id.
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Escenario B: mismo profesor, dos grupos ---")

        for nombre, hora in [("ISO-B Mañana", "07:00"), ("ISO-B Noche", "21:00")]:
            await c.post("/admin/class-series", headers=AH, json={
                "name": nombre, "course_id": cid, "level_id": lvl["id"],
                "teacher_id": PA["id"],  # EL MISMO PROFESOR en los dos
                "days_of_week": "mon,tue,wed,thu,fri,sat,sun",
                "start_time_hhmm": hora, "duration_min": 60,
                "start_date": hoy, "num_classes": 6, "modality": "online", "video_provider": "dorismon",
                "capacity": 10,
            })
        gb = (await c.get("/admin/groups", headers=AH)).json().get("items", [])
        GA = [x for x in gb if x["name"] == "ISO-B Mañana"]
        GB = [x for x in gb if x["name"] == "ISO-B Noche"]

        if GA and GB:
            enr2 = (await c.get("/admin/enrollments", headers=AH)).json()
            ei2 = enr2.get("items", enr2) if isinstance(enr2, dict) else enr2
            for alumno, grupo in [(E1, GA[0]), (E2, GB[0])]:
                m = [x for x in ei2 if x.get("student_id") == alumno["id"]
                     and x.get("is_active")]
                if m:
                    # MISMO profesor para los dos
                    await c.patch(f"/admin/enrollments/{m[0]['id']}", headers=AH,
                                  json={"level_id": lvl["id"], "teacher_id": PA["id"]})
                    await c.post(f"/admin/enrollments/{m[0]['id']}/assign-group",
                                 headers=AH, json={"series_id": grupo["id"],
                                                   "confirm_full": True})

            tokA2 = (await c.post("/auth/login", json={"email": PA["email"],
                                                        "password": "Profe2026!"})).json()
            TA2 = {"Authorization": f"Bearer {tokA2['access_token']}"}

            # --- TAREA dirigida SOLO al grupo de E1 ---
            tb = await c.post("/teacher/assignments", headers=TA2, json={
                "title": "ISO-B Tarea solo Mañana", "description": "x",
                "level_id": lvl["id"], "series_id": GA[0]["id"],
            })
            check("El profesor puede dirigir una tarea a UN grupo",
                  tb.status_code == 201)
            if tb.status_code == 201:
                tbid = tb.json().get("id")

                l1 = (await c.get("/student/assignments", headers=E1["H"])).json()
                i1 = l1.get("items", l1) if isinstance(l1, dict) else l1
                check("E1 (grupo destinatario) SÍ ve la tarea",
                      any(x.get("id") == tbid for x in i1))

                l2 = (await c.get("/student/assignments", headers=E2["H"])).json()
                i2 = l2.get("items", l2) if isinstance(l2, dict) else l2
                check("E2 (otro grupo, MISMO profesor) NO la lista",
                      not any(x.get("id") == tbid for x in i2))

                e2sub = await c.post(f"/student/assignments/{tbid}/submit",
                                     headers=E2["H"], json={"content": "intento"})
                check("E2 NO puede entregarla (conociendo el ID)",
                      e2sub.status_code == 404)

                e1sub = await c.post(f"/student/assignments/{tbid}/submit",
                                     headers=E1["H"], json={"content": "mi tarea"})
                check("E1 SÍ puede entregarla", e1sub.status_code in (200, 201))

            # --- QUIZ dirigido SOLO al grupo de E1 ---
            qb = await c.post("/teacher/quizzes", headers=TA2, json={
                "title": "ISO-B Quiz solo Mañana", "description": "x",
                "level_id": lvl["id"], "series_id": GA[0]["id"],
                "questions": [{"type": "multiple_choice", "statement": "We ___",
                               "options": ["go", "goes", "going", "gone"],
                               "correct_answer": "go", "points": 10}],
            })
            check("El profesor puede dirigir un quiz a UN grupo",
                  qb.status_code in (200, 201))
            if qb.status_code in (200, 201):
                qbid = qb.json().get("id")
                await c.post(f"/teacher/quizzes/{qbid}/publish", headers=TA2)

                lq1 = (await c.get("/student/quizzes", headers=E1["H"])).json()
                iq1 = lq1.get("items", lq1) if isinstance(lq1, dict) else lq1
                check("E1 SÍ ve el quiz de su grupo",
                      any(x.get("id") == qbid for x in iq1))

                lq2 = (await c.get("/student/quizzes", headers=E2["H"])).json()
                iq2 = lq2.get("items", lq2) if isinstance(lq2, dict) else lq2
                check("E2 (otro grupo) NO lo lista",
                      not any(x.get("id") == qbid for x in iq2))

                check("E2 NO puede abrirlo (conociendo el ID)",
                      (await c.get(f"/student/quizzes/{qbid}",
                                   headers=E2["H"])).status_code == 404)
                check("E2 NO puede responderlo",
                      (await c.post(f"/student/quizzes/{qbid}/submit",
                                    headers=E2["H"], json={"answers": []})).status_code == 404)
                check("E1 SÍ puede abrirlo",
                      (await c.get(f"/student/quizzes/{qbid}",
                                   headers=E1["H"])).status_code == 200)

            # --- CLASES: cada grupo ve la suya ---
            ses_b = (await c.get("/admin/sessions?filter_period=all&limit=100",
                                 headers=AH)).json()
            de_gb = [x for x in ses_b["items"] if x.get("series_id") == GB[0]["id"]]
            if de_gb:
                check("E1 NO puede entrar al video del otro grupo del mismo profesor",
                      (await c.post(f"/video/sessions/{de_gb[0]['id']}/join",
                                    headers=E1["H"])).status_code == 403)

            # --- Sin grupo indicado: llega a los dos (compatibilidad) ---
            tsin = await c.post("/teacher/assignments", headers=TA2, json={
                "title": "ISO-B Tarea sin grupo", "description": "x",
                "level_id": lvl["id"],
            })
            if tsin.status_code == 201:
                tsid = tsin.json().get("id")
                s1 = (await c.get("/student/assignments", headers=E1["H"])).json()
                si1 = s1.get("items", s1) if isinstance(s1, dict) else s1
                s2 = (await c.get("/student/assignments", headers=E2["H"])).json()
                si2 = s2.get("items", s2) if isinstance(s2, dict) else s2
                check("Sin grupo indicado, llega a AMBOS (compatibilidad)",
                      any(x.get("id") == tsid for x in si1)
                      and any(x.get("id") == tsid for x in si2))

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
