"""V3.9.69 — Cierre final antes de producción.

  1. Sede y aula en series (crear y editar)
  2. Una serie PRESENCIAL no necesita videollamada
  3. Validación de la configuración efectiva de una sesión
  4. Audiencia por TERNA COMPLETA (curso + nivel + profesor)

El punto 4 es el importante: con dos matrículas, comparar profesor y nivel
por separado dejaba pasar combinaciones que el alumno nunca tuvo.
"""
import sys
import asyncio
import datetime
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")
PASS = "Estudiante2026!"
LINK = "https://meet.google.com/v69-test-xy"


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
        nivel_a, nivel_b = niveles[0], niveles[1]
        profes = [p for p in (await c.get("/admin/users?role=teacher", headers=AH)).json()["items"]
                  if p["email"] in SEED_TEACHERS]
        luis, ana = profes[0], profes[1]

        sedes = (await c.get("/admin/branches", headers=AH)).json()
        sedes = sedes.get("items", sedes) if isinstance(sedes, dict) else sedes
        check("Hay al menos dos sedes", len(sedes) >= 2)
        sede1, sede2 = sedes[0], sedes[1] if len(sedes) > 1 else sedes[0]

        async def aulas_de(b):
            r = (await c.get(f"/admin/classrooms?branch_id={b['id']}", headers=AH)).json()
            return r.get("items", r) if isinstance(r, dict) else r

        aulas1, aulas2 = await aulas_de(sede1), await aulas_de(sede2)

        hoy = datetime.date.today().isoformat()
        base_serie = {
            "course_id": cid, "level_id": nivel_a["id"], "teacher_id": luis["id"],
            "days_of_week": "mon,tue,wed,thu,fri", "start_time_hhmm": "15:00",
            "duration_min": 60, "start_date": hoy, "num_classes": 8,
        }
        creadas = []

        # ═══ 1 y 2 — SEDE/AULA Y PRESENCIAL SIN VIDEO ══════════════════
        r = await c.post("/admin/class-series", headers=AH, json={
            **base_serie, "name": "V69 Presencial", "modality": "presencial",
            "branch_id": sede1["id"],
            **({"classroom_id": aulas1[0]["id"]} if aulas1 else {}),
        })
        check("Se crea una serie PRESENCIAL sin meeting_url", r.status_code == 201)
        s_pres = r.json().get("series_id") if r.status_code == 201 else None
        if s_pres:
            creadas.append(s_pres)

        r = await c.post("/admin/class-series", headers=AH, json={
            **base_serie, "name": "V69 Presencial sin sede", "modality": "presencial",
        })
        check("Una serie presencial SIN sede se rechaza", r.status_code == 400)

        # V3.9.70: una serie crea sesiones reales de inmediato; externo sin
        # enlace ya no se acepta como "configurar después". Si se quiere
        # crear sin fallback, se usa Video Dorismon.
        r = await c.post("/admin/class-series", headers=AH, json={
            **base_serie, "name": "V69 Online sin link", "modality": "online",
            "video_provider": "meet",
        })
        check("Crear una serie online EXTERNA sin link se rechaza",
              r.status_code == 400)

        r = await c.post("/admin/class-series", headers=AH, json={
            **base_serie, "name": "V69 Online dorismon", "modality": "online",
            "video_provider": "dorismon",
        })
        check("Online + Video Dorismon SIN respaldo se acepta", r.status_code == 201)
        if r.status_code == 201:
            creadas.append(r.json()["series_id"])

        r = await c.post("/admin/class-series", headers=AH, json={
            **base_serie, "name": "V69 Hibrida", "modality": "hibrida",
            "video_provider": "dorismon", "branch_id": sede1["id"],
        })
        check("Híbrida + Dorismon + sede, SIN respaldo, se acepta",
              r.status_code == 201)
        if r.status_code == 201:
            creadas.append(r.json()["series_id"])

        if s_pres:
            sl = (await c.get("/admin/class-series", headers=AH)).json()
            mia = [x for x in sl if x["id"] == s_pres]
            check("El listado devuelve branch_id para precargar el editor",
                  bool(mia) and mia[0].get("branch_id") == sede1["id"])

            # ⚠️ EL CASO CLAVE: editar SOLO el aula de una presencial sin video
            nueva_aula = aulas1[1]["id"] if len(aulas1) > 1 else (
                aulas1[0]["id"] if aulas1 else None)
            r = await c.patch(f"/admin/class-series/{s_pres}/reschedule", headers=AH,
                              json={"branch_id": sede1["id"],
                                    **({"classroom_id": nueva_aula} if nueva_aula else {})})
            check(f"Editar solo sede/aula de una presencial SIN link: 200 "
                  f"(HTTP {r.status_code})", r.status_code == 200)

            if aulas2:
                r = await c.patch(f"/admin/class-series/{s_pres}/reschedule", headers=AH,
                                  json={"branch_id": sede1["id"],
                                        "classroom_id": aulas2[0]["id"]})
                check("Un aula de OTRA sede se rechaza", r.status_code == 400)

            r = await c.patch(f"/admin/class-series/{s_pres}/reschedule", headers=AH,
                              json={"branch_id": 999999})
            check("Una sede inexistente se rechaza", r.status_code in (400, 404))

            # Cambiar de sede sin elegir aula no arrastra el aula vieja
            if aulas1 and sede2["id"] != sede1["id"]:
                r = await c.patch(f"/admin/class-series/{s_pres}/reschedule",
                                  headers=AH, json={"branch_id": sede2["id"]})
                if r.status_code == 200:
                    sl2 = (await c.get("/admin/class-series", headers=AH)).json()
                    m2 = [x for x in sl2 if x["id"] == s_pres]
                    check("Al cambiar de sede NO se conserva un aula de la otra",
                          bool(m2) and not m2[0].get("classroom_id"))
                else:
                    check("Al cambiar de sede NO se conserva un aula de la otra",
                          False)

        # ═══ 3 — VALIDACIÓN DE UNA SESIÓN ══════════════════════════════
        now = datetime.datetime.now(datetime.timezone.utc)
        st = (now + datetime.timedelta(hours=4)).isoformat()
        en = (now + datetime.timedelta(hours=5)).isoformat()
        r = await c.post("/admin/sessions", headers=AH, json={
            "title": "V69 Sesion", "starts_at_utc": st, "ends_at_utc": en,
            "modality": "online", "teacher_id": luis["id"],
            "course_id": cid, "level_id": nivel_a["id"],
            "meeting_url": LINK, "video_provider": "meet",
        })
        ses = r.json().get("id") if r.status_code in (200, 201) else None
        check("Se crea una sesión online", bool(ses))

        if ses:
            r = await c.patch(f"/admin/sessions/{ses}", headers=AH,
                              json={"modality": "presencial"})
            check("Pasar a presencial SIN sede se rechaza", r.status_code == 400)

            r = await c.patch(f"/admin/sessions/{ses}", headers=AH,
                              json={"modality": "presencial", "branch_id": sede1["id"]})
            check("Pasar a presencial CON sede se acepta", r.status_code == 200)

            r = await c.patch(f"/admin/sessions/{ses}", headers=AH,
                              json={"modality": "online", "video_provider": "meet",
                                    "meeting_url": ""})
            check("Online con externo y sin link se rechaza", r.status_code == 400)

            r = await c.patch(f"/admin/sessions/{ses}", headers=AH,
                              json={"modality": "online", "video_provider": "dorismon"})
            check("Online + Dorismon sin respaldo se acepta", r.status_code == 200)

            r = await c.patch(f"/admin/sessions/{ses}", headers=AH,
                              json={"title": "Solo el titulo"})
            check("Cambiar solo el título no dispara validación de video",
                  r.status_code == 200)

            if aulas2:
                r = await c.patch(f"/admin/sessions/{ses}", headers=AH,
                                  json={"modality": "hibrida", "branch_id": sede1["id"],
                                        "classroom_id": aulas2[0]["id"]})
                check("Aula de otra sede en una sesión se rechaza",
                      r.status_code == 400)

            await c.delete(f"/admin/sessions/{ses}", headers=AH)

        # ═══ 4 — TERNA COMPLETA (curso + nivel + profesor) ═════════════
        stamp = datetime.datetime.now().strftime("%H%M%S%f")[:10]

        async def crear_alumno(n):
            email = f"v69{stamp}{n}@dorismon.do"
            r = await c.post("/admin/users", headers=AH, json={
                "email": email, "full_name": f"V69 {n}", "password": PASS,
                "role": "student"})
            if r.status_code not in (200, 201):
                return None, None
            uid = r.json().get("id") or r.json().get("user_id")
            lg = await c.post("/auth/login", json={"email": email, "password": PASS})
            return uid, {"Authorization": f"Bearer {lg.json()['access_token']}"}

        cruzado, XH = await crear_alumno("cruz")
        legitimo, LH = await crear_alumno("legit")
        check("Se crean los estudiantes del caso cruzado",
              bool(cruzado) and bool(legitimo))

        async def matricular(uid, nivel, profe):
            r = await c.post("/admin/enrollments", headers=AH, json={
                "student_id": uid, "course_id": cid,
                "level_id": nivel["id"], "teacher_id": profe["id"]})
            return r.status_code in (200, 201)

        # El caso peligroso: A2 con Luis Y B1 con Ana
        check("Alumno cruzado: matrícula A con Luis",
              await matricular(cruzado, nivel_a, luis))
        check("Alumno cruzado: matrícula B con Ana",
              await matricular(cruzado, nivel_b, ana))
        # El legítimo: B1 con Luis
        check("Alumno legítimo: matrícula B con Luis",
              await matricular(legitimo, nivel_b, luis))

        # Clase suelta de nivel B con LUIS, sin audiencia explícita
        r = await c.post("/admin/sessions", headers=AH, json={
            "title": "V69 Suelta B con Luis", "starts_at_utc": st, "ends_at_utc": en,
            "modality": "online", "teacher_id": luis["id"],
            "course_id": cid, "level_id": nivel_b["id"],
            "meeting_url": LINK, "video_provider": "meet",
        })
        cruz = r.json().get("id") if r.status_code in (200, 201) else None
        check("Se crea la clase suelta B/Luis", bool(cruz))

        if cruz:
            async def la_ve(H):
                cal = (await c.get("/student/calendar", headers=H)).json()
                ev = cal.get("events", cal) if isinstance(cal, dict) else cal
                return any(e.get("id") == cruz for e in ev)

            check("NEGATIVO: el alumno cruzado (B/Ana + A/Luis) NO la ve",
                  not await la_ve(XH))
            check("POSITIVO: el alumno legítimo (B/Luis) SÍ la ve",
                  await la_ve(LH))

            rx = await c.post(f"/student/sessions/{cruz}/confirm", headers=XH)
            check(f"NEGATIVO: el cruzado NO puede acceder por API (HTTP {rx.status_code})",
                  rx.status_code == 403)
            rl = await c.post(f"/student/sessions/{cruz}/confirm", headers=LH)
            check(f"POSITIVO: el legítimo SÍ puede (HTTP {rl.status_code})",
                  rl.status_code == 200)

            # Simetría: el roster del profesor debe decir lo mismo
            TH = {"Authorization": f"Bearer {(await c.post('/auth/login', json={'email': luis['email'], 'password': 'Profe2026!'})).json()['access_token']}"}
            rr = await c.get(f"/teacher/sessions/{cruz}/attendance", headers=TH)
            if rr.status_code == 200:
                ids = {x.get("student_id") for x in rr.json().get("students", [])}
                check("SIMETRÍA: el roster incluye al legítimo",
                      legitimo in ids)
                check("SIMETRÍA: el roster NO incluye al cruzado",
                      cruzado not in ids)
            else:
                check("El roster responde", False)

            await c.delete(f"/admin/sessions/{cruz}", headers=AH)

        for g in creadas:
            await c.delete(f"/admin/class-series/{g}?future_only=false", headers=AH)

    print(f"\n{passed}/{total}")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
