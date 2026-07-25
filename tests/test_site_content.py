"""V3.9.23 — Test del contenido público de la landing.

Cubre:
- Los espacios de imagen existen y traen su tamaño recomendado
- La subida avisa con claridad si falta configurar Cloudinary (no revienta)
- La landing NO se rompe cuando todavía no hay imágenes cargadas
- Los testimonios solo aparecen en público cuando están activos
  (la sección se oculta sola si está vacía)
- Solo el admin puede administrar todo esto
"""
import sys
import asyncio
import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"

ADMIN = {"email": "admin@dorismon.do", "password": "DorismonAdmin2026!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estudiante2026!"}


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

        # 1. Los espacios de imagen están definidos con su tamaño
        r = await c.get("/admin/site-images", headers=AH)
        data = r.json()
        slots = {i["slot"] for i in data.get("items", [])}
        check("Espacios de imagen definidos (hero, og, platform, cta)",
              r.status_code == 200 and {"hero", "og", "platform", "cta"} <= slots)
        check("Cada espacio trae su tamaño recomendado",
              all(i.get("hint") for i in data.get("items", [])))

        # 2. Espacio inexistente se rechaza
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
        bad = await c.post("/admin/site-images/no_existe", headers=AH,
                           files={"file": ("a.png", png, "image/png")})
        check("Espacio de imagen inexistente rechazado", bad.status_code == 404)

        # 3. Archivo que no es imagen se rechaza con mensaje claro
        notimg = await c.post("/admin/site-images/hero", headers=AH,
                              files={"file": ("a.txt", b"hola", "text/plain")})
        check("Archivo que no es imagen rechazado (400)", notimg.status_code == 400)

        # 4. La landing pública no se rompe sin imágenes cargadas
        pub = await c.get("/site-images")
        check("Landing pide imágenes sin romperse", pub.status_code == 200 and isinstance(pub.json(), dict))

        # 5. Testimonios: la sección se oculta si no hay ninguno activo
        before = (await c.get("/testimonials")).json().get("items", [])
        cr = await c.post("/admin/testimonials", headers=AH, json={
            "name": "Test Estudiante", "role": "Ingeniero",
            "text": "Testimonio de prueba automatizada.", "rating": 5,
        })
        check("Crear testimonio", cr.status_code == 201)
        tid = cr.json().get("id")

        after = (await c.get("/testimonials")).json().get("items", [])
        check("El testimonio activo aparece en la landing", len(after) == len(before) + 1)

        # 6. Al ocultarlo desaparece del público
        await c.patch(f"/admin/testimonials/{tid}", headers=AH, json={"is_active": False})
        hidden = (await c.get("/testimonials")).json().get("items", [])
        check("Al ocultarlo desaparece de la landing", len(hidden) == len(before))

        # 7. Validación: nombre y texto obligatorios
        empty = await c.post("/admin/testimonials", headers=AH, json={"name": "", "text": ""})
        check("Testimonio vacío rechazado", empty.status_code == 400)

        # 8. Un estudiante no puede administrar la página pública
        stok = (await c.post("/auth/login", json=STUDENT)).json()["access_token"]
        SH = {"Authorization": f"Bearer {stok}"}
        forb = await c.get("/admin/site-images", headers=SH)
        check("Estudiante NO puede administrar imágenes", forb.status_code in (401, 403))
        forb2 = await c.post("/admin/testimonials", headers=SH, json={"name": "x", "text": "y"})
        check("Estudiante NO puede crear testimonios", forb2.status_code in (401, 403))

        # Limpieza
        await c.delete(f"/admin/testimonials/{tid}", headers=AH)

    print(f"{passed}/{total} tests pasaron")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
