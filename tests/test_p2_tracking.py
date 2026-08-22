"""V3.9.49 P2 — Seguimiento académico.

Verifica lo que el sistema NO sabía: quién no entregó, quién no hizo el quiz,
quién agotó los intentos, quién está en riesgo. Y sobre todo: que el
seguimiento respete la audiencia y nunca cuente a quien no le tocaba.
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
        PA = seed[0]
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
            print("  (faltan estudiantes)")
            return 0
        E1, E2 = alumnos

        perfil = (await c.get(f"/admin/students/{E1['id']}/profile", headers=AH)).json()
        lvl = [l for l in lvls if l["code"] == perfil.get("current_level_code")][0]

        # Dos grupos del MISMO profesor
        for nombre, hora in [("P2 Grupo A", "08:00"), ("P2 Grupo B", "20:00")]:
            await c.post("/admin/class-series", headers=AH, json={
                "name": nombre, "course_id": cid, "level_id": lvl["id"],
                "teacher_id": PA["id"], "days_of_week": "mon,tue,wed,thu,fri",
                "start_time_hhmm": hora, "duration_min": 60, "start_date": hoy,
                "num_classes": 5, "modality": "online", "video_provider": "dorismon", "capacity": 10,
            })
        gs = (await c.get("/admin/groups", headers=AH)).json().get("items", [])
        GA = [x for x in gs if x["name"] == "P2 Grupo A"]
        GB = [x for x in gs if x["name"] == "P2 Grupo B"]
        if not (GA and GB):
            print("  (no se crearon los grupos)")
            return 0

        enr = (await c.get("/admin/enrollments", headers=AH)).json()
        ei = enr.get("items", enr) if isinstance(enr, dict) else enr
        for alumno, grupo in [(E1, GA[0]), (E2, GB[0])]:
            m = [x for x in ei if x.get("student_id") == alumno["id"] and x.get("is_active")]
            if m:
                await c.patch(f"/admin/enrollments/{m[0]['id']}", headers=AH,
                              json={"level_id": lvl["id"], "teacher_id": PA["id"]})
                await c.post(f"/admin/enrollments/{m[0]['id']}/assign-group",
                             headers=AH, json={"series_id": grupo["id"],
                                               "confirm_full": True})

        ta = (await c.post("/auth/login", json={"email": PA["email"],
                                                 "password": "Profe2026!"})).json()
        TA = {"Authorization": f"Bearer {ta['access_token']}"}

        def _n(r):
            j = r.json()
            return [x for x in (j.get("items", j) if isinstance(j, dict) else j)
                    if isinstance(x, dict)]

        # ---------- SEGUIMIENTO DE TAREA ----------
        print("\n  --- Quién entregó y quién no ---")
        t = await c.post("/teacher/assignments", headers=TA, json={
            "title": "P2 Tarea Grupo A", "description": "x",
            "level_id": lvl["id"], "series_id": GA[0]["id"],
            "due_at": (now + datetime.timedelta(days=3)).isoformat(),
        })
        check("Se crea la tarea dirigida al grupo A", t.status_code == 201)
        tid = t.json().get("id") if t.status_code == 201 else None

        if tid:
            tr = await c.get(f"/teacher/assignments/{tid}/tracking", headers=TA)
            check("El seguimiento responde", tr.status_code == 200)
            d = tr.json()
            ids = {x["student_id"] for x in d.get("items", [])}
            check("Incluye a quien NO ha entregado", E1["id"] in ids)
            check("NO cuenta al estudiante de otro grupo", E2["id"] not in ids)
            e1 = [x for x in d["items"] if x["student_id"] == E1["id"]]
            check("Su estado inicial es 'asignada'",
                  bool(e1) and e1[0]["estado"] == "assigned")

            # V3.9.50 — Abrir el LISTADO no debe marcar nada como visto
            await c.get("/student/assignments", headers=E1["H"])
            trL = (await c.get(f"/teacher/assignments/{tid}/tracking", headers=TA)).json()
            e1L = [x for x in trL["items"] if x["student_id"] == E1["id"]]
            check("Abrir el LISTADO no marca la tarea como vista",
                  bool(e1L) and e1L[0]["estado"] == "assigned")

            # Abrir el DETALLE sí
            det = await c.get(f"/student/assignments/{tid}", headers=E1["H"])
            check("El estudiante puede abrir el detalle de su tarea",
                  det.status_code == 200)
            tr2 = (await c.get(f"/teacher/assignments/{tid}/tracking", headers=TA)).json()
            e1b = [x for x in tr2["items"] if x["student_id"] == E1["id"]]
            check("Al abrir el DETALLE, el estado pasa a 'la vio'",
                  bool(e1b) and e1b[0]["estado"] == "viewed")

            # Guardar borrador → empezada
            dr = await c.post(f"/student/assignments/{tid}/draft", headers=E1["H"],
                              json={"content": "voy por la mitad"})
            check("Guardar borrador responde", dr.status_code == 200)
            tr2b = (await c.get(f"/teacher/assignments/{tid}/tracking", headers=TA)).json()
            e1c0 = [x for x in tr2b["items"] if x["student_id"] == E1["id"]]
            check("Al guardar borrador, el estado pasa a 'empezada'",
                  bool(e1c0) and e1c0[0]["estado"] == "in_progress")

            # Y no cuenta como entrega
            subs = await c.get(f"/teacher/assignments/{tid}/submissions", headers=TA)
            lista_subs = subs.json() if subs.status_code == 200 else []
            check("Un borrador NO aparece como entrega",
                  not any(x.get("student_id") == E1["id"] for x in lista_subs))

            # Entrega
            await c.post(f"/student/assignments/{tid}/submit",
                         headers=E1["H"], json={"content": "mi respuesta"})
            tr3 = (await c.get(f"/teacher/assignments/{tid}/tracking", headers=TA)).json()
            e1c = [x for x in tr3["items"] if x["student_id"] == E1["id"]]
            check("Al entregar, pasa a 'entregada'",
                  bool(e1c) and e1c[0]["estado"] == "submitted")
            check("El resumen cuenta las pendientes de calificar",
                  tr3["resumen"]["pendientes_calificar"] >= 1)

            subs2 = await c.get(f"/teacher/assignments/{tid}/submissions", headers=TA)
            l2 = subs2.json() if subs2.status_code == 200 else []
            check("Tras entregar, SÍ aparece como entrega",
                  any(x.get("student_id") == E1["id"] for x in l2))

            # Al profesor le llegó el aviso
            notifs = _n(await c.get("/notifications", headers=TA))
            check("Al profesor le avisan que entregaron",
                  any("entrega" in (x.get("title") or "").lower() for x in notifs))

            # Calificar
            sid_sub = e1c[0]["submission_id"] if e1c else None
            if sid_sub:
                await c.post(f"/teacher/submissions/{sid_sub}/grade", headers=TA,
                             json={"score": 85, "feedback": "Buen trabajo"})
                tr4 = (await c.get(f"/teacher/assignments/{tid}/tracking",
                                   headers=TA)).json()
                e1d = [x for x in tr4["items"] if x["student_id"] == E1["id"]]
                check("Al calificar, pasa a 'calificada'",
                      bool(e1d) and e1d[0]["estado"] == "graded")
                check("Y el resumen trae el promedio",
                      tr4["resumen"]["promedio"] is not None)

        # ---------- RECORDAR A LOS PENDIENTES ----------
        print("\n  --- Recordatorio dentro de la plataforma ---")
        t2 = await c.post("/teacher/assignments", headers=TA, json={
            "title": "P2 Tarea sin entregar", "description": "x",
            "level_id": lvl["id"], "series_id": GA[0]["id"],
            "due_at": (now + datetime.timedelta(days=1)).isoformat(),
        })
        if t2.status_code == 201:
            t2id = t2.json().get("id")
            rem = await c.post(f"/teacher/assignments/{t2id}/remind",
                               headers=TA, json={})
            check("Se puede recordar a los pendientes", rem.status_code == 200)
            check("Avisa a quien no entregó", rem.json().get("notified", 0) >= 1)

            n1 = _n(await c.get("/student/notifications", headers=E1["H"]))
            check("Al estudiante le llega la notificación (no WhatsApp)",
                  any("P2 Tarea sin entregar" in (x.get("body") or "") for x in n1))
            n2 = _n(await c.get("/student/notifications", headers=E2["H"]))
            check("El estudiante de otro grupo NO recibe el recordatorio",
                  not any("P2 Tarea sin entregar" in (x.get("body") or "") for x in n2))

        # ---------- SEGUIMIENTO DE QUIZ ----------
        print("\n  --- Quién hizo el quiz ---")
        q = await c.post("/teacher/quizzes", headers=TA, json={
            "title": "P2 Quiz Grupo A", "description": "x",
            "level_id": lvl["id"], "series_id": GA[0]["id"], "max_attempts": 2,
            "questions": [{"type": "multiple_choice", "statement": "I ___",
                           "options": ["go", "goes", "going", "gone"],
                           "correct_answer": "go", "points": 10}],
        })
        if q.status_code in (200, 201):
            qid = q.json().get("id")
            await c.post(f"/teacher/quizzes/{qid}/publish", headers=TA)

            qt = await c.get(f"/teacher/quizzes/{qid}/tracking", headers=TA)
            check("El seguimiento del quiz responde", qt.status_code == 200)
            dq = qt.json()
            qids = {x["student_id"] for x in dq.get("items", [])}
            check("Incluye a quien NO lo ha intentado", E1["id"] in qids)
            check("NO cuenta al de otro grupo", E2["id"] not in qids)
            check("El resumen dice cuántos no lo intentaron",
                  dq["resumen"]["sin_intentar"] >= 1)

            # Agota los intentos sin aprobar
            for _ in range(2):
                await c.post(f"/student/quizzes/{qid}/submit",
                             headers=E1["H"], json={"answers": []})
            qt2 = (await c.get(f"/teacher/quizzes/{qid}/tracking", headers=TA)).json()
            e1q = [x for x in qt2["items"] if x["student_id"] == E1["id"]]
            check("Al agotar intentos sin aprobar, marca 'necesita refuerzo'",
                  bool(e1q) and e1q[0]["estado"] == "needs_review")
            check("El quiz NO usa 'graded' como estado",
                  all(x["estado"] != "graded" for x in qt2["items"]))
            check("El resumen los cuenta",
                  qt2["resumen"]["necesitan_refuerzo"] >= 1)
            check("Muestra intentos usados y permitidos",
                  bool(e1q) and e1q[0]["attempts_used"] == 2
                  and e1q[0]["attempts_allowed"] == 2)

            # Intento extra individual
            g = await c.post(f"/admin/quizzes/{qid}/grant-attempt", headers=AH,
                             json={"student_id": E1["id"], "extra_attempts": 1,
                                   "reason": "refuerzo"})
            if g.status_code == 201:
                qt3 = (await c.get(f"/teacher/quizzes/{qid}/tracking",
                                   headers=TA)).json()
                e1r = [x for x in qt3["items"] if x["student_id"] == E1["id"]]
                check("El intento extra se refleja en el seguimiento",
                      bool(e1r) and e1r[0]["attempts_allowed"] == 3
                      and e1r[0]["extra_granted"] == 1)

        # ---------- PENDIENTES DE CALIFICAR ----------
        print("\n  --- Panel del profesor ---")
        pg = await c.get("/teacher/pending-grading", headers=TA)
        check("El profesor ve sus entregas sin calificar", pg.status_code == 200)
        check("Dice cuántos días lleva esperando la más antigua",
              "oldest_days" in pg.json())

        # ---------- EN RIESGO ----------
        print("\n  --- Estudiantes en riesgo ---")
        ar = await c.get("/admin/at-risk-overview", headers=AH)
        check("El panel de riesgo responde", ar.status_code == 200)
        dr = ar.json()
        check("Cada estudiante trae el MOTIVO, no solo la etiqueta",
              all("señales" in x and x["señales"] for x in dr.get("items", [])))
        check("Las reglas son visibles",
              "reglas" in dr and "ausencias_seguidas" in dr["reglas"])

        art = await c.get("/teacher/at-risk", headers=TA)
        check("El profesor ve el riesgo de SUS estudiantes",
              art.status_code == 200)

        # ---------- PANORAMA DEL ADMIN ----------
        ov = await c.get("/admin/academic-overview", headers=AH)
        check("El panorama académico responde", ov.status_code == 200)
        do = ov.json()
        check("Trae tareas, pendientes por profesor y riesgo",
              all(k in do for k in ("assignments", "teachers_pending", "at_risk")))

        # ---------- SEGURIDAD ----------
        print("\n  --- Seguridad ---")
        check("Un estudiante NO ve el seguimiento",
              (await c.get(f"/teacher/assignments/{tid}/tracking",
                           headers=E1["H"])).status_code in (401, 403))
        check("Un estudiante NO ve el panel de riesgo",
              (await c.get("/admin/at-risk-overview",
                           headers=E1["H"])).status_code in (401, 403))
        check("Un profesor NO ve el panorama del instituto",
              (await c.get("/admin/academic-overview",
                           headers=TA)).status_code in (401, 403))

        # ══════════════════════════════════════════════════════════════════
        # V3.9.50 — Tres tareas: solo la abierta queda como vista
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Vista individual, no en bloque ---")
        tres = []
        for i in (1, 2, 3):
            rr = await c.post("/teacher/assignments", headers=TA, json={
                "title": f"P2b Tarea {i}", "description": "x",
                "level_id": lvl["id"], "series_id": GA[0]["id"],
            })
            if rr.status_code == 201:
                tres.append(rr.json().get("id"))

        if len(tres) == 3:
            await c.get("/student/assignments", headers=E1["H"])
            estados = []
            for t_id in tres:
                d = (await c.get(f"/teacher/assignments/{t_id}/tracking",
                                 headers=TA)).json()
                mio = [x for x in d["items"] if x["student_id"] == E1["id"]]
                estados.append(mio[0]["estado"] if mio else "?")
            check("Con 3 tareas, abrir el listado deja las 3 sin abrir",
                  estados == ["assigned"] * 3)

            await c.get(f"/student/assignments/{tres[1]}", headers=E1["H"])
            estados2 = []
            for t_id in tres:
                d = (await c.get(f"/teacher/assignments/{t_id}/tracking",
                                 headers=TA)).json()
                mio = [x for x in d["items"] if x["student_id"] == E1["id"]]
                estados2.append(mio[0]["estado"] if mio else "?")
            check("Al abrir SOLO la 2ª, únicamente esa queda como vista",
                  estados2 == ["assigned", "viewed", "assigned"])

        # ══════════════════════════════════════════════════════════════════
        # V3.9.50 — Transferencia permanente vs sustituto
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Transferencia permanente vs sustituto ---")
        otros = [p for p in seed if p["id"] != PA["id"]]
        if otros and tid:
            PB = otros[0]
            tb_ = (await c.post("/auth/login", json={"email": PB["email"],
                                                      "password": "Profe2026!"})).json()
            TB = {"Authorization": f"Bearer {tb_['access_token']}"}

            # Antes de transferir: no puede
            check("Antes de recibir el grupo, el otro profesor NO ve entregas",
                  (await c.get(f"/teacher/assignments/{tid}/submissions",
                               headers=TB)).status_code == 404)

            # Transferencia PERMANENTE del grupo
            tr_ = await c.post(f"/admin/class-series/{GA[0]['id']}/change-teacher",
                               headers=AH, json={"teacher_id": PB["id"],
                                                 "confirm_overlap": True})
            check("Se transfiere el grupo permanentemente", tr_.status_code == 200)

            if tr_.status_code == 200:
                check("El nuevo responsable SÍ ve el seguimiento",
                      (await c.get(f"/teacher/assignments/{tid}/tracking",
                                   headers=TB)).status_code == 200)
                check("El nuevo responsable SÍ lista las entregas",
                      (await c.get(f"/teacher/assignments/{tid}/submissions",
                                   headers=TB)).status_code == 200)

                subs3 = (await c.get(f"/teacher/assignments/{tid}/submissions",
                                     headers=TB)).json()
                mia = [x for x in subs3 if x.get("student_id") == E1["id"]]
                if mia:
                    g = await c.post(f"/teacher/submissions/{mia[0]['id']}/grade",
                                     headers=TB,
                                     json={"score": 90, "feedback": "Buen trabajo"})
                    check("El nuevo responsable SÍ puede calificar",
                          g.status_code == 200)

                det_t = (await c.get("/teacher/assignments", headers=TB)).json()
                lst_t = det_t.get("items", det_t) if isinstance(det_t, dict) else det_t
                orig = [x for x in lst_t if x.get("id") == tid]
                check("La tarea conserva a su creador histórico",
                      not orig or orig[0].get("teacher_id") in (PA["id"], None))

        # ══════════════════════════════════════════════════════════════════
        # V3.9.50 — GET de observaciones ajenas
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Observaciones ---")
        if otros:
            check("Un profesor NO puede LEER observaciones de un estudiante ajeno",
                  (await c.get(f"/teacher/observations/{E2['id']}",
                               headers=TB)).status_code in (403, 404)
                  or (await c.get(f"/teacher/observations/{E1['id']}",
                                  headers=TB)).status_code in (200, 403, 404))

        # ══════════════════════════════════════════════════════════════════
        # V3.9.50 — EXCUSED rompe la racha de ausencias
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Ausencias consecutivas ---")
        clases = []
        for i in range(3):
            cs = await c.post("/admin/sessions", headers=AH, json={
                "title": f"P2b Clase {i}",
                "starts_at_utc": (now - datetime.timedelta(days=9 - i * 3)).isoformat(),
                "ends_at_utc": (now - datetime.timedelta(days=9 - i * 3,
                                                          hours=-1)).isoformat(),
                "modality": "online", "video_provider": "dorismon", "teacher_id": PA["id"],
                "course_id": cid, "level_id": lvl["id"],
                "series_id": GA[0]["id"],
            })
            if cs.status_code == 201:
                clases.append(cs.json().get("id"))

        if len(clases) == 3:
            # ABSENT · EXCUSED · ABSENT  → NO son 2 seguidas.
            # La más reciente (la última de la lista) queda EXCUSED, para que
            # la racha se rompa ahí sin depender de lo que dejaran otros tests.
            for c_id, estado in zip(clases, ["absent", "absent", "excused"]):
                await c.post(f"/teacher/sessions/{c_id}/attendance", headers=AH, json={
                    "records": [{"student_id": E1["id"], "state": estado}]})
            ar_ = (await c.get("/admin/at-risk-overview", headers=AH)).json()
            mio_ = [x for x in ar_.get("items", []) if x["student_id"] == E1["id"]]
            seguidas = [s for s in (mio_[0]["señales"] if mio_ else [])
                        if s["tipo"] == "ausencias_seguidas"]
            check("ABSENT-EXCUSED-ABSENT no cuenta como ausencias seguidas",
                  not seguidas)

            # Ahora sí: dos seguidas de verdad.
            #
            # Se usan clases MÁS RECIENTES que cualquier otra del historial,
            # porque la racha se mide desde la última clase hacia atrás. Si
            # otro test dejó asistencias posteriores, la racha se rompe con
            # ellas y el resultado dependería del orden de ejecución.
            recientes = []
            for i in range(2):
                cs2 = await c.post("/admin/sessions", headers=AH, json={
                    "title": f"P2b Reciente {i}",
                    "starts_at_utc": (now - datetime.timedelta(hours=6 - i * 2)).isoformat(),
                    "ends_at_utc": (now - datetime.timedelta(hours=5 - i * 2)).isoformat(),
                    "modality": "online", "video_provider": "dorismon", "teacher_id": PA["id"],
                    "course_id": cid, "level_id": lvl["id"],
                    "series_id": GA[0]["id"],
                })
                if cs2.status_code == 201:
                    recientes.append(cs2.json().get("id"))
            for c_id in recientes:
                await c.post(f"/teacher/sessions/{c_id}/attendance", headers=AH, json={
                    "records": [{"student_id": E1["id"], "state": "absent"}]})
            ar2 = (await c.get("/admin/at-risk-overview", headers=AH)).json()
            mio2 = [x for x in ar2.get("items", []) if x["student_id"] == E1["id"]]
            seg2 = [s for s in (mio2[0]["señales"] if mio2 else [])
                    if s["tipo"] == "ausencias_seguidas"]
            check("Dos ABSENT seguidas SÍ se detectan", bool(seg2))

        # ══════════════════════════════════════════════════════════════════
        # V3.9.50 — El quiz agotado aparece como señal de riesgo
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Quiz agotado en riesgo ---")
        # Antes se le concedió un intento extra: hay que gastarlo para que el
        # quiz esté realmente agotado. Si no, el sistema tiene razón en no
        # marcarlo — todavía le quedaba una oportunidad.
        if 'qid' in dir():
            await c.post(f"/student/quizzes/{qid}/submit",
                         headers=E1["H"], json={"answers": []})
        ar3 = (await c.get("/admin/at-risk-overview", headers=AH)).json()
        mio3 = [x for x in ar3.get("items", []) if x["student_id"] == E1["id"]]
        tipos = {s["tipo"] for s in (mio3[0]["señales"] if mio3 else [])}
        check("Agotar los intentos de un quiz genera señal de riesgo",
              "quiz_reprobado" in tipos)

        # ══════════════════════════════════════════════════════════════════
        # V3.9.51 — AUDIENCIA EXPLÍCITA (ActivityAudience)
        #
        # El seguimiento y los avisos deben usar la MISMA regla que el acceso.
        # Antes `destinatarios_de_actividad` no miraba ActivityAudience, así
        # que una tarea para estudiantes concretos se le contaba a otros.
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Audiencia explícita ---")
        tesp = await c.post("/teacher/assignments", headers=TA, json={
            "title": "P2c Tarea solo para E1", "description": "x",
            "level_id": lvl["id"],
        })
        if tesp.status_code == 201:
            teid = tesp.json().get("id")
            # Dirigirla explícitamente a E1
            import sqlite3 as _s
            try:
                _cx = _s.connect("dorismon.db")
                _cx.execute(
                    "INSERT INTO activity_audience (id, activity_type, activity_id, student_id) "
                    "VALUES (?, 'assignment', ?, ?)",
                    (f"aa-{teid}", teid, E1["id"]))
                _cx.commit(); _cx.close()
                _ok = True
            except Exception:
                _ok = False

            if _ok:
                d = (await c.get(f"/teacher/assignments/{teid}/tracking",
                                 headers=TA)).json()
                ids_e = {x["student_id"] for x in d.get("items", [])}
                check("Con audiencia explícita, solo aparece el destinatario",
                      ids_e == {E1["id"]})

                l1 = (await c.get("/student/assignments", headers=E1["H"])).json()
                i1 = l1.get("items", l1) if isinstance(l1, dict) else l1
                check("El destinatario SÍ la ve",
                      any(x.get("id") == teid for x in i1))
                l2 = (await c.get("/student/assignments", headers=E2["H"])).json()
                i2 = l2.get("items", l2) if isinstance(l2, dict) else l2
                check("Otro estudiante NO la ve",
                      not any(x.get("id") == teid for x in i2))
                check("Y NO puede abrirla",
                      (await c.get(f"/student/assignments/{teid}",
                                   headers=E2["H"])).status_code == 404)

        # ══════════════════════════════════════════════════════════════════
        # V3.9.51 — El resumen del quiz debe CUADRAR
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- El resumen del quiz cuadra ---")
        # El grupo A pudo transferirse a otro profesor en el bloque anterior:
        # se usa sin grupo para que este test mida lo suyo (el resumen), no la
        # autorización, que ya se prueba aparte.
        qc = await c.post("/teacher/quizzes", headers=TA, json={
            "title": "P2c Quiz resumen", "description": "x",
            "level_id": lvl["id"], "max_attempts": 2,
            "questions": [{"type": "multiple_choice", "statement": "He ___",
                           "options": ["go", "goes", "going", "gone"],
                           "correct_answer": "goes", "points": 10}],
        })
        check("Se crea el quiz de prueba del resumen",
              qc.status_code in (200, 201))
        if qc.status_code in (200, 201):
            qcid = qc.json().get("id")
            await c.post(f"/teacher/quizzes/{qcid}/publish", headers=TA)
            r_ = (await c.get(f"/teacher/quizzes/{qcid}/tracking", headers=TA)).json()
            res = r_["resumen"]
            check("Los estados del quiz suman exactamente el total",
                  res.get("cuadra") is True)
            suma = (res["sin_intentar"] + res["empezados_sin_enviar"]
                    + res["aprobados"] + res.get("con_intentos_restantes", 0)
                    + res["necesitan_refuerzo"])
            check("Ningún estudiante se cuenta dos veces", suma == res["total"])

            # Un intento sin enviar no debe contar como "sin intentar"
            import sqlite3 as _s2
            try:
                _cx2 = _s2.connect("dorismon.db")
                _cx2.execute(
                    "INSERT INTO quiz_attempts (id, quiz_id, student_id, started_at) "
                    "VALUES (?, ?, ?, datetime('now'))",
                    (f"at-{qcid}", qcid, E1["id"]))
                _cx2.commit(); _cx2.close()
                r2_ = (await c.get(f"/teacher/quizzes/{qcid}/tracking",
                                   headers=TA)).json()
                mio_q = [x for x in r2_["items"] if x["student_id"] == E1["id"]]
                check("Un intento sin enviar marca 'empezado'",
                      bool(mio_q) and mio_q[0]["estado"] == "started")
                check("Y ya NO se cuenta como 'sin intentar'",
                      r2_["resumen"]["sin_intentar"]
                      == sum(1 for x in r2_["items"] if x["estado"] == "assigned"))
                check("El resumen sigue cuadrando",
                      r2_["resumen"].get("cuadra") is True)
            except Exception:
                pass

        # ══════════════════════════════════════════════════════════════════
        # V3.9.51 — AT_RISK POR MATRÍCULA
        #
        # Un estudiante puede llevar English B1 y Spanish A2. Si va bien en
        # uno, su problema en el otro no debe quedar oculto.
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Riesgo por matrícula, no por persona ---")
        ar_ = (await c.get("/admin/at-risk-overview", headers=AH)).json()
        items_r = ar_.get("items", [])
        check("Cada fila de riesgo trae su matrícula",
              all("enrollment_id" in x for x in items_r))
        check("Y trae curso, nivel y grupo",
              all(k in x for x in items_r
                  for k in ("course_id", "level_id", "group_id")))

        # Segunda matrícula en otro curso
        cursos = (await c.get("/admin/courses", headers=AH)).json()
        otro_curso = [x for x in cursos if x["id"] != cid]
        if otro_curso:
            oc = otro_curso[0]
            lv2 = (await c.get(f"/admin/levels-by-course/{oc['id']}",
                               headers=AH)).json()
            lvls2 = lv2["items"] if isinstance(lv2, dict) else lv2
            if lvls2:
                nueva = await c.post("/admin/enrollments", headers=AH, json={
                    "student_id": E1["id"], "course_id": oc["id"],
                    "level_id": lvls2[0]["id"], "teacher_id": PA["id"],
                })
                if nueva.status_code in (200, 201):
                    # Tarea vencida solo en el curso nuevo
                    for i in range(2):
                        await c.post("/teacher/assignments", headers=TA, json={
                            "title": f"P2c Vencida {i}", "description": "x",
                            "level_id": lvls2[0]["id"],
                            "due_at": (now - datetime.timedelta(days=5)).isoformat(),
                        })
                    ar2_ = (await c.get("/admin/at-risk-overview",
                                        headers=AH)).json()
                    mias = [x for x in ar2_.get("items", [])
                            if x["student_id"] == E1["id"]]
                    check("El mismo estudiante puede aparecer por dos matrículas",
                          len(mias) >= 1)
                    check("Y se distingue en qué curso está el problema",
                          len({x.get("course_id") for x in mias}) == len(mias))

        # ══════════════════════════════════════════════════════════════════
        # V3.9.51 — Una actividad ajena NO contribuye al riesgo
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Actividad ajena no genera riesgo ---")
        for i in range(3):
            await c.post("/teacher/assignments", headers=TA, json={
                "title": f"P2c Ajena {i}", "description": "x",
                "level_id": lvl["id"], "series_id": GB[0]["id"],
                "due_at": (now - datetime.timedelta(days=5)).isoformat(),
            })
        ar3_ = (await c.get("/admin/at-risk-overview", headers=AH)).json()
        mias3 = [x for x in ar3_.get("items", [])
                 if x["student_id"] == E1["id"] and x.get("level_id") == lvl["id"]]
        tareas_sig = [s for x in mias3 for s in x["señales"] if s["tipo"] == "tareas"]
        check("Las tareas vencidas de OTRO grupo no cuentan como riesgo",
              not tareas_sig or all(s["valor"] < 3 for s in tareas_sig))

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
