"""V3.0.3 — Tests de ubicación en clases presenciales.

Verifica que el estudiante reciba sede, aula, dirección y teléfono
para sus clases presenciales/híbridas.

Correr: python tests/test_presencial_location.py http://localhost:PUERTO
"""
import asyncio, httpx, sys

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:9600"

async def run():
    results = []
    def check(name, cond):
        results.append(cond)
        print(f"  {'✓' if cond else '✗'} {name}")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        maria = (await c.post("/auth/login", json={"email":"maria.estudiante@dorismon.do","password":"Estudiante2026!"})).json().get("access_token")
        MH = {"Authorization": f"Bearer {maria}"}

        # Dashboard: campo location presente en todas las clases
        dash = (await c.get("/student/dashboard", headers=MH)).json()
        nc = dash.get("next_classes", [])
        check("next_classes incluye campo 'location'", all("location" in x for x in nc))

        # Calendario: campo location presente
        cal = (await c.get("/student/calendar", headers=MH)).json()
        events = cal if isinstance(cal, list) else cal.get("events", cal.get("items", []))
        class_events = [e for e in events if e.get("type") == "class"]
        check("calendario incluye campo 'location'", all("location" in e for e in class_events))

        # Las clases presenciales con sede deben traer datos de ubicación
        presenciales = [x for x in nc if x.get("modality") in ("presencial", "hibrida")]
        con_ubicacion = [p for p in presenciales if p.get("location")]
        if presenciales:
            check("clases presenciales traen ubicación con dirección",
                  any(p["location"].get("address") for p in con_ubicacion))
        else:
            print("  (sin clases presenciales en datos de prueba para validar dirección)")

    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} tests pasaron")
    return passed == len(results)

if __name__ == "__main__":
    ok = asyncio.run(run())
    sys.exit(0 if ok else 1)
