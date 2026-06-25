"""V3.7 — Test de renovación automática de token.

Verifica que el refresh token permite obtener un nuevo access token,
para que el usuario no pierda su trabajo si el token expira (ej: en el test de nivel).
"""
import asyncio, httpx, sys

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9280"

async def run():
    results = []
    def check(name, cond):
        results.append((name, cond))
        print(f"  {'✓' if cond else '✗'} {name}")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        r = (await c.post("/auth/login", json={"email":"carlos.estudiante@dorismon.do","password":"Estudiante2026!"})).json()
        access = r.get("access_token")
        refresh = r.get("refresh_token")
        check("login devuelve access + refresh", bool(access) and bool(refresh))

        # Token válido funciona
        me = await c.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
        check("token válido da 200", me.status_code == 200)

        # Token corrupto da 401
        bad = await c.get("/auth/me", headers={"Authorization": "Bearer token_invalido"})
        check("token inválido da 401", bad.status_code == 401)

        # Renovar con refresh
        rf = await c.post("/auth/refresh", json={"refresh_token": refresh})
        check("refresh genera token nuevo", rf.status_code == 200 and "access_token" in rf.json())

        # El nuevo token funciona
        if rf.status_code == 200:
            new = rf.json()["access_token"]
            me2 = await c.get("/auth/me", headers={"Authorization": f"Bearer {new}"})
            check("token renovado funciona", me2.status_code == 200)

        # Refresh con token inválido falla
        bad_rf = await c.post("/auth/refresh", json={"refresh_token": "invalido"})
        check("refresh inválido se rechaza", bad_rf.status_code == 401)

    passed = sum(1 for _, c in results if c)
    print(f"\n{passed}/{len(results)} tests pasaron")
    return passed == len(results)

if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run()) else 1)
