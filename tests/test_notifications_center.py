"""V3.1 — Tests del centro de avisos universal.

Verifica que los 3 roles ven sus notificaciones, los contadores funcionan,
y la comunicación entre usuarios (mensajes) opera correctamente.
"""
import asyncio, httpx, sys

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9210"

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
        stu = await login(c, "maria.estudiante@dorismon.do", "Estudiante2026!")
        if not stu:
            stu = await login(c, "carlos.estudiante@dorismon.do", "Estudiante2026!")
        SH = {"Authorization": f"Bearer {stu}"}
        ana = await login(c, "ana@dorismon.do", "Profe2026!")
        TH = {"Authorization": f"Bearer {ana}"}

        # Los 3 roles pueden ver notificaciones
        for role, h in [("estudiante", SH), ("profe", TH), ("admin", AH)]:
            r = await c.get("/notifications", headers=h)
            check(f"{role} accede a /notifications", r.status_code == 200)
            r2 = await c.get("/notifications/unread-count", headers=h)
            check(f"{role} accede al contador", r2.status_code == 200)

        # Comunicación entre usuarios
        stu_id = (await c.get("/auth/me", headers=SH)).json()["id"]
        r = await c.post("/messages", headers=TH, json={
            "to_user_id": stu_id, "subject": "Test", "body": "Mensaje de prueba entre usuarios."
        })
        check("profe envía mensaje a estudiante", r.status_code == 201)

        r = await c.get("/messages/unread-count", headers=SH)
        check("estudiante ve el mensaje sin leer", r.json().get("unread", 0) >= 1)

        # Marcar todas leídas
        r = await c.post("/notifications/read-all", headers=SH)
        check("marcar todas leídas funciona", r.status_code == 200)
        r = await c.get("/notifications/unread-count", headers=SH)
        check("contador queda en 0 tras marcar todas", r.json().get("unread") == 0)

    passed = sum(1 for _, c in results if c)
    print(f"\n{passed}/{len(results)} tests pasaron")
    return passed == len(results)

if __name__ == "__main__":
    ok = asyncio.run(run())
    sys.exit(0 if ok else 1)
