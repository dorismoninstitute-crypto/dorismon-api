"""GATE 5A — Una sola experiencia de carga, premium y honesta.

Verificación ESTÁTICA del frontend (las líneas de tiempo reales se midieron en
navegador con build de producción + proxy lento local; ver informe Gate 5A):
  · un controlador compartido (CargaProvider) en la raíz, con UN reloj por sesión;
  · LoadingScreen conserva su interfaz y ya no tiene reloj propio;
  · umbrales 600 ms / 3 s / 12 s / 40 s, gracia 150 ms, estabilización ≤ 500 ms;
  · mensajes honestos (sin "servidor despertando", sin porcentajes) + Reintentar;
  · login: nada tapa la pantalla antes de aceptar credenciales; errores visibles;
  · /welcome usa la experiencia compartida y conserva router.prefetch (Gate 4);
  · diseño: comillas tipográficas, ~30 frases, reduced-motion, safe-areas, pie sin emoji;
  · NO se tocó el service worker ni el logo oficial.

Uso: python tests/test_gate5.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"


def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    rutas = {k: WEB / v for k, v in {
        "carga": "components/Carga.tsx", "ui": "components/ui.tsx", "root": "app/layout.tsx",
        "login": "app/login/page.tsx", "wel": "app/welcome/page.tsx",
        "dlayout": "app/dashboard/layout.tsx"}.items()}
    if not all(p.exists() for p in rutas.values()):
        check(f"existen los archivos del frontend en {WEB}", False)
        print(f"\n{passed}/{total} tests pasaron")
        return False
    t = {k: p.read_text(encoding="utf-8") for k, p in rutas.items()}
    c, ui = t["carga"], t["ui"]

    print("\n[1] Un solo controlador")
    check("CargaProvider envuelve la app en el layout raíz",
          'import { CargaProvider } from "@/components/Carga";' in t["root"]
          and "<CargaProvider>\n          <ErrorBoundary>{children}</ErrorBoundary>\n        </CargaProvider>" in t["root"])
    check("LoadingScreen conserva su interfaz (message opcional, 'Cargando...' por defecto)",
          'export function LoadingScreen({ message = "Cargando..." }: { message?: string })' in ui)
    ls = ui[ui.find("export function LoadingScreen"):ui.find("export function Skeleton")]
    check("LoadingScreen ya no tiene reloj propio ni pantalla motivacional propia",
          "setTimeout" not in ls and "fixed inset-0" not in ls and "useRegistroCarga(true)" in ls)
    check("el reloj se mide desde el INICIO de la sesión, no desde cada montaje",
          "inicio.current = Date.now();" in c and "const pasado = Date.now() - inicio.current;" in c)
    check("una espera nueva durante gracia/estabilización/salida continúa la MISMA sesión",
          "clearTimeout(finTimer.current); finTimer.current = null; setSaliendo(false);\n      if (sesion.current) programar();" in c)
    check("gracia de 150 ms entre layout y página", "const GRACIA_MS = 150;" in c
          and "finTimer.current = setTimeout(terminar, GRACIA_MS);" in c)
    usos = sum(1 for p in (WEB / "app").rglob("*.tsx") if "<LoadingScreen" in p.read_text(encoding="utf-8"))
    print(f"    · pantallas que usan <LoadingScreen>: {usos}")
    check("las pantallas existentes siguen usando LoadingScreen (≥ 66)", usos >= 66)

    print("\n[2] Tiempos")
    for nombre, ms in [("DISCRETA_MS", 600), ("MOTIVACIONAL_MS", 3000), ("LARGA_1_MS", 12000),
                       ("LARGA_2_MS", 40000), ("MIN_VISIBLE_MS", 500), ("SALIDA_MS", 250), ("ROTAR_MS", 6000)]:
        check(f"{nombre} = {ms}", f"const {nombre} = {ms};" in c)
    check("carga rápida: si la frase nunca se vio, se termina sin esperar",
          "if (motivDesde.current === null) { reiniciar(); return; }" in c)
    check("estabilización: solo lo que falte hasta 500 ms visibles (nunca 800 ms fijos)",
          "Math.max(0, MIN_VISIBLE_MS - (Date.now() - motivDesde.current))" in c
          and not re.search(r"setTimeout\([^;]*\b800\)", c))
    check("antes de 600 ms no se tapa nada (LoadingScreen solo pinta un hueco)",
          'fase === "discreta" ?' in ls and '<span className="sr-only">{message}</span>' in ls)

    print("\n[3] Mensajes honestos")
    check("primer aviso largo exacto",
          "Está tomando un poco más de tiempo de lo habitual. Seguimos preparando tu espacio." in c)
    check("segundo aviso exacto + Reintentar (recarga)",
          "La conexión está tardando más de lo normal." in c and "window.location.reload()" in c and ">\n                    Reintentar\n" in c)
    check("salida para no quedar bloqueado (seguir esperando en segundo plano)", "Seguir esperando en segundo plano" in c)
    check("sin 'servidor despertando' ni porcentajes inventados",
          "despertando" not in c.lower()
          and not re.search(r"\d+\s*%\s*<|\}\s*%|%\s*completado|progress=", c))  # % en texto visible
    check("sin botón Ayuda (no existe destino público adecuado)", "Ayuda" not in c)

    print("\n[4] Login")
    lg = t["login"]
    check("se registra en la carga compartida SOLO tras aceptar credenciales", "useRegistroCarga(aceptado);" in lg)
    check("aceptado = true justo después de guardar el token",
          "auth.saveToken(res.access_token);\n      setAceptado(true);" in lg)
    check("un error devuelve el formulario al instante (aceptado = false + mensaje)",
          "setAceptado(false);\n      setError(e.message);\n      setLoading(false);" in lg)
    check("avisos bajo el botón a los 3 s y 10 s, solo antes de aceptar",
          "Conectando con Dorismon…" in lg and "La conexión está tardando un poco más de lo habitual…" in lg
          and "setTimeout(() => setLento(1), 3000)" in lg and "setTimeout(() => setLento(2), 10000)" in lg
          and "{loading && !aceptado && lento > 0 && (" in lg)
    check("el login no pinta overlay propio", "fixed inset-0" not in lg and "LoadingScreen" not in lg)

    print("\n[5] /welcome")
    w = t["wel"]
    check("/welcome usa la experiencia compartida", "if (loading) return <LoadingScreen />;" in w)
    check("/welcome ya no tiene su 'Cargando...' aislado", "animate-pulse text-white/70" not in w)
    check("/welcome conserva router.prefetch de Gate 4", "router.prefetch(dashboardPath)" in w)

    print("\n[6] Diseño")
    n = len(re.findall(r'\{ en: "', c))
    print(f"    · frases bilingües: {n}")
    check("~30 frases bilingües", 28 <= n <= 32)
    check("sin repetición inmediata", "if (i === actual) i = (i + 1) % FRASES.length;" in c)
    check("frase elegida después de montar (sin riesgo de hidratación)",
          "useState<number | null>(null)" in c and "Math.random" not in c[:c.find("function otraFrase")])
    check("comillas tipográficas “ ”", "“{f.en}”" in c)
    check("prefers-reduced-motion respetado", "@media (prefers-reduced-motion: reduce)" in c and "motion-reduce:transition-none" in c)
    check("safe-areas en móvil", "env(safe-area-inset-top)" in c and "env(safe-area-inset-bottom)" in c)
    check("pie en el flujo normal, con texto y sin bandera emoji",
          "Dorismon Language Institute · Santo Domingo, República Dominicana" in c and "🇩🇴" not in c and "-bottom-24" not in c)
    check("accesible: role=status + aria-live", 'role="status"' in c and 'aria-live="polite"' in c)
    check("barra de actividad en vez de spinner grande", "animate-spin" not in c and "animate-spin" not in ls)

    print("\n[7] Fuera de alcance intacto")
    check("logo oficial referenciado tal cual", 'src="/logo-dorismon.svg"' in c)
    # Huellas SHA-256 de los archivos tal como están en el commit (main/candidate).
    # public/sw.js ya NO se fija aquí: Gate 5B (autorizado después) lo modifica
    # y lo verifica tests/test_gate5b.py. El logo oficial sigue sin tocarse.
    huellas = {
        "public/logo-dorismon.svg": "d38de230cead354821da3c0b867d325fb972f783293a618c8c0ff9f5dd6f3507",
    }
    for rel, h in huellas.items():
        p = WEB / rel
        check(f"{rel} sin cambios (misma huella que el commit)",
              p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() == h)
    check("el layout del panel sigue usando LoadingScreen (continuidad layout → página)",
          'if (!loaded) return <LoadingScreen message="Cargando..." />;' in t["dlayout"])

    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
