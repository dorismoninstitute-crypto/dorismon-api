"""V3.9.65 — Clases sueltas para estudiantes concretos.

EL CASO REAL: una clase de refuerzo creada para un alumno nuevo, todavía sin
grupo, le quedaba INVISIBLE. El backend le habría dejado entrar —la
autorización sí miraba SessionAudience— pero el dashboard y el calendario se
cortaban antes si no tenía Enrollment activo.

Y el reverso, igual de importante: quitar ese corte NO puede abrir la puerta
a que un compañero del mismo nivel vea una clase que no es suya.

REGLA QUE SE VERIFICA:
    sin Enrollment + está en SessionAudience  -> LA VE
    sin Enrollment + NO está en SessionAudience -> NO LA VE
    con Enrollment + mismo nivel + no elegido -> NO LA VE (ni por API)
"""
import sys
import asyncio
import datetime
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")
PASS = "Estudiante2026!"


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

        stamp = datetime.datetime.now().strftime("%H%M%S%f")[:10]

        async def crear_alumno(nombre):
            """Estudiante NUEVO: sin grupo, sin Enrollment de grupo."""
            email = f"aud{stamp}{nombre}@dorismon.do"
            r = await c.post("/admin/users", headers=AH, json={
                "email": email, "full_name": f"Aud {nombre}",
                "password": PASS, "role": "student",
            })
            if r.status_code not in (200, 201):
                return None, None, email
            uid = r.json().get("id") or r.json().get("user_id")
            lg = await c.post("/auth/login", json={"email": email, "password": PASS})
            h = {"Authorization": f"Bearer {lg.json()['access_token']}"} if lg.status_code == 200 else None
            return uid, h, email

        elegido_id, EH, _ = await crear_alumno("elegido")
        otro_id, OH, _ = await crear_alumno("otro")
        check("Se crean dos estudiantes nuevos SIN grupo",
              bool(elegido_id) and bool(otro_id) and EH and OH)
        if not (elegido_id and otro_id and EH and OH):
            print(f"\n{passed}/{total}")
            return False

        # Un tercero CON Enrollment en el mismo nivel: el compañero de clase
        mu = (await c.get("/admin/users?q=maria.estudiante", headers=AH)).json()
        companero = mu["items"][0]["id"]
        CH = {"Authorization": f"Bearer {(await c.post('/auth/login', json={'email': 'maria.estudiante@dorismon.do', 'password': PASS})).json()['access_token']}"}

        now = datetime.datetime.now(datetime.timezone.utc)
        st = (now + datetime.timedelta(hours=3)).isoformat()
        en = (now + datetime.timedelta(hours=4)).isoformat()

        # ═══ CLASE PARA UN SOLO ESTUDIANTE SIN GRUPO ═══════════════════
        r = await c.post("/admin/sessions", headers=AH, json={
            "title": "AUD Refuerzo individual", "starts_at_utc": st, "ends_at_utc": en,
            "modality": "online", "teacher_id": profe["id"],
            "course_id": cid, "level_id": nivel["id"],
            "video_provider": "dorismon",
            "student_ids": [elegido_id],
        })
        check("Se crea la clase con audiencia explícita", r.status_code in (200, 201))
        s1 = r.json().get("id") if r.status_code in (200, 201) else None
        check("Reporta la audiencia guardada",
              (r.json() if r.status_code in (200, 201) else {}).get("audience") == 1)

        async def ve_en_calendario(H, sid):
            cal = (await c.get("/student/calendar", headers=H)).json()
            ev = cal.get("events", cal) if isinstance(cal, dict) else cal
            return any(e.get("id") == sid for e in ev if e.get("type") == "class")

        async def ve_en_dashboard(H, sid):
            d = (await c.get("/progress/my-course", headers=H)).json()
            ns = d.get("next_session")
            if ns and ns.get("id") == sid:
                return True
            d2 = (await c.get("/student/dashboard", headers=H)).json()
            for k in ("next_classes", "sessions", "upcoming"):
                v = d2.get(k)
                if isinstance(v, list) and any(x.get("id") == sid for x in v):
                    return True
            return False

        if s1:
            check("SIN Enrollment + en la audiencia: LA VE en el calendario",
                  await ve_en_calendario(EH, s1))
            check("SIN Enrollment + en la audiencia: LA VE en el dashboard",
                  await ve_en_dashboard(EH, s1))
            check("SIN Enrollment + NO en la audiencia: NO la ve",
                  not await ve_en_calendario(OH, s1))
            check("Compañero del MISMO NIVEL no elegido: NO la ve",
                  not await ve_en_calendario(CH, s1))

            # ── Autorización por API: no basta con conocer el ID ──
            #
            # /video/.../join NO sirve para comprobar esto en local: mira si
            # LiveKit está configurado ANTES que los permisos, así que aquí
            # todo devuelve 503. Se usa /student/sessions/{id}/notify-absence,
            # que pasa por el MISMO `puede_acceder_a_clase` y sí responde.
            r = await c.post(f"/student/sessions/{s1}/notify-absence",
                             headers=EH, json={"reason": "prueba"})
            check(f"El elegido SÍ es reconocido como suya la clase (HTTP {r.status_code})",
                  r.status_code not in (403, 404))
            r = await c.post(f"/student/sessions/{s1}/notify-absence",
                             headers=OH, json={"reason": "prueba"})
            check(f"El no elegido es RECHAZADO aunque ponga el ID (HTTP {r.status_code})",
                  r.status_code in (403, 404))
            r = await c.post(f"/student/sessions/{s1}/notify-absence",
                             headers=CH, json={"reason": "prueba"})
            check(f"El compañero del mismo nivel TAMBIÉN es rechazado (HTTP {r.status_code})",
                  r.status_code in (403, 404))

            # Roster / asistencia usan la misma audiencia
            TH = {"Authorization": f"Bearer {(await c.post('/auth/login', json={'email': profe['email'], 'password': 'Profe2026!'})).json()['access_token']}"}
            rr = await c.get(f"/teacher/sessions/{s1}/attendance", headers=TH)
            if rr.status_code == 200:
                data = rr.json()
                items = data.get("students", data.get("items", data))
                ids = {x.get("student_id") or x.get("id") for x in items} if isinstance(items, list) else set()
                check("El roster del profesor trae SOLO al elegido",
                      elegido_id in ids and otro_id not in ids and companero not in ids)
                # Y debe poder pasarle lista de verdad, no solo verlo
                rp = await c.post(f"/teacher/sessions/{s1}/attendance", headers=TH,
                                  json={"records": [{"student_id": elegido_id,
                                                     "state": "present"}]})
                check("El profesor PUEDE pasar lista a un alumno sin grupo",
                      rp.status_code == 200)
                rr2 = await c.get(f"/teacher/sessions/{s1}/attendance", headers=TH)
                d2 = rr2.json() if rr2.status_code == 200 else {}
                st2 = d2.get("students", [])
                check("La asistencia quedó guardada",
                      any(x.get("student_id") == elegido_id and x.get("state") == "present"
                          for x in st2))
            else:
                check("El roster responde", False)

        # ═══ CLASE PARA VARIOS ═════════════════════════════════════════
        r = await c.post("/admin/sessions", headers=AH, json={
            "title": "AUD Refuerzo grupal", "starts_at_utc": st, "ends_at_utc": en,
            "modality": "online", "teacher_id": profe["id"],
            "course_id": cid, "level_id": nivel["id"],
            "video_provider": "dorismon",
            "student_ids": [elegido_id, otro_id],
        })
        s2 = r.json().get("id") if r.status_code in (200, 201) else None
        check("Se crea una clase para VARIOS estudiantes", bool(s2))
        if s2:
            check("Los DOS elegidos la ven",
                  await ve_en_calendario(EH, s2) and await ve_en_calendario(OH, s2))
            check("El compañero del mismo nivel sigue sin verla",
                  not await ve_en_calendario(CH, s2))

        # ═══ NOTIFICACIONES SOLO A LA AUDIENCIA ════════════════════════
        async def avisos(H, titulo):
            n = (await c.get("/notifications", headers=H)).json()
            items = n.get("items", n) if isinstance(n, dict) else n
            return [x for x in items if titulo in (x.get("body") or "") or titulo in (x.get("title") or "")]

        check("El elegido recibió aviso de la clase",
              len(await avisos(EH, "AUD Refuerzo individual")) > 0)
        check("El compañero del MISMO NIVEL no recibió ese aviso",
              len(await avisos(CH, "AUD Refuerzo individual")) == 0)

        # ═══ VALIDACIÓN DE IDs ═════════════════════════════════════════
        r = await c.post("/admin/sessions", headers=AH, json={
            "title": "AUD Inválida", "starts_at_utc": st, "ends_at_utc": en,
            "modality": "online", "video_provider": "dorismon", "teacher_id": profe["id"],
            "course_id": cid, "level_id": nivel["id"],
            "student_ids": ["no-existe-este-id"],
        })
        check("Rechaza un student_id que no existe", r.status_code == 400)

        r = await c.post("/admin/sessions", headers=AH, json={
            "title": "AUD Profe como alumno", "starts_at_utc": st, "ends_at_utc": en,
            "modality": "online", "video_provider": "dorismon", "teacher_id": profe["id"],
            "course_id": cid, "level_id": nivel["id"],
            "student_ids": [profe["id"]],
        })
        check("Rechaza un ID que no es de estudiante", r.status_code == 400)

        # Limpieza
        for sid in (s1, s2):
            if sid:
                await c.delete(f"/admin/sessions/{sid}", headers=AH)

    print(f"\n{passed}/{total}")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
