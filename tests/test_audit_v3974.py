"""V3.9.74 — hardening de autenticación, diagnóstico de email y heartbeat.

Se ejecuta contra una instancia local sembrada, igual que el resto de la suite HTTP.
No prueba envío real de email: solo comprueba que los endpoints de diagnóstico
quedaron protegidos.
"""
import asyncio
import datetime
import sys
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        if ok:
            passed += 1
        print(f"  {'✓' if ok else '✗'} {label}")

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        # Login conocidos del seed.
        ar = await c.post("/auth/login", json=ADMIN)
        sr = await c.post("/auth/login", json=STUDENT)
        check("admin puede iniciar sesión", ar.status_code == 200)
        check("estudiante puede iniciar sesión", sr.status_code == 200)
        if ar.status_code != 200 or sr.status_code != 200:
            print(f"\n{passed}/{total} tests pasaron")
            return False

        AH = {"Authorization": f"Bearer {ar.json()['access_token']}"}
        SH = {"Authorization": f"Bearer {sr.json()['access_token']}"}

        # ── Diagnóstico email: ya no es público ───────────────────────────
        r = await c.get("/auth/email-service-status")
        check("email-service-status anónimo -> 401", r.status_code == 401)
        r = await c.get("/auth/email-service-status", headers=SH)
        check("email-service-status estudiante -> 403", r.status_code == 403)
        r = await c.get("/auth/email-service-status", headers=AH)
        check("email-service-status admin -> 200", r.status_code == 200)

        r = await c.post("/auth/test-email", json={"to": "nobody@example.com"})
        check("test-email anónimo -> 401", r.status_code == 401)
        r = await c.post("/auth/test-email", headers=SH, json={"to": "nobody@example.com"})
        check("test-email estudiante -> 403", r.status_code == 403)

        # ── Refresh de cuenta desactivada ─────────────────────────────────
        items = (await c.get("/admin/users?q=maria.estudiante", headers=AH)).json().get("items", [])
        stu = items[0] if items else None
        check("admin encuentra estudiante de prueba", bool(stu))
        if stu:
            refresh_token = sr.json()["refresh_token"]
            try:
                off = await c.patch(f"/admin/users/{stu['id']}", headers=AH, json={"is_active": False})
                check("se desactiva cuenta de prueba", off.status_code == 200)
                rr = await c.post("/auth/refresh", json={"refresh_token": refresh_token})
                check("refresh de cuenta desactivada -> 403", rr.status_code == 403)
            finally:
                await c.patch(f"/admin/users/{stu['id']}", headers=AH, json={"is_active": True})

        # Re-login tras restaurar para usar un access token limpio.
        sr2 = await c.post("/auth/login", json=STUDENT)
        SH2 = {"Authorization": f"Bearer {sr2.json()['access_token']}"} if sr2.status_code == 200 else SH

        # ── Heartbeat no puede fabricar presencia ─────────────────────────
        # Datos académicos del estudiante, para crear una clase suelta válida.
        items = (await c.get("/admin/users?q=maria.estudiante", headers=AH)).json().get("items", [])
        stu = items[0] if items else None
        if stu:
            profile = (await c.get(f"/admin/students/{stu['id']}/profile", headers=AH)).json()
            cid = (await c.get("/admin/courses", headers=AH)).json()[0]["id"]
            levels_raw = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
            levels = levels_raw.get("items", levels_raw) if isinstance(levels_raw, dict) else levels_raw
            level = next((x for x in levels if x.get("code") == profile.get("current_level_code")), levels[0])
            teachers = (await c.get("/admin/users?role=teacher", headers=AH)).json().get("items", [])
            teacher = teachers[0] if teachers else None
            if teacher:
                now = datetime.datetime.now(datetime.timezone.utc)
                body = {
                    "title": "AUD3974 heartbeat",
                    "starts_at_utc": (now + datetime.timedelta(minutes=5)).isoformat(),
                    "ends_at_utc": (now + datetime.timedelta(minutes=65)).isoformat(),
                    "modality": "online",
                    "video_provider": "dorismon",
                    "teacher_id": teacher["id"],
                    "course_id": cid,
                    "level_id": level["id"],
                    "student_ids": [stu["id"]],
                }
                cr = await c.post("/admin/sessions", headers=AH, json=body)
                sid = cr.json().get("id") if cr.status_code in (200, 201) else None
                check("se crea clase de auditoría", bool(sid))
                if sid:
                    try:
                        hb = await c.post(f"/video/sessions/{sid}/heartbeat", headers=SH2)
                        check("heartbeat sin haber entrado -> 409", hb.status_code == 409)
                    finally:
                        await c.delete(f"/admin/sessions/{sid}", headers=AH)

    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
