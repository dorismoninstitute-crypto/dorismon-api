"""V3.9.65 — Excepciones de modalidad y destinatarios explícitos.

DOS PROBLEMAS REALES:

1. Dirección quería que UN miércoles fuera presencial y terminaba con todos
   los miércoles futuros presenciales, porque el único camino visible era el
   editor de la SERIE, que por diseño aplica a todo.

2. Una clase de refuerzo creada para un alumno nuevo (todavía sin grupo) le
   quedaba invisible: el backend le habría dejado entrar, pero el dashboard y
   el calendario se cortaban antes si no tenía Enrollment.

Y un tercero que salió auditando: una clase creada para María y Pedro se
anunciaba a TODO el nivel.
"""
import sys
import asyncio
import datetime
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")
MEET = "https://meet.google.com/mod-alid-add"


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
        # V3.9.69 — Una clase presencial ahora EXIGE sede (regla nueva): sin
        # ella el estudiante no sabría a dónde ir. Estos tests la aportan.
        _sedes = (await c.get("/admin/branches", headers=AH)).json()
        _sedes = _sedes.get("items", _sedes) if isinstance(_sedes, dict) else _sedes
        SEDE = _sedes[0]["id"]

        now = datetime.datetime.now(datetime.timezone.utc)

        async def sesiones(sid):
            out, page = [], 1
            while page <= 20:
                rr = await c.get(f"/admin/sessions?filter_period=all&limit=200&page={page}",
                                 headers=AH)
                if rr.status_code != 200:
                    break
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

        # ═══ A. MODALIDAD POR SESIÓN ═══════════════════════════════════
        hace2sem = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
        r = await c.post("/admin/class-series", headers=AH, json={
            "name": "MOD Serie Virtual", "course_id": cid, "level_id": nivel["id"],
            "teacher_id": profe["id"], "days_of_week": "mon,tue,wed,thu,fri",
            "start_time_hhmm": "11:00", "duration_min": 60,
            "start_date": hace2sem, "num_classes": 20, "modality": "online",
            "meeting_url": MEET, "video_provider": "meet",
        })
        check("Se crea una serie Virtual", r.status_code == 201)
        serie = r.json()["series_id"] if r.status_code == 201 else None
        if not serie:
            print(f"\n{passed}/{total}")
            return passed == total

        todas = await sesiones(serie)
        futuras = sorted([x for x in todas if cuando(x) > now], key=lambda x: x["starts_at_utc"])
        pasadas = [x for x in todas if cuando(x) <= now]
        check("La serie tiene pasadas y futuras", len(pasadas) > 0 and len(futuras) >= 3)

        lunes, miercoles, viernes = futuras[0], futuras[1], futuras[2]
        ids_todas = {x["id"] for x in futuras}

        # --- SOLO ESTA CLASE ---
        r = await c.patch(f"/admin/sessions/{miercoles['id']}", headers=AH,
                          json={"modality": "presencial", "branch_id": SEDE, "apply_to": "this"})
        check("Cambiar UNA sesión a presencial responde OK", r.status_code == 200)
        check("No propagó a ninguna otra",
              (r.json() if r.status_code == 200 else {}).get("following_updated") == 0)

        t2 = await sesiones(serie)
        d2 = {x["id"]: x for x in t2}
        check("El miércoles quedó PRESENCIAL",
              d2[miercoles["id"]]["modality"] == "presencial")
        check("El lunes sigue ONLINE", d2[lunes["id"]]["modality"] == "online")
        check("El viernes sigue ONLINE — no se alteró",
              d2[viernes["id"]]["modality"] == "online")
        check("Las demás futuras siguen online",
              all(d2[i]["modality"] == "online" for i in ids_todas
                  if i != miercoles["id"]))

        # La excepción presencial CONSERVA la config virtual heredada
        check("La excepción presencial NO destruyó meeting_url",
              d2[miercoles["id"]].get("meeting_url") == MEET)
        check("La excepción presencial NO destruyó video_provider",
              d2[miercoles["id"]].get("video_provider") == "meet")

        # Volver a virtual recupera la videollamada
        r = await c.patch(f"/admin/sessions/{miercoles['id']}", headers=AH,
                          json={"modality": "online", "apply_to": "this"})
        t3 = {x["id"]: x for x in await sesiones(serie)}
        check("Volver a virtual recupera la videollamada",
              t3[miercoles["id"]]["modality"] == "online"
              and t3[miercoles["id"]].get("meeting_url") == MEET)

        # --- ESTA Y LAS SIGUIENTES ---
        r = await c.patch(f"/admin/sessions/{miercoles['id']}", headers=AH,
                          json={"modality": "presencial", "branch_id": SEDE,
                                    "apply_to": "this_and_following"})
        check("'Esta y las siguientes' responde OK", r.status_code == 200)
        n_sig = (r.json() if r.status_code == 200 else {}).get("following_updated", 0)
        check("Reporta cuántas siguientes cambió", n_sig > 0)

        t4 = {x["id"]: x for x in await sesiones(serie)}
        check("El lunes ANTERIOR sigue online — no se tocó hacia atrás",
              t4[lunes["id"]]["modality"] == "online")
        check("El miércoles quedó presencial", t4[miercoles["id"]]["modality"] == "presencial")
        check("El viernes POSTERIOR también quedó presencial",
              t4[viernes["id"]]["modality"] == "presencial")
        check("Ninguna sesión PASADA cambió de modalidad",
              all(t4[x["id"]]["modality"] == "online" for x in pasadas))

        # --- TODA LA SERIE (endpoint existente, no duplicado) ---
        r = await c.patch(f"/admin/class-series/{serie}/reschedule", headers=AH,
                          json={"modality": "online"})
        check("'Toda la serie' usa el endpoint de serie", r.status_code == 200)
        t5 = {x["id"]: x for x in await sesiones(serie)}
        check("Toda la serie vuelve a online",
              all(t5[i]["modality"] == "online" for i in ids_todas))
        check("Las PASADAS siguen intactas tras el cambio de serie",
              all(t5[x["id"]]["modality"] == "online" for x in pasadas))
        check("No se borró ninguna sesión (IDs intactos)",
              ids_todas <= set(t5.keys()))

        # apply_to='all' se rechaza aquí a propósito
        r = await c.patch(f"/admin/sessions/{miercoles['id']}", headers=AH,
                          json={"modality": "presencial", "apply_to": "all"})
        check("apply_to='all' en la sesión se rechaza (usa el de serie)",
              r.status_code == 400)

        r = await c.patch(f"/admin/sessions/{miercoles['id']}", headers=AH,
                          json={"modality": "presencial", "apply_to": "inventado"})
        check("apply_to inválido se rechaza", r.status_code == 400)

        # ═══ SERIE PRESENCIAL → UNA VIRTUAL ════════════════════════════
        r = await c.post("/admin/class-series", headers=AH, json={
            "name": "MOD Serie Presencial", "course_id": cid, "level_id": nivel["id"],
            "teacher_id": profe["id"], "days_of_week": "mon,tue,wed,thu,fri",
            "start_time_hhmm": "16:00", "duration_min": 60,
            "start_date": datetime.date.today().isoformat(), "num_classes": 10,
            "modality": "presencial", "branch_id": SEDE,
        })
        serie2 = r.json().get("series_id") if r.status_code == 201 else None
        check("Se crea una serie Presencial", bool(serie2))
        if serie2:
            f2 = sorted([x for x in await sesiones(serie2) if cuando(x) > now],
                        key=lambda x: x["starts_at_utc"])
            if len(f2) >= 2:
                await c.patch(f"/admin/sessions/{f2[0]['id']}", headers=AH,
                              json={"modality": "online", "meeting_url": MEET,
                                    "apply_to": "this"})
                d = {x["id"]: x for x in await sesiones(serie2)}
                check("Una sesión suelta pasa a Virtual",
                      d[f2[0]["id"]]["modality"] == "online")
                check("La siguiente SIGUE presencial",
                      d[f2[1]["id"]]["modality"] == "presencial")
            await c.delete(f"/admin/class-series/{serie2}?future_only=false", headers=AH)

        await c.delete(f"/admin/class-series/{serie}?future_only=false", headers=AH)

    print(f"\n{passed}/{total}")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
