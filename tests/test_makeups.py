"""V3.9.36 — Regla estricta de clases, reposiciones y sin-horario."""
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
        est = mu["items"][0]["id"]
        perfil = (await c.get(f"/admin/students/{est}/profile", headers=AH)).json()
        lvl = [l for l in lvls if l["code"] == perfil.get("current_level_code")][0]
        profes = (await c.get("/admin/users?role=teacher", headers=AH)).json()
        profe = [p for p in profes["items"] if p["email"] in SEED_TEACHERS][0]
        hoy = datetime.date.today().isoformat()
        now = datetime.datetime.now(datetime.timezone.utc)

        # ---------- Regla estricta ----------
        enr = (await c.get("/admin/enrollments", headers=AH)).json()
        ei = enr.get("items", enr) if isinstance(enr, dict) else enr
        mia = [e for e in ei if e.get("student_id") == est and e.get("is_active")]
        if mia:
            # Sacarlo de cualquier grupo y darle profesor
            await c.post(f"/admin/enrollments/{mia[0]['id']}/assign-group",
                         headers=AH, json={"series_id": ""})
            await c.patch(f"/admin/enrollments/{mia[0]['id']}", headers=AH,
                          json={"teacher_id": profe["id"]})

            # Su profesor tiene DOS horarios del mismo nivel
            for nombre, hora in [("Test H1", "18:00"), ("Test H2", "20:00")]:
                await c.post("/admin/class-series", headers=AH, json={
                    "name": nombre, "course_id": cid, "level_id": lvl["id"],
                    "teacher_id": profe["id"], "days_of_week": "mon,tue,wed,thu,fri",
                    "start_time_hhmm": hora, "duration_min": 60,
                    "start_date": hoy, "num_classes": 8, "modality": "online", "video_provider": "dorismon",
                })

            pr = (await c.get("/progress/my-course", headers=SH)).json()
            check("Sin grupo NO ve ninguna clase grupal",
                  (pr.get("next_session") or {}).get("series_id") is None
                  and not (pr.get("next_session") or {}).get("title", "").startswith("Test H"))

            # Al asignarle uno, solo ve el suyo
            g = (await c.get("/admin/groups", headers=AH)).json().get("items", [])
            h1 = [x for x in g if x["name"] == "Test H1"]
            if h1:
                await c.post(f"/admin/enrollments/{mia[0]['id']}/assign-group",
                             headers=AH, json={"series_id": h1[0]["id"]})
                pr2 = (await c.get("/progress/my-course", headers=SH)).json()
                prox = pr2.get("next_session") or {}
                titulo = prox.get("title", "")
                # V3.9.68+: una clase SUELTA válida de su mismo curso+nivel+
                # profesor puede ser más próxima que la sesión de su grupo.
                # Eso es correcto. Lo que sigue prohibido es colarse en OTRA
                # SERIE/horario del mismo profesor.
                check("Con grupo, la próxima es su serie o una suelta válida",
                      prox.get("series_id") in (None, h1[0]["id"]))
                h2 = [x for x in g if x["name"] == "Test H2"]
                h2id = h2[0]["id"] if h2 else None
                check("NO ve el otro horario del mismo profesor",
                      prox.get("series_id") != h2id and "Test H2" not in titulo)

        # ---------- Estudiantes sin horario ----------
        sh = await c.get("/admin/students-without-schedule", headers=AH)
        check("El aviso de sin-horario responde", sh.status_code == 200)
        check("Trae el motivo de cada uno",
              all("motivo" in x for x in sh.json().get("items", [])))
        no = await c.get("/admin/students-without-schedule", headers=SH)
        check("Un estudiante NO ve ese listado", no.status_code in (401, 403))

        # ---------- Reposiciones ----------
        sid = (await c.post("/admin/sessions", headers=AH, json={
            "title": "Test clase perdida",
            "starts_at_utc": (now - datetime.timedelta(days=2)).isoformat(),
            "ends_at_utc": (now - datetime.timedelta(days=2, hours=-1)).isoformat(),
            "modality": "online", "video_provider": "dorismon", "teacher_id": profe["id"],
            "course_id": cid, "level_id": lvl["id"],
        })).json()["id"]

        sin_motivo = await c.post(f"/student/sessions/{sid}/request-makeup",
                                  headers=SH, json={"reason": ""})
        check("Sin motivo, no deja pedir reposición", sin_motivo.status_code == 400)

        req = await c.post(f"/student/sessions/{sid}/request-makeup", headers=SH,
                           json={"reason": "El profesor no llegó",
                                 "missed_by": "teacher",
                                 "preferred_date": "sábado"})
        check("El estudiante puede pedir reponer", req.status_code == 200)
        rid = req.json().get("id")

        dup = await c.post(f"/student/sessions/{sid}/request-makeup", headers=SH,
                           json={"reason": "otra vez"})
        check("No deja pedir dos veces la misma", dup.status_code == 400)

        lst = await c.get("/admin/makeup-requests", headers=AH)
        check("El admin ve las solicitudes", lst.status_code == 200)
        items = lst.json().get("items", [])
        check("La solicitud dice quién faltó",
              any(x.get("missed_by_label") for x in items))

        # Agendar
        sch = await c.post(f"/admin/makeup-requests/{rid}/schedule", headers=AH,
                           json={"starts_at_utc": (now + datetime.timedelta(days=3)).isoformat(),
                                 "duration_min": 60})
        check("El admin agenda la reposición", sch.status_code == 200)

        mias = (await c.get("/student/makeup-requests", headers=SH)).json()
        check("El estudiante ve el estado de su solicitud",
              any(x.get("status") == "scheduled" for x in mias.get("items", [])))

        # Fecha en el pasado
        sid2 = (await c.post("/admin/sessions", headers=AH, json={
            "title": "Test otra perdida",
            "starts_at_utc": (now - datetime.timedelta(days=3)).isoformat(),
            "ends_at_utc": (now - datetime.timedelta(days=3, hours=-1)).isoformat(),
            "modality": "online", "video_provider": "dorismon", "teacher_id": profe["id"],
            "course_id": cid, "level_id": lvl["id"],
        })).json()["id"]
        r2 = await c.post(f"/student/sessions/{sid2}/request-makeup", headers=SH,
                          json={"reason": "no pude"})
        if r2.status_code == 200:
            pasado = await c.post(f"/admin/makeup-requests/{r2.json()['id']}/schedule",
                                  headers=AH,
                                  json={"starts_at_utc": (now - datetime.timedelta(days=1)).isoformat()})
            check("No deja agendar una reposición en el pasado", pasado.status_code == 400)

        nop = await c.get("/admin/makeup-requests", headers=SH)
        check("Un estudiante NO ve el panel de reposiciones", nop.status_code in (401, 403))

        # ---------- V3.9.37: el admin agenda sin que la pidan ----------
        d1 = await c.post("/admin/makeup-requests/direct", headers=AH, json={
            "student_id": est, "teacher_id": profe["id"],
            "starts_at_utc": (now + datetime.timedelta(days=5)).isoformat(),
            "duration_min": 60, "counts_for_progress": False,
            "reason": "Clase pendiente",
        })
        check("El admin agenda una reposición sin clase original", d1.status_code == 201)
        check("Por defecto NO cuenta para el temario",
              d1.status_code == 201 and d1.json().get("counts_for_progress") is False)

        d2 = await c.post("/admin/makeup-requests/direct", headers=AH, json={
            "student_id": est, "teacher_id": profe["id"],
            "starts_at_utc": (now + datetime.timedelta(days=6)).isoformat(),
            "counts_for_progress": True, "title": "Test clase extra",
        })
        check("Se puede marcar que SÍ cuenta para el temario",
              d2.status_code == 201 and d2.json().get("counts_for_progress") is True)

        mias2 = (await c.get("/student/makeup-requests", headers=SH)).json()
        check("El estudiante ve también las que agendó el instituto",
              len(mias2.get("items", [])) >= 2)

        lst2 = (await c.get("/admin/makeup-requests?status=all", headers=AH)).json()
        check("El admin ve quién originó cada reposición",
              any(x.get("created_by") == "admin" for x in lst2.get("items", [])))

        sin_est = await c.post("/admin/makeup-requests/direct", headers=AH,
                               json={"student_id": ""})
        check("Sin estudiante, rechaza", sin_est.status_code == 400)

        pasado2 = await c.post("/admin/makeup-requests/direct", headers=AH, json={
            "student_id": est,
            "starts_at_utc": (now - datetime.timedelta(days=1)).isoformat(),
        })
        check("Fecha en el pasado, rechaza", pasado2.status_code == 400)

        noperm = await c.post("/admin/makeup-requests/direct", headers=SH,
                              json={"student_id": est})
        check("Un estudiante NO puede agendar reposiciones", noperm.status_code in (401, 403))

        mc = await c.get(f"/admin/students/{est}/missed-classes", headers=AH)
        check("Se pueden consultar sus clases perdidas", mc.status_code == 200)

        # ---------- V3.9.39: selector solo con inscritos ----------
        apt = await c.get("/admin/students-for-makeup", headers=AH)
        check("El selector de reposición responde", apt.status_code == 200)
        items_apt = apt.json().get("items", [])
        check("Solo trae estudiantes con inscripción activa",
              all(x.get("enrollment_id") for x in items_apt))
        check("Trae nivel, grupo y profesor de cada uno",
              all("level_code" in x and "display" in x for x in items_apt))
        noperm2 = await c.get("/admin/students-for-makeup", headers=SH)
        check("Un estudiante NO ve ese listado", noperm2.status_code in (401, 403))

        # ---------- V3.9.39: profesor sustituto ----------
        otros2 = [p for p in profes["items"]
                  if p["email"] in SEED_TEACHERS and p["id"] != profe["id"]]
        if otros2:
            futura = (await c.post("/admin/sessions", headers=AH, json={
                "title": "Test clase sustituto",
                "starts_at_utc": (now + datetime.timedelta(days=1)).isoformat(),
                "ends_at_utc": (now + datetime.timedelta(days=1, hours=1)).isoformat(),
                "modality": "online", "video_provider": "dorismon", "teacher_id": profe["id"],
                "course_id": cid, "level_id": lvl["id"],
            })).json()["id"]

            disp = await c.get(f"/admin/sessions/{futura}/available-teachers", headers=AH)
            check("Se ve qué profesores están libres", disp.status_code == 200)
            check("Cada uno trae su tarifa",
                  all("rate_group" in x for x in disp.json().get("items", [])))

            sub = await c.post(f"/admin/sessions/{futura}/substitute-teacher",
                               headers=AH, json={"teacher_id": otros2[0]["id"],
                                                 "confirm_overlap": True})
            check("Se puede poner un sustituto en una clase", sub.status_code == 200)

            mismo = await c.post(f"/admin/sessions/{futura}/substitute-teacher",
                                 headers=AH, json={"teacher_id": otros2[0]["id"]})
            check("No deja poner al que ya está", mismo.status_code == 400)

            vieja = (await c.post("/admin/sessions", headers=AH, json={
                "title": "Test clase pasada sust",
                "starts_at_utc": (now - datetime.timedelta(days=2)).isoformat(),
                "ends_at_utc": (now - datetime.timedelta(days=2, hours=-1)).isoformat(),
                "modality": "online", "video_provider": "dorismon", "teacher_id": profe["id"],
                "course_id": cid, "level_id": lvl["id"],
            })).json()["id"]
            pas = await c.post(f"/admin/sessions/{vieja}/substitute-teacher",
                               headers=AH, json={"teacher_id": otros2[0]["id"]})
            check("No deja sustituir en una clase que ya pasó", pas.status_code == 400)

            nop2 = await c.post(f"/admin/sessions/{futura}/substitute-teacher",
                                headers=SH, json={"teacher_id": profe["id"]})
            check("Un estudiante NO puede poner sustitutos", nop2.status_code in (401, 403))

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
