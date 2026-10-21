"""GATE 5B — PWA, caché y actualizaciones (verificación ESTÁTICA del frontend).

Las pruebas de comportamiento (registro según entorno, borrado de cachés,
stale-while-revalidate, aviso, bloqueo durante Video Dorismon, sin recargas
automáticas) se hicieron en navegador con builds simulados; ver informe 5B.

  · el SW se decide AL CONSTRUIR: solo VERCEL_ENV === "production";
  · versión pública = VERCEL_GIT_COMMIT_SHA (fallback constante "local");
  · /sw.js?v=<versión>, caché "dorismon-<versión>", limpieza de Dorismon anteriores;
  · estrategias por tipo; sin precache roto; sin otros dominios; sin API;
  · push / notificationclick / mensajes del SW INTACTOS;
  · /version sin caché; aviso con "Actualizar ahora" / "Más tarde";
  · nunca recarga automática; bloqueado durante cualquier modo de llamada;
  · CallProvider, AvisosTelefono y el logo oficial SIN cambios.

Uso: python tests/test_gate5b.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"


def sha(b: bytes) -> str:
    return hashlib.sha256(b.replace(b"\r\n", b"\n")).hexdigest()


def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    rutas = {k: WEB / v for k, v in {
        "cfg": "next.config.js", "inst": "components/PWAInstaller.tsx", "sw": "public/sw.js",
        "ver": "app/version/route.ts", "aviso": "components/AvisoVersion.tsx",
        "dlayout": "app/dashboard/layout.tsx", "api": "lib/api.ts"}.items()}
    if not all(p.exists() for p in rutas.values()):
        check(f"existen los archivos del frontend en {WEB}", False)
        print(f"\n{passed}/{total} tests pasaron")
        return False
    t = {k: p.read_text(encoding="utf-8").replace("\r\n", "\n") for k, p in rutas.items()}
    cfg, inst, sw, av = t["cfg"], t["inst"], t["sw"], t["aviso"]

    print("\n[1] Decisión en el build (no en el navegador)")
    check("SW habilitado SOLO si VERCEL_ENV === \"production\"",
          'const SW_ENABLED = process.env.VERCEL_ENV === "production" ? "1" : "0";' in cfg)
    check("versión = VERCEL_GIT_COMMIT_SHA, fallback constante \"local\"",
          'process.env.VERCEL_GIT_COMMIT_SHA || "local"' in cfg and "Date.now()" not in cfg)
    check("solo se exponen NEXT_PUBLIC_SW_ENABLED y NEXT_PUBLIC_APP_VERSION",
          "NEXT_PUBLIC_SW_ENABLED: SW_ENABLED," in cfg and "NEXT_PUBLIC_APP_VERSION: APP_VERSION," in cfg
          and len(re.findall(r"NEXT_PUBLIC_\w+:", cfg)) == 2)
    check("el cliente NO lee VERCEL_ENV", "process.env.VERCEL_ENV" not in inst + av)
    check("sin lista de dominios para decidir el registro",
          "hostname" not in inst and "dorismon.com" not in inst and "vercel.app" not in inst)

    print("\n[2] Registro / limpieza")
    check("registra /sw.js?v=<versión> solo con NEXT_PUBLIC_SW_ENABLED === \"1\"",
          'if (process.env.NEXT_PUBLIC_SW_ENABLED === "1") {' in inst
          and "navigator.serviceWorker.register(`/sw.js?v=${v}`)" in inst)
    check("donde no corresponde: desregistra SOLO el /sw.js de este sitio",
          'u.origin === window.location.origin && u.pathname === "/sw.js"' in inst and "reg.unregister()" in inst)
    check("donde no corresponde: borra SOLO cachés dorismon-*", 'k.startsWith("dorismon-")' in inst)

    print("\n[3] Service worker")
    check("versión tomada de la URL de registro", 'new URL(self.location.href).searchParams.get("v")' in sw)
    check("caché versionada dorismon-<versión>", 'const PREFIJO = "dorismon-";' in sw and "const CACHE = `${PREFIJO}${VERSION}`;" in sw)
    check("precache roto eliminado (sin /manifest.json ni addAll)", "manifest.json" not in sw and "addAll" not in sw)
    check("activate: borra Dorismon anteriores, conserva la actual y las ajenas",
          "key.startsWith(PREFIJO) && key !== CACHE" in sw and "self.clients.claim()" in sw)
    check("otros dominios nunca se guardan", "if (url.origin !== self.location.origin) return;" in sw)
    check("API y /version nunca se guardan", 'url.pathname.startsWith("/api/") || url.pathname === "/version"' in sw)
    privadas = ['"/dashboard"', '"/checkout"', '=== "/login"', '=== "/register"',
                '"/verify-email"', '"/reset-password"', '"/forgot-password"']
    check("exclusiones de datos privados intactas", all(p in sw for p in privadas))
    check("/_next/static/* → caché primero (versionada)",
          'url.pathname.startsWith("/_next/static/")) {\n    event.respondWith(primeroCache(event.request));' in sw)
    check("svg/png/jpg/webp/gif/ico e /icons/ → stale-while-revalidate",
          "const SIN_HASH = /\\.(svg|png|jpe?g|webp|gif|ico)$/i;" in sw
          and 'url.pathname.startsWith("/icons/") || SIN_HASH.test(url.pathname)' in sw
          and "event.waitUntil(deRed.catch(() => {}));" in sw)
    check("solo se guardan respuestas 200 propias (type basic)", sw.count('resp.status === 200 && resp.type === "basic"') == 2)
    cola = sw[sw.index("// Mensaje desde la app"):].encode("utf-8")
    # HOTFIX DE AUDITORÍA v3.9.76 (autorizado en la orden, punto 8): CLEAR_CACHE ahora borra solo las cachés
    # `dorismon-*`. Es el ÚNICO cambio en esta cola; el hash fija la versión nueva (la anterior era 9ce3f98b…).
    check("message / push / notificationclick idénticos a la versión del hotfix (solo cambia CLEAR_CACHE a dorismon-*)",
          sha(cola) == "f9c29bc78a16ff129f4f21c3c79b369d2a65a9e4ea26a9c8ebf625f478549162")

    print("\n[4] Nunca recarga automática")

    def codigo(txt):  # sin comentarios: se revisa lo que se EJECUTA
        txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
        return re.sub(r"(?m)//.*$", "", txt)

    for nombre, txt in (("sw.js", sw), ("PWAInstaller", inst), ("AvisoVersion", av)):
        check(f"{nombre}: sin controllerchange", "controllerchange" not in codigo(txt))
    check("sw.js y PWAInstaller no recargan", "reload" not in codigo(sw) and "reload" not in codigo(inst))
    check("AvisoVersion: única recarga = botón del usuario y nunca en llamada",
          av.count("window.location.reload()") == 1
          and "if (enLlamada) return;  // doble seguro: nunca durante Video Dorismon\n    window.location.reload();" in av)

    print("\n[5] Ruta /version")
    v = t["ver"]
    check("dinámica y sin caché", 'export const dynamic = "force-dynamic";' in v and '"Cache-Control": "no-store, max-age=0"' in v)
    check("devuelve la versión del build", "process.env.NEXT_PUBLIC_APP_VERSION" in v)

    print("\n[6] Aviso de versión nueva")
    check("compara con /version sin caché", 'fetch("/version", { cache: "no-store" })' in av and "version !== MI_VERSION" in av)
    check("comprueba al volver (visibilitychange / focus) y cada 45 min",
          '"visibilitychange"' in av and '"focus"' in av and "const CADA_MS = 45 * 60 * 1000;" in av)
    check("solo comprueba con la pestaña visible", 'document.visibilityState !== "visible"' in av)
    check("textos exactos", all(s in av for s in (
        "Hay una nueva versión de Dorismon disponible.", "Actualizar ahora", "Más tarde",
        "Hay una actualización disponible. Podrás aplicarla al finalizar tu clase.",
        "Guarda lo que estés haciendo antes de actualizar.")))
    check("\"Más tarde\" pospone 4 horas", "const POSPONER_MS = 4 * 60 * 60 * 1000;" in av)
    check("lee useLlamada() (mini, split y full = enLlamada)",
          "const { enLlamada } = useLlamada();" in av and "{enLlamada ? (" in av)
    d = t["dlayout"]
    check("montado DENTRO de <CallProvider> en el layout del panel",
          d.find("<CallProvider>") < d.find("<AvisoVersion />") < d.find("</CallProvider>"))

    print("\n[7] Intactos")
    # components/CallProvider.tsx ya NO se fija aquí: Gate 6A (autorizado
    # después) lo modifica y lo verifica tests/test_gate6a.py. Lo que 5B exige
    # de él (useLlamada / enLlamada) sigue comprobándose en [6].
    intactos = {
        "components/AvisosTelefono.tsx": "54beea2d907c89851610b0b87422718276117c935bd64db860c563bcc6c06426",
        "public/logo-dorismon.svg": None,
    }
    for rel, h in intactos.items():
        p = WEB / rel
        if h is None:
            ok = p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == \
                "d38de230cead354821da3c0b867d325fb972f783293a618c8c0ff9f5dd6f3507"
        else:
            ok = p.exists() and sha(p.read_bytes()) == h
        check(f"{rel} sin cambios", ok)
    # HOTFIX DE AUDITORÍA v3.9.76 (autorizado, punto 8): el cierre de sesión solo borra cachés `dorismon-*`.
    check("limpieza de cachés al cerrar sesión (limitada a dorismon-*)",
          'caches.keys().then((keys) => keys.filter((k) => k.startsWith("dorismon-")).forEach((k) => caches.delete(k)));' in t["api"]
          and 'navigator.serviceWorker.controller.postMessage({ type: "CLEAR_CACHE" });' in t["api"])

    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
