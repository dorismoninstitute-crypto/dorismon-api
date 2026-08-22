"""V3.9.68 — Cierre de flexibilidad de sesiones (2ª auditoría externa).

  1. Enrollment activo pero SIN grupo + SessionAudience -> tarjeta principal
  4. Sede y aula en el editor de serie (columnas existentes, cero migración)
  5. Reposición de una clase histórica tras cambiar de grupo
"""
import sys
import asyncio
import datetime
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")
PASS = "Estudiante2026!"
LINK = "https://meet.google.com/aud-2-oki"


async def main():
    passed = total = 0

    def check(label, ok):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
        print(f"  {'✓' if ok else '✗'} {label}")

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        tok = (await c.post("/auth/login", json=ADMIN)).json()["access_token"]
        AH = {"Authorization": f"Bearer {tok}"}

        cid = (await c.get("/admin/courses", headers=AH)).json()[0]["id"]
        lv = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        niveles = lv["items"] if isinstance(lv, dict) else lv
        nivel = niveles[0]
        profe = [p for p in (await c.get("/admin/users?role=teacher", headers=AH)).json()["items"]
                 if p["email"] in SEED_TEACHERS][0]
        TH = {"Authorization": f"Bearer {(await c.post('/auth/login', json={'email': profe['email'], 'password': 'Profe2026!'})).json()['access_token']}"}

        planes = (await c.get("/admin/plans", headers=AH)).json()
        planes = planes.get("items", planes) if isinstance(planes, dict) else planes
        plan_id = planes[0]["id"] if planes else None

        now = datetime.datetime.now(datetime.timezone.utc)
        stamp = datetime.datetime.now().strftime("%H%M%S%f")[:10]

        async def crear_alumno(nombre):
            email = f"a68{stamp}{nombre}@dorismon.do"
            r = await c.post("/admin/users", headers=AH, json={
                "email": email, "full_name": f"A68 {nombre}",
                "password": PASS, "role": "student"})
            if r.status_code not in (200, 201):
                return None, None
            uid = r.json().get("id") or r.json().get("user_id")
            lg = await c.post("/auth/login", json={"email": email, "password": PASS})
            return uid, {"Authorization": f"Bearer {lg.json()['access_token']}"}

        # ═══ 1 — EL CASO REAL: MATRÍCULA SIN GRUPO ═════════════════════
        maria, MH = await crear_alumno("maria")
        vecino, VH = await crear_alumno("vecino")
        check("Se crean los estudiantes", bool(maria) and bool(vecino))

        async def matricular(uid):
            r = await c.post("/admin/enrollments", headers=AH, json={
                "student_id": uid, "course_id": cid, "level_id": nivel["id"],
                "teacher_id": profe["id"], **({"plan_id": plan_id} if plan_id else {}),
            })
            return r.status_code in (200, 201)

        check("María se matricula (SIN grupo asignado)", await matricular(maria))
        check("El vecino se matricula en el MISMO nivel", await matricular(vecino))

        pc = (await c.get("/progress/my-course", headers=MH)).json()
        check("María tiene Enrollment activo", pc.get("enrolled") is True)

        # La tarjeta principal y el calendario deben coincidir SIEMPRE. Antes
        # no: progress.py era el único sitio más estricto que el resto, y por
        # eso una clase salía en el calendario pero no en la tarjeta.
        async def coherentes(H):
            p = (await c.get("/progress/my-course", headers=H)).json()
            ns = p.get("next_session")
            cal = (await c.get("/student/calendar", headers=H)).json()
            ev = cal.get("events", cal) if isinstance(cal, dict) else cal
            ids = {e.get("id") for e in ev if e.get("type") == "class"}
            return (ns is None) or (ns.get("id") in ids)

        check("La tarjeta principal coincide con el calendario",
              await coherentes(MH))

        st = (now + datetime.timedelta(hours=2)).isoformat()
        en = (now + datetime.timedelta(hours=3)).isoformat()
        r = await c.post("/admin/sessions", headers=AH, json={
            "title": "A68 Refuerzo explicito", "starts_at_utc": st, "ends_at_utc": en,
            "modality": "online", "teacher_id": profe["id"],
            "course_id": cid, "level_id": nivel["id"],
            "meeting_url": LINK, "video_provider": "dorismon",
            "student_ids": [maria],
        })
        refuerzo = r.json().get("id") if r.status_code in (200, 201) else None
        check("Se crea el refuerzo explícito para María", bool(refuerzo))

        if refuerzo:
            pc = (await c.get("/progress/my-course", headers=MH)).json()
            ns = pc.get("next_session")
            check("CON Enrollment y SIN grupo, la tarjeta principal la muestra",
                  bool(ns) and ns.get("id") == refuerzo)
            check("Trae lo que el botón necesita para ser USABLE",
                  bool(ns) and ns.get("video_provider") == "dorismon"
                  and ns.get("modality") == "online"
                  and ns.get("status") is not None)

            pv = (await c.get("/progress/my-course", headers=VH)).json()
            nsv = pv.get("next_session")
            check("El vecino del MISMO nivel NO la recibe",
                  not nsv or nsv.get("id") != refuerzo)

            cal = (await c.get("/student/calendar", headers=VH)).json()
            ev = cal.get("events", cal) if isinstance(cal, dict) else cal
            check("Ni le aparece en el calendario",
                  not any(e.get("id") == refuerzo for e in ev))

        # ═══ 1b — CON GRUPO, EL REFUERZO TAMBIÉN CUENTA ════════════════
        r = await c.post("/admin/class-series", headers=AH, json={
            "name": "A68 Grupo", "course_id": cid, "level_id": nivel["id"],
            "teacher_id": profe["id"], "days_of_week": "mon,tue,wed,thu,fri",
            "start_time_hhmm": "18:00", "duration_min": 60,
            "start_date": datetime.date.today().isoformat(), "num_classes": 8,
            "modality": "online", "meeting_url": LINK,
        })
        grupo = r.json().get("series_id") if r.status_code == 201 else None
        check("Se crea un grupo", bool(grupo))

        if grupo and refuerzo:
            enrs = (await c.get("/admin/enrollments", headers=AH)).json()
            ei = enrs.get("items", enrs) if isinstance(enrs, dict) else enrs
            mia = [e for e in ei if e.get("student_id") == maria and e.get("is_active")]
            if mia:
                await c.post(f"/admin/enrollments/{mia[0]['id']}/assign-group",
                             headers=AH, json={"series_id": grupo})
            pc = (await c.get("/progress/my-course", headers=MH)).json()
            ns = pc.get("next_session")
            check("Ya con grupo, el refuerzo más próximo sigue siendo la próxima clase",
                  bool(ns) and ns.get("id") == refuerzo)
            await c.delete(f"/admin/sessions/{refuerzo}", headers=AH)
            pc2 = (await c.get("/progress/my-course", headers=MH)).json()
            ns2 = pc2.get("next_session")
            check("Sin el refuerzo, pasa a ser una clase de su grupo",
                  bool(ns2) and ns2.get("id") != refuerzo)

        # ═══ 4 — SEDE Y AULA EN EL EDITOR DE SERIE ═════════════════════
        sedes = (await c.get("/admin/branches", headers=AH)).json()
        sedes = sedes.get("items", sedes) if isinstance(sedes, dict) else sedes
        check("Hay sedes en el sistema", len(sedes) > 0)

        if grupo and sedes:
            sede = sedes[0]
            aulas = (await c.get(f"/admin/classrooms?branch_id={sede['id']}", headers=AH)).json()
            aulas = aulas.get("items", aulas) if isinstance(aulas, dict) else aulas

            r = await c.patch(f"/admin/class-series/{grupo}/reschedule", headers=AH,
                              json={"modality": "presencial",
                                    "branch_id": sede["id"],
                                    **({"classroom_id": aulas[0]["id"]} if aulas else {})})
            check("El editor de serie acepta sede y aula", r.status_code == 200)

            ses = (await c.get("/admin/sessions?filter_period=all&limit=200", headers=AH)).json()
            items = ses.get("items", ses) if isinstance(ses, dict) else ses
            delg = [x for x in items if x.get("series_id") == grupo]
            futuras = []
            for x in delg:
                t = datetime.datetime.fromisoformat(x["starts_at_utc"].replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=datetime.timezone.utc)
                if t > now:
                    futuras.append(x)
            check("Las clases futuras quedaron presenciales",
                  bool(futuras) and all(x["modality"] == "presencial" for x in futuras))
            check("Ya no dicen 'Ubicación por confirmar': tienen sede",
                  bool(futuras) and all(x.get("location") or x.get("branch_id")
                                        for x in futuras))

            r = await c.patch(f"/admin/class-series/{grupo}/reschedule", headers=AH,
                              json={"branch_id": 999999})
            check("Una sede inexistente se rechaza", r.status_code in (400, 404))

        # ═══ 5 — REPOSICIÓN TRAS CAMBIAR DE GRUPO ══════════════════════
        pasada_st = (now - datetime.timedelta(days=3)).isoformat()
        pasada_en = (now - datetime.timedelta(days=3) + datetime.timedelta(hours=1)).isoformat()
        r = await c.post("/admin/sessions", headers=AH, json={
            "title": "A68 Clase de junio", "starts_at_utc": pasada_st,
            "ends_at_utc": pasada_en, "modality": "online",
            "teacher_id": profe["id"], "course_id": cid, "level_id": nivel["id"],
            "meeting_url": LINK, "student_ids": [maria],
        })
        vieja = r.json().get("id") if r.status_code in (200, 201) else None
        check("Se crea una clase pasada que fue de María", bool(vieja))

        if vieja:
            # Evidencia histórica: el profesor le pasó lista
            ra = await c.post(f"/teacher/sessions/{vieja}/attendance", headers=TH,
                              json={"records": [{"student_id": maria, "state": "absent"}]})
            check("Queda registro histórico de asistencia", ra.status_code == 200)

            # Ahora se la saca de la audiencia: simula que ya no pertenece
            r2 = await c.post("/admin/sessions", headers=AH, json={
                "title": "A68 Clase ajena", "starts_at_utc": pasada_st,
                "ends_at_utc": pasada_en, "modality": "online", "video_provider": "dorismon",
                "teacher_id": profe["id"], "course_id": cid, "level_id": nivel["id"],
                "student_ids": [vecino],
            })
            ajena = r2.json().get("id") if r2.status_code in (200, 201) else None

            rm = await c.post(f"/student/sessions/{vieja}/request-makeup",
                              headers=MH, json={"reason": "Estuve enferma"})
            check(f"Con evidencia histórica SÍ puede pedir reposición (HTTP {rm.status_code})",
                  rm.status_code in (200, 201))

            if ajena:
                rm2 = await c.post(f"/student/sessions/{ajena}/request-makeup",
                                   headers=MH, json={"reason": "Intento indebido"})
                check(f"Sin evidencia y sin pertenencia: 403 (HTTP {rm2.status_code})",
                      rm2.status_code == 403)
                await c.delete(f"/admin/sessions/{ajena}", headers=AH)

            await c.delete(f"/admin/sessions/{vieja}", headers=AH)

        if grupo:
            await c.delete(f"/admin/class-series/{grupo}?future_only=false", headers=AH)

    print(f"\n{passed}/{total}")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
