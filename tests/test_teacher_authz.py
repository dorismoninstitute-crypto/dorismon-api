"""V3.9.47 — Autorización del PROFESOR (pruebas negativas).

Los tests de aislamiento cubren estudiante contra estudiante. Estos cubren
profesor contra profesor: que el Profesor A no pueda usar el grupo de B, ni
sus estudiantes, ni publicar su quiz, ni ver su material privado — aunque
mande los IDs por API.
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

        alumnos = []
        for e in ESTUDIANTES:
            r = await c.post("/auth/login", json=e)
            if r.status_code != 200:
                continue
            u = (await c.get(f"/admin/users?q={e['email'].split('@')[0]}",
                             headers=AH)).json()
            alumnos.append({"id": u["items"][0]["id"]})
        if len(alumnos) < 2:
            print("  (faltan estudiantes)")
            return 0
        EA, EB = alumnos

        perfil = (await c.get(f"/admin/students/{EA['id']}/profile", headers=AH)).json()
        lvl = [l for l in lvls if l["code"] == perfil.get("current_level_code")][0]

        # Un grupo por profesor, MISMO nivel
        for nombre, profe, hora in [("AUTHZ Grupo A", PA, "08:00"),
                                     ("AUTHZ Grupo B", PB, "20:00")]:
            await c.post("/admin/class-series", headers=AH, json={
                "name": nombre, "course_id": cid, "level_id": lvl["id"],
                "teacher_id": profe["id"], "days_of_week": "mon,tue,wed,thu,fri",
                "start_time_hhmm": hora, "duration_min": 60, "start_date": hoy,
                "num_classes": 5, "modality": "online", "capacity": 10,
            })
        gs = (await c.get("/admin/groups", headers=AH)).json().get("items", [])
        GA = [x for x in gs if x["name"] == "AUTHZ Grupo A"]
        GB = [x for x in gs if x["name"] == "AUTHZ Grupo B"]
        if not (GA and GB):
            print("  (no se crearon los grupos)")
            return 0

        enr = (await c.get("/admin/enrollments", headers=AH)).json()
        ei = enr.get("items", enr) if isinstance(enr, dict) else enr
        for alumno, grupo, profe in [(EA, GA[0], PA), (EB, GB[0], PB)]:
            m = [x for x in ei if x.get("student_id") == alumno["id"] and x.get("is_active")]
            if m:
                await c.patch(f"/admin/enrollments/{m[0]['id']}", headers=AH,
                              json={"level_id": lvl["id"], "teacher_id": profe["id"]})
                await c.post(f"/admin/enrollments/{m[0]['id']}/assign-group",
                             headers=AH, json={"series_id": grupo["id"],
                                               "confirm_full": True})

        ta = (await c.post("/auth/login", json={"email": PA["email"],
                                                 "password": "Profe2026!"})).json()
        TA = {"Authorization": f"Bearer {ta['access_token']}"}
        tb = (await c.post("/auth/login", json={"email": PB["email"],
                                                 "password": "Profe2026!"})).json()
        TB = {"Authorization": f"Bearer {tb['access_token']}"}

        # ---------- 1. GRUPO AJENO ----------
        r = await c.post("/teacher/assignments", headers=TA, json={
            "title": "AUTHZ tarea al grupo de B", "description": "x",
            "level_id": lvl["id"], "series_id": GB[0]["id"],
        })
        check("Profesor A NO puede crear tarea para el grupo de B",
              r.status_code == 403)

        r = await c.post("/teacher/quizzes", headers=TA, json={
            "title": "AUTHZ quiz al grupo de B", "description": "x",
            "level_id": lvl["id"], "series_id": GB[0]["id"],
            "questions": [{"type": "multiple_choice", "statement": "x",
                           "options": ["a", "b", "c", "d"],
                           "correct_answer": "a", "points": 10}],
        })
        check("Profesor A NO puede crear quiz para el grupo de B",
              r.status_code == 403)

        r = await c.post("/teacher/materials", headers=TA, json={
            "title": "AUTHZ material al grupo de B", "type": "pdf",
            "url": "https://x.com/a.pdf", "level_id": lvl["id"],
            "series_id": GB[0]["id"],
        })
        check("Profesor A NO puede crear material para el grupo de B",
              r.status_code == 403)

        # Y con SU grupo sí puede
        ok = await c.post("/teacher/assignments", headers=TA, json={
            "title": "AUTHZ tarea propia", "description": "x",
            "level_id": lvl["id"], "series_id": GA[0]["id"],
        })
        check("Profesor A SÍ puede con su propio grupo", ok.status_code == 201)

        # ---------- 2. ESTUDIANTE AJENO ----------
        r = await c.post("/teacher/materials", headers=TA, json={
            "title": "AUTHZ material privado a EB", "type": "pdf",
            "url": "https://x.com/privado.pdf", "student_id": EB["id"],
        })
        check("Profesor A NO puede dirigir material a un estudiante de B",
              r.status_code == 403)

        r = await c.post(f"/teacher/observations/{EB['id']}", headers=TA,
                         json={"content": "observación intrusa"})
        check("Profesor A NO puede observar a un estudiante de B",
              r.status_code == 403)

        propio = await c.post("/teacher/materials", headers=TA, json={
            "title": "AUTHZ material privado a EA", "type": "pdf",
            "url": "https://x.com/mio.pdf", "student_id": EA["id"],
        })
        check("Profesor A SÍ puede con su propio estudiante",
              propio.status_code == 201)

        # ---------- 3. MATERIAL INSTITUCIONAL ----------
        # V3.9.48 — Ahora es un 403 explícito, no una conversión silenciosa
        inst = await c.post("/teacher/materials", headers=TA, json={
            "title": "AUTHZ intento institucional", "type": "pdf",
            "url": "https://x.com/i.pdf", "level_id": lvl["id"],
            "audience_kind": "institutional",
        })
        check("El profesor NO puede crear material institucional (403)",
              inst.status_code == 403)

        adm = await c.post("/teacher/materials", headers=AH, json={
            "title": "AUTHZ institucional del admin", "type": "pdf",
            "url": "https://x.com/of.pdf", "level_id": lvl["id"],
            "audience_kind": "institutional",
        })
        check("El admin SÍ puede crear material institucional",
              adm.status_code == 201)

        # ---------- 4. LISTADO DE MATERIALES ----------
        privado_b = await c.post("/teacher/materials", headers=TB, json={
            "title": "AUTHZ privado de B para EB", "type": "pdf",
            "url": "https://secreto.com/b.pdf", "student_id": EB["id"],
        })
        if privado_b.status_code == 201:
            lista_a = (await c.get("/teacher/materials", headers=TA)).json()
            ia = lista_a.get("items", lista_a) if isinstance(lista_a, dict) else lista_a
            check("Profesor A NO ve el material privado de B",
                  not any(x.get("title") == "AUTHZ privado de B para EB" for x in ia))
            check("Y tampoco recibe su URL",
                  not any("secreto.com" in str(x.get("url") or "") for x in ia))

            lista_b = (await c.get("/teacher/materials", headers=TB)).json()
            ib = lista_b.get("items", lista_b) if isinstance(lista_b, dict) else lista_b
            check("Profesor B SÍ ve su propio material",
                  any(x.get("title") == "AUTHZ privado de B para EB" for x in ib))

        # ---------- 5. PUBLICAR QUIZ AJENO ----------
        qb = await c.post("/teacher/quizzes", headers=TB, json={
            "title": "AUTHZ quiz de B", "description": "x",
            "level_id": lvl["id"], "series_id": GB[0]["id"],
            "questions": [{"type": "multiple_choice", "statement": "x",
                           "options": ["a", "b", "c", "d"],
                           "correct_answer": "a", "points": 10}],
        })
        if qb.status_code in (200, 201):
            qbid = qb.json().get("id")
            check("Profesor A NO puede publicar el quiz de B",
                  (await c.post(f"/teacher/quizzes/{qbid}/publish",
                                headers=TA)).status_code == 404)
            await c.post(f"/teacher/quizzes/{qbid}/publish", headers=TB)
            check("Profesor A NO puede despublicar el quiz de B",
                  (await c.post(f"/teacher/quizzes/{qbid}/unpublish",
                                headers=TA)).status_code == 404)
            check("Profesor B SÍ puede despublicar el suyo",
                  (await c.post(f"/teacher/quizzes/{qbid}/unpublish",
                                headers=TB)).status_code == 200)
            check("El admin también puede",
                  (await c.post(f"/teacher/quizzes/{qbid}/publish",
                                headers=AH)).status_code == 200)

        # ---------- 6. Simétrico: B contra A ----------
        r = await c.post("/teacher/assignments", headers=TB, json={
            "title": "AUTHZ inverso", "description": "x",
            "level_id": lvl["id"], "series_id": GA[0]["id"],
        })
        check("Y al revés: Profesor B tampoco puede usar el grupo de A",
              r.status_code == 403)

        # ══════════════════════════════════════════════════════════════════
        # V3.9.48 — /teacher/my-groups
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Endpoint /teacher/my-groups ---")

        ga = await c.get("/teacher/my-groups", headers=TA)
        check("El profesor puede consultar sus grupos", ga.status_code == 200)
        ia = ga.json().get("items", []) if ga.status_code == 200 else []
        check("Profesor A recibe SU grupo",
              any(x["id"] == GA[0]["id"] for x in ia))
        check("Profesor A NO recibe el grupo de B",
              not any(x["id"] == GB[0]["id"] for x in ia))
        check("Trae lo necesario para mostrarlo (nombre, nivel, horario, cupo)",
              bool(ia) and all(k in ia[0] for k in
                               ("name", "level_id", "days_of_week",
                                "start_time_hhmm", "students")))

        gb2 = await c.get("/teacher/my-groups", headers=TB)
        ib = gb2.json().get("items", []) if gb2.status_code == 200 else []
        check("Profesor B recibe SU grupo y no el de A",
              any(x["id"] == GB[0]["id"] for x in ib)
              and not any(x["id"] == GA[0]["id"] for x in ib))

        gadm = await c.get("/teacher/my-groups", headers=AH)
        check("El admin ve todos los grupos",
              gadm.status_code == 200 and len(gadm.json().get("items", [])) >= 2)

        # ══════════════════════════════════════════════════════════════════
        # V3.9.48 — SUSTITUIR UNA SESIÓN ≠ SER DUEÑO DEL GRUPO
        #
        # Carlos (PA) es titular de su grupo. Andrea (PB) sustituye UNA sola
        # sesión. Antes eso la convertía en dueña del grupo para siempre.
        # ══════════════════════════════════════════════════════════════════
        print("\n  --- Sustituto de una sesión ---")

        ses = (await c.get("/admin/sessions?filter_period=all&limit=100",
                           headers=AH)).json()
        del_a = [x for x in ses["items"] if x.get("series_id") == GA[0]["id"]
                 and x.get("status") != "cancelled"]
        futuras_a = [x for x in del_a
                     if x.get("starts_at_utc", "") > datetime.datetime.now(
                         datetime.timezone.utc).isoformat()]

        if futuras_a:
            sid = futuras_a[0]["id"]
            sub = await c.post(f"/admin/sessions/{sid}/substitute-teacher",
                               headers=AH, json={"teacher_id": PB["id"],
                                                 "confirm_overlap": True})
            check("Se asigna a B como sustituto de UNA sesión de A",
                  sub.status_code == 200)

            # LO QUE SÍ PUEDE: esa sesión
            att = await c.get(f"/teacher/sessions/{sid}/attendance", headers=TB)
            check("El sustituto SÍ puede abrir la asistencia de esa sesión",
                  att.status_code == 200)

            # LO QUE NO DEBE PODER: el grupo
            r = await c.post("/teacher/assignments", headers=TB, json={
                "title": "SUST tarea al grupo ajeno", "description": "x",
                "level_id": lvl["id"], "series_id": GA[0]["id"],
            })
            check("El sustituto NO puede crear tareas para ese grupo",
                  r.status_code == 403)

            r = await c.post("/teacher/quizzes", headers=TB, json={
                "title": "SUST quiz al grupo ajeno", "description": "x",
                "level_id": lvl["id"], "series_id": GA[0]["id"],
                "questions": [{"type": "multiple_choice", "statement": "x",
                               "options": ["a", "b", "c", "d"],
                               "correct_answer": "a", "points": 10}],
            })
            check("El sustituto NO puede crear quizzes para ese grupo",
                  r.status_code == 403)

            r = await c.post("/teacher/materials", headers=TB, json={
                "title": "SUST material al grupo ajeno", "type": "pdf",
                "url": "https://x.com/s.pdf", "series_id": GA[0]["id"],
            })
            check("El sustituto NO puede dirigir material a ese grupo",
                  r.status_code == 403)

            r = await c.post("/teacher/materials", headers=TB, json={
                "title": "SUST material a un estudiante ajeno", "type": "pdf",
                "url": "https://x.com/s2.pdf", "student_id": EA["id"],
            })
            check("Los estudiantes del grupo NO pasan a ser suyos",
                  r.status_code == 403)

            gb3 = await c.get("/teacher/my-groups", headers=TB)
            ib3 = gb3.json().get("items", []) if gb3.status_code == 200 else []
            check("El grupo sustituido NO aparece en sus grupos",
                  not any(x["id"] == GA[0]["id"] for x in ib3))

            # Y el titular no pierde nada
            ga3 = await c.get("/teacher/my-groups", headers=TA)
            ia3 = ga3.json().get("items", []) if ga3.status_code == 200 else []
            check("El titular conserva su grupo",
                  any(x["id"] == GA[0]["id"] for x in ia3))

        # ══════════════════════════════════════════════════════════════════
        # V3.9.48 — Material institucional: error explícito
        # ══════════════════════════════════════════════════════════════════
        r = await c.post("/teacher/materials", headers=TA, json={
            "title": "SUST intento institucional 2", "type": "pdf",
            "url": "https://x.com/i2.pdf", "level_id": lvl["id"],
            "audience_kind": "institutional",
        })
        check("Pedir 'institucional' siendo profesor devuelve 403 (no silencio)",
              r.status_code == 403)
        check("Y el mensaje lo explica",
              "Dirección" in str(r.json().get("detail", "")))

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
