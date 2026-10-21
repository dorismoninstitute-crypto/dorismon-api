"""GATE 4 — Rapidez del inicio de sesión sin cambiar seguridad ni reglas.

Backend:
  G4-7 · bcrypt (mismas 12 rondas, misma función) se ejecuta FUERA del event
         loop: mientras se verifica una contraseña, el servidor sigue atendiendo.
Frontend (estático):
  G4-1 · un solo NotificationCenter.
  G4-2/4/8 · peticiones compartidas en memoria (/auth/me, my-course, settings).
  G4-3 · estudiante: test de nivel + panel + progreso en paralelo, regla Gate 3 intacta.
  G4-5 · /welcome solo hace router.prefetch (sin pedir datos del panel).
  G4-6 · Dirección: principal primero y visible sin esperar a los secundarios.

Uso: python tests/test_gate4.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import re
import sys
import time
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}


async def parte_login(check):
    print("\n[G4-7] Login: mismo resultado, sin bloquear el servidor")
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        r = await c.post("/auth/login", json=STUDENT)
        check("contraseña correcta → 200 con access y refresh token",
              r.status_code == 200 and {"access_token", "refresh_token"} <= set(r.json()))
        r = await c.post("/auth/login", json={"email": TEACHER["email"], "password": "incorrecta-123"})
        check("contraseña incorrecta → 401 con el mismo mensaje",
              r.status_code == 401 and "Email o contraseña incorrectos" in r.text)
        r = await c.post("/auth/login", json={"email": "no-existe-g4@dorismon.do", "password": "x"})
        check("usuario inexistente → 401 (no se verifica nada)", r.status_code == 401)
        await c.post("/auth/login", json=TEACHER)  # limpia el contador de intentos del profesor

        AH = {"Authorization": f"Bearer {(await c.post('/auth/login', json=ADMIN)).json()['access_token']}"}
        email = f"g4baja{int(time.time())}@dorismon.do"
        u = await c.post("/admin/users", headers=AH, json={
            "email": email, "full_name": "G4 Baja", "password": "Estu-Test-4823!", "role": "student"})
        uid = u.json().get("id") or u.json().get("user_id")
        await c.patch(f"/admin/users/{uid}", headers=AH, json={"is_active": False})
        r = await c.post("/auth/login", json={"email": email, "password": "Estu-Test-4823!"})
        check("usuario desactivado → 403 'Cuenta desactivada'",
              r.status_code == 403 and "Cuenta desactivada" in r.text)

        print("  · concurrencia: 6 logins a la vez + /health en paralelo")
        latencias, fin_logins = [], []

        async def un_login(cred):
            async with httpx.AsyncClient(base_url=BASE, timeout=60) as cl:
                t = time.perf_counter()
                rr = await cl.post("/auth/login", json=cred)
                fin_logins.append(time.perf_counter() - t)
                return rr.status_code

        async def sondear(hasta):
            async with httpx.AsyncClient(base_url=BASE, timeout=60) as cl:
                await asyncio.sleep(0.05)
                while time.perf_counter() < hasta:
                    t = time.perf_counter()
                    await cl.get("/health")
                    latencias.append(time.perf_counter() - t)
                    await asyncio.sleep(0.02)

        creds = [STUDENT, TEACHER, ADMIN, STUDENT, TEACHER, ADMIN]
        t0 = time.perf_counter()
        tareas = [asyncio.create_task(un_login(x)) for x in creds]
        sonda = asyncio.create_task(sondear(t0 + 1.5))
        codigos = await asyncio.gather(*tareas)
        await sonda
        check("los 6 logins concurrentes → 200", all(x == 200 for x in codigos))
        peor = max(latencias) if latencias else 99
        print(f"    logins: más lento {max(fin_logins):.2f}s · /health: {len(latencias)} respuestas, peor {peor * 1000:.0f} ms")
        check("mientras se verifican contraseñas, /health responde (> 5 respuestas)", len(latencias) > 5)
        check("…y rápido: ninguna respuesta de /health tarda más de 300 ms", peor < 0.3)


def parte_estatica(check):
    print("\n[estático] Backend y frontend")
    auth_py = (ROOT / "app" / "routers" / "auth.py").read_text(encoding="utf-8")
    sec_py = (ROOT / "app" / "core" / "security.py").read_text(encoding="utf-8")
    check("G4-7: verify_password se ejecuta en run_in_threadpool",
          "await run_in_threadpool(\n        verify_password, body.password, user.password_hash)" in auth_py)
    check("G4-7: sin usuario no se verifica (misma regla de antes)", "password_ok = bool(user) and" in auth_py)
    check("G4-7: bcrypt intacto (mismo CryptContext, sin cambiar rondas)",
          'CryptContext(schemes=["bcrypt"], deprecated="auto")' in sec_py and "rounds" not in sec_py)

    archivos = {k: WEB / v for k, v in {
        "layout": "app/dashboard/layout.tsx", "api": "lib/api.ts",
        "est": "app/dashboard/student/page.tsx", "adm": "app/dashboard/admin/page.tsx",
        "wel": "app/welcome/page.tsx", "dash": "app/dashboard/page.tsx"}.items()}
    if not all(p.exists() for p in archivos.values()):
        check(f"existen los archivos del frontend en {WEB}", False)
        return
    t = {k: p.read_text(encoding="utf-8") for k, p in archivos.items()}

    check("G4-1: un solo <NotificationCenter /> en el layout", t["layout"].count("<NotificationCenter />") == 1)
    check("G4-1: la cabecera única conserva menú y logo solo en móvil (lg:hidden)",
          'onClick={() => setSidebarOpen(true)} className="lg:hidden' in t["layout"]
          and '<div className="lg:hidden flex items-center gap-2">' in t["layout"])

    a = t["api"]
    check("G4-2: /auth/me compartido por token, en memoria", "compartida(`me:${tok}`" in a and "new Map<string, Compartida>()" in a)
    check("G4-2: se olvida al cerrar sesión, al guardar token y en cualquier escritura",
          "clearToken();\n    olvidarCompartidas();" in a
          and "saveToken: (t: string) => { olvidarCompartidas(); setToken(t); }" in a
          and 'if ((opts.method || "GET") !== "GET") olvidarCompartidas();' in a
          and "olvidarCompartidas();  // GATE 4 — una subida" in a)
    check("G4-2: un error nunca queda guardado", "if (compartidas.get(clave) === entrada) compartidas.delete(clave); throw e;" in a)
    check("G4-2: no se guarda una copia persistente del usuario",
          not re.search(r"(localStorage|sessionStorage)\.setItem\([\"'](me|user|usuario|current_user)", a))
    check("G4-4: my-course compartido 5 s por token; institute-settings 60 s",
          "compartida(`myCourse:${getToken() || \"\"}`, 5000" in a and 'compartida("institute-settings", 60000' in a)
    check("G4-8: /dashboard decide la ruta con auth.me() (backend), no con un rol guardado",
          "auth.me().then((u) =>" in t["dash"] and "localStorage" not in t["dash"])

    e = t["est"]
    check("G4-3: test de nivel, panel y progreso arrancan a la vez",
          e.find("const pPanel = studentApi.dashboard();") < e.find("placement.status()")
          and e.find("const pCurso = progress.myCourse()") < e.find("placement.status()"))
    check("G4-3: regla de Gate 3 intacta (único redirect y misma condición)",
          e.count('router.replace("/placement")') == 1 and "if (!tieneClaseAutorizada)" in e
          and "safeArray(previos[0]?.next_classes).length > 0 || !!previos[1]?.next_session" in e)
    check("G4-3: eventos y prueba siguen pidiéndose solo si el panel se muestra",
          e.find("events.list()") > e.find("if (!tieneClaseAutorizada)")
          and e.find("studentPayments.trialStatus()") > e.find("if (!tieneClaseAutorizada)"))

    w = t["wel"]
    check("G4-5: /welcome hace router.prefetch del panel", "router.prefetch(dashboardPath)" in w)
    check("G4-5: /welcome NO pide datos del panel por adelantado",
          not re.search(r"studentApi|teacherApi|adminApi|progress\.|placement\.|events\.", w))

    d = t["adm"]
    check("G4-6: lo principal se pide antes que los 7 secundarios",
          0 < d.find("const principal = Promise.all([") < d.find("const secundarios = Promise.allSettled(["))
    check("G4-6: el panel se muestra sin esperar a los secundarios",
          d.find("setLoading(false);\n\n        const [sinHorario") > 0
          and d.find("setLoading(false);\n\n        const [sinHorario") < d.find("await secundarios"))
    check("G4-6: AlertasAdmin se monta con sus datos (sin petición duplicada)",
          "{secListo ? (\n        <AlertasAdmin initialData={alertas.actionables}" in d)
    check("G4-6: 'Todo al día' solo cuando ya llegaron los secundarios", "const todoAlDia = secListo &&" in d)
    check("G4-6: espacio reservado con esqueletos (Hueso / filas fantasma)",
          "const Hueso" in d and "Revisando pendientes…" in d and "filas fantasma" in d)
    check("G4-6: la identidad sigue saliendo de /auth/me", "setCurrentUser(me);" in d and "auth.me()," in d)


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    await parte_login(check)
    parte_estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
