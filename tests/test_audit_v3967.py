"""V3.9.67 — Regresiones de la auditoría externa de v3.9.65.

Cada bloque corresponde a un hallazgo concreto:

  1. Propagar solo lo modificado (no borrar excepciones futuras sin querer)
  2. La frontera de "esta y las siguientes" usa la hora ORIGINAL
  4. Presencial es efectivo también en backend (.ics, google-link, video)
  5. Seguridad de los endpoints de calendario
  6. Autorización de ESCRITURA (confirm, request-makeup, attendance)
  7. Coherencia de la audiencia al crear
  9. Estudiante sin Enrollment puede USAR la clase, no solo verla
"""
import sys
import asyncio
import datetime
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")
PASS = "Estudiante2026!"
LINK = "https://meet.google.com/aud-itor-ia1"


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
        nivel, otro_nivel = niveles[0], niveles[1]
        profe = [p for p in (await c.get("/admin/users?role=teacher", headers=AH)).json()["items"]
                 if p["email"] in SEED_TEACHERS][0]
        # V3.9.69 — presencial exige sede.
        _sd = (await c.get("/admin/branches", headers=AH)).json()
        _sd = _sd.get("items", _sd) if isinstance(_sd, dict) else _sd
        SEDE = _sd[0]["id"]
        TH = {"Authorization": f"Bearer {(await c.post('/auth/login', json={'email': profe['email'], 'password': 'Profe2026!'})).json()['access_token']}"}

        now = datetime.datetime.now(datetime.timezone.utc)

        async def sesiones(sid):
            out, page = [], 1
            while page <= 20:
                rr = await c.get(f"/admin/sessions?filter_period=all&limit=200&page={page}",
                                 headers=AH)
                d = rr.json()
                items = d.get("items", d) if isinstance(d, dict) else d
                if not items:
                    break
                out.extend(x for x in items if x.get("series_id") == sid)
                if len(items) < 200:
                    break
                page += 1
            return out

        def cuando(x):
            t = datetime.datetime.fromisoformat(x["starts_at_utc"].replace("Z", "+00:00"))
            return t.replace(tzinfo=datetime.timezone.utc) if t.tzinfo is None else t

        # ═══ 1 y 2 — PROPAGACIÓN Y FRONTERA ════════════════════════════
        r = await c.post("/admin/class-series", headers=AH, json={
            "name": "AUD Serie Delta", "course_id": cid, "level_id": nivel["id"],
            "teacher_id": profe["id"], "days_of_week": "mon,tue,wed,thu,fri",
            "start_time_hhmm": "13:00", "duration_min": 60,
            "start_date": datetime.date.today().isoformat(), "num_classes": 12,
            "modality": "online", "meeting_url": LINK, "video_provider": "meet",
        })
        serie = r.json().get("series_id") if r.status_code == 201 else None
        check("Se crea la serie de prueba", bool(serie))

        if serie:
            fut = sorted([x for x in await sesiones(serie) if cuando(x) > now],
                         key=lambda x: x["starts_at_utc"])
            check("La serie tiene al menos 4 futuras", len(fut) >= 4)
            s1, s2, s3, s4 = fut[0], fut[1], fut[2], fut[3]

            # s3 se vuelve una EXCEPCIÓN presencial
            await c.patch(f"/admin/sessions/{s3['id']}", headers=AH,
                          json={"modality": "presencial", "branch_id": SEDE, "apply_to": "this"})
            d = {x["id"]: x for x in await sesiones(serie)}
            check("s3 es ahora una excepción presencial",
                  d[s3["id"]]["modality"] == "presencial")

            # HALLAZGO 1: editar SOLO el título de s1 con "esta y siguientes"
            # NO debe tocar la modalidad de la excepción s3.
            r = await c.patch(f"/admin/sessions/{s1['id']}", headers=AH,
                              json={"title": "Titulo cambiado",
                                    "apply_to": "this_and_following"})
            check("Editar solo el título responde OK", r.status_code == 200)
            d = {x["id"]: x for x in await sesiones(serie)}
            check("La excepción presencial SOBREVIVE (no se pisó)",
                  d[s3["id"]]["modality"] == "presencial")
            check("Las demás siguen online",
                  d[s2["id"]]["modality"] == "online"
                  and d[s4["id"]]["modality"] == "online")
            check("El enlace no se borró en ninguna",
                  all(d[x["id"]].get("meeting_url") == LINK
                      for x in (s1, s2, s4)))

            # HALLAZGO 2: mover s2 HACIA ATRÁS (antes de s1) y propagar.
            # La frontera debe ser la hora ORIGINAL de s2, no la nueva.
            nuevo_inicio = (cuando(s1) - datetime.timedelta(hours=5))
            r = await c.patch(f"/admin/sessions/{s2['id']}", headers=AH,
                              json={"starts_at_utc": nuevo_inicio.isoformat(),
                                    "ends_at_utc": (nuevo_inicio + datetime.timedelta(hours=1)).isoformat(),
                                    "capacity": 7,
                                    "apply_to": "this_and_following"})
            check("Mover la clase y propagar responde OK", r.status_code == 200)
            d = {x["id"]: x for x in await sesiones(serie)}
            check("s1 (anterior en el orden original) NO se vio afectada",
                  d[s1["id"]].get("capacity") != 7)
            check("s3 y s4 (posteriores) SÍ recibieron el cambio",
                  d[s3["id"]].get("capacity") == 7 and d[s4["id"]].get("capacity") == 7)

            await c.delete(f"/admin/class-series/{serie}?future_only=false", headers=AH)

        # ═══ 4 — PRESENCIAL EFECTIVO EN BACKEND ════════════════════════
        stamp = datetime.datetime.now().strftime("%H%M%S%f")[:10]

        async def crear_alumno(nombre):
            email = f"a67{stamp}{nombre}@dorismon.do"
            r = await c.post("/admin/users", headers=AH, json={
                "email": email, "full_name": f"A67 {nombre}",
                "password": PASS, "role": "student"})
            if r.status_code not in (200, 201):
                return None, None
            uid = r.json().get("id") or r.json().get("user_id")
            lg = await c.post("/auth/login", json={"email": email, "password": PASS})
            return uid, {"Authorization": f"Bearer {lg.json()['access_token']}"}

        elegido, EH = await crear_alumno("ok")
        intruso, IH = await crear_alumno("no")
        check("Se crean los estudiantes de prueba", bool(elegido) and bool(intruso))

        st = (now + datetime.timedelta(hours=2)).isoformat()
        en = (now + datetime.timedelta(hours=3)).isoformat()

        r = await c.post("/admin/sessions", headers=AH, json={
            "title": "AUD Presencial con link", "starts_at_utc": st, "ends_at_utc": en,
            "modality": "presencial", "branch_id": SEDE, "teacher_id": profe["id"],
            "course_id": cid, "level_id": nivel["id"],
            "meeting_url": LINK, "video_provider": "meet",
            "student_ids": [elegido],
        })
        pres = r.json().get("id") if r.status_code in (200, 201) else None
        check("Se crea una clase PRESENCIAL que conserva el link", bool(pres))

        if pres:
            ics = await c.get(f"/calendar/session/{pres}.ics", headers=EH)
            check("El .ics responde al alumno de la clase", ics.status_code == 200)
            check("El .ics de una presencial NO trae el link",
                  ics.status_code == 200 and LINK not in ics.text)

            gl = await c.get(f"/calendar/session/{pres}/google-link", headers=EH)
            check("El link de Google Calendar responde", gl.status_code == 200)
            check("Google Calendar de una presencial NO trae el link",
                  gl.status_code == 200 and LINK not in str(gl.json()))

            vj = await c.post(f"/video/sessions/{pres}/join", headers=EH)
            check(f"Entrar al video de una PRESENCIAL se rechaza (HTTP {vj.status_code})",
                  vj.status_code == 400)
            vjt = await c.post(f"/video/sessions/{pres}/join", headers=TH)
            check("Ni siquiera el profesor entra al video de una presencial",
                  vjt.status_code == 400)

            # ═══ 5 — SEGURIDAD DE CALENDARIO ═══════════════════════════
            ics2 = await c.get(f"/calendar/session/{pres}.ics", headers=IH)
            check(f"Un NO destinatario no puede bajar el .ics (HTTP {ics2.status_code})",
                  ics2.status_code == 403)
            gl2 = await c.get(f"/calendar/session/{pres}/google-link", headers=IH)
            check(f"Un NO destinatario no obtiene el link de Google (HTTP {gl2.status_code})",
                  gl2.status_code == 403)
            check("El profesor de la clase SÍ puede",
                  (await c.get(f"/calendar/session/{pres}.ics", headers=TH)).status_code == 200)
            check("El admin SÍ puede",
                  (await c.get(f"/calendar/session/{pres}.ics", headers=AH)).status_code == 200)

            # ═══ 6 — AUTORIZACIÓN DE ESCRITURA ═════════════════════════
            cf = await c.post(f"/student/sessions/{pres}/confirm", headers=IH)
            check(f"Un NO destinatario no puede confirmar asistencia (HTTP {cf.status_code})",
                  cf.status_code == 403)
            cf2 = await c.post(f"/student/sessions/{pres}/confirm", headers=EH)
            check("El destinatario SÍ puede confirmar", cf2.status_code == 200)

            # Asistencia: el profesor no puede escribir fuera del roster
            at = await c.post(f"/teacher/sessions/{pres}/attendance", headers=TH,
                              json={"records": [{"student_id": intruso, "state": "present"}]})
            check(f"El profesor NO puede pasar lista a un ajeno (HTTP {at.status_code})",
                  at.status_code == 403)
            at2 = await c.post(f"/teacher/sessions/{pres}/attendance", headers=TH,
                               json={"records": [{"student_id": elegido, "state": "present"}]})
            check("El profesor SÍ puede pasar lista al de su roster",
                  at2.status_code == 200)

            await c.delete(f"/admin/sessions/{pres}", headers=AH)

        # ═══ 4b — ONLINE SIGUE FUNCIONANDO ═════════════════════════════
        r = await c.post("/admin/sessions", headers=AH, json={
            "title": "AUD Online normal", "starts_at_utc": st, "ends_at_utc": en,
            "modality": "online", "teacher_id": profe["id"],
            "course_id": cid, "level_id": nivel["id"],
            "meeting_url": LINK, "video_provider": "meet",
            "student_ids": [elegido],
        })
        onl = r.json().get("id") if r.status_code in (200, 201) else None
        if onl:
            ics = await c.get(f"/calendar/session/{onl}.ics", headers=EH)
            check("Una clase ONLINE sí trae el link en el .ics",
                  ics.status_code == 200 and LINK in ics.text)

        # ═══ 9 — SIN ENROLLMENT, LA CLASE ES USABLE ════════════════════
        if onl:
            pc = (await c.get("/progress/my-course", headers=EH)).json()
            check("Sin Enrollment sigue devolviendo enrolled=False",
                  pc.get("enrolled") is False)
            ns = pc.get("next_session")
            check("Pero AHORA trae su próxima clase", bool(ns) and ns.get("id") == onl)
            check("Con los datos que el botón necesita",
                  bool(ns) and ns.get("video_provider") is not None
                  and ns.get("modality") is not None and ns.get("status") is not None)
            await c.delete(f"/admin/sessions/{onl}", headers=AH)

        # ═══ 7 — COHERENCIA DE AUDIENCIA AL CREAR ══════════════════════
        base = {
            "title": "AUD Combi", "starts_at_utc": st, "ends_at_utc": en,
            "modality": "online", "teacher_id": profe["id"],
            "course_id": cid, "level_id": nivel["id"],
            # V3.9.70 — Una clase online con proveedor EXTERNO ya exige enlace
            # al crearse. Este fixture prueba la audiencia, no el video, así
            # que usa Video Dorismon: online sin enlace externo obligatorio.
            "video_provider": "dorismon",
        }
        # Se crean grupos propios: el seed no garantiza que existan.
        async def crear_grupo(nombre, nivel_obj):
            r = await c.post("/admin/class-series", headers=AH, json={
                "name": nombre, "course_id": cid, "level_id": nivel_obj["id"],
                "teacher_id": profe["id"], "days_of_week": "mon,wed",
                "start_time_hhmm": "09:00", "duration_min": 60,
                "start_date": datetime.date.today().isoformat(),
                "num_classes": 4, "modality": "online", "meeting_url": LINK,
            })
            return r.json().get("series_id") if r.status_code == 201 else None

        g_nivel = await crear_grupo("AUD Grupo Nivel", nivel)
        g_otro = await crear_grupo("AUD Grupo Otro", otro_nivel)
        check("Se crean grupos de dos niveles distintos",
              bool(g_nivel) and bool(g_otro))
        serie_nivel = [{"id": g_nivel}] if g_nivel else []
        serie_otro = [{"id": g_otro}] if g_otro else []

        r = await c.post("/admin/sessions", headers=AH,
                         json={**base, "is_open_event": True, "student_ids": [elegido]})
        check("Evento abierto + student_ids se rechaza", r.status_code == 400)

        if serie_nivel:
            r = await c.post("/admin/sessions", headers=AH,
                             json={**base, "student_ids": [elegido],
                                   "series_id": serie_nivel[0]["id"]})
            check("student_ids + series_id a la vez se rechaza", r.status_code == 400)

        r = await c.post("/admin/sessions", headers=AH,
                         json={**base, "series_id": "grupo-que-no-existe"})
        check("Un grupo inexistente se rechaza", r.status_code in (400, 404))

        if serie_otro:
            r = await c.post("/admin/sessions", headers=AH,
                             json={**base, "series_id": serie_otro[0]["id"]})
            check("Un grupo de OTRO nivel se rechaza", r.status_code == 400)

        if serie_nivel:
            r = await c.post("/admin/sessions", headers=AH,
                             json={**base, "series_id": serie_nivel[0]["id"]})
            check("Un grupo del MISMO curso y nivel se acepta",
                  r.status_code in (200, 201))
            if r.status_code in (200, 201):
                await c.delete(f"/admin/sessions/{r.json()['id']}", headers=AH)

        for g in (g_nivel, g_otro):
            if g:
                await c.delete(f"/admin/class-series/{g}?future_only=false", headers=AH)

    print(f"\n{passed}/{total}")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
