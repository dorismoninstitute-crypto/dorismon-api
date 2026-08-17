"""Regression v3.9.61 — /admin/dashboard must never leak a teacher as `user`.

The bug was caused by reusing the variable `u` for the authenticated admin and
again inside the teachers loop. If at least one teacher existed, the dashboard
returned the last teacher's name/email while hardcoding role=super_admin.
"""
import sys
import asyncio
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}


async def main():
    passed = total = 0

    def check(label, ok):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
        print(f"  {'✓' if ok else '✗'} {label}")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        login = await c.post("/auth/login", json=ADMIN)
        check("Admin puede iniciar sesión", login.status_code == 200)
        if login.status_code != 200:
            print(f"\n{passed}/{total}")
            return passed == total

        H = {"Authorization": f"Bearer {login.json()['access_token']}"}
        me = await c.get("/auth/me", headers=H)
        dash = await c.get("/admin/dashboard", headers=H)
        check("/auth/me responde", me.status_code == 200)
        check("/admin/dashboard responde", dash.status_code == 200)
        if me.status_code == 200 and dash.status_code == 200:
            m = me.json()
            du = dash.json().get("user") or {}
            check("Dashboard devuelve el MISMO user id autenticado", du.get("id") == m.get("id"))
            check("Dashboard devuelve el MISMO email autenticado", du.get("email") == m.get("email"))
            check("Dashboard devuelve el MISMO nombre autenticado", du.get("full_name") == m.get("full_name"))
            check("Dashboard devuelve el MISMO rol autenticado", du.get("role") == m.get("role"))

    print(f"\n{passed}/{total}")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
