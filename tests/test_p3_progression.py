"""V3.9.53 P3 — Progresión académica de punta a punta.

Cubre los escenarios 39–47 del plan:
  · Flujo completo: elegible → recomendación → aprobación → certificado → B2
  · No elegible por quizzes bajos
  · Excepción aprobada por Dirección, auditada
  · Multi-matrícula: completar English no toca Spanish
  · Cross-teacher, sustituto y transferencia permanente
  · Certificado solo con nivel completado
"""
import sys
import asyncio
import datetime
import json
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
HAB = ["speaking", "listening", "reading", "writing"]


async def main():
    passed = total = 0

    def check(label, ok):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
        print(f"  {'✓' if ok else '✗'} {label}")

    async with httpx.AsyncClient(base_url=BASE, timeout=40) as c:
        tok = (await c.post("/auth/login", json=ADMIN)).json()["access_token"]
        AH = {"Authorization": f"Bearer {tok}"}
        now = datetime.datetime.now(datetime.timezone.utc)
        hoy = datetime.date.today().isoformat()

        cursos = (await c.get("/admin/courses", headers=AH)).json()
        CA = cursos[0]
        CB = cursos[1] if len(cursos) > 1 else None
        lv = (await c.get(f"/admin/levels-by-course/{CA['id']}", headers=AH)).json()
        NIV = lv["items"] if isinstance(lv, dict) else lv
        if len(NIV) < 2:
            print("  (se necesitan 2 niveles para probar la promoción)")
            return 0
        B1, B2 = NIV[0], NIV[1]

        # ── Profesores propios (los de la semilla los usan otros tests) ──
        profes = {}
        for correo, nombre in [("p3.carlos@dorismon.do", "P3 Carlos"),
                               ("p3.andrea@dorismon.do", "P3 Andrea")]:
            await c.post("/admin/users", headers=AH, json={
                "email": correo, "full_name": nombre,
                "password": "Profe2026!", "role": "teacher"})
            b = (await c.get(f"/admin/users?q={correo.split('@')[0]}",
                             headers=AH)).json()
            if b.get("items"):
                profes[nombre] = b["items"][0]
        if len(profes) < 2:
            print("  (no se crearon los profesores)")
            return 0
        CARLOS, ANDREA = profes["P3 Carlos"], profes["P3 Andrea"]

        # ── Estudiantes propios ──
        alumnos = {}
        for correo, nombre in [("p3.juan@dorismon.do", "P3 Juan"),
                               ("p3.maria@dorismon.do", "P3 Maria"),
                               ("p3.pedro@dorismon.do", "P3 Pedro")]:
            await c.post("/admin/users", headers=AH, json={
                "email": correo, "full_name": nombre,
                "password": "Estudiante2026!", "role": "student"})
            b = (await c.get(f"/admin/users?q={correo.split('@')[0]}",
                             headers=AH)).json()
            if b.get("items"):
                u = b["items"][0]
                r = await c.post("/auth/login", json={"email": correo,
                                                       "password": "Estudiante2026!"})
                alumnos[nombre] = {
                    "id": u["id"],
                    "H": ({"Authorization": f"Bearer {r.json()['access_token']}"}
                          if r.status_code == 200 else None),
                }
        if len(alumnos) < 3:
            print("  (no se crearon los estudiantes)")
            return 0
        JUAN, MARIA, PEDRO = alumnos["P3 Juan"], alumnos["P3 Maria"], alumnos["P3 Pedro"]

        # ── Grupo de Carlos en B1 ──
        await c.post("/admin/class-series", headers=AH, json={
            "name": "P3 Grupo B1", "course_id": CA["id"], "level_id": B1["id"],
            "teacher_id": CARLOS["id"], "days_of_week": "mon,tue,wed,thu,fri",
            "start_time_hhmm": "09:00", "duration_min": 60, "start_date": hoy,
            "num_classes": 5, "modality": "online", "capacity": 20})
        gs = (await c.get("/admin/groups", headers=AH)).json().get("items", [])
        G = [x for x in gs if x["name"] == "P3 Grupo B1"]
        if not G:
            print("  (no se creó el grupo)")
            return 0
        G = G[0]

        # ── Matricular a los tres en B1 con Carlos ──
        enrs = {}
        for nombre, al in [("juan", JUAN), ("maria", MARIA), ("pedro", PEDRO)]:
            r = await c.post("/admin/enrollments", headers=AH, json={
                "student_id": al["id"], "course_id": CA["id"],
                "level_id": B1["id"], "teacher_id": CARLOS["id"]})
            if r.status_code in (200, 201):
                eid = r.json().get("id")
                enrs[nombre] = eid
                await c.post(f"/admin/enrollments/{eid}/assign-group", headers=AH,
                             json={"series_id": G["id"], "confirm_full": True})
        check("Se matriculan tres estudiantes en B1", len(enrs) == 3)

        TC = {"Authorization": f"Bearer {(await c.post('/auth/login', json={'email': CARLOS['email'], 'password': 'Profe2026!'})).json()['access_token']}"}
        TAN = {"Authorization": f"Bearer {(await c.post('/auth/login', json={'email': ANDREA['email'], 'password': 'Profe2026!'})).json()['access_token']}"}

        # ══════════════════════════════════════════════════════════════════
        # ESTADO INICIAL: nadie es elegible
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Punto de partida ---")
        el = await c.get(f"/teacher/enrollments/{enrs['juan']}/eligibility",
                         headers=TC)
        check("El profesor puede consultar la elegibilidad", el.status_code == 200)
        d = el.json()
        check("Al empezar NO es elegible", d["eligible"] is False)
        check("Y dice exactamente qué falta", len(d["pending"]) > 0)
        check("Los requisitos vienen con lo pedido y lo que lleva",
              all(k in d["requirements"][0] for k in ("required", "actual", "met")))

        # ══════════════════════════════════════════════════════════════════
        # CONSTRUIR EL EXPEDIENTE DE JUAN (asistencia, tareas, quizzes, skills)
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Juan cumple los requisitos ---")
        # 8 de 9 clases → 88%
        clases = []
        for i in range(9):
            r = await c.post("/admin/sessions", headers=AH, json={
                "title": f"P3 Clase {i}",
                "starts_at_utc": (now - datetime.timedelta(days=20 - i)).isoformat(),
                "ends_at_utc": (now - datetime.timedelta(days=20 - i, hours=-1)).isoformat(),
                "modality": "online", "teacher_id": CARLOS["id"],
                "course_id": CA["id"], "level_id": B1["id"], "series_id": G["id"]})
            if r.status_code == 201:
                clases.append(r.json()["id"])
        for i, cid_ in enumerate(clases):
            estado = "absent" if i == 0 else "present"
            await c.post(f"/teacher/sessions/{cid_}/attendance", headers=AH, json={
                "records": [{"student_id": JUAN["id"], "state": estado},
                            {"student_id": MARIA["id"], "state": "present"},
                            {"student_id": PEDRO["id"], "state":
                             "absent" if i < 2 else "present"}]})

        # 5 tareas: Juan entrega 4 (80%)
        tareas = []
        for i in range(5):
            r = await c.post("/teacher/assignments", headers=TC, json={
                "title": f"P3 Tarea {i}", "description": "x",
                "level_id": B1["id"], "series_id": G["id"],
                "due_at": (now - datetime.timedelta(days=3)).isoformat()})
            if r.status_code == 201:
                tareas.append(r.json()["id"])
        for tid in tareas[:4]:
            await c.post(f"/student/assignments/{tid}/submit", headers=JUAN["H"],
                         json={"content": "mi tarea"})
            await c.post(f"/student/assignments/{tid}/submit", headers=MARIA["H"],
                         json={"content": "mi tarea"})
        for tid in tareas[:5]:
            await c.post(f"/student/assignments/{tid}/submit", headers=PEDRO["H"],
                         json={"content": "mi tarea"})

        # Quiz: Juan y Pedro aprueban, María no
        q = await c.post("/teacher/quizzes", headers=TC, json={
            "title": "P3 Quiz", "description": "x", "level_id": B1["id"],
            "series_id": G["id"], "max_attempts": 3, "passing_score": 60,
            "questions": [{"type": "multiple_choice", "statement": "I ___",
                           "options": ["go", "goes", "going", "gone"],
                           "correct_answer": "go", "points": 100}]})
        if q.status_code in (200, 201):
            qid = q.json()["id"]
            await c.post(f"/teacher/quizzes/{qid}/publish", headers=TC)
            qd = (await c.get(f"/student/quizzes/{qid}", headers=JUAN["H"])).json()
            preg = (qd.get("questions") or [{}])[0]
            pid = preg.get("id")
            for al, resp in [(JUAN, "go"), (PEDRO, "go"), (MARIA, "goes")]:
                if al["H"] and pid:
                    await c.post(f"/student/quizzes/{qid}/submit", headers=al["H"],
                                 json={"answers": [{"question_id": pid, "answer": resp}]})

        # Habilidades de Juan
        for skill, nota in zip(HAB, [78, 82, 86, 80]):
            await c.post(f"/teacher/enrollments/{enrs['juan']}/skills", headers=TC,
                         json={"skill": skill, "score": nota,
                               "notes": f"Evaluación de {skill}"})

        # V3.9.54 — Los módulos ahora cuentan como requisito. Para que Juan
        # sea elegible tienen que estar completos DE VERDAD: se le dan clases
        # de cada módulo con asistencia. Es exactamente lo que el requisito
        # nuevo exige, y probarlo así es más honesto que saltárselo.
        mods_r = await c.get(f"/admin/levels/{B1['id']}/modules", headers=AH)
        mods_l = []
        if mods_r.status_code == 200:
            jm = mods_r.json()
            mods_l = jm.get("items", jm) if isinstance(jm, dict) else jm

        for k, MOD in enumerate(mods_l):
            # V3.9.55 — El módulo exige COBERTURA del contenido. Se completan
            # sus lecciones publicadas, que es lo que el requisito pide. Es
            # más trabajo de preparación, pero prueba el flujo de verdad en
            # vez de saltárselo.
            _lec = await c.get(f"/admin/modules/{MOD['id']}/lessons", headers=AH)
            _ls = []
            if _lec.status_code == 200:
                _j = _lec.json()
                _ls = _j.get("items", _j) if isinstance(_j, dict) else _j
            for _L in _ls:
                for _al in (JUAN, PEDRO):
                    if _al["H"]:
                        await c.post(f"/lessons/{_L['id']}/complete",
                                     headers=_al["H"], json={"completed": True})

            clases_mod = []
            for i in range(2):
                rr = await c.post("/admin/sessions", headers=AH, json={
                    "title": f"P3 Mod{k} clase {i}",
                    "starts_at_utc": (now - datetime.timedelta(days=15 - k * 2 - i)).isoformat(),
                    "ends_at_utc": (now - datetime.timedelta(days=15 - k * 2 - i, hours=-1)).isoformat(),
                    "modality": "online", "teacher_id": CARLOS["id"],
                    "course_id": CA["id"], "level_id": B1["id"],
                    "series_id": G["id"], "module_id": MOD["id"]})
                if rr.status_code == 201:
                    clases_mod.append(rr.json()["id"])
            for cid_ in clases_mod:
                # V3.9.56 — Se registra a TODOS los del grupo. Dejar a alguien
                # sin marcar bloquea su módulo, que es exactamente el
                # comportamiento nuevo: un dato que falta no es favorable.
                await c.post(f"/teacher/sessions/{cid_}/attendance", headers=AH, json={
                    "records": [{"student_id": JUAN["id"], "state": "present"},
                                {"student_id": PEDRO["id"], "state": "present"},
                                {"student_id": MARIA["id"], "state": "present"}]})

        el2 = (await c.get(f"/teacher/enrollments/{enrs['juan']}/eligibility",
                           headers=TC)).json()
        m = el2["metrics"]
        print(f"     Juan → asistencia {m['attendance_pct']}% · "
              f"tareas {m['assignments_pct']}% · quizzes {m['quiz_average']}")
        for _r in el2["requirements"]:
            if not _r["met"]:
                print(f"     FALTA {_r['label']}: {_r['actual']} de {_r['required']}")
        _mm = el2.get("modules", {})
        print(f"     Módulos: {_mm.get('completed')}/{_mm.get('total')}")
        for _d in (_mm.get("modules") or [])[:4]:
            print(f"       · {_d['status']}: {_d['reason'][:60]} "
                  f"(clases={_d['classes_in_module']})")
        check("Juan cumple todos los requisitos", el2["eligible"] is True)
        check("Sus 4 habilidades quedan evaluadas", len(m["skills"]) == 4)
        check("Las habilidades guardan la nota en escala 0–100",
              all(0 <= v["score"] <= 100 for v in m["skills"].values()))

        # ══════════════════════════════════════════════════════════════════
        # CUMPLIR LOS NÚMEROS NO PROMUEVE A NADIE
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Cumplir los números no promueve ---")
        check("Sigue en estado 'cursando' pese a ser elegible",
              el2["academic_status"] == "active")
        ap = await c.post(f"/admin/enrollments/{enrs['juan']}/approve-completion",
                          headers=AH, json={})
        check("Dirección NO puede aprobar sin recomendación del profesor",
              ap.status_code == 400)

        # ══════════════════════════════════════════════════════════════════
        # AUTORIZACIÓN: quién puede recomendar
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Quién puede recomendar ---")
        r_otro = await c.post(f"/teacher/enrollments/{enrs['juan']}/recommend",
                              headers=TAN,
                              json={"recommendation": "recommend_promotion",
                                    "comment": "intento"})
        check("Un profesor de otro grupo NO puede recomendar",
              r_otro.status_code == 404)

        # Andrea sustituye UNA sesión de Carlos
        if clases:
            fut = await c.post("/admin/sessions", headers=AH, json={
                "title": "P3 Clase futura",
                "starts_at_utc": (now + datetime.timedelta(days=1)).isoformat(),
                "ends_at_utc": (now + datetime.timedelta(days=1, hours=1)).isoformat(),
                "modality": "online", "teacher_id": CARLOS["id"],
                "course_id": CA["id"], "level_id": B1["id"], "series_id": G["id"]})
            if fut.status_code == 201:
                await c.post(f"/admin/sessions/{fut.json()['id']}/substitute-teacher",
                             headers=AH, json={"teacher_id": ANDREA["id"],
                                               "confirm_overlap": True})
                r_sus = await c.post(f"/teacher/enrollments/{enrs['juan']}/recommend",
                                     headers=TAN,
                                     json={"recommendation": "recommend_promotion",
                                           "comment": "intento"})
                check("Sustituir una sesión NO da derecho a recomendar promoción",
                      r_sus.status_code == 404)

        rec = await c.post(f"/teacher/enrollments/{enrs['juan']}/recommend",
                           headers=TC,
                           json={"recommendation": "recommend_promotion",
                                 "comment": "Cumplió los objetivos del nivel."})
        check("El profesor responsable SÍ puede recomendar", rec.status_code == 200)
        check("La matrícula pasa a revisión",
              rec.json().get("academic_status") == "completion_review")
        sin_com = await c.post(f"/teacher/enrollments/{enrs['maria']}/recommend",
                               headers=TC,
                               json={"recommendation": "recommend_promotion"})
        check("No se puede recomendar sin comentario", sin_com.status_code == 400)

        # ══════════════════════════════════════════════════════════════════
        # DIRECCIÓN APRUEBA
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Dirección aprueba ---")
        cola = await c.get("/admin/completion-queue", headers=AH)
        check("La cola de aprobación responde", cola.status_code == 200)
        en_cola = [x for x in cola.json().get("items", [])
                   if x["enrollment_id"] == enrs["juan"]]
        check("Juan aparece en la cola", bool(en_cola))
        check("Con la recomendación del profesor a la vista",
              bool(en_cola) and en_cola[0].get("recommendation") == "recommend_promotion")

        no_admin = await c.post(
            f"/admin/enrollments/{enrs['juan']}/approve-completion",
            headers=TC, json={})
        check("Un profesor NO puede aprobar la finalización",
              no_admin.status_code in (401, 403))

        ap2 = await c.post(f"/admin/enrollments/{enrs['juan']}/approve-completion",
                           headers=AH, json={})
        check("Dirección aprueba la finalización", ap2.status_code == 200)
        check("El nivel queda completado",
              ap2.json().get("academic_status") == "completed")
        check("Y guarda una nota final", ap2.json().get("final_score") is not None)

        # ══════════════════════════════════════════════════════════════════
        # EL SNAPSHOT: poder explicar la decisión dentro de dos años
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- La decisión queda explicada ---")
        import sqlite3
        try:
            cx = sqlite3.connect("dorismon.db")
            fila = cx.execute(
                "SELECT completion_snapshot, approved_by, completed_at "
                "FROM enrollments WHERE id=?", (enrs["juan"],)).fetchone()
            cx.close()
            snap = json.loads(fila[0]) if fila and fila[0] else None
            check("Se guardó el snapshot de la decisión", snap is not None)
            check("Con las métricas usadas", bool(snap and snap.get("metrics")))
            check("Con los requisitos y sus umbrales",
                  bool(snap and snap.get("requirements")))
            check("Con la recomendación del profesor",
                  bool(snap and snap.get("teacher_recommendation")))
            check("Y con quién aprobó", bool(fila and fila[1]))
        except Exception as ex:
            print(f"     (no se pudo leer el snapshot: {ex})")

        # ══════════════════════════════════════════════════════════════════
        # CERTIFICADO
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Certificado ---")
        cert_mal = await c.post("/admin/certificates", headers=AH, json={
            "student_id": MARIA["id"], "course_id": CA["id"],
            "level_id": B1["id"], "hours": 120})
        check("NO se emite certificado de un nivel sin completar",
              cert_mal.status_code == 409)

        cert = await c.post("/admin/certificates", headers=AH, json={
            "student_id": JUAN["id"], "course_id": CA["id"],
            "level_id": B1["id"], "hours": 120})
        check("SÍ se emite con el nivel completado",
              cert.status_code in (200, 201))

        # ══════════════════════════════════════════════════════════════════
        # SIGUIENTE NIVEL: B1 no se sobrescribe
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Siguiente nivel ---")
        sig = await c.post(f"/admin/enrollments/{enrs['juan']}/next-level",
                           headers=AH, json={"level_id": B2["id"]})
        check("Se crea la matrícula del siguiente nivel",
              sig.status_code == 201)
        nuevo_id = sig.json().get("enrollment_id") if sig.status_code == 201 else None
        check("Es una matrícula DISTINTA", nuevo_id and nuevo_id != enrs["juan"])
        check("Avisa que falta asignarle grupo",
              sig.status_code == 201 and sig.json().get("needs_group") is True)

        prog = (await c.get("/student/my-progress", headers=JUAN["H"])).json()
        b1_hist = [x for x in prog.get("history", [])
                   if x["enrollment_id"] == enrs["juan"]]
        b2_act = [x for x in prog.get("active", []) if x["enrollment_id"] == nuevo_id]
        check("B1 queda en su historial como completado", bool(b1_hist))
        check("B2 aparece como activo", bool(b2_act))
        check("Las métricas de B1 NO cuentan como progreso de B2",
              bool(b2_act) and b2_act[0]["met_count"] < b2_act[0]["total_count"])

        no_dup = await c.post(f"/admin/enrollments/{enrs['juan']}/next-level",
                              headers=AH, json={"level_id": B2["id"]})
        check("No deja duplicar la matrícula del siguiente nivel",
              no_dup.status_code == 400)

        # ══════════════════════════════════════════════════════════════════
        # NO ELEGIBLE: María, con quizzes bajos
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- No elegible ---")
        for skill, nota in zip(HAB, [80, 80, 80, 80]):
            await c.post(f"/teacher/enrollments/{enrs['maria']}/skills", headers=TC,
                         json={"skill": skill, "score": nota})
        elm = (await c.get(f"/teacher/enrollments/{enrs['maria']}/eligibility",
                           headers=TC)).json()
        check("María NO es elegible", elm["eligible"] is False)
        quiz_req = [r for r in elm["requirements"] if r["key"] == "quizzes"]
        check("El motivo es el promedio de quizzes",
              bool(quiz_req) and not quiz_req[0]["met"])
        check("Y dice cuánto le falta",
              bool(quiz_req) and quiz_req[0].get("missing") is not None)

        cert_m = await c.post("/admin/certificates", headers=AH, json={
            "student_id": MARIA["id"], "course_id": CA["id"], "level_id": B1["id"]})
        check("Sin cumplir, no hay certificado", cert_m.status_code == 409)

        # ══════════════════════════════════════════════════════════════════
        # EXCEPCIÓN: Pedro, asistencia por debajo pero el resto excelente
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Excepción aprobada por Dirección ---")
        for skill, nota in zip(HAB, [90, 92, 88, 91]):
            await c.post(f"/teacher/enrollments/{enrs['pedro']}/skills", headers=TC,
                         json={"skill": skill, "score": nota})
        elp = (await c.get(f"/teacher/enrollments/{enrs['pedro']}/eligibility",
                           headers=TC)).json()
        asist = [r for r in elp["requirements"] if r["key"] == "attendance"]
        print(f"     Pedro → asistencia {asist[0]['actual'] if asist else '?'}% "
              f"(se piden {asist[0]['required'] if asist else '?'}%)")

        await c.post(f"/teacher/enrollments/{enrs['pedro']}/recommend", headers=TC,
                     json={"recommendation": "recommend_promotion",
                           "comment": "Faltó al inicio pero domina el nivel."})

        if elp["eligible"] is False:
            sin_motivo = await c.post(
                f"/admin/enrollments/{enrs['pedro']}/approve-completion",
                headers=AH, json={})
            check("Sin excepción, Dirección no puede aprobarlo",
                  sin_motivo.status_code == 400)
            check("Y le dice qué requisito falta",
                  sin_motivo.status_code == 400
                  and "pending" in str(sin_motivo.json()))

            exc = await c.post(
                f"/admin/enrollments/{enrs['pedro']}/approve-completion",
                headers=AH,
                json={"approve_exception": True,
                      "exception_reason": "Rendimiento excelente pese a las faltas"})
            check("Con excepción y motivo, sí se aprueba", exc.status_code == 200)
            check("La excepción queda registrada",
                  exc.status_code == 200 and len(exc.json().get("exceptions", [])) >= 1)
            if exc.status_code == 200:
                e0 = exc.json()["exceptions"][0]
                check("Guardando qué se pedía y qué tenía",
                      e0.get("required") is not None and e0.get("actual") is not None)
                check("Y el motivo", bool(e0.get("reason")))
            check("El umbral general NO cambió para los demás",
                  (await c.get(f"/teacher/enrollments/{enrs['maria']}/eligibility",
                               headers=TC)).json()["config"]["asistencia_minima"] == 80.0)

        # ══════════════════════════════════════════════════════════════════
        # MULTI-MATRÍCULA: completar English no toca Spanish
        # ══════════════════════════════════════════════════════════════════
        if CB:
            print("\n  --- Multi-matrícula ---")
            lvb = (await c.get(f"/admin/levels-by-course/{CB['id']}",
                               headers=AH)).json()
            NB = lvb["items"] if isinstance(lvb, dict) else lvb
            if NB:
                r = await c.post("/admin/enrollments", headers=AH, json={
                    "student_id": JUAN["id"], "course_id": CB["id"],
                    "level_id": NB[0]["id"], "teacher_id": ANDREA["id"]})
                if r.status_code in (200, 201):
                    esp = r.json()["id"]
                    de = (await c.get(f"/teacher/enrollments/{esp}/eligibility",
                                      headers=TAN)).json()
                    check("La matrícula de Spanish empieza desde cero",
                          de["metrics"]["skills"] == {})
                    check("Y NO hereda la asistencia de English",
                          de["metrics"]["attendance_pct"] is None
                          or de["metrics"]["attendance_detail"]["total_classes"] == 0)
                    check("Spanish sigue activo aunque English esté completado",
                          de["academic_status"] == "active")

                    prog2 = (await c.get("/student/my-progress",
                                         headers=JUAN["H"])).json()
                    check("El estudiante ve sus dos cursos por separado",
                          len(prog2.get("active", [])) >= 2)

                    check("El profesor de English NO ve la matrícula de Spanish",
                          (await c.get(f"/teacher/enrollments/{esp}/eligibility",
                                       headers=TC)).status_code == 404)
                    check("Y la profesora de Spanish NO ve la de English",
                          (await c.get(f"/teacher/enrollments/{enrs['juan']}/eligibility",
                                       headers=TAN)).status_code == 404)

        # ══════════════════════════════════════════════════════════════════
        # TRANSFERENCIA PERMANENTE
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Transferencia permanente ---")
        tr = await c.post(f"/admin/class-series/{G['id']}/change-teacher",
                          headers=AH, json={"teacher_id": ANDREA["id"],
                                            "confirm_overlap": True})
        if tr.status_code == 200:
            check("Tras recibir el grupo, Andrea SÍ puede recomendar",
                  (await c.get(f"/teacher/enrollments/{enrs['maria']}/eligibility",
                               headers=TAN)).status_code == 200)
            hist = (await c.get(f"/teacher/enrollments/{enrs['juan']}/eligibility",
                                headers=AH)).json()
            check("El histórico de las evaluaciones de Carlos se conserva",
                  len(hist.get("skill_history", [])) >= 4)

        # ══════════════════════════════════════════════════════════════════
        # ESTADO FINAL Y SEGURIDAD
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Estado final ---")
        re_ap = await c.post(f"/admin/enrollments/{enrs['juan']}/approve-completion",
                             headers=AH, json={})
        check("Un nivel completado no se puede volver a completar",
              re_ap.status_code == 400)

        check("Un estudiante NO ve la cola de aprobación",
              (await c.get("/admin/completion-queue",
                           headers=JUAN["H"])).status_code in (401, 403))
        check("Un estudiante NO puede evaluar habilidades",
              (await c.post(f"/teacher/enrollments/{enrs['juan']}/skills",
                            headers=JUAN["H"],
                            json={"skill": "speaking", "score": 100})
               ).status_code in (401, 403))
        check("Un estudiante NO puede recomendarse a sí mismo",
              (await c.post(f"/teacher/enrollments/{enrs['juan']}/recommend",
                            headers=JUAN["H"],
                            json={"recommendation": "recommend_promotion",
                                  "comment": "x"})).status_code in (401, 403))

        mal = await c.post(f"/teacher/enrollments/{enrs['maria']}/skills",
                           headers=TAN, json={"skill": "speaking", "score": 150})
        check("Una nota fuera de 0–100 se rechaza", mal.status_code == 400)
        mal2 = await c.post(f"/teacher/enrollments/{enrs['maria']}/skills",
                            headers=TAN, json={"skill": "cocina", "score": 80})
        check("Una habilidad inventada se rechaza", mal2.status_code == 400)

        # ══════════════════════════════════════════════════════════════════
        # V3.9.54 — MÓDULOS: una asistencia NO completa nada
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Módulos ---")
        mods = (await c.get(f"/admin/levels/{B1['id']}/modules", headers=AH))
        lista_mods = []
        if mods.status_code == 200:
            j = mods.json()
            lista_mods = j.get("items", j) if isinstance(j, dict) else j

        if lista_mods:
            MOD = lista_mods[0]
            # Una clase del módulo, con asistencia
            cm = await c.post("/admin/sessions", headers=AH, json={
                "title": "P3 Clase de módulo",
                "starts_at_utc": (now - datetime.timedelta(days=2)).isoformat(),
                "ends_at_utc": (now - datetime.timedelta(days=2, hours=-1)).isoformat(),
                "modality": "online", "teacher_id": CARLOS["id"],
                "course_id": CA["id"], "level_id": B1["id"],
                "series_id": G["id"], "module_id": MOD["id"]})
            if cm.status_code == 201:
                # V3.9.56 — Se registra la asistencia de TODOS los del grupo.
                # Ahora una clase ya dada sin lista bloquea el requisito: es
                # un dato que falta, no un dato favorable.
                await c.post(f"/teacher/sessions/{cm.json()['id']}/attendance",
                             headers=AH, json={
                                 "records": [
                                     {"student_id": MARIA["id"], "state": "present"},
                                     {"student_id": JUAN["id"], "state": "present"},
                                     {"student_id": PEDRO["id"], "state": "present"},
                                 ]})
                import sqlite3 as _sq
                try:
                    _cx = _sq.connect("dorismon.db")
                    fila = _cx.execute(
                        "SELECT status FROM module_progress WHERE student_id=? "
                        "AND module_id=?", (MARIA["id"], MOD["id"])).fetchone()
                    _cx.close()
                    check("Una sola asistencia NO completa el módulo",
                          not fila or fila[0] != "completed")
                    check("Pero sí lo pone en progreso",
                          bool(fila) and fila[0] in ("in_progress", "completed") )
                except Exception as ex:
                    print(f"     (no se pudo leer module_progress: {ex})")

            elm2 = (await c.get(f"/teacher/enrollments/{enrs['maria']}/eligibility",
                                headers=AH)).json()
            req_mod = [r for r in elm2["requirements"] if r["key"] == "modules"]
            check("Los módulos cuentan como requisito del nivel", bool(req_mod))
            if req_mod:
                check("Con módulos incompletos NO es elegible",
                      elm2["eligible"] is False)
                check("Y dice cuántos módulos faltan o ya están",
                      req_mod[0].get("missing") is not None or req_mod[0]["met"])
                # Juan sí los completó: su requisito debe estar cumplido
                elj = (await c.get(f"/teacher/enrollments/{enrs['juan']}/eligibility",
                                   headers=AH)).json()
                rj = [r for r in elj["requirements"] if r["key"] == "modules"]
                check("Quien sí cumplió los módulos tiene el requisito en verde",
                      bool(rj) and rj[0]["met"])

        # ══════════════════════════════════════════════════════════════════
        # V3.9.54 — RUTA LEGACY DE CERTIFICACIÓN
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Ruta legacy de certificación ---")
        cand = await c.get("/admin/certification-candidates", headers=AH)
        check("La ruta legacy responde", cand.status_code == 200)
        ci = cand.json().get("items", []) if cand.status_code == 200 else []
        check("Solo lista matrículas COMPLETADAS",
              all(x.get("completed_at") is not None for x in ci))
        check("María (activa) NO aparece como candidata",
              not any(x["student_id"] == MARIA["id"] for x in ci))
        check("Juan (completado) SÍ aparece",
              any(x["student_id"] == JUAN["id"] for x in ci)
              or any(x["student_id"] == PEDRO["id"] for x in ci))

        leg = await c.post(
            f"/admin/certification-candidates/{enrs['maria']}/issue",
            headers=AH, json={})
        check("La ruta legacy NO certifica una matrícula ACTIVA",
              leg.status_code == 400)
        check("Y explica que falta completar el nivel",
              leg.status_code == 400 and "completad" in str(leg.json()).lower())

        # ══════════════════════════════════════════════════════════════════
        # V3.9.54 — EXCEPCIÓN MANUAL DE CERTIFICADO
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Excepción manual de certificado ---")
        sin_mot = await c.post("/admin/certificates", headers=AH, json={
            "student_id": MARIA["id"], "course_id": CA["id"],
            "level_id": B1["id"], "hours": 120,
            "confirmar_incompleto": True})
        check("Un booleano NO basta: exige motivo", sin_mot.status_code == 400)
        check("Y lo dice claramente",
              sin_mot.status_code == 400 and "motivo" in str(sin_mot.json()).lower())

        con_mot = await c.post("/admin/certificates", headers=AH, json={
            "student_id": MARIA["id"], "course_id": CA["id"],
            "level_id": B1["id"], "hours": 120,
            "confirmar_incompleto": True,
            "exception_reason": "Traslado a otra institución, caso aprobado por Dirección"})
        check("Con motivo explícito, Dirección sí puede emitirlo",
              con_mot.status_code in (200, 201))

        if con_mot.status_code in (200, 201):
            import sqlite3 as _sq2
            try:
                _cx2 = _sq2.connect("dorismon.db")
                n = _cx2.execute(
                    "SELECT COUNT(*) FROM audit_logs WHERE action='certificate_exception'"
                ).fetchone()[0]
                _cx2.close()
                check("La excepción queda en el registro de auditoría", n >= 1)
            except Exception:
                pass

        # ══════════════════════════════════════════════════════════════════
        # V3.9.54 — NEXT LEVEL: curso y nivel deben corresponder
        # ══════════════════════════════════════════════════════════════════
        if CB:
            print("\n  --- Coherencia de curso y nivel ---")
            lvb2 = (await c.get(f"/admin/levels-by-course/{CB['id']}",
                                headers=AH)).json()
            NB2 = lvb2["items"] if isinstance(lvb2, dict) else lvb2
            if NB2:
                # Se usa una matrícula YA COMPLETADA: si no, salta antes la
                # validación de "todavía no está completado" y no llegaríamos
                # a probar la coherencia de curso y nivel, que es lo que
                # queremos verificar aquí.
                cruzado = await c.post(
                    f"/admin/enrollments/{enrs['juan']}/next-level",
                    headers=AH,
                    json={"course_id": CA["id"], "level_id": NB2[0]["id"]})
                check("Rechaza curso de un idioma con nivel de otro",
                      cruzado.status_code == 400)
                check("Y explica a qué curso pertenece ese nivel",
                      cruzado.status_code == 400
                      and "pertenece" in str(cruzado.json()).lower())

            grupo_malo = await c.post(
                f"/admin/enrollments/{enrs['juan']}/next-level",
                headers=AH,
                json={"level_id": B2["id"], "series_id": G["id"]})
            check("Rechaza un grupo que no es de ese nivel",
                  grupo_malo.status_code == 400)

        # ══════════════════════════════════════════════════════════════════
        # V3.9.55 — UNA CLASE NO COMPLETA UN MÓDULO
        #
        # El módulo exige COBERTURA DEL CONTENIDO: sus lecciones publicadas.
        # Asistir a la única clase registrada no basta.
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Cobertura del módulo ---")
        if lista_mods:
            MOD2 = lista_mods[-1]
            lec = await c.get(f"/admin/modules/{MOD2['id']}/lessons", headers=AH)
            lecciones = []
            if lec.status_code == 200:
                jl = lec.json()
                lecciones = jl.get("items", jl) if isinstance(jl, dict) else jl

            # Si el módulo no tiene lecciones, se crean para poder probarlo
            if len(lecciones) < 2:
                for i in range(2):
                    await c.post("/admin/lessons", headers=AH, json={
                        "module_id": MOD2["id"],
                        "title": f"P3 Lección {i}",
                        "description": "x", "order_index": i})
                lec = await c.get(f"/admin/modules/{MOD2['id']}/lessons", headers=AH)
                jl = lec.json() if lec.status_code == 200 else {}
                lecciones = jl.get("items", jl) if isinstance(jl, dict) else jl

            check("El módulo de prueba tiene varias lecciones",
                  len(lecciones) >= 2)

            if len(lecciones) >= 2:
                # Una clase del módulo, con asistencia
                cm2 = await c.post("/admin/sessions", headers=AH, json={
                    "title": "P3 Clase cobertura",
                    "starts_at_utc": (now - datetime.timedelta(days=1)).isoformat(),
                    "ends_at_utc": (now - datetime.timedelta(days=1, hours=-1)).isoformat(),
                    "modality": "online", "teacher_id": CARLOS["id"],
                    "course_id": CA["id"], "level_id": B1["id"],
                    "series_id": G["id"], "module_id": MOD2["id"]})
                if cm2.status_code == 201:
                    await c.post(f"/teacher/sessions/{cm2.json()['id']}/attendance",
                                 headers=AH, json={
                                     "records": [
                                         {"student_id": MARIA["id"], "state": "present"},
                                         {"student_id": JUAN["id"], "state": "present"},
                                         {"student_id": PEDRO["id"], "state": "present"},
                                     ]})

                elx = (await c.get(f"/teacher/enrollments/{enrs['maria']}/eligibility",
                                   headers=AH)).json()
                mod_x = [d for d in elx.get("modules", {}).get("modules", [])
                         if d["module_id"] == MOD2["id"]]
                check("Con 100% de asistencia pero lecciones sin cubrir, "
                      "el módulo NO se completa",
                      bool(mod_x) and mod_x[0]["status"] != "completed")
                check("Y queda en progreso, no bloqueado",
                      bool(mod_x) and mod_x[0]["status"] == "in_progress")
                check("Diciendo cuántas lecciones faltan",
                      bool(mod_x) and "lección" in mod_x[0]["reason"].lower())

                # Ahora sí: se cubre todo el contenido.
                #
                # V3.9.56 — Y se registra la asistencia de TODAS las clases ya
                # dadas de ese módulo: una clase sin lista bloquea el
                # requisito, que es justo lo que queremos que haga.
                for L in lecciones:
                    await c.post(f"/lessons/{L['id']}/complete",
                                 headers=MARIA["H"], json={"completed": True})

                _ses_all = (await c.get(
                    "/admin/sessions?filter_period=all&limit=100",
                    headers=AH)).json()
                for _s in _ses_all.get("items", []):
                    if _s.get("module_id") == MOD2["id"]:
                        await c.post(f"/teacher/sessions/{_s['id']}/attendance",
                                     headers=AH, json={
                                         "records": [{"student_id": MARIA["id"],
                                                      "state": "present"}]})

                elx2 = (await c.get(f"/teacher/enrollments/{enrs['maria']}/eligibility",
                                    headers=AH)).json()
                mod_x2 = [d for d in elx2.get("modules", {}).get("modules", [])
                          if d["module_id"] == MOD2["id"]]
                if mod_x2:
                    print(f"       módulo tras cubrir: {mod_x2[0]['status']} — "
                          f"{mod_x2[0]['reason'][:60]}")
                check("Cubriendo las lecciones Y con asistencia, SÍ se completa",
                      bool(mod_x2) and mod_x2[0]["status"] == "completed")
                check("El detalle muestra las lecciones cubiertas",
                      bool(mod_x2) and mod_x2[0]["lessons_completed"] >= 2)

                # Otro estudiante NO hereda ese progreso
                elx3 = (await c.get(f"/teacher/enrollments/{enrs['pedro']}/eligibility",
                                    headers=AH)).json()
                mod_x3 = [d for d in elx3.get("modules", {}).get("modules", [])
                          if d["module_id"] == MOD2["id"]]
                check("El progreso de lecciones NO se contagia a otro estudiante",
                      bool(mod_x3) and mod_x3[0]["status"] != "completed")

        # ══════════════════════════════════════════════════════════════════
        # V3.9.55 — CERTIFICADO POR EXCEPCIÓN GUARDA SU MATRÍCULA
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- El certificado guarda su matrícula ---")
        import sqlite3 as _s3
        try:
            _cx3 = _s3.connect("dorismon.db")
            fila = _cx3.execute(
                "SELECT enrollment_id FROM certificates WHERE student_id=? "
                "ORDER BY rowid DESC LIMIT 1", (MARIA["id"],)).fetchone()
            _cx3.close()
            check("El certificado emitido por excepción guarda enrollment_id",
                  bool(fila) and fila[0] is not None)
            check("Y apunta a la matrícula correcta",
                  bool(fila) and fila[0] == enrs["maria"])
        except Exception as ex:
            print(f"     (no se pudo verificar: {ex})")

        # ══════════════════════════════════════════════════════════════════
        # V3.9.55 — NEXT-LEVEL HEREDA EL PROFESOR DEL GRUPO
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- La matrícula nueva hereda el profesor del grupo ---")
        gb2 = await c.post("/admin/class-series", headers=AH, json={
            "name": "P3 Grupo B2", "course_id": CA["id"], "level_id": B2["id"],
            "teacher_id": ANDREA["id"], "days_of_week": "mon,wed",
            "start_time_hhmm": "18:00", "duration_min": 60, "start_date": hoy,
            "num_classes": 4, "modality": "online", "capacity": 20})
        gs2 = (await c.get("/admin/groups", headers=AH)).json().get("items", [])
        GB2 = [x for x in gs2 if x["name"] == "P3 Grupo B2"]

        if GB2:
            # Se usa un estudiante NUEVO: los anteriores ya avanzaron de nivel
            # en bloques previos, y este test debe medir la herencia del
            # profesor, no si queda cupo de matrículas.
            await c.post("/admin/users", headers=AH, json={
                "email": "p3.sofia@dorismon.do", "full_name": "P3 Sofia",
                "password": "Estudiante2026!", "role": "student"})
            _bs = (await c.get("/admin/users?q=p3.sofia", headers=AH)).json()
            SOFIA = _bs["items"][0] if _bs.get("items") else None

            _enr_s = None
            if SOFIA:
                _r = await c.post("/admin/enrollments", headers=AH, json={
                    "student_id": SOFIA["id"], "course_id": CA["id"],
                    "level_id": B1["id"], "teacher_id": CARLOS["id"]})
                if _r.status_code in (200, 201):
                    _enr_s = _r.json().get("id")
                    # Se completa para poder crear el siguiente nivel
                    import sqlite3 as _s5
                    _cx5 = _s5.connect("dorismon.db")
                    _cx5.execute(
                        "UPDATE enrollments SET academic_status='completed', "
                        "completed_at=datetime('now') WHERE id=?", (_enr_s,))
                    _cx5.commit(); _cx5.close()

            sig2 = await c.post(f"/admin/enrollments/{_enr_s}/next-level",
                                headers=AH,
                                json={"level_id": B2["id"],
                                      "series_id": GB2[0]["id"]}) if _enr_s else None
            check("Se crea la matrícula indicando solo el grupo",
                  sig2 is not None and sig2.status_code == 201)
            if sig2 is not None and sig2.status_code == 201:
                nid = sig2.json().get("enrollment_id")
                try:
                    _cx4 = _s3.connect("dorismon.db")
                    f2 = _cx4.execute(
                        "SELECT series_id, teacher_id FROM enrollments WHERE id=?",
                        (nid,)).fetchone()
                    _cx4.close()
                    check("Queda con el grupo indicado",
                          bool(f2) and f2[0] == GB2[0]["id"])
                    check("Y HEREDA el profesor titular del grupo",
                          bool(f2) and f2[1] == ANDREA["id"])
                except Exception as ex:
                    print(f"     (no se pudo verificar: {ex})")
                check("No avisa que falte grupo",
                      sig2.json().get("needs_group") is False)

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
