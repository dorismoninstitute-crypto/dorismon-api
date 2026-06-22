"""V3.0.2 — Tests del ciclo de vida de la clase de prueba.

Cubre: solicitar, agendar (crea sesión + link), aparece en calendario,
detección de no_show/completed cuando pasa, y reagenda.

Correr con: python tests/test_trial_lifecycle.py  (con el server en :PORT)
"""
import asyncio, httpx, sys
from datetime import datetime, timezone, timedelta

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9500"

async def login(c, email, pw):
    r = (await c.post("/auth/login", json={"email": email, "password": pw})).json()
    return r.get("access_token")

async def run():
    results = []
    def check(name, cond):
        results.append((name, cond))
        print(f"  {'✓' if cond else '✗'} {name}")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        admin = await login(c, "admin@dorismon.do", "DorismonAdmin2026!")
        AH = {"Authorization": f"Bearer {admin}"}

        # Estudiante de prueba sin trial
        stu = await login(c, "juana.estudiante@dorismon.do", "Estudiante2026!")
        if not stu:
            stu = await login(c, "carlos.estudiante@dorismon.do", "Estudiante2026!")
        SH = {"Authorization": f"Bearer {stu}"}

        # 1. Solicitar trial
        r = await c.post("/payments/trial-class/request", headers=SH,
                         json={"modality": "online", "preferred_level": "A2"})
        check("solicitar clase de prueba", r.status_code == 201)

        # 2. Dashboard muestra "requested"
        dash = (await c.get("/student/dashboard", headers=SH)).json()
        check("dashboard muestra estado 'requested'",
              dash.get("trial_info", {}).get("status") == "requested")

        # 3. Admin agenda CON link, en el pasado (para simular que ya pasó)
        ana = await login(c, "ana@dorismon.do", "Profe2026!")
        ana_id = (await c.get("/auth/me", headers={"Authorization": f"Bearer {ana}"})).json()["id"]
        trials = (await c.get("/admin/trial-classes", headers=AH)).json()
        tlist = trials if isinstance(trials, list) else trials.get("items", [])
        trial_id = tlist[0]["id"]
        # Agendar en el pasado (hace 2 horas) para simular clase pasada
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        r = await c.post(f"/admin/trial-classes/{trial_id}/schedule", headers=AH,
                         json={"teacher_id": ana_id, "scheduled_at": past,
                               "meeting_url": "https://meet.google.com/test-link"})
        check("admin agenda con link", r.status_code == 200 and r.json().get("session_created"))

        # 4. Como la clase ya pasó y no hubo asistencia → dashboard debe marcar no_show
        dash2 = (await c.get("/student/dashboard", headers=SH)).json()
        ti = dash2.get("trial_info", {})
        check("clase pasada se detecta como no_show", ti.get("status") == "no_show")
        check("puede reagendar (1 vez)", ti.get("can_reschedule") == True)

        # 5. Reagendar
        r = await c.post("/student/trial-class/reschedule", headers=SH)
        check("reagendar funciona", r.status_code == 200)

        # 6. No puede reagendar dos veces
        dash3 = (await c.get("/student/dashboard", headers=SH)).json()
        check("ya no puede reagendar de nuevo",
              dash3.get("trial_info", {}).get("can_reschedule") == False)

    passed = sum(1 for _, c in results if c)
    print(f"\n{passed}/{len(results)} tests pasaron")
    return passed == len(results)

if __name__ == "__main__":
    ok = asyncio.run(run())
    sys.exit(0 if ok else 1)


async def test_reschedule_visible_to_admin(base=None):
    """V3.0.4: la solicitud de reagenda DEBE aparecerle al admin."""
    import httpx
    from datetime import datetime, timezone, timedelta
    b = base or BASE
    async with httpx.AsyncClient(base_url=b, timeout=30) as c:
        admin = (await c.post("/auth/login", json={"email":"admin@dorismon.do","password":"DorismonAdmin2026!"})).json()["access_token"]
        AH = {"Authorization": f"Bearer {admin}"}
        stu = (await c.post("/auth/login", json={"email":"carlos.estudiante@dorismon.do","password":"Estudiante2026!"})).json()
        SH = {"Authorization": f"Bearer {stu['access_token']}"}
        ana = (await c.post("/auth/login", json={"email":"ana@dorismon.do","password":"Profe2026!"})).json()["access_token"]
        ana_id = (await c.get("/auth/me", headers={"Authorization":f"Bearer {ana}"})).json()["id"]

        await c.post("/payments/trial-class/request", headers=SH, json={"modality":"online","preferred_level":"A2"})
        trials = (await c.get("/admin/trial-classes", headers=AH)).json()
        tlist = trials if isinstance(trials, list) else trials.get("items", [])
        trial_id = tlist[0]["id"]
        past = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        await c.post(f"/admin/trial-classes/{trial_id}/schedule", headers=AH,
                     json={"teacher_id": ana_id, "scheduled_at": past})
        await c.get("/student/dashboard", headers=SH)  # dispara deteccion no_show
        await c.post("/student/trial-class/reschedule", headers=SH)
        trials2 = (await c.get("/admin/trial-classes", headers=AH)).json()
        tlist2 = trials2 if isinstance(trials2, list) else trials2.get("items", [])
        reagenda = [t for t in tlist2 if t.get("reschedule_requested")]
        ok = len(reagenda) > 0
        print(f"  {'✓' if ok else '✗'} admin ve la solicitud de reagenda")
        return ok

if __name__ == "__main__" and "--reschedule" in sys.argv:
    asyncio.run(test_reschedule_visible_to_admin())
