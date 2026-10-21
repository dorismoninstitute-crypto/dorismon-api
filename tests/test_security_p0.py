"""v3.9.76 — SECURITY P0 (cierre preproducción).

1A  Limitador de login: fuerza bruta, memoria acotada, limpieza real, expiración, concurrencia, proxy/IP.
1B  SECRET_KEY: producción falla cerrado; local/tests siguen.
1C  Credenciales por defecto: seed bloqueado en producción, README sin contraseñas, aviso de arranque.
1D  Aprobación de pago: UNA sola aprobación efectiva bajo concurrencia (sin duplicar inscripción/pago/aviso/auditoría).
1E  Endpoint destructivo: deshabilitado en producción (la simulación sigue disponible).
1F  CORS: producción sin localhost, sin comodín, previews cerrados.

Las partes de limitador y configuración no usan servidor. La de pagos corre la app EN ESTE PROCESO sobre la
base de DATABASE_URL (SQLite de la suite o PostgreSQL real: lo importante es la concurrencia).
Uso: python tests/test_security_p0.py [BASE_URL]   (BASE_URL se ignora)
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CLAVE_FUERTE = "k9Xm2Vq7LpR4tYw8Zc3NbH6JdF1sAeG5uQoT0iUy"
_ENV_LIMPIO = ("RENDER", "ENVIRONMENT", "APP_ENV", "SECRET_KEY", "CORS_ORIGINS", "ALLOW_VERCEL_PREVIEWS",
               "TRUSTED_PROXY_COUNT", "DATABASE_URL", "ALLOW_DESTRUCTIVE_MAINTENANCE", "ALLOW_DEV_SEED",
               "SEED_ADMIN_PASSWORD", "SEED_TEACHER_PASSWORD", "SEED_STUDENT_PASSWORD")


def correr(codigo, extra=None, timeout=180):
    env = {k: v for k, v in os.environ.items() if k not in _ENV_LIMPIO}
    env.update(extra or {})
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run([sys.executable, "-c", codigo], cwd=str(ROOT), env=env, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    return r.returncode, r.stdout, r.stderr


class _Req:
    """Petición mínima (client + headers) para probar client_ip()."""
    def __init__(self, host, headers=None):
        self.client = type("C", (), {"host": host})() if host else None
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}


# ──────────────────────────────────────────────────────────────────────────────────────────────
def parte_limitador(check):
    print("\n[1A] Limitador de login (unidad)")
    import app.services.rate_limiter as rl
    t = [1_000_000.0]
    original = (rl._now, rl.MAX_ENTRIES, rl.PURGE_EVERY)
    rl._now = lambda: t[0]
    try:
        rl.reset()
        res = [rl.register_failure("1.1.1.1:v@x") for _ in range(5)]
        check("5 fallos del par ip:email bloquean ese par (429)", res[-1][0] and not rl.check_rate_limit("1.1.1.1:v@x")[0])
        check("…otra IP con el MISMO email NO queda bloqueada (un atacante no bloquea a la persona dueña)",
              rl.check_rate_limit("2.2.2.2:v@x")[0])
        check("…ni otro email desde la misma IP", rl.check_rate_limit("1.1.1.1:otro@x")[0])
        t[0] += rl.LOCKOUT_SECONDS + 1
        check("el bloqueo EXPIRA pasado el tiempo", rl.check_rate_limit("1.1.1.1:v@x")[0])
        rl.reset()
        for _ in range(4):
            rl.register_failure("3.3.3.3:a@x")
        t[0] += rl.WINDOW_SECONDS + 1
        check("fallos fuera de la ventana NO cuentan (el 5.º tras 15 min no bloquea)", not rl.register_failure("3.3.3.3:a@x")[0])
        rl.reset()
        bloqueada = [rl.register_failure("9.9.9.9:*", rl.IP_MAX_ATTEMPTS)[0] for _ in range(rl.IP_MAX_ATTEMPTS)]
        check(f"rociado: {rl.IP_MAX_ATTEMPTS} fallos de una IP (cuentas distintas) bloquean la IP", bloqueada[-1] and not any(bloqueada[:-1]))
        rl.register_success("9.9.9.9:otro")
        check("un login correcto limpia solo SU par, no el contador de la IP", not rl.check_rate_limit("9.9.9.9:*")[0])

        print("  · memoria acotada y limpieza real")
        rl.reset()
        rl.MAX_ENTRIES, rl.PURGE_EVERY = 100, 10
        pico = 0
        for i in range(1000):
            rl.register_failure(f"10.0.{i // 250}.{i % 250}:u{i}@x")
            pico = max(pico, rl.stats()["entradas"])
        check(f"1000 claves distintas vigentes: nunca más de MAX_ENTRIES ({pico} ≤ 100)", pico <= 100)
        rl.reset()
        rl.MAX_ENTRIES, rl.PURGE_EVERY = 10_000, 10
        for i in range(50):
            rl.register_failure(f"20.0.0.{i}:x@y")        # 1 fallo cada una: nunca se reintentan
        t[0] += 2 * rl.WINDOW_SECONDS
        check("la limpieza global borra entradas caducadas que NUNCA se reintentan", rl._purge_old() == 50 and rl.stats()["entradas"] == 0)
        for i in range(30):
            rl.register_failure(f"21.0.0.{i}:x@y")
        t[0] += 2 * rl.WINDOW_SECONDS
        for _ in range(12):
            rl.check_rate_limit("cualquiera")             # sin llamar a _purge_old: la limpieza es automática
        check("…y la limpieza oportunista corre sola (sin que nadie la llame)", rl.stats()["entradas"] == 0)
        rl.register_failure("k" * 5000 + ":x")
        check("una clave gigante (email del cliente) se recorta", max(len(k) for k in rl._store) <= rl.MAX_KEY_LEN)

        print("  · concurrencia")
        rl.reset()
        rl.MAX_ENTRIES = 10_000
        resultados, barrera = [], threading.Barrier(64)

        def golpear():
            barrera.wait()
            for _ in range(10):
                resultados.append(rl.register_failure("c.c.c.c:z@x"))
        hilos = [threading.Thread(target=golpear) for _ in range(64)]
        [h.start() for h in hilos]; [h.join() for h in hilos]
        locks = sum(1 for r in resultados if r[0])
        check(f"640 fallos desde 64 hilos: {locks} bloqueos = 640 // 5 (ningún conteo perdido) y fallos restantes = 0",
              locks == 640 // rl.MAX_ATTEMPTS and len(rl._store["c.c.c.c:z@x"]["fails"]) == 640 % rl.MAX_ATTEMPTS)
    finally:
        rl._now, rl.MAX_ENTRIES, rl.PURGE_EVERY = original
        rl.reset()


def parte_ip(check):
    print("\n[1A] IP del cliente detrás de proxy")
    from app.services.rate_limiter import client_ip, ip_confiable, claves_login, resolver_ip_cliente
    guardado = {k: os.environ.get(k) for k in ("RENDER", "TRUSTED_PROXY_COUNT")}
    try:
        os.environ.pop("RENDER", None)
        os.environ.pop("TRUSTED_PROXY_COUNT", None)

        directo = _Req("8.8.8.8")
        check("conexión local/directa sin proxy: socket IPv4 válida es fiable",
              resolver_ip_cliente(directo) == ("8.8.8.8", True)
              and claves_login(directo, "A@X.com") == ("8.8.8.8:a@x.com", "8.8.8.8:*"))

        no_confiado = _Req("10.0.0.9", {"X-Forwarded-For": "6.6.6.6"})
        check("no-Render + cabecera proxy no confiada: se ignora XFF y la identidad queda no fiable",
              client_ip(no_confiado) == "10.0.0.9" and not ip_confiable(no_confiado)
              and claves_login(no_confiado, "A@X.com") == (None, None))

        os.environ["TRUSTED_PROXY_COUNT"] = "1"
        check("proxy no-Render explícito: usa el último salto válido de XFF",
              resolver_ip_cliente(_Req("10.0.0.9", {"X-Forwarded-For": "198.51.100.7, 203.0.113.5"}))
              == ("203.0.113.5", True))
        check("proxy no-Render: XFF inválido queda no fiable y no crea buckets",
              resolver_ip_cliente(_Req("10.0.0.9", {"X-Forwarded-For": "no-es-ip"})) == ("10.0.0.9", False)
              and claves_login(_Req("10.0.0.9", {"X-Forwarded-For": "no-es-ip"}), "a@x.com") == (None, None))

        os.environ["TRUSTED_PROXY_COUNT"] = "2"
        check("proxy no-Render: XFF insuficiente queda no fiable",
              resolver_ip_cliente(_Req("10.0.0.9", {"X-Forwarded-For": "203.0.113.5"})) == ("10.0.0.9", False))

        os.environ["TRUSTED_PROXY_COUNT"] = "abc"
        check("TRUSTED_PROXY_COUNT inválido nunca activa confianza por accidente",
              resolver_ip_cliente(_Req("10.0.0.9", {"X-Forwarded-For": "6.6.6.6"})) == ("10.0.0.9", False))

        os.environ["RENDER"] = "true"
        os.environ.pop("TRUSTED_PROXY_COUNT", None)
        check("Render + CF-Connecting-IP IPv4 válida → fiable",
              resolver_ip_cliente(_Req("10.0.0.9", {"CF-Connecting-IP": "203.0.113.5"})) == ("203.0.113.5", True))
        check("Render + CF-Connecting-IP IPv6 válida → fiable",
              resolver_ip_cliente(_Req("10.0.0.9", {"CF-Connecting-IP": "2001:db8::8"})) == ("2001:db8::8", True))
        check("Render: XFF falsificado no gana a CF-Connecting-IP",
              resolver_ip_cliente(_Req("10.0.0.9", {"X-Forwarded-For": "6.6.6.6", "CF-Connecting-IP": "203.0.113.7"}))
              == ("203.0.113.7", True))
        check("Render: CF inválida → no fiable y no se crean buckets de proxy",
              resolver_ip_cliente(_Req("10.0.0.9", {"CF-Connecting-IP": "no-es-ip"})) == ("10.0.0.9", False)
              and claves_login(_Req("10.0.0.9", {"CF-Connecting-IP": "no-es-ip"}), "A@X.com") == (None, None))
        check("Render: CF ausente → no fiable y no se crean buckets de proxy",
              resolver_ip_cliente(_Req("10.0.0.9")) == ("10.0.0.9", False)
              and claves_login(_Req("10.0.0.9"), "A@X.com") == (None, None))
        check("sin cliente no revienta y nunca se marca fiable", resolver_ip_cliente(_Req(None)) == ("unknown", False))
    finally:
        for k, v in guardado.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


async def parte_login_http(check):
    print("\n[1A] Login por HTTP: un atacante no bloquea a la persona dueña")
    import httpx
    import app.services.rate_limiter as rl
    from app.main import app
    from app.core.db import engine
    from app.models import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    rl.reset()
    mal = {"email": "luis@dorismon.do", "password": "mal-mal-mal"}
    bien = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}

    def cli(ip, cabeceras=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(ip, 5000)), base_url="http://t",
                                 timeout=60, headers=cabeceras or {})
    guardado = {k: os.environ.get(k) for k in ("RENDER", "TRUSTED_PROXY_COUNT")}
    try:
        os.environ.pop("RENDER", None)
        os.environ.pop("TRUSTED_PROXY_COUNT", None)
        async with cli("203.0.113.5") as atacante, cli("198.51.100.7") as dueno:
            codigos = [(await atacante.post("/auth/login", json=mal)).status_code for _ in range(5)]
            check(f"directo: atacante recibe 401×4 y 429 al 5.º ({codigos})", codigos == [401, 401, 401, 401, 429])
            r = await dueno.post("/auth/login", json=bien)
            check(f"directo: la persona dueña desde otra IP entra ({r.status_code})", r.status_code == 200)

        rl.reset()
        print("  · Render: CF-Connecting-IP decide la identidad, no XFF")
        os.environ["RENDER"] = "true"
        async with cli("10.0.0.1") as proxy:
            for _ in range(5):
                r = await proxy.post("/auth/login", json=mal, headers={
                    "X-Forwarded-For": "198.51.100.7",
                    "CF-Connecting-IP": "203.0.113.5",
                })
            check(f"Render: atacante queda bloqueado en SU CF IP ({r.status_code})", r.status_code == 429)
            r = await proxy.post("/auth/login", json=bien, headers={"CF-Connecting-IP": "198.51.100.7"})
            check(f"Render: víctima desde otra CF IP no queda bloqueada ({r.status_code})", r.status_code == 200)

        rl.reset()
        print("  · Render sin CF válida: fail-safe contra bloqueo cruzado")
        async with cli("10.0.0.1") as proxy:
            codigos = [(await proxy.post("/auth/login", json=mal, headers={"X-Forwarded-For": "7.7.7.7"})).status_code for _ in range(6)]
            check(f"sin CF fiable no se crea bucket compartido del proxy ({codigos})", all(c == 401 for c in codigos))
            check("…y el store permanece vacío", rl.stats()["entradas"] == 0)
    finally:
        for k, v in guardado.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v
        rl.reset()


# ──────────────────────────────────────────────────────────────────────────────────────────────
def parte_config(check):
    print("\n[1B] SECRET_KEY y entorno de producción")
    cod = "import app.core.config as c; print('OK', c.is_production())"
    casos = [
        ("producción (RENDER) SIN SECRET_KEY → no arranca", {"RENDER": "true"}, False),
        ("producción con el valor de ejemplo del .env.example → no arranca", {"RENDER": "true", "SECRET_KEY": "cambiar-en-produccion"}, False),
        ("producción con el fallback conocido del código → no arranca", {"RENDER": "true", "SECRET_KEY": "cambiar-en-produccion-usar-secreto-largo-y-aleatorio"}, False),
        ("producción con una clave corta → no arranca", {"RENDER": "true", "SECRET_KEY": "abc123"}, False),
        ("producción con una clave que dice 'cambiar' → no arranca", {"RENDER": "true", "SECRET_KEY": "x" * 20 + "CAMBIAR" + "y" * 20}, False),
        ("ENVIRONMENT=production sin clave → no arranca", {"ENVIRONMENT": "production"}, False),
        ("RENDER no se puede anular con ENVIRONMENT=development (falla cerrado)", {"RENDER": "true", "ENVIRONMENT": "development"}, False),
        ("producción con una clave aleatoria de 40 caracteres → arranca", {"RENDER": "true", "SECRET_KEY": CLAVE_FUERTE}, True),
        ("local / tests (sin RENDER ni ENVIRONMENT) con el fallback → arranca", {}, True),
        ("local con una clave explícita → arranca", {"SECRET_KEY": "solo-pruebas-locales-0123456789"}, True),
    ]
    for nombre, env, debe in casos:
        rc, out, err = correr(cod, env)
        ok = (rc == 0 and out.startswith("OK")) if debe else (rc != 0 and "SECRET_KEY" in err)
        check(nombre + ("" if ok else f" [rc={rc} err={err.strip()[-120:]}]"), ok)
    rc, out, _ = correr(cod, {"RENDER": "true", "SECRET_KEY": CLAVE_FUERTE})
    check("is_production() = True bajo RENDER", "True" in out)
    rc, out, _ = correr(cod, {})
    check("is_production() = False en local", "False" in out)
    from app.core.config import exigir_configuracion_segura, secret_key_valida
    try:
        exigir_configuracion_segura("abc", produccion=False); fuera = True
    except Exception:
        fuera = False
    check("la validación no actúa fuera de producción", fuera and not secret_key_valida("abc") and secret_key_valida(CLAVE_FUERTE))


def parte_seed(check):
    print("\n[1C] Seed sin contraseñas por defecto")
    import re
    ejecutar = "import runpy; runpy.run_path('seed.py', run_name='__main__')"
    claves = {"SEED_ADMIN_PASSWORD": "Dev-Admin-Prueba-91!", "SEED_TEACHER_PASSWORD": "Dev-Profe-Prueba-92!", "SEED_STUDENT_PASSWORD": "Dev-Estu-Prueba-93!"}
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "seed_prod.db"
        rc, out, err = correr(ejecutar, {"RENDER": "true", "SECRET_KEY": CLAVE_FUERTE, "ALLOW_DEV_SEED": "true", **claves,
                                         "DATABASE_URL": f"sqlite+aiosqlite:///{db.as_posix()}"}, timeout=120)
        check(f"el seed se NIEGA a ejecutarse en producción aunque tenga autorización y claves (rc={rc})", rc == 1 and "SEED BLOQUEADO" in out)
        check("…y no creó ni tocó ninguna base de datos", not db.exists())
        db1 = Path(d) / "seed_sin_permiso.db"
        rc, out, err = correr(ejecutar, {"DATABASE_URL": f"sqlite+aiosqlite:///{db1.as_posix()}", **claves}, timeout=120)
        check(f"sin ALLOW_DEV_SEED el seed se niega (rc={rc})", rc == 1 and "ALLOW_DEV_SEED" in out and not db1.exists())
        db2 = Path(d) / "seed_sin_claves.db"
        rc, out, err = correr(ejecutar, {"DATABASE_URL": f"sqlite+aiosqlite:///{db2.as_posix()}", "ALLOW_DEV_SEED": "true"}, timeout=120)
        check(f"con permiso pero SIN contraseñas el seed falla con mensaje claro y sin base (rc={rc})",
              rc == 1 and all(n in out for n in claves) and not db2.exists())
        rc, out, err = correr(ejecutar, {"DATABASE_URL": f"sqlite+aiosqlite:///{db2.as_posix()}", "ALLOW_DEV_SEED": "true",
                                         "SEED_ADMIN_PASSWORD": claves["SEED_ADMIN_PASSWORD"]}, timeout=120)
        check("…también si falta solo una de las tres", rc == 1 and "SEED_TEACHER_PASSWORD" in out and "SEED_ADMIN_PASSWORD," not in out)
        db3 = Path(d) / "seed_dev.db"
        rc, out, err = correr(
            ejecutar + "\n"
            "import sqlite3; from app.core.security import verify_password\n"
            f"h = sqlite3.connect(r'{db3}').execute(\"select password_hash from users where email='admin@dorismon.do'\").fetchone()[0]\n"
            "print('ADMIN_OK', verify_password('" + claves["SEED_ADMIN_PASSWORD"] + "', h))",
            {"DATABASE_URL": f"sqlite+aiosqlite:///{db3.as_posix()}", "ALLOW_DEV_SEED": "true", **claves}, timeout=300)
        check("en desarrollo, con permiso y claves propias, el seed funciona y usa SEED_ADMIN_PASSWORD", rc == 0 and "ADMIN_OK True" in out)
        check("…y NO imprime ninguna contraseña", not any(v in out + err for v in claves.values()))
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    patron = re.compile(r"(?:DorismonAdmin|Profe|Estudiante)2026")
    check("el README no contiene contraseñas (solo placeholders)", not patron.search(readme) and "<SEED_ADMIN_PASSWORD>" in readme)
    seed = (ROOT / "seed.py").read_text(encoding="utf-8")
    check("seed.py no contiene contraseñas literales: solo las lee del entorno y exige ALLOW_DEV_SEED",
          not patron.search(seed) and 'os.getenv("SEED_ADMIN_PASSWORD") or ""' in seed and "ALLOW_DEV_SEED" in seed)
    sa = (ROOT / "app" / "services" / "seguridad_arranque.py").read_text(encoding="utf-8")
    check("el código de producción no conserva contraseñas históricas", not patron.search(sa) and "verify_password" not in sa)
    ocurrencias = [str(f.relative_to(ROOT)) for f in ROOT.rglob("*") if f.is_file() and f.suffix in (".py", ".md", ".txt", ".yaml", ".yml", ".json", ".example")
                   and "__pycache__" not in f.parts and patron.search(f.read_text(encoding="utf-8", errors="ignore"))]
    check(f"ningún archivo del backend candidato contiene contraseñas históricas ({len(ocurrencias)})", not ocurrencias)


async def parte_credenciales(check):
    print("\n[1C] Aviso de arranque (producción): cuentas con correo del seed")
    import logging
    import app.services.seguridad_arranque as sa
    from app.core.db import SessionLocal, engine
    from app.core.security import hash_password
    from app.models import Base, User, UserRole
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    temp = []
    original = sa.CORREOS_DEL_SEED
    try:
        async with SessionLocal() as db:
            for k, activo in (("a", True), ("b", False)):
                u = User(email=f"p0.{k}.{uuid.uuid4().hex[:6]}@p0.dorismon.do", password_hash=hash_password("Clave-Propia-P0-77!"), full_name=f"P0 {k}",
                         role=UserRole.student, email_verified=True, is_active=activo)
                db.add(u); await db.flush(); temp.append(u)
            await db.commit()
        sa.CORREOS_DEL_SEED = (temp[0].email, temp[1].email)
        async with SessionLocal() as db:
            presentes = await sa.cuentas_del_seed_existentes(db)
        check("detecta SOLO la cuenta activa con correo del seed (la desactivada, no)", presentes == [temp[0].email])
        registros = []

        class H(logging.Handler):
            def emit(self, r):
                registros.append(r)
        h = H(); sa.log.addHandler(h); sa.log.setLevel(logging.DEBUG)
        await sa.auditar_cuentas_del_seed()
        sa.log.removeHandler(h)
        check("deja un aviso CRÍTICO en los logs, sin lanzar ni bloquear nada", any(r.levelno == logging.CRITICAL and temp[0].email in r.getMessage() for r in registros))
        check("el módulo ya no contiene ninguna función que verifique contraseñas", not hasattr(sa, "CUENTAS_DEL_SEED") and not hasattr(sa, "cuentas_con_contrasena_por_defecto"))
    finally:
        sa.CORREOS_DEL_SEED = original
        async with SessionLocal() as db:
            from sqlalchemy import delete
            await db.execute(delete(User).where(User.id.in_([u.id for u in temp])))
            await db.commit()


# ──────────────────────────────────────────────────────────────────────────────────────────────
async def parte_pagos(check):
    print("\n[1D] Aprobación de pago concurrente")
    import datetime as dt
    import httpx
    from sqlalchemy import select, func, delete
    from app.main import app
    from app.core.db import SessionLocal, engine
    from app.core.security import hash_password
    from app.models import (Base, User, UserRole, Student, Plan, Course, Level, PaymentProof, PaymentProofStatus, Enrollment,
                            Payment, Notification, AuditLog)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    creados = {"users": [], "proofs": []}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=120) as c:
        r = await c.post("/auth/login", json={"email": "admin@dorismon.do", "password": "Adm-Test-4821!"})
        AH = {"Authorization": "Bearer " + r.json()["access_token"]}
        async with SessionLocal() as db:
            plan = (await db.execute(select(Plan).limit(1))).scalar_one()
            curso = (await db.execute(select(Course).limit(1))).scalar_one()
            nivel = (await db.execute(select(Level).limit(1))).scalar_one()

        async def nueva_prueba(k):
            async with SessionLocal() as db:
                u = User(email=f"p0.pago.{k}.{uuid.uuid4().hex[:6]}@p0.dorismon.do", password_hash=hash_password("Solo-Pruebas-9!"),
                         full_name=f"P0 pago {k}", role=UserRole.student, email_verified=True, is_active=True)
                db.add(u); await db.flush(); db.add(Student(user_id=u.id)); await db.flush()   # FK students antes que la prueba (PostgreSQL)
                p = PaymentProof(student_id=u.id, plan_id=plan.id, course_id=curso.id, level_id=nivel.id, amount=1500, payment_date=dt.date.today(),
                                 reference_number=f"P0-{uuid.uuid4().hex[:10]}", voucher_url="https://ejemplo.invalid/v.png")
                db.add(p); await db.commit()
                creados["users"].append(u.id); creados["proofs"].append(p.id)
                return u.id, p.id, p.reference_number

        async def estado(uid, pid, ref):
            async with SessionLocal() as db:
                q = lambda m, *w: db.execute(select(func.count()).select_from(m).where(*w))
                p = await db.get(PaymentProof, pid)
                return {"estado": p.status.value, "inscripciones": (await q(Enrollment, Enrollment.student_id == uid)).scalar(),
                        "pagos": (await q(Payment, Payment.reference == ref)).scalar(),
                        "avisos_aprobado": (await q(Notification, Notification.user_id == uid, Notification.title.like("%Pago aprobado%"))).scalar(),
                        "avisos_rechazo": (await q(Notification, Notification.user_id == uid, Notification.title.like("%Pago rechazado%"))).scalar(),
                        "aud_aprobar": (await q(AuditLog, AuditLog.action == "approve_payment_proof", AuditLog.target_id == pid)).scalar(),
                        "aud_rechazar": (await q(AuditLog, AuditLog.action == "reject_payment_proof", AuditLog.target_id == pid)).scalar(),
                        "enrollment_id": p.enrollment_id}
        try:
            uid, pid, ref = await nueva_prueba("a")
            rs = await asyncio.gather(*[c.post(f"/admin/payment-proofs/{pid}/approve", headers=AH, json={"admin_notes": "p0"}) for _ in range(10)])
            cods = sorted(r_.status_code for r_ in rs)
            e = await estado(uid, pid, ref)
            check(f"10 aprobaciones a la vez: UNA efectiva (200) y el resto 400 ({cods.count(200)}×200, {cods.count(400)}×400, otros {len(cods) - cods.count(200) - cods.count(400)})",
                  cods.count(200) == 1 and cods.count(400) == 9)
            check(f"…una sola inscripción, un solo pago, un solo aviso y una sola auditoría ({e['inscripciones']}/{e['pagos']}/{e['avisos_aprobado']}/{e['aud_aprobar']})",
                  (e["inscripciones"], e["pagos"], e["avisos_aprobado"], e["aud_aprobar"]) == (1, 1, 1, 1) and e["estado"] == "approved")
            ganadora = next(r_ for r_ in rs if r_.status_code == 200).json()
            check("…y la inscripción de la respuesta ganadora es la que quedó enlazada en la prueba", ganadora.get("enrollment_id") == e["enrollment_id"])
            r1 = await c.post(f"/admin/payment-proofs/{pid}/approve", headers=AH, json={})
            r2 = await c.post(f"/admin/payment-proofs/{pid}/reject", headers=AH, json={"reason": "motivo largo suficiente"})
            e2 = await estado(uid, pid, ref)
            check(f"aprobar o rechazar DESPUÉS de aprobada → 400 y nada cambia ({r1.status_code}/{r2.status_code})",
                  (r1.status_code, r2.status_code) == (400, 400) and (e2["inscripciones"], e2["pagos"], e2["avisos_aprobado"], e2["avisos_rechazo"]) == (1, 1, 1, 0))

            uid, pid, ref = await nueva_prueba("b")
            tareas = [c.post(f"/admin/payment-proofs/{pid}/approve", headers=AH, json={}) if i % 2 == 0 else
                      c.post(f"/admin/payment-proofs/{pid}/reject", headers=AH, json={"reason": "motivo largo suficiente"}) for i in range(10)]
            rs = await asyncio.gather(*tareas)
            e = await estado(uid, pid, ref)
            ok200 = sum(1 for r_ in rs if r_.status_code == 200)
            check(f"5 aprobar + 5 rechazar a la vez: UN solo ganador ({ok200}×200)", ok200 == 1 and all(r_.status_code in (200, 400) for r_ in rs))
            if e["estado"] == "approved":
                coherente = (e["inscripciones"], e["pagos"], e["avisos_aprobado"], e["avisos_rechazo"], e["aud_aprobar"], e["aud_rechazar"]) == (1, 1, 1, 0, 1, 0)
            else:
                coherente = (e["inscripciones"], e["pagos"], e["avisos_aprobado"], e["avisos_rechazo"], e["aud_aprobar"], e["aud_rechazar"]) == (0, 0, 0, 1, 0, 1)
            check(f"…y el estado final es coherente con el ganador ({e['estado']}: {e['inscripciones']} insc., {e['pagos']} pagos, avisos {e['avisos_aprobado']}/{e['avisos_rechazo']}, auditorías {e['aud_aprobar']}/{e['aud_rechazar']})", coherente)

            print("  · el email se envía DESPUÉS del commit y es best-effort")
            import app.services.email_service as es
            from sqlalchemy.ext.asyncio import AsyncSession
            orig = (es.is_email_configured, es.send_email, AsyncSession.commit)
            enviados = []
            modo = {"falla": False}

            async def falso_envio(to, subject, html, **kw):
                async with SessionLocal() as db2:   # sesión independiente: ¿ya está confirmado lo que el email anuncia?
                    pr = await db2.get(PaymentProof, modo["pid"])
                    enviados.append((to, subject, pr.status.value))
                if modo["falla"]:
                    raise RuntimeError("SMTP caído simulado")
                return True
            es.is_email_configured = lambda: True
            es.send_email = falso_envio
            try:
                for accion, k, falla in (("approve", "c", False), ("reject", "d", False), ("approve", "e", True), ("reject", "f", True)):
                    uid, pid, ref = await nueva_prueba(k)
                    modo.update(pid=pid, falla=falla)
                    enviados.clear()
                    cuerpo = {} if accion == "approve" else {"reason": "motivo largo suficiente"}
                    r = await c.post(f"/admin/payment-proofs/{pid}/{accion}", headers=AH, json=cuerpo)
                    esperado = "approved" if accion == "approve" else "rejected"
                    e = await estado(uid, pid, ref)
                    check(f"{accion}{' con SMTP caído' if falla else ''}: 200, queda {esperado} y el email se intentó una vez ({r.status_code}, {len(enviados)})",
                          r.status_code == 200 and e["estado"] == esperado and len(enviados) == 1)
                    check(f"…y cuando el email salió, la decisión YA estaba confirmada en la base ({enviados[0][2] if enviados else None})",
                          bool(enviados) and enviados[0][2] == esperado)
                # el commit falla → NO sale ningún email
                uid, pid, ref = await nueva_prueba("g")
                modo.update(pid=pid, falla=False)
                enviados.clear()

                async def commit_roto(self):
                    raise RuntimeError("commit simulado roto")
                AsyncSession.commit = commit_roto
                try:
                    try:
                        r = await c.post(f"/admin/payment-proofs/{pid}/approve", headers=AH, json={})
                        codigo = r.status_code
                    except Exception:
                        codigo = 500
                finally:
                    AsyncSession.commit = orig[2]
                e = await estado(uid, pid, ref)
                check(f"si el commit falla NO se envía ningún email y la prueba sigue pendiente ({codigo}, emails={len(enviados)}, estado={e['estado']})",
                      codigo >= 500 and not enviados and e["estado"] == "pending" and e["inscripciones"] == 0)
            finally:
                es.is_email_configured, es.send_email, AsyncSession.commit = orig
        finally:
            async with SessionLocal() as db:
                uids = creados["users"]
                await db.execute(delete(PaymentProof).where(PaymentProof.id.in_(creados["proofs"])))
                await db.execute(delete(Payment).where(Payment.student_id.in_(uids)))
                await db.execute(delete(Enrollment).where(Enrollment.student_id.in_(uids)))
                await db.execute(delete(Notification).where(Notification.user_id.in_(uids)))
                await db.execute(delete(AuditLog).where(AuditLog.target_id.in_(creados["proofs"])))
                await db.execute(delete(Student).where(Student.user_id.in_(uids)))
                await db.execute(delete(User).where(User.id.in_(uids)))
                await db.commit()


async def parte_destructivo(check):
    print("\n[1E] Endpoint destructivo")
    import httpx
    from sqlalchemy import select, func
    from app.main import app
    from app.core.db import SessionLocal
    from app.models import ClassSession
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=60) as c:
        r = await c.post("/auth/login", json={"email": "admin@dorismon.do", "password": "Adm-Test-4821!"})
        AH = {"Authorization": "Bearer " + r.json()["access_token"]}
        U = "/admin/maintenance/clean-operational-data"
        quitar = ("RENDER", "ENVIRONMENT", "ALLOW_DESTRUCTIVE_MAINTENANCE")
        guardado = {k: os.environ.pop(k, None) for k in quitar}

        async def clases():
            async with SessionLocal() as db:
                return (await db.execute(select(func.count()).select_from(ClassSession))).scalar()
        try:
            n0 = await clases()
            os.environ["RENDER"] = "true"
            r = await c.post(U, headers=AH, json={"dry_run": True})
            check(f"producción: la SIMULACIÓN (dry_run) sigue disponible y no borra ({r.status_code})", r.status_code == 200 and r.json().get("dry_run") is True)
            r = await c.post(U, headers=AH, json={"dry_run": False, "confirm": "BORRAR DATOS DE PRUEBA"})
            check(f"producción: el borrado real con la frase de confirmación correcta → 403 ({r.status_code})", r.status_code == 403 and "deshabilitado" in r.text)
            check("…y no se borró nada (clases intactas)", await clases() == n0)
            r = await c.post(U, headers=AH, json={"dry_run": False})
            check(f"producción: sin frase → 403 también (la puerta está ANTES de la confirmación) ({r.status_code})", r.status_code == 403)
            os.environ["ALLOW_DESTRUCTIVE_MAINTENANCE"] = "true"
            r = await c.post(U, headers=AH, json={"dry_run": False, "confirm": "frase equivocada"})
            check(f"producción con la habilitación explícita: se llega a la confirmación (400, NO se borra) ({r.status_code})", r.status_code == 400 and await clases() == n0)
            os.environ.pop("ALLOW_DESTRUCTIVE_MAINTENANCE"); os.environ.pop("RENDER")
            r = await c.post(U, headers=AH, json={"dry_run": False, "confirm": "frase equivocada"})
            check(f"desarrollo: sin cambios (400 por la confirmación, no 403) ({r.status_code})", r.status_code == 400)
            r = await c.post(U, headers={}, json={"dry_run": True})
            check(f"sin sesión → 401/403 ({r.status_code})", r.status_code in (401, 403))
        finally:
            for k, v in guardado.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v


def parte_cors(check):
    print("\n[1F] CORS")
    cod = (
        "import asyncio, json, httpx\n"
        "from app.main import app\n"
        "async def m():\n"
        "    out = {}\n"
        "    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:\n"
        "        for o in ['http://localhost:3000','https://dorismon.com','https://www.dorismon.com','https://evil.example','null',\n"
        "                  'https://dorismon-web-abc123.vercel.app','https://evil-dorismon-web-abc123.vercel.app','https://dorismon-web-abc123.vercel.app.evil.com']:\n"
        "            r = await c.options('/health', headers={'Origin': o, 'Access-Control-Request-Method': 'POST', 'Access-Control-Request-Headers': 'authorization,content-type'})\n"
        "            out[o] = [r.headers.get('access-control-allow-origin'), r.headers.get('access-control-allow-credentials'), r.status_code]\n"
        "    print('JSON' + json.dumps(out))\n"
        "asyncio.run(m())\n"
    )

    def sondeo(extra):
        rc, out, err = correr(cod, dict({"DATABASE_URL": "sqlite+aiosqlite:///:memory:"}, **extra))
        m = re.search(r"JSON(\{.*\})", out)
        return json.loads(m.group(1)) if m else {"_error": err[-200:]}
    dev = sondeo({"SECRET_KEY": "solo-pruebas-locales-0123456789"})
    check("desarrollo: localhost y los dominios de Dorismon permitidos; orígenes ajenos, no",
          dev["http://localhost:3000"][0] == "http://localhost:3000" and dev["https://dorismon.com"][0] == "https://dorismon.com"
          and dev["https://evil.example"][0] is None and dev["null"][0] is None)
    prod = sondeo({"RENDER": "true", "SECRET_KEY": CLAVE_FUERTE})
    check("producción: SIN localhost; solo dorismon.com / www / el proyecto de Vercel",
          prod["http://localhost:3000"][0] is None and prod["https://dorismon.com"][0] == "https://dorismon.com"
          and prod["https://www.dorismon.com"][0] == "https://www.dorismon.com")
    check("producción: orígenes ajenos y 'null' → 400 'Disallowed CORS origin' y SIN Access-Control-Allow-Origin",
          prod["https://evil.example"][0] is None and prod["null"][0] is None and prod["https://evil.example"][2] == prod["null"][2] == 400)
    check("producción: los previews de Vercel NO están abiertos por defecto", prod["https://dorismon-web-abc123.vercel.app"][0] is None)
    check("producción: un origen permitido recibe credenciales y NUNCA el comodín '*'",
          prod["https://dorismon.com"][1] == "true" and all(v[0] != "*" for v in prod.values()))
    comodin = sondeo({"RENDER": "true", "SECRET_KEY": CLAVE_FUERTE, "CORS_ORIGINS": "*"})
    check("producción con CORS_ORIGINS='*': no se abre a todo el mundo", all(v[0] != "*" for v in comodin.values()) and comodin["https://evil.example"][0] is None)
    lista = sondeo({"RENDER": "true", "SECRET_KEY": CLAVE_FUERTE, "CORS_ORIGINS": "https://app.dorismon.com"})
    check("CORS_ORIGINS explícito sustituye la lista (solo ese origen)", lista["https://dorismon.com"][0] is None)
    prev = sondeo({"RENDER": "true", "SECRET_KEY": CLAVE_FUERTE, "ALLOW_VERCEL_PREVIEWS": "true"})
    check("ALLOW_VERCEL_PREVIEWS=true: solo previews del proyecto (dorismon-web-*), no imitaciones",
          prev["https://dorismon-web-abc123.vercel.app"][0] == "https://dorismon-web-abc123.vercel.app"
          and prev["https://evil-dorismon-web-abc123.vercel.app"][0] is None and prev["https://dorismon-web-abc123.vercel.app.evil.com"][0] is None)


def parte_estatica(check):
    print("\n[ESTÁTICO]")
    auth = (ROOT / "app/routers/auth.py").read_text(encoding="utf-8")
    check("auth.py: el login usa claves_login() (IP validada) y registra también el límite por IP",
          "claves_login(request, body.email)" in auth and "request.client.host" not in auth and "IP_MAX_ATTEMPTS" in auth)
    adm = (ROOT / "app/routers/admin.py").read_text(encoding="utf-8")
    check("admin.py: aprobar y rechazar reclaman el estado de forma ATÓMICA (UPDATE … WHERE status = pending)",
          adm.count("PaymentProof.status == PaymentProofStatus.pending)") >= 2 and adm.count("reclamo.rowcount != 1") == 2)
    check("admin.py: el borrado operativo comprueba is_production() ANTES de la confirmación",
          adm.index("ALLOW_DESTRUCTIVE_MAINTENANCE") < adm.index('if confirm != "BORRAR DATOS DE PRUEBA":'))
    check(".env.example conserva un valor de ejemplo que producción rechaza", "cambiar" in (ROOT / ".env.example").read_text(encoding="utf-8").lower())


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}", flush=True)

    secciones = [("limitador", lambda: parte_limitador(check)), ("ip", lambda: parte_ip(check)),
                 ("login_http", lambda: parte_login_http(check)), ("config", lambda: parte_config(check)),
                 ("seed", lambda: parte_seed(check)), ("credenciales", lambda: parte_credenciales(check)),
                 ("pagos", lambda: parte_pagos(check)), ("destructivo", lambda: parte_destructivo(check)),
                 ("cors", lambda: parte_cors(check)), ("estatico", lambda: parte_estatica(check))]
    for nombre, f in secciones:
        try:
            r = f()
            if asyncio.iscoroutine(r):
                await r
        except Exception as e:
            check(f"sección «{nombre}» terminó sin errores ({type(e).__name__}: {str(e)[:150]})", False)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
