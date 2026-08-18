"""V3.9.64 — Entrar a la videollamada en los tres sitios donde se puede.

EL PROBLEMA: con Video Dorismon el `meeting_url` es un RESPALDO OPCIONAL,
pero el panel del estudiante, el del profesor y el calendario solo mostraban
el botón de entrar si existía `meeting_url`. Resultado: un grupo con video
propio y sin enlace de respaldo se quedaba sin forma de entrar a clase.

Este test comprueba los DATOS que el backend entrega a cada pantalla, que es
lo que determina si el botón puede aparecer y a dónde lleva:

  · dorismon SIN respaldo  -> profesor y estudiante deben poder entrar
  · dorismon CON respaldo  -> sigue siendo dorismon, no se abre el Meet
  · meet/zoom externo      -> sigue abriendo el enlace externo
  · pasar una serie de meet a dorismon se refleja en dashboard y calendario

La condición de render del frontend es, en las tres pantallas:
    video_provider === "dorismon" || meeting_url
así que aquí se verifica que ambos campos lleguen siempre con el valor
correcto.
"""
import sys
import asyncio
import datetime
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estudiante2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")

MEET = "https://meet.google.com/res-pald-oxx"


def puede_entrar(obj) -> bool:
    """La MISMA condición que usan las tres pantallas del frontend."""
    if not obj:
        return False
    return obj.get("video_provider") == "dorismon" or bool(obj.get("meeting_url"))


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
        stok = (await c.post("/auth/login", json=STUDENT)).json()["access_token"]
        SH = {"Authorization": f"Bearer {stok}"}

        mu = (await c.get("/admin/users?q=maria.estudiante", headers=AH)).json()
        est = mu["items"][0]["id"]
        perfil = (await c.get(f"/admin/students/{est}/profile", headers=AH)).json()

        cid = (await c.get("/admin/courses", headers=AH)).json()[0]["id"]
        lv = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        niveles = lv["items"] if isinstance(lv, dict) else lv
        nivel = [l for l in niveles if l["code"] == perfil.get("current_level_code")]
        nivel = nivel[0] if nivel else niveles[0]

        profes = (await c.get("/admin/users?role=teacher", headers=AH)).json()["items"]
        profe = [p for p in profes if p["email"] in SEED_TEACHERS][0]
        PT = (await c.post("/auth/login", json={"email": profe["email"],
                                                "password": "Profe2026!"})).json()
        TH = {"Authorization": f"Bearer {PT['access_token']}"} if "access_token" in PT else None
        check("El profesor puede iniciar sesión", TH is not None)

        now = datetime.datetime.now(datetime.timezone.utc)
        creadas = []

        async def crear_clase(titulo, video_provider, meeting_url, mins):
            """Clase suelta para este estudiante y este profesor."""
            st = (now + datetime.timedelta(minutes=mins)).isoformat()
            en = (now + datetime.timedelta(minutes=mins + 60)).isoformat()
            body = {
                "title": titulo, "starts_at_utc": st, "ends_at_utc": en,
                "modality": "online", "teacher_id": profe["id"],
                "course_id": cid, "level_id": nivel["id"],
                "video_provider": video_provider,
                "student_ids": [est],
            }
            if meeting_url is not None:
                body["meeting_url"] = meeting_url
            r = await c.post("/admin/sessions", headers=AH, json=body)
            if r.status_code in (200, 201):
                sid = r.json().get("id")
                creadas.append(sid)
                return sid
            return None

        # ═══ CASO 1 — DORISMON SIN RESPALDO ════════════════════════════
        s_sin = await crear_clase("VE Dorismon sin respaldo", "dorismon", None, 10)
        check("Se crea una clase con Video Dorismon y SIN enlace", bool(s_sin))

        if s_sin:
            ses = (await c.get("/admin/sessions?filter_period=all&limit=200",
                               headers=AH)).json()
            items = ses.get("items", ses) if isinstance(ses, dict) else ses
            m = [x for x in items if x["id"] == s_sin]
            check("La clase quedó con video_provider='dorismon'",
                  bool(m) and m[0].get("video_provider") == "dorismon")
            check("La clase NO tiene meeting_url (el respaldo es opcional)",
                  bool(m) and not m[0].get("meeting_url"))
            check("ADMIN: aun así se puede entrar", puede_entrar(m[0] if m else None))

        # PROFESOR: sus clases de hoy
        if TH:
            td = (await c.get("/teacher/dashboard", headers=TH)).json()
            todas_t = []
            # `today_schedule` es la lista que el panel del profesor usa para
            # renderizar JoinClassButton. `week_schedule` es solo un listado
            # informativo y no muestra el botón.
            v = td.get("today_schedule")
            if isinstance(v, list):
                todas_t.extend(v)
            mt = [x for x in todas_t if x.get("id") == s_sin]
            check("PROFESOR: la clase aparece en su panel", bool(mt))
            if mt:
                check("PROFESOR: recibe video_provider='dorismon'",
                      mt[0].get("video_provider") == "dorismon")
                check("PROFESOR: PUEDE ENTRAR sin enlace de respaldo",
                      puede_entrar(mt[0]))

        # ESTUDIANTE: calendario
        cal = (await c.get("/student/calendar", headers=SH)).json()
        eventos = cal.get("events", cal) if isinstance(cal, dict) else cal
        clases_cal = [e for e in eventos if e.get("type") == "class"]
        check("ESTUDIANTE: el calendario devuelve clases", len(clases_cal) > 0)
        check("CALENDARIO: todas las clases traen video_provider",
              all("video_provider" in e for e in clases_cal))
        check("CALENDARIO: todas traen ends_at_utc",
              all("ends_at_utc" in e for e in clases_cal))
        check("CALENDARIO: todas traen status",
              all("status" in e for e in clases_cal))

        ec = [e for e in clases_cal if e.get("id") == s_sin]
        check("CALENDARIO: aparece la clase con video propio", bool(ec))
        if ec:
            check("CALENDARIO: video_provider='dorismon'",
                  ec[0].get("video_provider") == "dorismon")
            check("CALENDARIO: PUEDE ENTRAR sin enlace de respaldo",
                  puede_entrar(ec[0]))

        # ═══ CASO 2 — DORISMON CON MEET DE RESPALDO ════════════════════
        s_con = await crear_clase("VE Dorismon con respaldo", "dorismon", MEET, 20)
        check("Se crea una clase con Video Dorismon Y enlace de respaldo",
              bool(s_con))
        if s_con:
            ses = (await c.get("/admin/sessions?filter_period=all&limit=200",
                               headers=AH)).json()
            items = ses.get("items", ses) if isinstance(ses, dict) else ses
            m = [x for x in items if x["id"] == s_con]
            check("Guarda el respaldo en meeting_url",
                  bool(m) and m[0].get("meeting_url") == MEET)
            # LO IMPORTANTE: el proveedor manda. El botón entra a Dorismon,
            # NO abre el Meet, aunque el enlace exista.
            check("Tener respaldo NO cambia el proveedor (sigue 'dorismon')",
                  bool(m) and m[0].get("video_provider") == "dorismon")

            cal2 = (await c.get("/student/calendar", headers=SH)).json()
            ev2 = cal2.get("events", cal2) if isinstance(cal2, dict) else cal2
            ec2 = [e for e in ev2 if e.get("id") == s_con]
            if ec2:
                check("CALENDARIO: con respaldo sigue siendo 'dorismon'",
                      ec2[0].get("video_provider") == "dorismon")
                check("CALENDARIO: el respaldo viaja como meeting_url",
                      ec2[0].get("meeting_url") == MEET)

        # ═══ CASO 3 — ENLACE EXTERNO ═══════════════════════════════════
        s_ext = await crear_clase("VE Meet externo", "meet", MEET, 30)
        check("Se crea una clase con enlace externo", bool(s_ext))
        if s_ext:
            ses = (await c.get("/admin/sessions?filter_period=all&limit=200",
                               headers=AH)).json()
            items = ses.get("items", ses) if isinstance(ses, dict) else ses
            m = [x for x in items if x["id"] == s_ext]
            check("La clase externa mantiene video_provider='meet'",
                  bool(m) and m[0].get("video_provider") == "meet")
            check("La clase externa conserva su enlace",
                  bool(m) and m[0].get("meeting_url") == MEET)
            check("El enlace externo permite entrar", puede_entrar(m[0] if m else None))

            cal3 = (await c.get("/student/calendar", headers=SH)).json()
            ev3 = cal3.get("events", cal3) if isinstance(cal3, dict) else cal3
            ec3 = [e for e in ev3 if e.get("id") == s_ext]
            if ec3:
                check("CALENDARIO: la clase externa sigue abriendo el enlace",
                      ec3[0].get("video_provider") == "meet"
                      and ec3[0].get("meeting_url") == MEET)

        # ═══ CASO 4 — UNA SERIE PASA DE MEET A DORISMON ════════════════
        #
        # El cambio se hace con el editor de series (v3.9.63) y debe verse
        # tanto en el panel del profesor como en el calendario del alumno.
        hoy = datetime.date.today().isoformat()
        r = await c.post("/admin/class-series", headers=AH, json={
            "name": "VE Serie Migra", "course_id": cid, "level_id": nivel["id"],
            "teacher_id": profe["id"], "days_of_week": "mon,tue,wed,thu,fri",
            "start_time_hhmm": "07:00", "duration_min": 60,
            "start_date": hoy, "num_classes": 10, "modality": "online",
            "meeting_url": MEET, "video_provider": "meet",
        })
        check("Se crea una serie con enlace externo", r.status_code == 201)
        serie = r.json().get("series_id") if r.status_code == 201 else None

        if serie:
            # Matricular a María en ESE grupo para que lo vea en su calendario
            enr = (await c.get("/admin/enrollments", headers=AH)).json()
            ei = enr.get("items", enr) if isinstance(enr, dict) else enr
            mia = [e for e in ei if e.get("student_id") == est and e.get("is_active")]
            if mia:
                await c.post(f"/admin/enrollments/{mia[0]['id']}/assign-group",
                             headers=AH, json={"series_id": serie})

            def sesiones_serie():
                return c.get("/admin/sessions?filter_period=all&limit=200", headers=AH)

            ses = (await sesiones_serie()).json()
            items = ses.get("items", ses) if isinstance(ses, dict) else ses
            de_serie = [x for x in items if x.get("series_id") == serie]
            check("La serie generó clases con 'meet'",
                  bool(de_serie) and all(x.get("video_provider") == "meet"
                                         for x in de_serie))

            # Migrar a Video Dorismon con el editor de series
            r = await c.patch(f"/admin/class-series/{serie}/reschedule",
                              headers=AH, json={"video_provider": "dorismon"})
            check("El editor de series pasa el grupo a Video Dorismon",
                  r.status_code == 200)

            ses = (await sesiones_serie()).json()
            items = ses.get("items", ses) if isinstance(ses, dict) else ses
            fut = []
            for x in items:
                if x.get("series_id") != serie:
                    continue
                t = datetime.datetime.fromisoformat(
                    x["starts_at_utc"].replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=datetime.timezone.utc)
                if t > now:
                    fut.append(x)
            check("Las clases futuras de la serie quedan en 'dorismon'",
                  bool(fut) and all(x.get("video_provider") == "dorismon"
                                    for x in fut))
            check("Se puede entrar a todas ellas", all(puede_entrar(x) for x in fut))

            cal4 = (await c.get("/student/calendar", headers=SH)).json()
            ev4 = cal4.get("events", cal4) if isinstance(cal4, dict) else cal4
            ids_fut = {x["id"] for x in fut}
            en_cal = [e for e in ev4 if e.get("id") in ids_fut]
            check("CALENDARIO: el cambio de la serie se refleja",
                  bool(en_cal) and all(e.get("video_provider") == "dorismon"
                                       for e in en_cal))
            check("CALENDARIO: se puede entrar tras la migración",
                  bool(en_cal) and all(puede_entrar(e) for e in en_cal))

            if TH:
                td2 = (await c.get("/teacher/dashboard", headers=TH)).json()
                tt = []
                v2 = td2.get("today_schedule")
                if isinstance(v2, list):
                    tt.extend(v2)
                mt2 = [x for x in tt if x.get("id") in ids_fut]
                if mt2:
                    check("PROFESOR: ve el cambio de la serie a 'dorismon'",
                          all(x.get("video_provider") == "dorismon" for x in mt2))
                    check("PROFESOR: puede entrar tras la migración",
                          all(puede_entrar(x) for x in mt2))

            # Limpieza
            await c.delete(f"/admin/class-series/{serie}?future_only=false",
                           headers=AH)

        for sid in creadas:
            if sid:
                await c.delete(f"/admin/sessions/{sid}", headers=AH)

    print(f"\n{passed}/{total}")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
