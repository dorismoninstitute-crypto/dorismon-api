"""V3.9.30 — Alertas resolubles, tipos de clase y entrega con archivo."""
import sys
import asyncio
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estudiante2026!"}


async def main():
    passed = total = 0

    def check(label, ok):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
        print(f"  {'✓' if ok else '✗'} {label}")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        tok = (await c.post("/auth/login", json=ADMIN)).json()["access_token"]
        AH = {"Authorization": f"Bearer {tok}"}
        stok = (await c.post("/auth/login", json=STUDENT)).json()["access_token"]
        SH = {"Authorization": f"Bearer {stok}"}

        # ---------- Alertas ----------
        r = await c.get("/admin/alerts", headers=AH)
        check("El panel de alertas responde", r.status_code == 200)
        data = r.json()
        check("Trae los grupos y el contador", "groups" in data and "pending" in data)
        check("Cuenta las resueltas de la semana", "resolved_this_week" in data)

        # Cada grupo debe traer una forma consistente
        forma_ok = all(
            ("title" in g and "type" in g and ("key" in g or g.get("items")))
            for g in data.get("groups", [])
        )
        check("Cada grupo trae título y una forma de resolverlo", forma_ok)

        # Resolver una alerta la hace desaparecer
        clave = None
        for g in data.get("groups", []):
            if g.get("items"):
                clave = g["items"][0]["key"]
                break
            if g.get("key"):
                clave = g["key"]
                break
        if clave:
            antes = data.get("pending", 0)
            res = await c.post("/admin/alerts/action", headers=AH,
                               json={"key": clave, "action": "resolved", "note": "test"})
            check("Se puede resolver una alerta", res.status_code == 200)
            despues = (await c.get("/admin/alerts", headers=AH)).json().get("pending", 0)
            check("Al resolverla, desaparece del panel", despues < antes)

            snz = await c.post("/admin/alerts/action", headers=AH,
                               json={"key": clave, "action": "snoozed", "days": 3})
            check("Se puede posponer una alerta", snz.status_code == 200)

        mala = await c.post("/admin/alerts/action", headers=AH,
                            json={"key": "x", "action": "accion_inventada"})
        check("Acción inválida rechazada", mala.status_code == 400)

        sin_key = await c.post("/admin/alerts/action", headers=AH, json={"action": "resolved"})
        check("Sin indicar la alerta, rechaza", sin_key.status_code == 400)

        no = await c.get("/admin/alerts", headers=SH)
        check("Un estudiante NO ve las alertas", no.status_code in (401, 403))

        # ---------- Tipos de clase ----------
        s = await c.get("/admin/sessions?filter_period=all&limit=100", headers=AH)
        check("El listado de clases responde", s.status_code == 200)
        items = s.json().get("items", [])
        if items:
            check("Cada clase trae su tipo para agrupar",
                  all(i.get("kind") for i in items))
            check("Los tipos son los esperados",
                  all(i.get("kind") in ("series", "single", "private", "trial", "event")
                      for i in items))

        # ---------- Entrega con archivo ----------
        tareas = (await c.get("/student/assignments", headers=SH)).json()
        lst = tareas.get("items", tareas) if isinstance(tareas, dict) else tareas
        if lst:
            tid = lst[0]["id"]
            mal = await c.post(f"/student/assignments/{tid}/upload", headers=SH,
                               files={"file": ("v.exe", b"MZ", "application/x-msdownload")})
            check("Rechaza archivos que no son imagen, PDF ni audio", mal.status_code == 400)

            img = await c.post(f"/student/assignments/{tid}/upload", headers=SH,
                               files={"file": ("t.jpg", b"\xff\xd8\xff\xe0x", "image/jpeg")})
            check("Acepta foto de la tarea (o avisa si falta Cloudinary)",
                  img.status_code in (200, 503))

            aud = await c.post(f"/student/assignments/{tid}/upload", headers=SH,
                               files={"file": ("p.mp3", b"ID3x", "audio/mpeg")})
            check("Acepta grabación de audio", aud.status_code in (200, 503))

            nada = await c.post("/student/assignments/999999/upload", headers=SH,
                                files={"file": ("t.jpg", b"\xff\xd8x", "image/jpeg")})
            check("Tarea inexistente devuelve 404", nada.status_code == 404)

        # ---------- IA generadora de contenido (V3.9.31) ----------
        st = await c.get("/ai/status", headers=AH)
        check("El estado de la IA responde", st.status_code == 200)
        configurada = bool(st.json().get("ready"))

        v1 = await c.post("/ai/quiz", headers=AH, json={"topic": "", "level": "B1"})
        check("Sin tema, la IA rechaza", v1.status_code == 400)

        v2 = await c.post("/ai/quiz", headers=AH, json={"topic": "x", "level": "ZZ"})
        check("Nivel inválido rechazado", v2.status_code == 400)

        perm = await c.post("/ai/quiz", headers=SH, json={"topic": "x", "level": "B1"})
        check("Un estudiante NO puede generar contenido", perm.status_code == 403)

        if not configurada:
            sin = await c.post("/ai/quiz", headers=AH, json={"topic": "Presente perfecto", "level": "B1"})
            check("Sin clave, avisa con claridad", sin.status_code == 503)

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
