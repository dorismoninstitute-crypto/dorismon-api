"""V3.8 — Test de conversión de rol (estudiante → profesor)."""
import asyncio, httpx, sys, random

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9301"

async def run():
    results = []
    def check(name, cond):
        results.append((name, cond))
        print(f"  {'✓' if cond else '✗'} {name}")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        email = f"convtest{random.randint(10000,99999)}@gmail.com"
        reg = await c.post("/auth/register", json={"email": email, "password": "Test2026!", "full_name": "Conv Test"})
        check("registro público crea cuenta", reg.status_code == 201)

        admin = (await c.post("/auth/login", json={"email":"admin@dorismon.do","password":"DorismonAdmin2026!"})).json()["access_token"]
        AH = {"Authorization": f"Bearer {admin}"}

        dup = await c.post("/admin/users", headers=AH, json={"email": email, "password":"x","full_name":"x","role":"teacher"})
        check("crear con email duplicado da 409", dup.status_code == 409)

        users = (await c.get(f"/admin/users?q={email}", headers=AH)).json()
        items = users.get("items", users if isinstance(users, list) else [])
        uid = items[0]["id"] if items else None
        check("encuentro al usuario (rol student)", bool(uid) and items[0]["role"] == "student")

        chg = await c.post(f"/admin/users/{uid}/change-role", headers=AH, json={"new_role":"teacher","modalities":"online"})
        check("conversión a profesor exitosa", chg.status_code == 200 and chg.json().get("new_role") == "teacher")

        login = await c.post("/auth/login", json={"email": email, "password": "Test2026!"})
        if login.status_code == 200:
            pt = login.json()["access_token"]
            dash = await c.get("/teacher/dashboard", headers={"Authorization": f"Bearer {pt}"})
            check("entra al panel de profesor", dash.status_code == 200)

        profes = (await c.get("/admin/users?role=teacher", headers=AH)).json()
        pitems = profes.get("items", profes if isinstance(profes, list) else [])
        check("aparece en lista de profesores", any(p.get("email") == email for p in pitems))

        admins = (await c.get("/admin/users?q=admin@dorismon.do", headers=AH)).json()
        aitems = admins.get("items", admins if isinstance(admins, list) else [])
        if aitems:
            blocked = await c.post(f"/admin/users/{aitems[0]['id']}/change-role", headers=AH, json={"new_role":"teacher"})
            check("no permite cambiar rol de admin", blocked.status_code == 403)

    passed = sum(1 for _, c in results if c)
    print(f"\n{passed}/{len(results)} tests pasaron")
    return passed == len(results)



async def run_archived():
    """V3.9: verifica el archivado del perfil de estudiante."""
    import random
    results = []
    def check(name, cond):
        results.append((name, cond))
        print(f"  {'✓' if cond else '✗'} {name}")
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        email = f"arch{random.randint(10000,99999)}@gmail.com"
        await c.post("/auth/register", json={"email": email, "password": "Test2026!", "full_name": "Arch Test"})
        admin = (await c.post("/auth/login", json={"email":"admin@dorismon.do","password":"DorismonAdmin2026!"})).json()["access_token"]
        AH = {"Authorization": f"Bearer {admin}"}
        before = (await c.get("/admin/users?role=student", headers=AH)).json()
        before_count = before.get("total", len(before.get("items", [])))
        users = (await c.get(f"/admin/users?q={email}", headers=AH)).json()
        uid = users["items"][0]["id"]
        await c.post(f"/admin/users/{uid}/change-role", headers=AH, json={"new_role":"teacher","modalities":"online"})
        after = (await c.get("/admin/users?role=student", headers=AH)).json()
        after_count = after.get("total", len(after.get("items", [])))
        check("al convertir, baja el conteo de estudiantes", after_count == before_count - 1)
        # Reversible
        await c.post(f"/admin/users/{uid}/change-role", headers=AH, json={"new_role":"student"})
        back = (await c.get("/admin/users?role=student", headers=AH)).json()
        back_count = back.get("total", len(back.get("items", [])))
        check("al revertir, se des-archiva (vuelve el conteo)", back_count == before_count)
    passed = sum(1 for _, c in results if c)
    print(f"  ({passed}/{len(results)} de archivado)")
    return passed == len(results)




async def run_block_history():
    """V3.9.2: verifica que NO se puede convertir un estudiante con clases."""
    import random
    results = []
    def check(name, cond):
        results.append((name, cond))
        print(f"  {'✓' if cond else '✗'} {name}")
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        admin = (await c.post("/auth/login", json={"email":"admin@dorismon.do","password":"DorismonAdmin2026!"})).json()["access_token"]
        AH = {"Authorization": f"Bearer {admin}"}
        # Estudiante con clases (maria del seed) → bloqueado
        u = (await c.get("/admin/users?q=maria.estudiante", headers=AH)).json()
        if u.get("items"):
            uid = u["items"][0]["id"]
            r = await c.post(f"/admin/users/{uid}/change-role", headers=AH, json={"new_role":"teacher","modalities":"online"})
            check("estudiante con clases NO se puede convertir", r.status_code == 409)
            # Y el listado lo marca
            check("listado marca has_enrollments", u["items"][0].get("has_enrollments") == True)
        # Usuario limpio → permitido
        email = f"clean{random.randint(10000,99999)}@gmail.com"
        await c.post("/auth/register", json={"email": email, "password": "Test2026!", "full_name": "Clean User"})
        u2 = (await c.get(f"/admin/users?q={email}", headers=AH)).json()
        uid2 = u2["items"][0]["id"]
        check("usuario limpio marca has_enrollments=False", u2["items"][0].get("has_enrollments") == False)
        r2 = await c.post(f"/admin/users/{uid2}/change-role", headers=AH, json={"new_role":"teacher","modalities":"online"})
        check("usuario limpio SÍ se convierte", r2.status_code == 200)
    passed = sum(1 for _, c in results if c)
    print(f"  ({passed}/{len(results)} de bloqueo por historial)")
    return passed == len(results)


if __name__ == "__main__":
    ok1 = asyncio.run(run())
    ok2 = asyncio.run(run_archived())
    ok3 = asyncio.run(run_block_history())
    sys.exit(0 if (ok1 and ok2 and ok3) else 1)
