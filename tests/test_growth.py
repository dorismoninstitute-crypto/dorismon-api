"""V3.9.29 — Reactivación (leads e inactivos) y avisos al teléfono."""
import sys
import asyncio
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estudiante2026!"}

SUB_PRUEBA = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/PRUEBA_TEST_AUTOMATICO",
    "keys": {
        "p256dh": "BKagOnyLGL0oIQKKidTMCQVjhqK6EL0k5wjHTMHnhc2SKHi_yaJEHLPYVFbHYnMV0KEXcXlnLHV2nCk6nGxKlXY",
        "auth": "k8JV6sjdbhAi1n3_LDBLvA",
    },
}


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

        # ---------- Reactivación ----------
        r = await c.get("/admin/reactivation", headers=AH)
        check("El panel de reactivación responde", r.status_code == 200)
        data = r.json()
        check("Trae las dos listas (leads e inactivos)",
              "leads" in data and "inactive" in data)
        check("Trae los totales", "totals" in data)

        campos_ok = True
        for grupo in ("leads", "inactive"):
            for p in data.get(grupo, [])[:3]:
                if not ("student_id" in p and "name" in p and "phone" in p):
                    campos_ok = False
        check("Cada persona trae nombre y teléfono para escribirle", campos_ok)

        # Marcar contactado
        alguien = (data.get("leads") or data.get("inactive") or [{}])[0].get("student_id")
        if alguien:
            m = await c.post(f"/admin/reactivation/{alguien}/contacted",
                             headers=AH, json={"via": "whatsapp"})
            check("Se puede marcar como contactado", m.status_code == 200)

        no = await c.get("/admin/reactivation", headers=SH)
        check("Un estudiante NO ve el panel de reactivación", no.status_code in (401, 403))

        # ---------- Avisos al teléfono ----------
        cfg = await c.get("/push/config")
        check("La configuración de avisos responde", cfg.status_code == 200)
        configurado = bool(cfg.json().get("ready"))

        if not configurado:
            # Sin claves, debe avisar con claridad y no romper nada
            s = await c.post("/push/subscribe", headers=SH, json={"subscription": SUB_PRUEBA})
            check("Sin claves, avisa que no está configurado", s.status_code == 503)
            st = await c.get("/push/status", headers=SH)
            check("El estado de avisos responde igual", st.status_code == 200)
        else:
            check("La clave pública tiene el largo correcto",
                  len(cfg.json().get("public_key") or "") == 87)

            s = await c.post("/push/subscribe", headers=SH,
                             json={"subscription": SUB_PRUEBA, "device": "test"})
            check("Se registra el dispositivo", s.status_code == 200)

            st = (await c.get("/push/status", headers=SH)).json()
            check("El dispositivo queda registrado", st.get("devices", 0) >= 1)

            await c.post("/push/subscribe", headers=SH, json={"subscription": SUB_PRUEBA})
            st2 = (await c.get("/push/status", headers=SH)).json()
            check("El mismo dispositivo no se duplica", st2.get("devices") == st.get("devices"))

            mal = await c.post("/push/subscribe", headers=SH, json={"subscription": {"endpoint": ""}})
            check("Rechaza datos incompletos", mal.status_code == 400)

            u = await c.post("/push/unsubscribe", headers=SH,
                             json={"endpoint": SUB_PRUEBA["endpoint"]})
            check("Se puede quitar el dispositivo", u.status_code == 200)

        sin_sesion = await c.get("/push/status")
        check("Sin sesión no se consulta el estado", sin_sesion.status_code in (401, 403))

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
