"""V3.9.52 — AT_RISK por MATRÍCULA, no por persona.

Escenario real de una academia de idiomas:

    Juan → English B1 con el Profesor A → todo bien
    Juan → Spanish A2 con el Profesor B → faltas y tareas atrasadas

Lo que debe pasar:
  · Solo Spanish aparece en riesgo
  · El Profesor A no recibe NADA de Spanish (es de otra profesora, otro curso)
  · Las señales de un curso nunca contaminan al otro
"""
import sys
import asyncio
import datetime
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")
# Se usa a Juana a propósito: María y Carlos los usan otros tests, y este
# escenario le CAMBIA el curso a su matrícula. Un test no debe dejar el
# terreno alterado para los demás.
JUAN = {"email": "juana.estudiante@dorismon.do", "password": "Estudiante2026!"}


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

        cursos = (await c.get("/admin/courses", headers=AH)).json()
        if len(cursos) < 2:
            print("  (se necesitan 2 cursos para este escenario)")
            return 0
        CA, CB = cursos[0], cursos[1]

        def _niveles(curso_id):
            return c.get(f"/admin/levels-by-course/{curso_id}", headers=AH)

        lva = (await _niveles(CA["id"])).json()
        lvb = (await _niveles(CB["id"])).json()
        LA = (lva["items"] if isinstance(lva, dict) else lva)
        LB = (lvb["items"] if isinstance(lvb, dict) else lvb)
        if not (LA and LB):
            print("  (faltan niveles)")
            return 0

        # V3.9.52 — Profesores PROPIOS de este test.
        #
        # Los de la semilla los usan otros tests, que les dejan grupos de
        # varios cursos. Como aquí se comprueba justamente qué NO debe ver
        # cada profesor, hace falta partir de dos profesores limpios: si no,
        # el resultado depende del orden de ejecución.
        PA = PB = None
        for correo, nombre in [("me.profa@dorismon.do", "ME Profesora English"),
                               ("me.profb@dorismon.do", "ME Profesor Spanish")]:
            r = await c.post("/admin/users", headers=AH, json={
                "email": correo, "full_name": nombre,
                "password": "Profe2026!", "role": "teacher",
            })
            # El endpoint solo devuelve el id, así que en ambos casos se
            # busca el usuario para tener también su email.
            b = (await c.get(f"/admin/users?q={correo.split('@')[0]}",
                             headers=AH)).json()
            u_ = b["items"][0] if b.get("items") else None
            if u_:
                if PA is None:
                    PA = u_
                else:
                    PB = u_
        if not (PA and PB):
            print("  (no se pudieron crear los profesores de prueba)")
            return 0

        jl = await c.post("/auth/login", json=JUAN)
        u = (await c.get("/admin/users?q=juana.estudiante", headers=AH)).json()
        JID = u["items"][0]["id"]
        JH = {"Authorization": f"Bearer {jl.json()['access_token']}"}

        hoy = datetime.date.today().isoformat()
        now = datetime.datetime.now(datetime.timezone.utc)

        # ── Dos grupos, uno por profesor y por curso ──
        grupos = {}
        for nombre, curso, nivel, profe, hora in [
            ("ME Grupo English", CA, LA[0], PA, "08:00"),
            ("ME Grupo Spanish", CB, LB[0], PB, "20:00"),
        ]:
            await c.post("/admin/class-series", headers=AH, json={
                "name": nombre, "course_id": curso["id"], "level_id": nivel["id"],
                "teacher_id": profe["id"], "days_of_week": "mon,tue,wed,thu,fri",
                "start_time_hhmm": hora, "duration_min": 60, "start_date": hoy,
                "num_classes": 5, "modality": "online", "video_provider": "dorismon", "capacity": 10,
            })
        gs = (await c.get("/admin/groups", headers=AH)).json().get("items", [])
        for g in gs:
            if g["name"] in ("ME Grupo English", "ME Grupo Spanish"):
                grupos[g["name"]] = g
        if len(grupos) < 2:
            print("  (no se crearon los grupos)")
            return 0
        GE, GS = grupos["ME Grupo English"], grupos["ME Grupo Spanish"]

        # ── Las DOS matrículas de Juan ──
        enr = (await c.get("/admin/enrollments", headers=AH)).json()
        ei = enr.get("items", enr) if isinstance(enr, dict) else enr
        mias = [x for x in ei if x.get("student_id") == JID and x.get("is_active")]

        # La primera se reconvierte a English con el Profesor A
        if mias:
            await c.patch(f"/admin/enrollments/{mias[0]['id']}", headers=AH, json={
                "course_id": CA["id"], "level_id": LA[0]["id"],
                "teacher_id": PA["id"]})
            await c.post(f"/admin/enrollments/{mias[0]['id']}/assign-group",
                         headers=AH, json={"series_id": GE["id"], "confirm_full": True})
            ENR_A = mias[0]["id"]
        else:
            ENR_A = None

        # La segunda: Spanish con la Profesora B
        nueva = await c.post("/admin/enrollments", headers=AH, json={
            "student_id": JID, "course_id": CB["id"],
            "level_id": LB[0]["id"], "teacher_id": PB["id"],
        })
        check("Juan puede tener dos matrículas a la vez",
              nueva.status_code in (200, 201))
        ENR_B = nueva.json().get("id") if nueva.status_code in (200, 201) else None
        if ENR_B:
            await c.post(f"/admin/enrollments/{ENR_B}/assign-group",
                         headers=AH, json={"series_id": GS["id"], "confirm_full": True})

        ta = (await c.post("/auth/login", json={"email": PA["email"],
                                                 "password": "Profe2026!"})).json()
        TA = {"Authorization": f"Bearer {ta['access_token']}"}
        tb = (await c.post("/auth/login", json={"email": PB["email"],
                                                 "password": "Profe2026!"})).json()
        TB = {"Authorization": f"Bearer {tb['access_token']}"}

        # ══ SOLO EN SPANISH: 3 tareas vencidas sin entregar ══
        for i in range(3):
            await c.post("/teacher/assignments", headers=TB, json={
                "title": f"ME Spanish vencida {i}", "description": "x",
                "level_id": LB[0]["id"], "series_id": GS["id"],
                "due_at": (now - datetime.timedelta(days=6 - i)).isoformat(),
            })

        # ══ SOLO EN SPANISH: 3 faltas seguidas ══
        clases_es = []
        for i in range(3):
            cs = await c.post("/admin/sessions", headers=AH, json={
                "title": f"ME Spanish clase {i}",
                "starts_at_utc": (now - datetime.timedelta(days=5 - i)).isoformat(),
                "ends_at_utc": (now - datetime.timedelta(days=5 - i, hours=-1)).isoformat(),
                "modality": "online", "video_provider": "dorismon", "teacher_id": PB["id"],
                "course_id": CB["id"], "level_id": LB[0]["id"],
                "series_id": GS["id"],
            })
            if cs.status_code == 201:
                clases_es.append(cs.json().get("id"))
        for cid_ in clases_es:
            await c.post(f"/teacher/sessions/{cid_}/attendance", headers=AH, json={
                "records": [{"student_id": JID, "state": "absent"}]})

        # ══ EN ENGLISH: todo bien — asistió a su última clase ══
        ce = await c.post("/admin/sessions", headers=AH, json={
            "title": "ME English clase ok",
            "starts_at_utc": (now - datetime.timedelta(hours=3)).isoformat(),
            "ends_at_utc": (now - datetime.timedelta(hours=2)).isoformat(),
            "modality": "online", "video_provider": "dorismon", "teacher_id": PA["id"],
            "course_id": CA["id"], "level_id": LA[0]["id"],
            "series_id": GE["id"],
        })
        if ce.status_code == 201:
            await c.post(f"/teacher/sessions/{ce.json()['id']}/attendance",
                         headers=AH, json={
                             "records": [{"student_id": JID, "state": "present"}]})

        # ══════════ LO QUE DEBE PASAR ══════════
        print("\n  --- Las señales no se cruzan ---")
        ar = (await c.get("/admin/at-risk-overview", headers=AH)).json()
        suyas = [x for x in ar.get("items", []) if x["student_id"] == JID]

        en_es = [x for x in suyas if x.get("course_id") == CB["id"]]
        en_en = [x for x in suyas if x.get("course_id") == CA["id"]]

        check("Spanish A2 aparece en riesgo", bool(en_es))
        check("English B1 NO aparece en riesgo", not en_en)

        if en_es:
            tipos = {s["tipo"] for s in en_es[0]["señales"]}
            check("El riesgo de Spanish incluye sus faltas",
                  "ausencias_seguidas" in tipos or "ausencias" in tipos)
            check("Y sus tareas vencidas", "tareas" in tipos)
            check("La fila dice a qué curso pertenece",
                  en_es[0].get("course_name") is not None)
            check("Y a qué grupo", en_es[0].get("group_id") == GS["id"])

        # ══════════ PRIVACIDAD ENTRE PROFESORES ══════════
        print("\n  --- Cada profesor ve solo lo suyo ---")
        ra = (await c.get("/teacher/at-risk", headers=TA)).json()
        ia = ra.get("items", [])
        check("El Profesor A (English) NO recibe datos de Spanish",
              not any(x.get("course_id") == CB["id"] for x in ia))

        rb = (await c.get("/teacher/at-risk", headers=TB)).json()
        ib = rb.get("items", [])
        check("La Profesora B (Spanish) SÍ ve el riesgo de Spanish",
              any(x.get("course_id") == CB["id"] and x["student_id"] == JID
                  for x in ib))
        check("Y no recibe datos de English",
              not any(x.get("course_id") == CA["id"] for x in ib))

        # ══════════ INACTIVIDAD POR MATRÍCULA ══════════
        print("\n  --- La actividad de un curso no resetea el otro ---")
        # Juan entrega algo en Spanish: no debe "limpiar" English
        lst = (await c.get("/student/assignments", headers=JH)).json()
        items = lst.get("items", lst) if isinstance(lst, dict) else lst
        de_spanish = [x for x in items if "ME Spanish" in (x.get("title") or "")]
        check("Juan ve las tareas de AMBOS cursos", bool(de_spanish))

        # ══════════ EVENTOS OPCIONALES ══════════
        print("\n  --- Faltar a un evento opcional no es riesgo académico ---")
        ev = await c.post("/admin/events", headers=AH, json={
            "title": "ME Conversation Club", "modality": "online",
            "teacher_id": PA["id"], "course_id": CA["id"],
            "starts_at_utc": (now - datetime.timedelta(days=2)).isoformat(),
            "ends_at_utc": (now - datetime.timedelta(days=2, hours=-1)).isoformat(),
            "capacity": 30, "video_provider": "dorismon",
        })
        if ev.status_code == 201:
            eid = ev.json().get("id")
            await c.post(f"/teacher/sessions/{eid}/attendance", headers=AH, json={
                "records": [{"student_id": JID, "state": "absent"}]})
            ar2 = (await c.get("/admin/at-risk-overview", headers=AH)).json()
            en_en2 = [x for x in ar2.get("items", [])
                      if x["student_id"] == JID and x.get("course_id") == CA["id"]]
            check("Faltar al evento NO pone English en riesgo", not en_en2)

        # ══════════ QUIZ DE UN CURSO NO MARCA EL OTRO ══════════
        print("\n  --- Un quiz reprobado en un curso no marca el otro ---")
        q = await c.post("/teacher/quizzes", headers=TB, json={
            "title": "ME Quiz Spanish", "description": "x",
            "level_id": LB[0]["id"], "series_id": GS["id"], "max_attempts": 1,
            "questions": [{"type": "multiple_choice", "statement": "Hola ___",
                           "options": ["mundo", "world", "monde", "mondo"],
                           "correct_answer": "mundo", "points": 10}],
        })
        if q.status_code in (200, 201):
            qid = q.json().get("id")
            await c.post(f"/teacher/quizzes/{qid}/publish", headers=TB)
            await c.post(f"/student/quizzes/{qid}/submit", headers=JH,
                         json={"answers": []})
            ar3 = (await c.get("/admin/at-risk-overview", headers=AH)).json()
            es3 = [x for x in ar3.get("items", [])
                   if x["student_id"] == JID and x.get("course_id") == CB["id"]]
            en3 = [x for x in ar3.get("items", [])
                   if x["student_id"] == JID and x.get("course_id") == CA["id"]]
            tipos_es = {s["tipo"] for x in es3 for s in x["señales"]}
            tipos_en = {s["tipo"] for x in en3 for s in x["señales"]}
            check("El quiz agotado marca SPANISH", "quiz_reprobado" in tipos_es)
            check("Y NO marca English", "quiz_reprobado" not in tipos_en)

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
