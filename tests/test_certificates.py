"""V3.9.28 — Test de certificados: red de seguridad y anulación.

Cubre el error real que ocurrió en producción: se emitió un certificado a un
estudiante activo y no había forma de anularlo.
"""
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

        mu = (await c.get("/admin/users?q=maria.estudiante", headers=AH)).json()
        student_id = mu["items"][0]["id"]
        cid = (await c.get("/admin/courses", headers=AH)).json()[0]["id"]
        levels = (await c.get(f"/admin/levels-by-course/{cid}", headers=AH)).json()
        lvls = levels["items"] if isinstance(levels, dict) else levels
        perfil = (await c.get(f"/admin/students/{student_id}/profile", headers=AH)).json()
        lvl = [l for l in lvls if l["code"] == perfil.get("current_level_code")][0]

        base = {"student_id": student_id, "course_id": cid, "level_id": lvl["id"], "hours": 120}

        # 1. Avisar antes de certificar a alguien que sigue activo
        r = await c.post("/admin/certificates", headers=AH, json=base)
        check("Avisa al certificar a un estudiante activo", r.status_code == 409)
        check("El aviso explica el motivo en español",
              bool((r.json().get("detail") or {}).get("mensaje")))

        # 2. Confirmando a propósito, sí se emite
        r2 = await c.post("/admin/certificates", headers=AH, json={**base, "confirmar_incompleto": True})
        check("Confirmando a propósito, se emite", r2.status_code == 201)
        cert_id = r2.json().get("id")
        code = r2.json().get("code")

        # 3. Avisa si ya tiene certificado de ese nivel
        r3 = await c.post("/admin/certificates", headers=AH, json=base)
        check("Avisa si ya existe un certificado de ese nivel", r3.status_code == 409)

        # 4. El estudiante lo ve con los datos para imprimirlo
        antes = (await c.get("/student/certificates", headers=SH)).json()
        check("El estudiante ve su certificado", len(antes) >= 1)
        check("Trae el nombre del estudiante para imprimir",
              bool(antes and antes[0].get("student_name")))

        # 5. Anular
        rev = await c.post(f"/admin/certificates/{cert_id}/revoke", headers=AH,
                           json={"reason": "Prueba automatizada"})
        check("Se puede anular un certificado", rev.status_code == 200)

        despues = (await c.get("/student/certificates", headers=SH)).json()
        check("El anulado desaparece del panel del estudiante", len(despues) < len(antes))

        ver = await c.get(f"/certificate/verify/{code}")
        check("El código anulado ya no verifica", ver.status_code == 404)

        # 6. Protecciones
        otra = await c.post(f"/admin/certificates/{cert_id}/revoke", headers=AH,
                            json={"reason": "otra vez"})
        check("No se puede anular dos veces", otra.status_code == 400)

        # 7. Deshacer
        res = await c.post(f"/admin/certificates/{cert_id}/restore", headers=AH)
        check("Se puede deshacer la anulación", res.status_code == 200)
        vuelto = (await c.get("/student/certificates", headers=SH)).json()
        check("Al restaurar vuelve al panel del estudiante", len(vuelto) == len(antes))

        # 8. Solo el admin
        no = await c.post(f"/admin/certificates/{cert_id}/revoke", headers=SH,
                          json={"reason": "x"})
        check("Un estudiante NO puede anular certificados", no.status_code in (401, 403))

        # Limpieza
        await c.post(f"/admin/certificates/{cert_id}/revoke", headers=AH,
                     json={"reason": "limpieza de prueba"})

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
