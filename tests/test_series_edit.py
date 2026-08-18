"""V3.9.62 — Editar una serie recurrente sin destruir el grupo.

EL CASO REAL: una serie con un Google Meet viejo que dejó de funcionar. Hasta
ahora, "reprogramar" BORRABA las clases futuras y las recreaba, así que
arreglar un enlace costaba los IDs de sesión — y con ellos la asistencia, las
entregas y las grabaciones que cuelgan de esos IDs.

LO QUE SE VERIFICA AQUÍ:
  · cambiar solo el video NO borra ni una clase (mismos IDs)
  · cambiar la hora sin cambiar los días tampoco borra
  · cambiar los DÍAS sí regenera, pero conserva módulo, profesor programado,
    video y demás campos
  · las clases pasadas nunca se tocan
  · el aviso llega al grupo real y NO al otro grupo del mismo nivel
  · las validaciones no dejan la serie a medio cambiar
"""
import sys
import asyncio
import datetime
import httpx
from zoneinfo import ZoneInfo

RD = ZoneInfo("America/Santo_Domingo")

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")

MEET_VIEJO = "https://meet.google.com/aaa-bbbb-ccc"
MEET_NUEVO = "https://meet.google.com/zzz-yyyy-xxx"
ZOOM = "https://us02web.zoom.us/j/123456789"


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

        profes = (await c.get("/admin/users?role=teacher", headers=AH)).json()["items"]
        seed = [p for p in profes if p["email"] in SEED_TEACHERS]
        profe_a, profe_b = seed[0], seed[1]

        hoy = datetime.date.today()
        # Empezar hace 2 semanas para tener clases PASADAS de verdad
        inicio = (hoy - datetime.timedelta(days=14)).isoformat()

        # ── Serie de prueba: Google Meet viejo ──────────────────────────
        r = await c.post("/admin/class-series", headers=AH, json={
            "name": "EDIT Grupo Mañana", "course_id": cid, "level_id": nivel["id"],
            "teacher_id": profe_a["id"], "days_of_week": "mon,tue,wed,thu,fri",
            "start_time_hhmm": "08:00", "duration_min": 60,
            "start_date": inicio, "num_classes": 20, "modality": "online",
            "meeting_url": MEET_VIEJO, "video_provider": "meet",
        })
        check("Se crea la serie de prueba", r.status_code == 201)
        if r.status_code != 201:
            print(f"\n{passed}/{total}")
            return passed == total
        serie_id = r.json()["series_id"]

        # Un SEGUNDO grupo del MISMO nivel: el que nunca debe enterarse
        r2 = await c.post("/admin/class-series", headers=AH, json={
            "name": "EDIT Grupo Noche", "course_id": cid, "level_id": nivel["id"],
            "teacher_id": profe_a["id"], "days_of_week": "mon,tue,wed,thu,fri",
            "start_time_hhmm": "20:00", "duration_min": 60,
            "start_date": inicio, "num_classes": 20, "modality": "online",
            "meeting_url": MEET_VIEJO, "video_provider": "meet",
        })
        serie_noche = r2.json().get("series_id") if r2.status_code == 201 else None

        async def sesiones(sid):
            """Todas las clases de una serie, pasadas y futuras.

            /admin/sessions no filtra por serie y por defecto solo devuelve
            las próximas, con tope de 200 por página. Hay que pedir
            explícitamente filter_period=all y paginar, o las clases pasadas
            —justo las que este test debe comprobar que no se tocan— no
            aparecerían nunca.
            """
            out, page = [], 1
            while page <= 20:
                rr = await c.get(
                    f"/admin/sessions?filter_period=all&limit=200&page={page}",
                    headers=AH)
                if rr.status_code != 200:
                    break
                data = rr.json()
                items = data.get("items", data) if isinstance(data, dict) else data
                if not items:
                    break
                out.extend(s for s in items if s.get("series_id") == sid)
                if len(items) < 200:
                    break
                page += 1
            return out

        todas = await sesiones(serie_id)

        check("La serie generó clases", len(todas) > 0)
        ahora = datetime.datetime.now(datetime.timezone.utc)

        def cuando(s):
            """El backend serializa en UTC; SQLite lo devuelve sin sufijo."""
            t = datetime.datetime.fromisoformat(
                s["starts_at_utc"].replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=datetime.timezone.utc)
            return t

        def parte(lista):
            fut, pas = [], []
            for s in lista:
                (fut if cuando(s) > ahora else pas).append(s)
            return fut, pas

        fut0, pas0 = parte(todas)
        check("Hay clases pasadas y futuras", len(pas0) > 0 and len(fut0) > 0)
        ids_fut0 = {s["id"] for s in fut0}
        ids_pas0 = {s["id"] for s in pas0}
        horas_pas0 = {s["id"]: s["starts_at_utc"] for s in pas0}

        # ═══ CASO 1 — SOLO EL ENLACE ═══════════════════════════════════
        # La regla crítica: no se borra ni se regenera NADA.
        r = await c.patch(f"/admin/class-series/{serie_id}/reschedule",
                          headers=AH, json={"meeting_url": MEET_NUEVO})
        check("Cambiar solo el enlace responde OK", r.status_code == 200)
        j = r.json() if r.status_code == 200 else {}
        check("Modo 'en_sitio' (no regeneró)", j.get("modo") == "en_sitio")
        check("Cero clases regeneradas", j.get("regenerated_classes") == 0)

        todas1 = await sesiones(serie_id)
        fut1, pas1 = parte(todas1)
        ids_fut1 = {s["id"] for s in fut1}
        check("Los IDs de las clases futuras se CONSERVAN", ids_fut1 == ids_fut0)
        check("Las clases pasadas siguen ahí", {s["id"] for s in pas1} == ids_pas0)
        check("Las clases pasadas no cambiaron de hora",
              all(horas_pas0[s["id"]] == s["starts_at_utc"] for s in pas1))
        check("Las clases futuras tienen el enlace NUEVO",
              all(s.get("meeting_url") == MEET_NUEVO for s in fut1))
        check("Las clases PASADAS conservan el enlace viejo",
              all(s.get("meeting_url") == MEET_VIEJO for s in pas1))

        series_list = (await c.get("/admin/class-series", headers=AH)).json()
        mia = [s for s in series_list if s["id"] == serie_id]
        check("La serie devuelve meeting_url para precargar el modal",
              bool(mia) and mia[0].get("meeting_url") == MEET_NUEVO)
        check("La serie devuelve video_provider",
              bool(mia) and mia[0].get("video_provider") == "meet")

        # ═══ CASO 2 — CAMBIAR A ZOOM ═══════════════════════════════════
        r = await c.patch(f"/admin/class-series/{serie_id}/reschedule",
                          headers=AH, json={"meeting_url": ZOOM})
        check("Se puede pasar de Meet a Zoom", r.status_code == 200)
        fut2, _ = parte(await sesiones(serie_id))
        check("Zoom quedó en las clases futuras",
              all(s.get("meeting_url") == ZOOM for s in fut2))
        check("Los IDs siguen intactos tras cambiar a Zoom",
              {s["id"] for s in fut2} == ids_fut0)

        # ═══ CASO 3 — VIDEO DORISMON CON RESPALDO ══════════════════════
        r = await c.patch(f"/admin/class-series/{serie_id}/reschedule",
                          headers=AH, json={"video_provider": "dorismon"})
        check("Se puede pasar a Video Dorismon", r.status_code == 200)
        fut3, _ = parte(await sesiones(serie_id))
        check("Las clases futuras usan el video propio",
              all(s.get("video_provider") == "dorismon" for s in fut3))
        check("El enlace externo se conserva como RESPALDO",
              all(s.get("meeting_url") == ZOOM for s in fut3))
        check("Sigue sin borrarse ninguna clase",
              {s["id"] for s in fut3} == ids_fut0)

        # ═══ CASO 4 — SOLO LA HORA (mismos días) ═══════════════════════
        r = await c.patch(f"/admin/class-series/{serie_id}/reschedule",
                          headers=AH, json={"start_time_hhmm": "09:30"})
        check("Cambiar solo la hora responde OK", r.status_code == 200)
        j4 = r.json() if r.status_code == 200 else {}
        check("Cambiar la hora NO regenera", j4.get("modo") == "en_sitio")
        fut4, pas4 = parte(await sesiones(serie_id))
        check("Los IDs sobreviven al cambio de hora",
              {s["id"] for s in fut4} == ids_fut0)
        # Las movidas deben estar a las 09:30 hora RD (13:30 UTC)
        movidas = [s for s in fut4
                   if cuando(s).hour == 13 and cuando(s).minute == 30]
        check("Las clases futuras se movieron a la hora nueva",
              len(movidas) >= len(fut4) - 1)
        check("Las clases pasadas NO se movieron",
              all(horas_pas0[s["id"]] == s["starts_at_utc"] for s in pas4))

        # ═══ CASO 5 — VALIDACIONES ═════════════════════════════════════
        r = await c.patch(f"/admin/class-series/{serie_id}/reschedule",
                          headers=AH, json={"meeting_url": "no-es-un-link"})
        check("Rechaza un enlace que no es https", r.status_code == 400)

        r = await c.patch(f"/admin/class-series/{serie_id}/reschedule",
                          headers=AH, json={"days_of_week": "lunes,martes"})
        check("Rechaza días inválidos", r.status_code == 400)

        r = await c.patch(f"/admin/class-series/{serie_id}/reschedule",
                          headers=AH, json={"start_time_hhmm": "99:99"})
        check("Rechaza una hora imposible", r.status_code == 400)

        r = await c.patch(f"/admin/class-series/{serie_id}/reschedule",
                          headers=AH, json={"video_provider": "meet",
                                            "meeting_url": ""})
        check("Enlace externo SIN enlace se rechaza", r.status_code == 400)

        fut5, _ = parte(await sesiones(serie_id))
        check("Tras los rechazos, la serie quedó intacta",
              {s["id"] for s in fut5} == ids_fut0)
        check("Tras los rechazos, el video no cambió",
              all(s.get("video_provider") == "dorismon" for s in fut5))

        r = await c.patch(f"/admin/class-series/{serie_id}/reschedule",
                          headers=AH, json={})
        check("Un cuerpo vacío se rechaza", r.status_code == 400)

        # ═══ CASO 6 — CAMBIAR LOS DÍAS SÍ REGENERA, PERO CONSERVA ══════
        antes = sorted(fut5, key=lambda s: s["starts_at_utc"])
        modulos_antes = [s.get("module_id") for s in antes]

        r = await c.patch(f"/admin/class-series/{serie_id}/reschedule",
                          headers=AH, json={"days_of_week": "sat"})
        check("Cambiar los días responde OK", r.status_code == 200)
        j6 = r.json() if r.status_code == 200 else {}
        check("Cambiar los días SÍ regenera", j6.get("modo") == "regenerada")

        todas6 = await sesiones(serie_id)
        fut6, pas6 = parte(todas6)
        check("Se regeneró la misma cantidad de clases", len(fut6) == len(antes))
        check("Las clases pasadas siguen intactas tras regenerar",
              {s["id"] for s in pas6} == ids_pas0
              and all(horas_pas0[s["id"]] == s["starts_at_utc"] for s in pas6))
        check("Todas las clases nuevas caen en sábado",
              all(cuando(s).astimezone(RD).weekday() == 5 for s in fut6))
        check("La regeneración CONSERVÓ el proveedor de video",
              all(s.get("video_provider") == "dorismon" for s in fut6))
        check("La regeneración CONSERVÓ el enlace de respaldo",
              all(s.get("meeting_url") == ZOOM for s in fut6))
        nuevos = sorted(fut6, key=lambda s: s["starts_at_utc"])
        check("La regeneración CONSERVÓ la rotación de módulos",
              [s.get("module_id") for s in nuevos] == modulos_antes)

        # ═══ CASO 7 — CAMBIO DE PROFESOR DELEGADO ══════════════════════
        r = await c.patch(f"/admin/class-series/{serie_id}/reschedule",
                          headers=AH, json={"teacher_id": profe_b["id"],
                                            "confirm_overlap": True})
        check("Se puede cambiar el profesor desde editar serie",
              r.status_code == 200)
        fut7, pas7 = parte(await sesiones(serie_id))
        check("Las clases futuras pasaron al profesor nuevo",
              all(s.get("teacher_id") == profe_b["id"] for s in fut7))
        check("Las clases PASADAS conservan su profesor original",
              all(s.get("teacher_id") == profe_a["id"] for s in pas7))
        sl = (await c.get("/admin/class-series", headers=AH)).json()
        m7 = [s for s in sl if s["id"] == serie_id]
        check("La serie quedó con el profesor nuevo",
              bool(m7) and m7[0].get("teacher_id") == profe_b["id"])

        # El endpoint clásico sigue funcionando igual (no se duplicó lógica)
        r = await c.post(f"/admin/class-series/{serie_id}/change-teacher",
                         headers=AH, json={"teacher_id": profe_a["id"],
                                           "confirm_overlap": True})
        check("El endpoint change-teacher sigue funcionando", r.status_code == 200)
        r = await c.post(f"/admin/class-series/{serie_id}/change-teacher",
                         headers=AH, json={"teacher_id": profe_a["id"]})
        check("change-teacher rechaza asignar al mismo profesor",
              r.status_code == 400)

        # ═══ CASO 8 — EL OTRO GRUPO NO SE ENTERA ═══════════════════════
        if serie_noche:
            noche = await sesiones(serie_noche)
            futn, _ = parte(noche)
            check("El otro grupo del mismo nivel conserva SU enlace",
                  all(s.get("meeting_url") == MEET_VIEJO for s in futn))
            check("El otro grupo conserva SU horario",
                  all(cuando(s).astimezone(RD).hour == 20 for s in futn))
            check("El otro grupo conserva SU profesor",
                  all(s.get("teacher_id") == profe_a["id"] for s in futn))

        # ═══ CASO 9 — SERIE INEXISTENTE ════════════════════════════════
        r = await c.patch("/admin/class-series/no-existe-xyz/reschedule",
                          headers=AH, json={"meeting_url": MEET_NUEVO})
        check("Serie inexistente devuelve 404", r.status_code == 404)

        # ═══ CASO 10 — PASADO 'meet' + FUTURO 'dorismon' ═══════════════
        #
        # V3.9.63. El escenario que obliga a DERIVAR el proveedor en vez de
        # guardarlo en la serie: un grupo que empezó por Google Meet y se
        # pasó al Video Dorismon. Sus clases pasadas siguen siendo 'meet'
        # (así ocurrieron) y las futuras son 'dorismon'.
        #
        # El editor debe representar la configuración FUTURA vigente, y
        # abrirlo y guardar NO puede convertir esas futuras de vuelta a
        # 'meet' por el simple hecho de tocar otra cosa.
        r = await c.post("/admin/class-series", headers=AH, json={
            "name": "EDIT Grupo Mixto", "course_id": cid, "level_id": nivel["id"],
            "teacher_id": profe_a["id"], "days_of_week": "mon,tue,wed,thu,fri",
            "start_time_hhmm": "14:00", "duration_min": 60,
            "start_date": inicio, "num_classes": 20, "modality": "online",
            "meeting_url": MEET_VIEJO, "video_provider": "meet",
        })
        check("Se crea el grupo mixto", r.status_code == 201)
        mixto_id = r.json()["series_id"] if r.status_code == 201 else None

        if mixto_id:
            todasm = await sesiones(mixto_id)
            futm0, pasm0 = parte(todasm)
            check("El grupo mixto tiene pasadas y futuras",
                  len(pasm0) > 0 and len(futm0) > 0)

            # Solo las FUTURAS pasan a Video Dorismon. Las pasadas se quedan
            # en 'meet': es como se dieron de verdad.
            r = await c.patch(f"/admin/class-series/{mixto_id}/reschedule",
                              headers=AH, json={"video_provider": "dorismon"})
            check("Se pasa el futuro a Video Dorismon", r.status_code == 200)

            todasm1 = await sesiones(mixto_id)
            futm1, pasm1 = parte(todasm1)
            check("Las PASADAS conservan video_provider='meet'",
                  all(s.get("video_provider") == "meet" for s in pasm1))
            check("Las FUTURAS quedan en video_provider='dorismon'",
                  all(s.get("video_provider") == "dorismon" for s in futm1))

            # LO QUE EXIGE EL EDITOR: el listado debe decir 'dorismon',
            # porque es lo que el estudiante se va a encontrar.
            sl = (await c.get("/admin/class-series", headers=AH)).json()
            mx = [s for s in sl if s["id"] == mixto_id]
            check("GET /admin/class-series deriva 'dorismon' (config futura)",
                  bool(mx) and mx[0].get("video_provider") == "dorismon")

            # Estado ANTES de abrir/guardar el editor, para comparar
            ids_m = {s["id"] for s in futm1}
            mods_m = {s["id"]: s.get("module_id") for s in futm1}
            sched_m = {s["id"]: s.get("scheduled_teacher_id") for s in futm1}
            pas_m = {s["id"]: (s["starts_at_utc"], s.get("video_provider"),
                               s.get("meeting_url"), s.get("teacher_id"))
                     for s in pasm1}

            # ABRIR Y GUARDAR el editor tocando SOLO el enlace de respaldo.
            # Esto es lo que hace el frontend: manda únicamente lo que cambió.
            r = await c.patch(f"/admin/class-series/{mixto_id}/reschedule",
                              headers=AH, json={"meeting_url": ZOOM})
            check("Guardar solo el enlace responde OK", r.status_code == 200)
            check("Guardar solo el enlace NO regenera",
                  (r.json() if r.status_code == 200 else {}).get("modo") == "en_sitio")

            todasm2 = await sesiones(mixto_id)
            futm2, pasm2 = parte(todasm2)
            check("Las FUTURAS SIGUEN en 'dorismon' (no se convirtieron)",
                  all(s.get("video_provider") == "dorismon" for s in futm2))
            check("Las futuras tomaron el enlace de respaldo nuevo",
                  all(s.get("meeting_url") == ZOOM for s in futm2))
            check("Las PASADAS siguen intactas por completo",
                  all(pas_m[s["id"]] == (s["starts_at_utc"], s.get("video_provider"),
                                         s.get("meeting_url"), s.get("teacher_id"))
                      for s in pasm2))
            check("Los IDs futuros no cambiaron",
                  {s["id"] for s in futm2} == ids_m)
            check("Los módulos no cambiaron",
                  all(mods_m[s["id"]] == s.get("module_id") for s in futm2))
            check("scheduled_teacher_id no cambió",
                  all(sched_m[s["id"]] == s.get("scheduled_teacher_id") for s in futm2))

            sl2 = (await c.get("/admin/class-series", headers=AH)).json()
            mx2 = [s for s in sl2 if s["id"] == mixto_id]
            check("Tras guardar, el listado sigue derivando 'dorismon'",
                  bool(mx2) and mx2[0].get("video_provider") == "dorismon")

            # Sin clases futuras, se cae a la más reciente (regla 6)
            await c.delete(f"/admin/class-series/{mixto_id}?future_only=true",
                           headers=AH)
            sl3 = (await c.get("/admin/class-series", headers=AH)).json()
            mx3 = [s for s in sl3 if s["id"] == mixto_id]
            if mx3:
                check("Sin futuras, deriva de la clase más reciente ('meet')",
                      mx3[0].get("video_provider") == "meet")
            else:
                # La serie queda inactiva y sale del listado: también correcto
                check("Sin futuras, la serie sale del listado de activos", True)

        # ── Limpieza: no dejar basura de test en la base ───────────────
        for sid in (serie_id, serie_noche, mixto_id):
            if sid:
                await c.delete(f"/admin/class-series/{sid}?future_only=false",
                               headers=AH)

    print(f"\n{passed}/{total}")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
