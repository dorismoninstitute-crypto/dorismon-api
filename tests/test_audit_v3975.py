"""V3.9.75 — cierre de hardening sin migraciones.

Comprueba que el diagnóstico de email no exponga metadatos de la clave y que
los endpoints sigan protegidos. La corrección de init_db se verifica además
por revisión estática porque su propósito es impedir un UPDATE global en cada
arranque.
"""
import asyncio
import sys
from pathlib import Path
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    db_source = Path(__file__).resolve().parents[1] / "app" / "core" / "db.py"
    db_text = db_source.read_text(encoding="utf-8")
    check(
        "init_db no fuerza email_verified=TRUE globalmente",
        "UPDATE users SET email_verified = TRUE WHERE email_verified IS NULL OR email_verified = FALSE" not in db_text,
    )

    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        ar = await c.post("/auth/login", json=ADMIN)
        sr = await c.post("/auth/login", json=STUDENT)
        check("admin puede iniciar sesión", ar.status_code == 200)
        check("estudiante puede iniciar sesión", sr.status_code == 200)
        if ar.status_code == 200 and sr.status_code == 200:
            AH = {"Authorization": f"Bearer {ar.json()['access_token']}"}
            SH = {"Authorization": f"Bearer {sr.json()['access_token']}"}
            r = await c.get("/auth/email-service-status", headers=AH)
            check("diagnóstico admin -> 200", r.status_code == 200)
            data = r.json() if r.status_code == 200 else {}
            check("diagnóstico no expone key_length", "key_length" not in data)
            r = await c.get("/auth/email-service-status", headers=SH)
            check("diagnóstico estudiante -> 403", r.status_code == 403)
            r = await c.get("/auth/email-service-status")
            check("diagnóstico anónimo -> 401", r.status_code == 401)

    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
