"""V3.9.26 — Test del video propio (LiveKit).

Lo importante que cubre es el CONTROL DE ACCESO: con un enlace de Google Meet
cualquiera que lo tenga entra. Aquí, solo entra quien pertenece a la clase.

Correr con las credenciales de prueba:
  LIVEKIT_URL=wss://prueba.livekit.cloud \
  LIVEKIT_API_KEY=APIprueba \
  LIVEKIT_API_SECRET=secretodepruebaparatest123456789 \
  python tests/test_video_rooms.py http://localhost:8000

Sin esas variables, el test verifica el otro caso importante: que la
plataforma siga funcionando igual cuando el video propio NO está configurado.
"""
import os
import sys
import asyncio
import datetime

import httpx
import jwt

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
SECRET = os.getenv("LIVEKIT_API_SECRET")

ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estudiante2026!"}
SEED_TEACHERS = ("ana@dorismon.do", "luis@dorismon.do", "sara@dorismon.do")


async def main():
    passed = 0
    total = 0

    def check(label, ok):
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
        print(f"  {'✓' if ok else '✗'} {label}")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        tok = (await c.post("/auth/login", json=ADMIN)).json()["access_token"]
        AH = {"Authorization": f"Bearer {tok}"}

        status = (await c.get("/video/status", headers=AH)).json()
        configurado = bool(status.get("ready"))

        if not configurado:
            # Caso igual de importante: sin configurar, nada se rompe
            check("Estado del video responde sin romperse", "ready" in status)
            s = await c.get("/admin/sessions", headers=AH)
            check("El listado de clases sigue funcionando", s.status_code == 200)
            stok = (await c.post("/auth/login", json=STUDENT)).json()["access_token"]
            p = await c.get("/progress/my-course", headers={"Authorization": f"Bearer {stok}"})
            check("El panel del estudiante sigue funcionando", p.status_code == 200)
            print(f"{passed}/{total} tests pasaron (video propio no configurado)")
            return 0 if passed == total else 1

        # ---------- Con LiveKit configurado ----------
        courses = (await c.get("/admin/courses", headers=AH)).json()
        cid = courses[0]["id"]
        levels = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        lvls = levels["items"] if isinstance(levels, dict) else levels
        mu = (await c.get("/admin/users?q=maria.estudiante", headers=AH)).json()
        perfil = (await c.get(f"/admin/students/{mu['items'][0]['id']}/profile", headers=AH)).json()
        mi_nivel = [l for l in lvls if l["code"] == perfil.get("current_level_code")][0]
        otro_nivel = [l for l in lvls if l["code"] != perfil.get("current_level_code")][-1]
        profes = (await c.get("/admin/users?role=teacher", headers=AH)).json()
        profe = [p for p in profes["items"] if p["email"] in SEED_TEACHERS][0]

        ptok = (await c.post("/auth/login", json={"email": profe["email"], "password": "Profe2026!"})).json()
        TH = {"Authorization": f"Bearer {ptok['access_token']}"}
        stok = (await c.post("/auth/login", json=STUDENT)).json()
        SH = {"Authorization": f"Bearer {stok['access_token']}"}

        now = datetime.datetime.now(datetime.timezone.utc)
        en_curso = {
            "starts_at_utc": (now - datetime.timedelta(minutes=10)).isoformat(),
            "ends_at_utc": (now + datetime.timedelta(minutes=50)).isoformat(),
        }

        mia = (await c.post("/admin/sessions", headers=AH, json={
            "title": "Test video propio", "modality": "online",
            "teacher_id": profe["id"], "course_id": cid, "level_id": mi_nivel["id"],
            "video_provider": "dorismon", **en_curso,
        })).json()["id"]

        # El profesor entra como moderador
        r = await c.post(f"/video/sessions/{mia}/join", headers=TH)
        check("El profesor entra a su clase", r.status_code == 200)
        if r.status_code == 200:
            claims = jwt.decode(r.json()["token"], SECRET, algorithms=["HS256"])
            check("El profesor puede administrar la sala", claims["video"].get("roomAdmin") is True)

        # El estudiante inscrito entra, pero sin poder administrar
        r2 = await c.post(f"/video/sessions/{mia}/join", headers=SH)
        check("El estudiante inscrito entra", r2.status_code == 200)
        if r2.status_code == 200:
            claims2 = jwt.decode(r2.json()["token"], SECRET, algorithms=["HS256"])
            check("El estudiante NO administra la sala", not claims2["video"].get("roomAdmin"))

        # Clase de otro nivel: acceso denegado
        ajena = (await c.post("/admin/sessions", headers=AH, json={
            "title": "Test clase ajena", "modality": "online",
            "teacher_id": profe["id"], "course_id": cid, "level_id": otro_nivel["id"],
            "video_provider": "dorismon", **en_curso,
        })).json()["id"]
        r3 = await c.post(f"/video/sessions/{ajena}/join", headers=SH)
        check("Un estudiante NO entra a una clase que no es suya", r3.status_code == 403)

        # Fuera de la ventana de tiempo
        futura = (await c.post("/admin/sessions", headers=AH, json={
            "title": "Test clase futura", "modality": "online",
            "teacher_id": profe["id"], "course_id": cid, "level_id": mi_nivel["id"],
            "video_provider": "dorismon",
            "starts_at_utc": (now + datetime.timedelta(hours=5)).isoformat(),
            "ends_at_utc": (now + datetime.timedelta(hours=6)).isoformat(),
        })).json()["id"]
        r4 = await c.post(f"/video/sessions/{futura}/join", headers=SH)
        check("La sala no abre mucho antes de la clase", r4.status_code == 400)

        # Sin sesión iniciada
        r5 = await c.post(f"/video/sessions/{mia}/join")
        check("Sin haber iniciado sesión no se entra", r5.status_code in (401, 403))

        # Clase inexistente
        r6 = await c.post("/video/sessions/no-existe-12345/join", headers=TH)
        check("Clase inexistente devuelve 404", r6.status_code == 404)

        # El frontend sabe qué video usar
        prog = (await c.get("/progress/my-course", headers=SH)).json()
        ns = prog.get("next_session") or {}
        check("La próxima clase informa qué video usa", "video_provider" in ns)

        # Limpieza
        for s in (mia, ajena, futura):
            await c.delete(f"/admin/sessions/{s}", headers=AH)

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
