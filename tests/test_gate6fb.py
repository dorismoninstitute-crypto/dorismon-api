"""GATE 6F-B — Contenido educativo de la clase (vocabulario, frase, nota, enlace).

[INTEGRACIÓN] App EN ESTE PROCESO (sin LiveKit: el contenido no depende de la sala).
  · autoría: profesor de la clase y Dirección sí; estudiante y profesor ajeno no;
  · tipos y límites; texto tal cual (acentos, francés, sin traducir); XSS como texto literal;
    esquemas peligrosos rechazados (javascript:, data:, vbscript:, //host, credenciales…);
  · editar sin cambiar el orden ni crear otro; ocultar / mostrar sin borrar; auditoría;
  · quien entra tarde / reconecta / recupera: GET completo y en orden (fuente: la base);
  · histórico: foto acumulativa por clase; inscrito DESPUÉS de la clase → no; salir del grupo
    después → conserva; cuenta desactivada → no; durante la clase quien entra tarde → sí;
  · clase finalizada: no se publica (sí se corrige); cancelada: nada; IDOR entre clases;
  · rate limit; 10 publicaciones a la vez sin 500 y con posiciones únicas; sin notificaciones.
[ESTÁTICO] frontend: texto plano (sin dangerouslySetInnerHTML), enlaces seguros, señal por
  paquete de datos + base como fuente de verdad, indicador en mini, sin localStorage.

Uso: python tests/test_gate6fb.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import datetime as dt
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import test_gate6ha as base  # noqa: E402

WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
JUANA = {"email": "juana.estudiante@dorismon.do", "password": "Estu-Test-4823!"}


async def integracion(check):
    print("\n[INTEGRACIÓN] App en proceso")
    try:
        from app.main import app
        from app.core.db import engine
        from app.models import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        check(f"la app se importa ({type(e).__name__}: {e})", False)
        return
    cx = sqlite3.connect(str(Path(base.DB_URL.split("///", 1)[-1])), timeout=30)
    creadas, borrar = [], []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(cred):
            return {"Authorization": f"Bearer {(await c.post('/auth/login', json=cred)).json()['access_token']}"}
        AH, TH, OH, MH, JH, CH = [await login(x) for x in (base.ADMIN, base.TEACHER, base.OTRO_PROFE, base.MARIA, JUANA, base.CARLOS)]
        ids = {k: (await c.get("/auth/me", headers=h)).json()["id"]
               for k, h in (("prof", TH), ("otro", OH), ("maria", MH), ("juana", JH), ("carlos", CH))}
        now = dt.datetime.now(dt.timezone.utc)

        async def evento(nombre, profe=None, **extra):
            body = {"title": nombre, "teacher_id": profe or ids["prof"], "modality": "online", "video_provider": "dorismon",
                    "starts_at_utc": (now - dt.timedelta(minutes=5)).isoformat(),
                    "ends_at_utc": (now + dt.timedelta(minutes=55)).isoformat()}
            body.update(extra)
            r = await c.post("/admin/events", headers=AH, json=body)
            i = r.json().get("id") if r.status_code in (200, 201) else None
            if i:
                creadas.append(i)
            return i

        GET = lambda h, sid: c.get(f"/video/sessions/{sid}/contenido", headers=h)
        POST = lambda h, sid, **b: c.post(f"/video/sessions/{sid}/contenido", headers=h, json=b)
        PATCH = lambda h, sid, cid, **b: c.patch(f"/video/sessions/{sid}/contenido/{cid}", headers=h, json=b)
        try:
            cols = [r[1] for r in cx.execute("pragma table_info(enrollments)")]
            orig = {k: dict(zip(cols, cx.execute("select * from enrollments where student_id=? and is_active=1",
                                                 (ids[k],)).fetchone())) for k in ("maria", "juana", "carlos")}
            serie = str(uuid.uuid4())
            cx.execute("insert into class_series (id, name, course_id, level_id, teacher_id, days_of_week, start_time_hhmm,"
                       " duration_min, start_date, modality, capacity, is_active) values (?,?,?,?,?,?,?,?,date('now'),?,?,1)",
                       (serie, "G6FB grupo de prueba", orig["maria"]["course_id"], orig["maria"]["level_id"], ids["prof"],
                        "mon", "10:00", 60, "online", 10))
            borrar.append(("class_series", serie))
            insc = {}

            def inscribir(k):
                fila = dict(orig[k], id=str(uuid.uuid4()), series_id=serie, previous_enrollment_id=None)
                cx.execute(f"insert into enrollments ({','.join(cols)}) values ({','.join('?' * len(cols))})", [fila[x] for x in cols])
                cx.commit()
                insc[k] = fila["id"]
                borrar.insert(0, ("enrollments", fila["id"]))
            inscribir("maria")
            H, K, R, X, Y, M = (await evento("G6FB Past simple"), await evento("G6FB entra tarde"), await evento("G6FB ráfaga"),
                                await evento("G6FB cancelada"), await evento("G6FB ajena", ids["otro"]),
                                await evento("G6FB Meet", video_provider="meet", meeting_url="https://meet.google.com/abc-defg-hij"))
            for sid in (H, K, R, X):
                cx.execute("update class_sessions set series_id=?, is_open_event=0, student_id=NULL where id=?", (serie, sid))
            # Y: clase de OTRO profesor en un grupo sin estudiantes (no es un evento abierto)
            serie_y = str(uuid.uuid4())
            cx.execute("insert into class_series (id, name, course_id, level_id, teacher_id, days_of_week, start_time_hhmm,"
                       " duration_min, start_date, modality, capacity, is_active) values (?,?,?,?,?,?,?,?,date('now'),?,?,1)",
                       (serie_y, "G6FB grupo ajeno", orig["maria"]["course_id"], orig["maria"]["level_id"], ids["otro"],
                        "tue", "10:00", 60, "online", 10))
            borrar.append(("class_series", serie_y))
            cx.execute("update class_sessions set series_id=?, is_open_event=0, student_id=NULL where id=?", (serie_y, Y))
            cx.commit()
            check("se preparan las clases (grupo con María)", all((H, K, R, X, Y, M)))
            notif0 = cx.execute("select count(*) from notifications").fetchone()[0]

            print("\n  · Publicar (profesor de la clase)")
            try:
                r1 = await POST(TH, H, tipo="vocabulario", texto="acheivement", traduccion="logro",
                                ejemplo="Finishing the course was a big achievement.")
                r2 = await POST(TH, H, tipo="vocabulario", texto="réussite", traduccion="success", ejemplo=None)
                r3 = await POST(TH, H, tipo="vocabulario", texto="to look forward to")
                r4 = await POST(TH, H, tipo="frase", texto="How long have you lived here?")
                r5 = await POST(TH, H, tipo="nota", texto="Remember: use “did” + base verb.\nNOT: did went ✗")
                r6 = await POST(TH, H, tipo="enlace", texto="British Council exercise",
                                url="https://learnenglish.britishcouncil.org/grammar/a1-a2-grammar/past-simple")
                check("vocabulario (con traducción y ejemplo, solo traducción, solo palabra), frase, nota y enlace → 200",
                      all(r.status_code == 200 for r in (r1, r2, r3, r4, r5, r6)))
                it = [r.json()["item"] for r in (r1, r2, r3, r4, r5, r6)]
                check("texto EXACTO del profesor (acentos, francés, comillas, salto de línea), sin traducir",
                      it[1]["texto"] == "réussite" and it[1]["traduccion"] == "success" and it[2]["traduccion"] is None
                      and it[4]["texto"] == "Remember: use “did” + base verb.\nNOT: did went ✗")
                check("posiciones cronológicas 1..6", [i["posicion"] for i in it] == [1, 2, 3, 4, 5, 6])
                ra = await POST(AH, H, tipo="nota", texto="Nota de Dirección")
                check(f"Dirección también publica ({ra.status_code})", ra.status_code == 200)
                check("sin notificaciones por cada palabra (tabla notifications sin cambios)",
                      cx.execute("select count(*) from notifications").fetchone()[0] == notif0)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Publicar (profesor de la clase)» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Quién NO publica")
            try:
                rm = await POST(MH, H, tipo="nota", texto="soy estudiante")
                check(f"estudiante → {rm.status_code}", rm.status_code == 403)
                ro = await POST(OH, H, tipo="nota", texto="soy otro profesor")
                check(f"profesor ajeno → {ro.status_code}", ro.status_code == 403)
                check("clase de Meet (externa) → 400", (await POST(TH, M, tipo="nota", texto="x")).status_code == 400)
                check("clase inexistente → 404", (await POST(TH, "no-existe", tipo="nota", texto="x")).status_code == 404)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Quién NO publica» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Validación y seguridad")
            try:
                xss = '<script>alert(1)</script><img src=x onerror=alert(1)> <a href="javascript:alert(1)">x</a>'
                rx = await POST(TH, H, tipo="nota", texto=xss)
                check("HTML/JS en una nota se guarda y devuelve como TEXTO literal (el frontend no lo interpreta)",
                      rx.status_code == 200 and rx.json()["item"]["texto"] == xss)
                malos = ["javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "data:text/html,<script>alert(1)</script>",
                         "vbscript:msgbox(1)", "file:///etc/passwd", "ftp://example.com/x", "//evil.example/x", "https://",
                         "https://user:pass@example.com/", "http://exa mple.com", "https://ejemplo.com/\u0000x", "https://good.example\u0000.evil.example/",
                         "https://good.example\u200b.evil.example/", "mailto:a@b.c"]
                res = {u: (await POST(TH, H, tipo="enlace", url=u)).status_code for u in malos}
                check(f"esquemas / URLs peligrosas → 400 ({sorted(set(res.values()))})", all(v == 400 for v in res.values()))
                check("enlace http:// también válido", (await POST(TH, H, tipo="enlace", url="http://example.com/a?b=1#c")).status_code == 200)
                casos = {
                    "vocabulario de 121 caracteres": dict(tipo="vocabulario", texto="a" * 121),
                    "nota de 501 caracteres": dict(tipo="nota", texto="n" * 501),
                    "frase de 301 caracteres": dict(tipo="frase", texto="f" * 301),
                    "vocabulario sin texto": dict(tipo="vocabulario", texto="   "),
                    "enlace sin URL": dict(tipo="enlace", texto="solo título"),
                    "URL en un vocabulario": dict(tipo="vocabulario", texto="x", url="https://example.com"),
                    "ejemplo en una nota": dict(tipo="nota", texto="x", ejemplo="y"),
                    "tipo inventado": dict(tipo="html", texto="x"),
                    "texto que no es texto": dict(tipo="nota", texto={"a": 1}),
                }
                res = {k: (await POST(TH, H, **v)).status_code for k, v in casos.items()}
                check(f"límites y campos por tipo → 400 ({[k for k, v in res.items() if v != 400] or 'todos'})",
                      all(v == 400 for v in res.values()))
                rc = await POST(TH, H, tipo="vocabulario", texto="  bonjour​\u0007  ", traduccion="hola\tamigo")
                check("caracteres de control eliminados y espacios normalizados",
                      rc.status_code == 200 and rc.json()["item"]["texto"] == "bonjour" and rc.json()["item"]["traduccion"] == "hola amigo")
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Validación y seguridad» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Editar y ocultar")
            try:
                cid = it[0]["id"]
                re1 = await PATCH(TH, H, cid, texto="achievement")
                g = (await GET(TH, H)).json()["items"]
                mismo = [i for i in g if i["id"] == cid]
                check("corrige «acheivement» → «achievement»: MISMO recurso, misma posición, marcado editado",
                      re1.status_code == 200 and mismo and mismo[0]["texto"] == "achievement" and mismo[0]["posicion"] == 1
                      and mismo[0]["editado"] is True and g[0]["id"] == cid)
                check("al editar no se tocan los campos no enviados", mismo[0]["traduccion"] == "logro")
                check("editar valida igual (vocabulario de 121 → 400; vaciar el texto → 400)",
                      (await PATCH(TH, H, cid, texto="b" * 121)).status_code == 400 and (await PATCH(TH, H, cid, texto="")).status_code == 400)
                check("no se puede cambiar el tipo ni poner URL a un vocabulario",
                      (await PATCH(TH, H, cid, url="https://x.com")).status_code == 400)
                idx = it[5]["id"]
                await PATCH(TH, H, idx, oculto=True)
                vm = [i["id"] for i in (await GET(MH, H)).json()["items"]]
                vt = {i["id"]: i for i in (await GET(TH, H)).json()["items"]}
                check("ocultar: el estudiante deja de verlo; el profesor lo ve marcado oculto; NO se borra",
                      idx not in vm and vt[idx]["oculto"] is True
                      and cx.execute("select count(*) from class_session_content where id=?", (idx,)).fetchone()[0] == 1)
                await PATCH(TH, H, idx, oculto=False)
                vm2 = (await GET(MH, H)).json()["items"]
                check("mostrar de nuevo: vuelve en SU posición", [i["id"] for i in vm2].index(idx) == 5)
                acciones = {a for (a,) in cx.execute("select action from audit_logs where target_id=?", (H,))}
                check("auditoría: crear, editar, ocultar, mostrar", {"class_content_create", "class_content_edit",
                                                                    "class_content_hide", "class_content_show"} <= acciones)
                check(f"estudiante no edita ({(await PATCH(MH, H, cid, texto='x')).status_code}) ni profesor ajeno ({(await PATCH(OH, H, cid, texto='x')).status_code})",
                      (await PATCH(MH, H, cid, texto="x")).status_code == 403 and (await PATCH(OH, H, cid, texto="x")).status_code == 403)
                ry = await POST(OH, Y, tipo="nota", texto="nota de la clase ajena")
                cy = ry.json()["item"]["id"]
                check("IDOR: editar el contenido de OTRA clase con mi session_id → 404",
                      (await PATCH(TH, H, cy, texto="hackeado")).status_code == 404
                      and cx.execute("select texto from class_session_content where id=?", (cy,)).fetchone()[0] == "nota de la clase ajena")
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Editar y ocultar» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Estudiantes: autorizada, ajeno, entra tarde, reconecta")
            try:
                gm = (await GET(MH, H)).json()
                check(f"María (de la clase) lee todo en orden ({len(gm['items'])}) y no puede publicar",
                      [i["posicion"] for i in gm["items"]] == sorted(i["posicion"] for i in gm["items"]) and len(gm["items"]) >= 8
                      and gm["puede_publicar"] is False and "oculto" not in gm["items"][0])
                check("Carlos (no es de la clase) → 403", (await GET(CH, H)).status_code == 403)
                check("estudiante leyendo contenido de una clase ajena → 403", (await GET(MH, Y)).status_code == 403)
                await POST(TH, K, tipo="vocabulario", texto="late")
                check("Juana fuera del grupo de la clase K → 403", (await GET(JH, K)).status_code == 403)
                inscribir("juana")                         # entra al grupo DURANTE la clase
                gj = await GET(JH, K)
                check("…se inscribe durante la clase y entra tarde → recibe todo (queda en la foto)",
                      gj.status_code == 200 and [i["texto"] for i in gj.json()["items"]] == ["late"]
                      and cx.execute("select reason from class_session_audience where session_id=? and user_id=?", (K, ids["juana"])).fetchone() is not None)
                check("reconectar / recargar / recuperar (6B / 6E) = mismo GET → mismo contenido",
                      (await GET(MH, H)).json()["items"] == gm["items"])
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Estudiantes: autorizada, ajeno, entra tarde, reconecta» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Concurrencia y rate limit")
            try:
                rs = await asyncio.gather(*[POST(TH, R, tipo="vocabulario", texto=f"palabra {i}") for i in range(10)])
                pos = [r.json()["item"]["posicion"] for r in rs if r.status_code == 200]
                check(f"10 publicaciones a la vez → {sorted({r.status_code for r in rs})}, posiciones únicas 1..10",
                      all(r.status_code == 200 for r in rs) and sorted(pos) == list(range(1, 11)))
                import time as _t
                t0 = _t.monotonic()
                rs70 = [await POST(TH, R, tipo="vocabulario", texto=f"script {i}") for i in range(70)]
                segundos = _t.monotonic() - t0
                cods = [r.status_code for r in rs70]
                n_r = cx.execute("select count(*) from class_session_content where session_id=?", (R,)).fetchone()[0]
                # Invariante del token bucket (60 de golpe, luego 1/s), sin depender de la velocidad de la máquina:
                # lo aceptado en total no supera 60 + lo repuesto (+3 de margen: la ráfaga previa de 10 también repone)
                check(f"script de 70 más en {segundos:.1f} s → {cods.count(429)}×429 con Retry-After; aceptadas {n_r} ≤ 60 + {int(segundos) + 3} (sin límite serían 80)",
                      cods.count(429) >= 1 and all(r.headers.get("retry-after") for r in rs70 if r.status_code == 429)
                      and n_r <= 60 + int(segundos) + 3 and set(cods) <= {200, 429})
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Concurrencia y rate limit» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Después de la clase (histórico)")
            try:
                rf = await c.post(f"/teacher/sessions/{H}/finalize", headers=TH)
                check(f"la clase H se finaliza ({rf.status_code})", rf.status_code == 200)
                check("finalizada: ya no se publica (400)", (await POST(TH, H, tipo="nota", texto="tarde")).status_code == 400)
                check("finalizada: el profesor SÍ puede corregir una errata",
                      (await PATCH(TH, H, it[1]["id"], texto="réussite!")).status_code == 200)
                check("María (estaba) sigue leyendo después de la clase", (await GET(MH, H)).status_code == 200)
                inscribir("carlos")                        # se inscribe DESPUÉS de la clase
                check("Carlos inscrito DESPUÉS de la clase → 403 (no ve materiales históricos)", (await GET(CH, H)).status_code == 403)
                cx.execute("update enrollments set is_active=0 where id=?", (insc["maria"],))
                cx.commit()
                check("María sale del grupo después → conserva el acceso histórico", (await GET(MH, H)).status_code == 200)
                cx.execute("update users set is_active=0 where id=?", (ids["maria"],))
                cx.commit()
                try:
                    check("cuenta desactivada → 403", (await GET(MH, H)).status_code == 403)
                finally:
                    cx.execute("update users set is_active=1 where id=?", (ids["maria"],))
                    cx.commit()
                lm = (await c.get("/video/contenido-clases", headers=MH)).json()
                lc = (await c.get("/video/contenido-clases", headers=CH)).json()
                lt = (await c.get("/video/contenido-clases", headers=TH)).json()
                la = (await c.get("/video/contenido-clases", headers=AH)).json()
                sm = {i["session_id"] for i in lm["items"]}
                check("lista de clases con contenido: María ve H; Carlos no ve H; el profesor ve las suyas; Dirección todas",
                      H in sm and Y not in sm and H not in {i["session_id"] for i in lc["items"]}
                      and {H, K, R} <= {i["session_id"] for i in lt["items"]} and Y not in {i["session_id"] for i in lt["items"]}
                      and {H, Y} <= {i["session_id"] for i in la["items"]})
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Después de la clase (histórico)» terminó sin errores ({type(e).__name__}: {e})", False)

            print("\n  · Cancelada")
            try:
                await POST(TH, X, tipo="nota", texto="antes de cancelar")
                cx.execute("update class_sessions set status='cancelled' where id=?", (X,))
                cx.commit()
                cid_x = cx.execute("select id from class_session_content where session_id=?", (X,)).fetchone()[0]
                check("cancelada: no se publica, no se edita, el estudiante no la lee",
                      (await POST(TH, X, tipo="nota", texto="x")).status_code == 400
                      and (await PATCH(TH, X, cid_x, texto="x")).status_code == 400 and (await GET(MH, X)).status_code == 403)
            except Exception as e:   # una sección rota no oculta las demás
                check(f"sección «Cancelada» terminó sin errores ({type(e).__name__}: {e})", False)

        finally:
            for tabla, i in borrar:
                cx.execute(f"delete from {tabla} where id=?", (i,))
            cx.commit()
            cx.close()
            n = base.limpiar(creadas)
            print(f"    · limpieza local: {n} sesión(es), {len(borrar)} filas de prueba")


def estatica(check):
    print("\n[ESTÁTICO] Frontend")
    p = WEB / "components/ContenidoClase.tsx"
    if not p.is_file():
        check("existe components/ContenidoClase.tsx", False)
        return
    t = p.read_text(encoding="utf-8")
    cp = (WEB / "components/CallProvider.tsx").read_text(encoding="utf-8")
    check("texto plano: sin dangerouslySetInnerHTML ni innerHTML", "dangerouslySetInnerHTML" not in t and "innerHTML" not in t)
    check("enlaces: solo http(s) (revalidado en el navegador) y rel=noopener noreferrer, nueva pestaña",
          "/^https?:\\/\\//i" in t and 'rel="noopener noreferrer nofollow"' in t and 'target="_blank"' in t)
    check("señal inmediata por paquete de datos con tema propio; la base es la fuente de verdad",
          'TEMA_CONTENIDO = "dorismon-contenido"' in t and "RoomEvent.DataReceived" in t and "/contenido`" in t)
    check("se vuelve a leer al conectar / reconectar y periódicamente (no depende del paquete)",
          "RoomEvent.Reconnected" in t and "setInterval" in t)
    check("sin localStorage / sessionStorage", "localStorage" not in t and "sessionStorage" not in t)
    check("sin notificaciones push", "push" not in t.lower() and "/notifications" not in t)
    check("en la llamada: panel en completa/dividida e indicador compacto en mini",
          "<PanelContenido" in cp and "<IndicadorContenido" in cp and "<BotonContenido" in cp)
    check("textos de la sección: «Contenido de la clase» y «recursos de clase»",
          "Contenido de la clase" in t and "recursos de clase" in t)


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    try:
        await integracion(check)
    except Exception as e:
        check(f"integración sin errores ({type(e).__name__}: {e})", False)
    estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
