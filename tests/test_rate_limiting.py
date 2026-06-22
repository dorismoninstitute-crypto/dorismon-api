"""V2.9.3 — Test del rate limiting en login."""
import asyncio, httpx, sys

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9700"

async def run():
    results = []
    def check(name, cond):
        results.append((name, cond)); print(f"  {'✓' if cond else '✗'} {name}")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        # Usar un email único para no chocar con otros tests
        email = "ratetest@dorismon.do"
        codes = []
        for i in range(6):
            r = await c.post("/auth/login", json={"email": email, "password": "wrong"})
            codes.append(r.status_code)
        check("primeros intentos dan 401", codes[0] == 401)
        check("tras 5 fallos bloquea con 429", 429 in codes)
        # Otro email no afectado
        r = await c.post("/auth/login", json={"email": "ana@dorismon.do", "password": "Profe2026!"})
        check("otro usuario no se bloquea", r.status_code == 200)

    passed = sum(1 for _, c in results if c)
    print(f"\n{passed}/{len(results)} tests pasaron")
    return passed == len(results)

if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run()) else 1)
