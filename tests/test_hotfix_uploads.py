"""HOTFIX DE AUDITORÍA v3.9.76 — subidas multipart con python-multipart 0.0.32 / FastAPI 0.139 / Starlette 1.x.

En proceso (ASGI real, sin red). Cloudinary se simula (no se sube nada a ningún servicio): lo que se prueba es el
análisis multipart, los tipos MIME, el archivo vacío y los tamaños máximos de cada endpoint de subida.
"""
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
os.environ["CLOUDINARY_URL"] = "cloudinary://clave-falsa:secreto-falso@nube-falsa"   # solo para que cloudinary_ready() sea True

import httpx  # noqa: E402

ADMIN = {"email": "admin@dorismon.do", "password": os.getenv("SEED_ADMIN_PASSWORD", "Adm-Test-4821!")}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": os.getenv("SEED_STUDENT_PASSWORD", "Estu-Test-4823!")}
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


async def main():
    passed = total = 0

    def check(label, ok):
        nonlocal passed, total
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    import starlette, fastapi, multipart
    from importlib.metadata import version
    print(f"  · fastapi {fastapi.__version__} · starlette {starlette.__version__} · python-multipart {version('python-multipart')}")

    from app.main import app
    import app.services.cloudinary_service as cs
    import app.routers.student as st
    from app.core.db import SessionLocal
    from app.models import AssignmentSubmission
    from sqlalchemy import select, delete

    subidos = []

    def falso_imagen(data, public_id, folder="dorismon"):
        subidos.append(("imagen", len(data), public_id, folder))
        return {"url": f"https://cdn.invalid/{public_id}.png", "public_id": public_id}

    def falso_entrega(datos, public_id, es_medio):
        subidos.append(("entrega", len(datos), public_id, es_medio))
        return {"url": f"https://cdn.invalid/{public_id}", "public_id": public_id}
    orig = (cs.upload_image_sync, st._subir_entrega)
    cs.upload_image_sync, st._subir_entrega = falso_imagen, falso_entrega

    creadas = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=120) as c:
        AH = {"Authorization": "Bearer " + (await c.post("/auth/login", json=ADMIN)).json()["access_token"]}
        SH = {"Authorization": "Bearer " + (await c.post("/auth/login", json=STUDENT)).json()["access_token"]}
        try:
            tareas = (await c.get("/student/assignments", headers=SH)).json()
            lst = tareas.get("items", tareas) if isinstance(tareas, dict) else tareas
            check("hay una tarea de la estudiante para probar la entrega con archivo", bool(lst))
            tid = lst[0]["id"]
            async with SessionLocal() as db:
                antes = set((await db.execute(select(AssignmentSubmission.id).where(AssignmentSubmission.assignment_id == tid))).scalars().all())

            U = f"/student/assignments/{tid}/upload"
            print("  · tarea de estudiante")
            r = await c.post(U, headers=SH, files={"file": ("hoja.jpg", b"\xff\xd8\xff\xe0" + b"x" * 100, "image/jpeg")})
            check(f"imagen JPG → 200 ({r.status_code})", r.status_code == 200)
            r = await c.post(U, headers=SH, files={"file": ("lectura.mp3", b"ID3" + b"x" * 500, "audio/mpeg")})
            check(f"audio MP3 → 200 ({r.status_code})", r.status_code == 200)
            r = await c.post(U, headers=SH, files={"file": ("grabación ñandú.webm", b"x" * 300, "audio/webm")})
            check(f"audio con nombre de archivo con acentos/ñ → 200 ({r.status_code})", r.status_code == 200)
            r = await c.post(U, headers=SH, files={"file": ("tarea.pdf", b"%PDF-1.4\n" + b"x" * 200, "application/pdf")})
            check(f"PDF → 200 ({r.status_code})", r.status_code == 200)
            n = len(subidos)
            r = await c.post(U, headers=SH, files={"file": ("vacio.pdf", b"", "application/pdf")})
            check(f"archivo vacío → 400 'llegó vacío' y no se sube nada ({r.status_code})", r.status_code == 400 and "vac" in r.text and len(subidos) == n)
            r = await c.post(U, headers=SH, files={"file": ("virus.exe", b"MZ" + b"x" * 50, "application/x-msdownload")})
            check(f"MIME inválido (exe) → 400 ({r.status_code})", r.status_code == 400 and len(subidos) == n)
            r = await c.post(U, headers=SH, files={"file": ("texto.txt", b"hola", "text/plain")})
            check(f"MIME inválido (text/plain) → 400 ({r.status_code})", r.status_code == 400)
            r = await c.post(U, headers=SH, files={"file": ("grande.pdf", b"%PDF" + b"0" * (20 * 1024 * 1024), "application/pdf")})
            check(f"tamaño máximo (20 MB + 4 B) → 400 'muy pesado' ({r.status_code})", r.status_code == 400 and "pesado" in r.text and len(subidos) == n)
            r = await c.post(U, headers=SH, files={"file": ("justo.pdf", b"%PDF" + b"0" * (20 * 1024 * 1024 - 4), "application/pdf")})
            check(f"exactamente 20 MB → 200 ({r.status_code})", r.status_code == 200)
            r = await c.post(U, headers=SH, data={"otro": "campo"})
            check(f"multipart sin el campo 'file' → 422 ({r.status_code})", r.status_code == 422)
            r = await c.post(U, headers={**SH, "Content-Type": "multipart/form-data; boundary=zzz"}, content=b"--zzz\r\nbasura sin cabeceras\r\n")
            check(f"cuerpo multipart malformado → error de cliente, no 500 ({r.status_code})", 400 <= r.status_code < 500)
            r = await c.post(U, files={"file": ("a.jpg", b"\xff\xd8x", "image/jpeg")})
            check(f"sin sesión → 401/403 ({r.status_code})", r.status_code in (401, 403))
            r = await c.post("/student/assignments/99999999/upload", headers=SH, files={"file": ("a.jpg", b"\xff\xd8x", "image/jpeg")})
            check(f"tarea inexistente → 404 ({r.status_code})", r.status_code == 404)

            print("  · imagen de Site Image")
            slot = None
            lista = (await c.get("/admin/site-images", headers=AH)).json()
            slot = lista["items"][0]["slot"]
            S = f"/admin/site-images/{slot}"
            r = await c.post(S, headers=AH, files={"file": ("a.png", PNG, "image/png")})
            check(f"PNG → 200 con URL ({r.status_code})", r.status_code == 200 and r.json().get("url", "").startswith("https://cdn.invalid/"))
            n = len(subidos)
            r = await c.post(S, headers=AH, files={"file": ("vacio.png", b"", "image/png")})
            check(f"archivo vacío → 400 ({r.status_code})", r.status_code == 400 and len(subidos) == n)
            r = await c.post(S, headers=AH, files={"file": ("a.txt", b"hola", "text/plain")})
            check(f"MIME inválido → 400 ({r.status_code})", r.status_code == 400)
            r = await c.post(S, headers=AH, files={"file": ("a.pdf", b"%PDF", "application/pdf")})
            check(f"PDF en un espacio de imagen → 400 ({r.status_code})", r.status_code == 400)
            r = await c.post(S, headers=AH, files={"file": ("g.png", PNG + b"0" * (10 * 1024 * 1024), "image/png")})
            check(f"más de 10 MB → 400 'muy pesada' ({r.status_code})", r.status_code == 400 and "pesada" in r.text and len(subidos) == n)
            r = await c.post(S, headers=SH, files={"file": ("a.png", PNG, "image/png")})
            check(f"una estudiante NO puede subir imágenes del sitio → 401/403 ({r.status_code})", r.status_code in (401, 403))
            r = await c.post("/admin/site-images/espacio_inexistente", headers=AH, files={"file": ("a.png", PNG, "image/png")})
            check(f"espacio inexistente → 404 ({r.status_code})", r.status_code == 404)
            await c.delete(S, headers=AH)

            print("  · foto de testimonio")
            r = await c.post("/admin/testimonials", headers=AH, json={"name": "Prueba Hotfix", "text": "Prueba de subida", "rating": 5, "is_active": False})
            tid_t = r.json().get("id") if r.status_code in (200, 201) else None
            check(f"se crea un testimonio de prueba (inactivo) ({r.status_code})", bool(tid_t))
            if tid_t:
                T = f"/admin/testimonials/{tid_t}/photo"
                try:
                    r = await c.post(T, headers=AH, files={"file": ("f.png", PNG, "image/png")})
                    check(f"PNG → 200 con URL ({r.status_code})", r.status_code == 200 and "cdn.invalid" in r.text)
                    r = await c.post(T, headers=AH, files={"file": ("f.txt", b"x", "text/plain")})
                    check(f"MIME inválido → 400 ({r.status_code})", r.status_code == 400)
                    r = await c.post(T, headers=AH, files={"file": ("f.png", PNG + b"0" * (10 * 1024 * 1024), "image/png")})
                    check(f"más de 10 MB → 400 ({r.status_code})", r.status_code == 400)
                    r = await c.post(T, headers=AH, files={"file": ("f.png", b"", "image/png")})
                    check(f"archivo vacío → no es un 500 ({r.status_code})", r.status_code < 500)
                    r = await c.post("/admin/testimonials/no-existe/photo", headers=AH, files={"file": ("f.png", PNG, "image/png")})
                    check(f"testimonio inexistente → 404 ({r.status_code})", r.status_code == 404)
                finally:
                    await c.delete(f"/admin/testimonials/{tid_t}", headers=AH)
        finally:
            cs.upload_image_sync, st._subir_entrega = orig
            async with SessionLocal() as db:   # deja la base como estaba: borra solo las entregas creadas aquí
                await db.execute(delete(AssignmentSubmission).where(AssignmentSubmission.assignment_id == tid,
                                                                   AssignmentSubmission.id.notin_(antes) if antes else True))
                await db.commit()
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
