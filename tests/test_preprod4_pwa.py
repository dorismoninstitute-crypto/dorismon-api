"""PREPROD-4 (automatizable) — PWA, service worker, versionado, aviso de actualización y push.

[SW]      Carga el public/sw.js REAL en un vm de Node (tests/preprod4_sw_harness.js): qué se guarda en caché
          y qué no (API, otros dominios, páginas con datos), versionado, limpieza de cachés Dorismon
          anteriores sin tocar las ajenas, mensajes, push y clic en la notificación, y que NUNCA recarga.
[ESTÁTICO] manifiesto + iconos (tamaño real del PNG), registro solo en producción de Vercel y limpieza en
          los demás entornos, /version sin caché, aviso de versión que no interrumpe una videoclase,
          sin recargas en el motor de video, texto sin promesa falsa de "offline".
No sustituye al navegador real (ver el informe) ni a los dispositivos físicos (pendientes del smoke).

Uso: python tests/test_preprod4_pwa.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import json
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
HARNESS = Path(__file__).resolve().parent / "preprod4_sw_harness.js"


def leer(rel):
    return (WEB / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def png_tam(p: Path):
    b = p.read_bytes()[:24]
    return struct.unpack(">II", b[16:24]) if b[:8] == b"\x89PNG\r\n\x1a\n" else None


def parte_sw(check):
    print("\n[SW] public/sw.js real en un vm de Node")
    if not shutil.which("node"):
        check("node disponible para el arnés del service worker", False)
        return
    r = subprocess.run(["node", str(HARNESS), str(WEB / "public/sw.js")], capture_output=True, text=True, timeout=120, encoding="utf-8")
    m = re.search(r"JSON(\{.*\})", r.stdout)
    check(f"el arnés se ejecuta sin errores (rc={r.returncode} {r.stderr[-160:].strip()})", r.returncode == 0 and bool(m))
    if not m:
        return
    o = json.loads(m.group(1))
    check("instalación: toma el control sin esperar y SIN precache (no hay offline que precargar)", o["install_salta_espera"] and o["install_sin_precache"])
    malos = [k for k, v in o["intercepta"].items() if v["esperado"] != v["real"]]
    check(f"solo se interceptan estáticos con hash, iconos e imágenes públicas; páginas, /version, /api y manifiesto van a la RED ({len(o['intercepta'])} rutas)", not malos)
    if malos:
        print("      rutas mal clasificadas:", malos)
    check("otros dominios (API en Render, Cloudinary, LiveKit, fuentes, CDN) nunca se interceptan", not any(o["ajenos"].values()))
    check("POST / PUT / PATCH / DELETE nunca se interceptan", not any(o["metodos"].values()))
    check("un estático con hash se guarda en la caché versionada y la segunda vez sale de la caché (sin red)",
          o["static_primera"] == {"red": 1, "guardado": True} and o["static_segunda_desde_cache"])
    check("respuestas 404 u opacas NO se guardan", o["no_guarda_404_ni_opaco"])
    check("archivos sin hash (logo/iconos): se sirve lo guardado y se renueva en segundo plano", o["sin_hash_sirve_guardado_y_renueva"])
    check("en toda la caché solo hay estáticos/iconos/imágenes del propio dominio (nada de API ni páginas)", o["solo_estaticos_guardados"])
    check(f"activación: borra las cachés Dorismon ANTERIORES y conserva la actual y las AJENAS ({o['activate']['quedan']})",
          o["activate"]["quedan"] == ["dorismon-NUEVA", "otra-app-cache", "workbox-precache"] and o["activate"]["claim"] == 1)
    check("sin ?v= en la URL usa la versión 'sin-version' y también limpia las anteriores", o["sin_version"]["quedan"] == [])
    check("mensaje SKIP_WAITING funciona", o["message_skip"])
    check(f"mensaje CLEAR_CACHE borra SOLO las cachés dorismon-* y conserva las ajenas ({o['message_clear']}); los mensajes basura se ignoran",
          o["message_clear"] == ["otra"] and o["message_ignora_basura"])
    p = o["push_json"]
    check(f"push JSON: título, cuerpo, icono, insignia, etiqueta y destino ({p['titulo']!r}, {p['url']})",
          (p["titulo"], p["cuerpo"], p["icono"], p["badge"], p["tag"], p["url"], p["renotify"]) ==
          ("Clase en 10 min", "Inglés B1", "/icons/icon-192.png", "/icons/icon-96.png", "clase-42", "/clase/42", True))
    p = o["push_texto_plano"]
    check("push con texto plano (no JSON): no falla; título por defecto 'Dorismon' y el texto como cuerpo", p == {"titulo": "Dorismon", "cuerpo": "texto plano", "url": "/dashboard"})
    check("push vacío o sin datos: título de la institución y destino /dashboard",
          o["push_vacio"]["titulo"] == o["push_objeto_vacio"]["titulo"] == "Dorismon Language Institute"
          and o["push_vacio"]["url"] == o["push_objeto_vacio"]["url"] == "/dashboard" and o["push_vacio"]["tag"] == "dorismon")
    c = o["click_con_ventana"]
    check("clic en la notificación con la plataforma abierta: la trae al frente y navega SIN abrir otra pestaña",
          c == {"cerrada": 1, "navegados": ["/clase/42"], "enfocados": 1, "abiertos": 0})
    check("clic sin ventana abierta: abre el destino; sin datos, /dashboard",
          o["click_sin_ventana"]["abiertos"] == ["/dashboard/student"] and o["click_sin_datos"]["abiertos"] == ["/dashboard"])
    check("el service worker NUNCA recarga la aplicación (ni controllerchange ni reload)", o["nunca_recarga"])


def parte_estatica(check):
    print("\n[ESTÁTICO] manifiesto, iconos, registro, versión, aviso")
    mf = leer("app/manifest.ts")
    iconos = re.findall(r'src: `(/icons/[^`]+)`, sizes: "(\d+)x(\d+)", type: "image/png", purpose: "(\w+)"', mf)
    faltan = [s for s, w, h, _ in iconos if not (WEB / "public" / s.lstrip("/")).is_file()]
    mal = [s for s, w, h, _ in iconos if (WEB / "public" / s.lstrip("/")).is_file() and png_tam(WEB / "public" / s.lstrip("/")) != (int(w), int(h))]
    check(f"manifiesto: {len(iconos)} iconos, todos existen ({faltan or 'sí'}) y su tamaño REAL coincide con el declarado ({mal or 'sí'})", len(iconos) >= 8 and not faltan and not mal)
    check("manifiesto: hay iconos maskable de 192 y 512 (instalación en Android)",
          {(w, p) for _, w, _, p in iconos} >= {("192", "maskable"), ("512", "maskable")})
    check("manifiesto: nombre, start_url /dashboard, scope /, display standalone, colores, idioma es",
          all(x in mf for x in ('name: "Dorismon Language Institute"', 'start_url: "/dashboard"', 'scope: "/"', 'display: "standalone"',
                                'theme_color: "#4361ee"', 'background_color: "#ffffff"', 'lang: "es"', 'id: "/dashboard"')))
    ly = leer("app/layout.tsx")
    check("layout: enlace al manifiesto, apple-touch-icon, themeColor y metadatos de app en iOS",
          'rel="manifest" href="/manifest.webmanifest"' in ly and "apple-touch-icon" in ly and 'themeColor: "#4361ee"' in ly and "apple-mobile-web-app-capable" in ly)
    check("el apple-touch-icon existe y es 180×180", png_tam(WEB / "public/icons/apple-touch-icon.png") == (180, 180))
    nx = leer("next.config.js")
    check("service worker habilitado SOLO si VERCEL_ENV === production (decidido al construir)",
          'process.env.VERCEL_ENV === "production" ? "1" : "0"' in nx)
    check("versión del build = commit de Vercel (saneada) o 'local'", 'process.env.VERCEL_GIT_COMMIT_SHA || "local"' in nx and "replace(/[^a-zA-Z0-9._-]/g" in nx)
    check("cabeceras de seguridad globales (nosniff, frame DENY, referrer, HSTS, permisos de cámara/micro solo propios)",
          all(x in nx for x in ("nosniff", "'DENY'", "strict-origin-when-cross-origin", "Strict-Transport-Security", "camera=(self), microphone=(self)")))
    inst = leer("components/PWAInstaller.tsx")
    check("registro: solo con NEXT_PUBLIC_SW_ENABLED=1 y con la versión en la URL (/sw.js?v=…)",
          'process.env.NEXT_PUBLIC_SW_ENABLED === "1"' in inst and "register(`/sw.js?v=${v}`)" in inst)
    check("fuera de producción: se desregistra SOLO el SW de Dorismon (mismo origen, /sw.js) y se borran SOLO las cachés dorismon-*",
          "u.origin === window.location.origin && u.pathname === \"/sw.js\"" in inst and 'k.startsWith("dorismon-")' in inst)
    check("el texto de instalación NO promete 'offline' (el SW no lo ofrece) y avisa que necesita conexión",
          "Funciona offline" not in inst and "Necesita conexión a internet" in inst)
    sw = leer("public/sw.js")
    check("sw.js declara que NO hay offline real y que nunca recarga", "NO hay offline real" in sw and "NUNCA recarga" in sw.replace("\n// ", " "))
    rv = leer("app/version/route.ts")
    check("/version: dinámica y SIN caché (no-store)", 'dynamic = "force-dynamic"' in rv and "no-store" in rv and "NEXT_PUBLIC_APP_VERSION" in rv)
    av = leer("components/AvisoVersion.tsx")
    check("aviso: compara la versión del build con /version (no-store) al volver a la app y cada 45 min, como mucho 1 vez/30 s, y solo visible",
          'fetch("/version", { cache: "no-store" })' in av and "visibilitychange" in av and "45 * 60 * 1000" in av and "MIN_ENTRE_MS = 30 * 1000" in av
          and 'document.visibilityState !== "visible"' in av)
    check("aviso: NUNCA recarga solo; recargar exige 'Actualizar ahora' + confirmación ('Guarda lo que estés haciendo')",
          av.count("window.location.reload()") == 1 and "const actualizar = () => {\n    if (enLlamada) return;" in av and "Guarda lo que estés haciendo" in av)
    check("aviso: durante una videoclase (mini, dividida o completa) solo INFORMA; no ofrece botones",
          "const { enLlamada } = useLlamada();" in av and "Podrás aplicarla al finalizar tu clase." in av and "{enLlamada ? (" in av)
    check("aviso: 'Más tarde' pospone 4 h y se cierra una confirmación abierta si empieza una clase",
          "POSPONER_MS = 4 * 60 * 60 * 1000" in av and "if (enLlamada) setConfirmar(false)" in av)
    dl = leer("app/dashboard/layout.tsx")
    check("el aviso está montado DENTRO de CallProvider (sabe si hay clase)", dl.index("<CallProvider>") < dl.index("<AvisoVersion />") < dl.index("</CallProvider>"))
    motores = ["components/CallProvider.tsx", "components/ClassRoomExtras.tsx", "components/InteraccionClase.tsx", "components/ContenidoClase.tsx"]
    check("el motor de Video Dorismon no recarga ni navega con window.location (sin Rooms duplicados por recargas)",
          all(not re.search(r"location\.reload|window\.location\.(href|assign|replace)", leer(m)) for m in motores if (WEB / m).is_file()))
    check("el service worker no intercepta nada de video: LiveKit va a otro dominio (nunca interceptado) y /clase no se guarda",
          "url.origin !== self.location.origin" in sw and "/clase" not in sw)
    # ── Recuperación: una caída de la API NO cierra la sesión (hallazgo real de PREPROD-4)
    api = leer("lib/api.ts")
    check("api(): un fallo de RED (fetch rechazado) se convierte en ApiError de estado 0 con mensaje claro (también al reintentar tras refrescar el token)",
          api.count('throw new ApiError("No pudimos conectar con el servidor. Revisa tu conexión e inténtalo de nuevo.", 0)') == 2
          and "res = await doFetch(opts.auth ? getToken() : null);" in api)
    check("dashboard: SOLO 401 / 403 / 404 cierran la sesión; cualquier otro fallo (red, 5xx, 429) conserva la sesión",
          "estado === 401 || estado === 403 || estado === 404" in dl and "setSinConexion(true)" in dl
          and not re.search(r"\.catch\(\(\) => \{\s*auth\.logout\(\);", dl))
    check("dashboard: pantalla 'No pudimos conectar' con aviso de que la sesión sigue abierta y botón Reintentar",
          "No pudimos conectar con Dorismon" in dl and "Tu sesión sigue abierta" in dl and "Reintentar" in dl and "data-sin-conexion" in dl)
    check("el único otro auth.logout() del dashboard es el botón explícito de la persona", len(re.findall(r"auth\.logout\(\)", dl)) == 2)

    print("\n[ESTÁTICO] hotfix de auditoría: base de la API y limpieza de cachés")
    fuentes = [f for d in ("app", "components", "lib", "hooks", "public") for f in (WEB / d).rglob("*") if f.is_file() and f.suffix in (".ts", ".tsx", ".js")]
    texto = {f: f.read_text(encoding="utf-8", errors="ignore") for f in fuentes}
    check("ningún archivo del frontend usa localStorage.api_base ni window.__API_BASE__ (el Bearer no se puede redirigir a otro host)",
          not [f.name for f, x in texto.items() if "api_base" in x or "__API_BASE__" in x])
    ui = leer("components/ui.tsx")
    check("descarga .ics: usa calendarApi.icsUrl (base oficial NEXT_PUBLIC_API_URL) y no una base editable",
          "calendarApi.icsUrl(sessionId)" in ui and 'localStorage.getItem("api_base")' not in ui
          and "`${API_URL}/calendar/session/${sessionId}.ics`" in api)
    check("logout (api.ts) borra solo cachés 'dorismon-*'",
          'keys.filter((k) => k.startsWith("dorismon-")).forEach((k) => caches.delete(k))' in api and "keys.forEach((k) => caches.delete(k))" not in api)
    sw = leer("public/sw.js")
    check("ninguna llamada a caches.delete borra cachés ajenas (todas filtradas por el prefijo Dorismon)",
          "keys.map((k) => caches.delete(k))" not in sw and sw.count("startsWith(PREFIJO)") >= 2)
    ejemplo = (WEB / ".env.example").read_text(encoding="utf-8")
    check("frontend .env.example apunta a la API LOCAL, nunca a producción",
          "NEXT_PUBLIC_API_URL=http://127.0.0.1:8000" in ejemplo and "onrender" not in ejemplo and "dorismon.com" not in ejemplo)


def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    for f in (parte_sw, parte_estatica):
        try:
            f(check)
        except Exception as e:
            check(f"{f.__name__} sin errores ({type(e).__name__}: {str(e)[:160]})", False)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
