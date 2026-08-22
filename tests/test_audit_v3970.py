"""V3.9.70 — Cierre definitivo pre-producción.

Comprueba las reglas que deben ser idénticas al CREAR y EDITAR:
  * online/híbrida + externo -> https obligatorio
  * Dorismon -> fallback opcional
  * presencial/híbrida -> sede obligatoria
  * aula pertenece a sede
  * cambiar sede sin aula limpia el aula anterior
  * series no son borradores: externo sin link se rechaza al crear
"""
import sys
import asyncio
import datetime
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")
HTTPS = "https://meet.google.com/v3970-valid"


async def main():
    passed = total = 0

    def check(label, ok):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
        print(f"  {'✓' if ok else '✗'} {label}")

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        login = await c.post("/auth/login", json=ADMIN)
        if login.status_code != 200:
            print("No se pudo iniciar sesión de admin")
            return False
        AH = {"Authorization": f"Bearer {login.json()['access_token']}"}

        cid = (await c.get("/admin/courses", headers=AH)).json()[0]["id"]
        lv = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        niveles = lv.get("items", lv) if isinstance(lv, dict) else lv
        nivel = niveles[0]
        profes = (await c.get("/admin/users?role=teacher", headers=AH)).json().get("items", [])
        profe = next((p for p in profes if p.get("email") in SEED_TEACHERS), profes[0])

        sedes = (await c.get("/admin/branches", headers=AH)).json()
        sedes = sedes.get("items", sedes) if isinstance(sedes, dict) else sedes
        check("Hay al menos una sede para validar presencial", bool(sedes))
        if not sedes:
            print(f"\n{passed}/{total}")
            return False
        sede1 = sedes[0]
        sede2 = sedes[1] if len(sedes) > 1 else None

        async def aulas(sede):
            r = (await c.get(f"/admin/classrooms?branch_id={sede['id']}", headers=AH)).json()
            return r.get("items", r) if isinstance(r, dict) else r

        aulas1 = await aulas(sede1)
        aulas2 = await aulas(sede2) if sede2 else []

        now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=6)
        base_session = {
            "teacher_id": profe["id"], "course_id": cid, "level_id": nivel["id"],
            "title": "V3970 validación", "starts_at_utc": now.isoformat(),
            "ends_at_utc": (now + datetime.timedelta(hours=1)).isoformat(),
        }
        sesiones = []
        series = []

        # ── CREAR SESIÓN: misma regla que PATCH ────────────────────────
        r = await c.post("/admin/sessions", headers=AH, json={
            **base_session, "modality": "online", "video_provider": "meet",
        })
        check("POST sesión online externa SIN link -> 400", r.status_code == 400)

        r = await c.post("/admin/sessions", headers=AH, json={
            **base_session, "title": "V3970 http", "modality": "online",
            "video_provider": "meet", "meeting_url": "http://inseguro.test",
        })
        check("POST sesión online externa con http:// -> 400", r.status_code == 400)

        r = await c.post("/admin/sessions", headers=AH, json={
            **base_session, "title": "V3970 Dorismon", "modality": "online",
            "video_provider": "dorismon",
        })
        check("POST online + Dorismon SIN fallback -> 201", r.status_code == 201)
        if r.status_code == 201:
            sesiones.append(r.json()["id"])

        r = await c.post("/admin/sessions", headers=AH, json={
            **base_session, "title": "V3970 presencial sin sede", "modality": "presencial",
        })
        check("POST presencial SIN sede -> 400", r.status_code == 400)

        r = await c.post("/admin/sessions", headers=AH, json={
            **base_session, "title": "V3970 presencial", "modality": "presencial",
            "branch_id": str(sede1["id"]),
            **({"classroom_id": str(aulas1[0]["id"])} if aulas1 else {}),
        })
        check("POST presencial CON sede -> 201", r.status_code == 201)
        pres_id = r.json().get("id") if r.status_code == 201 else None
        if pres_id:
            sesiones.append(pres_id)

        r = await c.post("/admin/sessions", headers=AH, json={
            **base_session, "title": "V3970 híbrida Dorismon", "modality": "hibrida",
            "video_provider": "dorismon", "branch_id": sede1["id"],
        })
        check("POST híbrida + Dorismon + sede SIN fallback -> 201", r.status_code == 201)
        if r.status_code == 201:
            sesiones.append(r.json()["id"])

        if aulas1 and aulas2 and sede2:
            r = await c.post("/admin/sessions", headers=AH, json={
                **base_session, "title": "V3970 aula cruzada", "modality": "presencial",
                "branch_id": sede1["id"], "classroom_id": aulas2[0]["id"],
            })
            check("POST aula de otra sede -> 400", r.status_code == 400)

            # La sesión existente parte con sede1/aula1. Cambiar SOLO sede debe
            # limpiar el aula vieja, no rechazar el cambio ni conservarla.
            if pres_id:
                r = await c.patch(f"/admin/sessions/{pres_id}", headers=AH,
                                  json={"branch_id": sede2["id"]})
                check("PATCH cambiar sede sin aula -> 200", r.status_code == 200)
                lista = (await c.get("/admin/sessions", headers=AH)).json()
                items = lista.get("items", lista) if isinstance(lista, dict) else lista
                fila = next((x for x in items if x.get("id") == pres_id), None)
                check("Al cambiar sede se limpia el aula incompatible",
                      bool(fila) and fila.get("branch_id") == sede2["id"] and not fila.get("classroom_id"))

        # ── CREAR SERIE: no es borrador ────────────────────────────────
        hoy = datetime.date.today().isoformat()
        base_series = {
            "course_id": cid, "level_id": nivel["id"], "teacher_id": profe["id"],
            "days_of_week": "mon,tue,wed,thu,fri", "start_time_hhmm": "16:00",
            "duration_min": 60, "start_date": hoy, "num_classes": 3,
        }

        r = await c.post("/admin/class-series", headers=AH, json={
            **base_series, "name": "V3970 externa sin link", "modality": "online",
            "video_provider": "meet",
        })
        check("POST serie online externa SIN link -> 400", r.status_code == 400)

        r = await c.post("/admin/class-series", headers=AH, json={
            **base_series, "name": "V3970 externa http", "modality": "online",
            "video_provider": "meet", "meeting_url": "http://inseguro.test",
        })
        check("POST serie online externa con http:// -> 400", r.status_code == 400)

        r = await c.post("/admin/class-series", headers=AH, json={
            **base_series, "name": "V3970 externa válida", "modality": "online",
            "video_provider": "meet", "meeting_url": HTTPS,
        })
        check("POST serie online externa con https -> 201", r.status_code == 201)
        if r.status_code == 201:
            series.append(r.json()["series_id"])

        r = await c.post("/admin/class-series", headers=AH, json={
            **base_series, "name": "V3970 Dorismon", "modality": "online",
            "video_provider": "dorismon",
        })
        check("POST serie Online + Dorismon SIN fallback -> 201", r.status_code == 201)
        if r.status_code == 201:
            series.append(r.json()["series_id"])

        r = await c.post("/admin/class-series", headers=AH, json={
            **base_series, "name": "V3970 presencial", "modality": "presencial",
            "branch_id": str(sede1["id"]),
        })
        check("POST serie presencial con sede, sin video -> 201", r.status_code == 201)
        if r.status_code == 201:
            sid = r.json()["series_id"]
            series.append(sid)
            lista = (await c.get("/admin/class-series", headers=AH)).json()
            fila = next((x for x in lista if x.get("id") == sid), None)
            check("Serie persiste branch_id normalizado",
                  bool(fila) and fila.get("branch_id") == sede1["id"])

        r = await c.post("/admin/class-series", headers=AH, json={
            **base_series, "name": "V3970 híbrida Dorismon", "modality": "hibrida",
            "video_provider": "dorismon", "branch_id": sede1["id"],
        })
        check("POST serie híbrida + Dorismon + sede sin fallback -> 201", r.status_code == 201)
        if r.status_code == 201:
            series.append(r.json()["series_id"])

        # Limpieza
        for sid in sesiones:
            await c.delete(f"/admin/sessions/{sid}", headers=AH)
        for gid in series:
            await c.delete(f"/admin/class-series/{gid}?future_only=false", headers=AH)

    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
