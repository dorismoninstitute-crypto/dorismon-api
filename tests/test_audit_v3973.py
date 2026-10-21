"""V3.9.73 — Cierre de seguridad de alcance en sesiones recurrentes.

Comprueba el punto crítico de ``this_and_following``: una modificación que es
válida para la sesión ancla NO puede dejar futuras sesiones en un estado que
el propio sistema rechazaría si se creara directamente.

Casos:
  * híbrida con sede -> presencial desde aquí; futuras sin sede => 400 y cero
    cambios futuros.
  * externo con link en el ancla -> proveedor externo desde aquí; futuras sin
    link => 400 y cero cambios futuros.
  * si se envían también las dependencias necesarias, la propagación sí pasa.
"""
import asyncio
import datetime
import sys
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")
HTTPS = "https://meet.google.com/v3973-valid"


async def main():
    passed = total = 0

    def check(label, ok):
        nonlocal passed, total
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        login = await c.post("/auth/login", json=ADMIN)
        if login.status_code != 200:
            print("No se pudo iniciar sesión de admin")
            return False
        AH = {"Authorization": f"Bearer {login.json()['access_token']}"}

        courses = (await c.get("/admin/courses", headers=AH)).json()
        cid = courses[0]["id"]
        lv = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        niveles = lv.get("items", lv) if isinstance(lv, dict) else lv
        nivel = niveles[0]
        profes = (await c.get("/admin/users?role=teacher", headers=AH)).json().get("items", [])
        profe = next((p for p in profes if p.get("email") in SEED_TEACHERS), profes[0])
        sedes = (await c.get("/admin/branches", headers=AH)).json()
        sedes = sedes.get("items", sedes) if isinstance(sedes, dict) else sedes
        check("Hay una sede para probar presencial/híbrida", bool(sedes))
        if not sedes:
            print(f"\n{passed}/{total}")
            return False
        sede = sedes[0]

        serie_id = None
        try:
            # Fechas futuras y suficientes días para generar varias sesiones.
            start_date = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
            body = {
                "name": "V3973 propagación segura",
                "course_id": cid,
                "level_id": nivel["id"],
                "teacher_id": profe["id"],
                "days_of_week": "mon,tue,wed,thu,fri,sat,sun",
                "start_time_hhmm": "16:00",
                "duration_min": 60,
                "start_date": start_date,
                "num_classes": 4,
                "modality": "online",
                "video_provider": "dorismon",
            }
            r = await c.post("/admin/class-series", headers=AH, json=body)
            check("Serie base se crea", r.status_code == 201)
            if r.status_code != 201:
                print(r.text)
                return False
            serie_id = r.json()["series_id"]

            async def sesiones_serie():
                rr = await c.get("/admin/sessions?filter_period=all&limit=200", headers=AH)
                data = rr.json()
                items = data.get("items", data) if isinstance(data, dict) else data
                return sorted(
                    [x for x in items if x.get("series_id") == serie_id],
                    key=lambda x: x.get("starts_at_utc") or "",
                )

            ss = await sesiones_serie()
            check("Serie generó al menos 3 sesiones", len(ss) >= 3)
            if len(ss) < 3:
                return False
            anchor, future = ss[0], ss[1]

            # El ancla se convierte SOLO a híbrida con sede. Las futuras siguen
            # online y sin sede.
            r = await c.patch(f"/admin/sessions/{anchor['id']}", headers=AH, json={
                "modality": "hibrida", "video_provider": "dorismon",
                "branch_id": sede["id"],
            })
            check("Ancla híbrida con sede es válida", r.status_code == 200)

            # Ahora cambiar SOLO modalidad a presencial y propagar sería válido
            # para el ancla, pero inválido para las futuras sin sede. Debe
            # rechazarse todo el lote.
            r = await c.patch(f"/admin/sessions/{anchor['id']}", headers=AH, json={
                "modality": "presencial", "apply_to": "this_and_following",
            })
            check("No propaga presencial a futuras sin sede", r.status_code == 400)
            ss2 = await sesiones_serie()
            a2 = next(x for x in ss2 if x["id"] == anchor["id"])
            f2 = next(x for x in ss2 if x["id"] == future["id"])
            check("Fallo es atómico: ancla sigue híbrida", a2.get("modality") == "hibrida")
            check("Fallo es atómico: futura sigue online", f2.get("modality") == "online")

            # Enviando explícitamente la dependencia necesaria, sí debe pasar.
            r = await c.patch(f"/admin/sessions/{anchor['id']}", headers=AH, json={
                "modality": "presencial", "branch_id": sede["id"],
                "apply_to": "this_and_following",
            })
            check("Presencial + sede sí propaga", r.status_code == 200)
            ss3 = await sesiones_serie()
            check("Futuras quedan presenciales", all(x.get("modality") == "presencial" for x in ss3))
            check("Futuras reciben sede", all(x.get("branch_id") == sede["id"] for x in ss3))

            # Volvemos SOLO el ancla a online externo con link. Las futuras se
            # dejan presenciales, pero eso no sirve para probar link. Primero
            # realineamos todas a online Dorismon con el endpoint de serie.
            r = await c.patch(f"/admin/class-series/{serie_id}/reschedule", headers=AH, json={
                "modality": "online", "video_provider": "dorismon",
            })
            check("Serie vuelve a online Dorismon", r.status_code == 200)
            ss4 = await sesiones_serie()
            anchor, future = ss4[0], ss4[1]

            r = await c.patch(f"/admin/sessions/{anchor['id']}", headers=AH, json={
                "video_provider": "meet", "meeting_url": HTTPS,
            })
            check("Ancla externa con link es válida", r.status_code == 200)

            # Propagar únicamente el proveedor: el ancla tiene link heredado,
            # pero las futuras no. Debe rechazarse antes de tocar ninguna.
            r = await c.patch(f"/admin/sessions/{anchor['id']}", headers=AH, json={
                "video_provider": "meet", "apply_to": "this_and_following",
            })
            # Puede no haber delta real en el ancla, pero el backend acepta el
            # body y debe validar prospectivamente las futuras.
            check("No propaga proveedor externo a futuras sin link", r.status_code == 400)
            ss5 = await sesiones_serie()
            f5 = next(x for x in ss5 if x["id"] == future["id"])
            check("Futura conserva Dorismon tras rechazo", f5.get("video_provider") == "dorismon")

            r = await c.patch(f"/admin/sessions/{anchor['id']}", headers=AH, json={
                "video_provider": "meet", "meeting_url": HTTPS,
                "apply_to": "this_and_following",
            })
            check("Proveedor externo + link sí propaga", r.status_code == 200)
            ss6 = await sesiones_serie()
            check("Futuras reciben proveedor externo",
                  all(x.get("video_provider") == "meet" for x in ss6))
            check("Futuras reciben link HTTPS",
                  all(x.get("meeting_url") == HTTPS for x in ss6))

        finally:
            if serie_id:
                await c.delete(f"/admin/class-series/{serie_id}?future_only=false", headers=AH)

    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
