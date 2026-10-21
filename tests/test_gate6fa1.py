"""GATE 6F-A.1 — Temporizador robusto ante relojes de dispositivo desfasados.

[API] GET /video/hora: requiere sesión; devuelve la hora del servidor (ms).
[RELOJ] Simulación EJECUTABLE con Node del cálculo real del frontend
    (lib/relojClase.ts + restanteMs de InteraccionClase.tsx) con relojes inyectados:
    profesor con hora correcta; estudiantes a +120 s, −120 s, +30 s; latencias
    simétricas y asimétricas; iniciar, pausar, esperar, reanudar, reiniciar,
    entrada tardía, reconexión (nueva sincronización), recuperación 6E (cliente
    nuevo) y cambio de la hora del teléfono en plena clase. Todos deben coincidir
    con el profesor (≤ 250 ms). Control: el cálculo ANTERIOR (reloj del teléfono)
    se desvía decenas de segundos.
[ESTÁTICO] el cliente no usa el reloj del dispositivo ni la hora de los metadatos;
    sincroniza pocas veces (al haber temporizador, al reconectar, al volver a la
    pestaña), nunca cada segundo; nada más cambió (manos, reacciones, turno, foco,
    permisos, CallProvider, tokens).

Uso: python tests/test_gate6fa1.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
STUDENT = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
TOLERANCIA_MS = 250


def sha(b: bytes) -> str:
    return hashlib.sha256(b.replace(b"\r\n", b"\n")).hexdigest()


def codigo(txt: str) -> str:
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"\{/\*.*?\*/\}", "", txt, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", txt)


# ============================================================================ API
def parte_api(check):
    print("\n[API] GET /video/hora")
    with httpx.Client(base_url=BASE, timeout=30) as c:
        r = c.get("/video/hora")
        check(f"sin sesión → rechazado ({r.status_code})", r.status_code in (401, 403))
        tok = c.post("/auth/login", json=STUDENT).json()["access_token"]
        H = {"Authorization": f"Bearer {tok}"}
        a = c.get("/video/hora", headers=H)
        b = c.get("/video/hora", headers=H)
        ok = a.status_code == 200 and b.status_code == 200
        ja, jb = (a.json(), b.json()) if ok else ({}, {})
        check("con sesión → 200 {ahora: entero en ms}", ok and isinstance(ja.get("ahora"), int) and set(ja) == {"ahora"})
        check("es la hora actual del servidor (no un valor guardado) y avanza",
              ok and abs(ja["ahora"] / 1000 - time.time()) < 300 and jb["ahora"] >= ja["ahora"])


# ============================================================================ RELOJ
SIMULACION = r"""
import { sincronizar, horaServidor, referenciaDe } from "./relojClase.ts";
import { restanteMs } from "./restante.ts";

let T = 1_700_000_000_000;               // hora REAL (= reloj del servidor)
const avanzar = (ms) => { T += ms; };

// Dispositivo: reloj de pared desfasado (skew), reloj monotónico con otro origen,
// latencia de ida y de vuelta (pueden ser distintas).
function dispositivo(nombre, skew, ida, vuelta) {
  return { nombre, skew, ida, vuelta, monoOrigen: -T + Math.random() * 1e6, ref: null };
}
const mono = (d) => T + d.monoOrigen;
async function sync(d) {
  d.ref = await sincronizar(async () => { const s = T + d.ida; avanzar(d.ida + d.vuelta); return s; }, () => mono(d), 3);
}
const nuevo = (d) => restanteMs(tz, horaServidor(d.ref, mono(d)));   // 6F-A.1
const viejo = (d) => restanteMs(tz, T + d.skew);                      // 6F-A (reloj del teléfono)

// Backend (misma lógica que /interaccion): todo con la hora del servidor
let tz = null;
const iniciar = (s) => { tz = { duracion: s * 1000, restante: s * 1000, inicio: T, pausado: false }; };
const pausar = () => { tz = { ...tz, restante: Math.max(0, tz.restante - (T - tz.inicio)), inicio: T, pausado: true }; };
const reanudar = () => { tz = { ...tz, inicio: T, pausado: false }; };

const prof = dispositivo("profesor (hora correcta)", 0, 20, 20);
const est = [
  dispositivo("estudiante +120 s", 120_000, 40, 60),
  dispositivo("estudiante −120 s", -120_000, 30, 30),
  dispositivo("estudiante +30 s (latencia asimétrica 150/20 ms)", 30_000, 150, 20),
];
const resultados = [];
function medir(paso, todos) {
  const base = nuevo(prof);
  const real = restanteMs(tz, T);
  for (const d of todos) {
    resultados.push({ paso, quien: d.nombre, nuevo: nuevo(d), profesor: base, real, viejo: viejo(d),
                      difProfesor: Math.abs(nuevo(d) - base), difReal: Math.abs(nuevo(d) - real) });
  }
}

for (const d of [prof, ...est]) await sync(d);
iniciar(180);                       avanzar(30_000);  medir("iniciar 3 min + 30 s", est);
pausar();                           avanzar(20_000);  medir("pausar y esperar 20 s", est);
reanudar();                         avanzar(15_000);  medir("reanudar + 15 s", est);
iniciar(60);                        avanzar(5_000);   medir("reiniciar a 1 min + 5 s", est);
const tarde = dispositivo("entra tarde (+120 s)", 120_000, 60, 60);
await sync(tarde);                  avanzar(3_000);   medir("entrada tardía", [...est, tarde]);
est[0].ida = 300; est[0].vuelta = 100;                // reconexión con otra red
await sync(est[0]);                 avanzar(2_000);   medir("reconexión (se vuelve a sincronizar)", [est[0]]);
const recuperado = dispositivo("recuperado tras recarga 6E (−30 s)", -30_000, 25, 25);
await sync(recuperado);             avanzar(1_000);   medir("recuperación 6E (cliente nuevo)", [recuperado]);
est[1].skew = +600_000;             avanzar(1_000);   medir("cambian la hora del teléfono en plena clase", [est[1]]);
avanzar(120_000);                                     medir("vencido", [...est, tarde, recuperado]);

// Unidad: elige la muestra más rápida y descarta las inválidas
const r = referenciaDe([{ envio: 0, llegada: 500, servidor: 1000 }, { envio: 600, llegada: 640, servidor: 1700 },
                        { envio: 900, llegada: 800, servidor: 9 }, { envio: NaN, llegada: 1, servidor: 1 }]);
const sinMuestras = referenciaDe([]);
const fallos = await sincronizar(async () => { throw new Error("red"); }, () => 0, 3);
console.log(JSON.stringify({ resultados, unidad: { r, sinMuestras, fallos } }));
"""


def parte_reloj(check):
    print("\n[RELOJ] Simulación con relojes desfasados (cálculo real del frontend, en Node)")
    reloj_ts = WEB / "lib/relojClase.ts"
    ix_p = WEB / "components/InteraccionClase.tsx"
    node = shutil.which("node")
    if not (reloj_ts.exists() and ix_p.exists() and node):
        check(f"existen lib/relojClase.ts, InteraccionClase.tsx y Node ({bool(node)})", False)
        return
    ix = ix_p.read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.search(r"export function restanteMs\(.*?\n\}\n", ix, re.S)
    if not m:
        check("se encuentra restanteMs en InteraccionClase.tsx", False)
        return
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        shutil.copy(reloj_ts, t / "relojClase.ts")
        (t / "restante.ts").write_text("type Temporizador = any;\n" + m.group(0), encoding="utf-8")
        (t / "package.json").write_text('{"type": "module"}', encoding="utf-8")
        (t / "sim.mts").write_text(SIMULACION, encoding="utf-8")
        p = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "sim.mts"], cwd=tmp,
                           capture_output=True, text=True, encoding="utf-8", timeout=60)
    if p.returncode != 0:
        check("la simulación se ejecuta", False)
        print(p.stderr[-800:])
        return
    datos = json.loads(p.stdout.strip().splitlines()[-1])
    filas = datos["resultados"]
    pasos = []
    for f in filas:
        if f["paso"] not in pasos:
            pasos.append(f["paso"])
    for paso in pasos:
        fs = [f for f in filas if f["paso"] == paso]
        peor = max(f["difProfesor"] for f in fs)
        detalle = ", ".join(f"{f['quien'].split(' (')[0]} {f['nuevo'] / 1000:.2f}s" for f in fs)
        check(f"{paso}: todos ≈ profesor ({fs[0]['profesor'] / 1000:.2f} s; peor dif {peor:.0f} ms) — {detalle}",
              peor <= TOLERANCIA_MS and all(f["difReal"] <= TOLERANCIA_MS for f in fs))
    peor_viejo = max(abs(f["viejo"] - f["profesor"]) for f in filas if f["paso"] == "iniciar 3 min + 30 s")
    check(f"control: con el cálculo ANTERIOR (reloj del teléfono) la diferencia era de {peor_viejo / 1000:.0f} s",
          peor_viejo >= 30_000)
    u = datos["unidad"]
    check("referencia: elige la muestra más rápida (ida y vuelta 40 ms) y descarta inválidas",
          u["r"] == {"servidor": 1720, "mono": 640, "idaVuelta": 40})
    check("sin muestras / red caída → sin referencia (no inventa una hora)", u["sinMuestras"] is None and u["fallos"] is None)


# ============================================================================ ESTÁTICO
def parte_estatica(check):
    print("\n[ESTÁTICO]")
    video_py = (ROOT / "app/routers/video.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    i = video_py.find("# GATE 6F-A.1")
    # Actualizado en Gate 6G-A: después de esta sección hay otra (retirados)
    fin_hora = video_py.find("\n# ====", video_py.find("\n", video_py.find("# ====", i) + 1) + 1)
    hora = video_py[i:fin_hora if fin_hora > i else None]
    check("endpoint /video/hora: solo lectura (sin base de datos), requiere sesión",
          i > 0 and '@router.get("/hora")' in hora and "Depends(get_current_user)" in hora
          and not re.search(r"db\.|Depends\(get_db\)", hora))
    from _base_gate6ga import sin_gate6ga  # Actualizado en Gate 6G-A
    sin_6ga = sin_gate6ga("app/routers/video.py", video_py)
    antes = sin_6ga[:sin_6ga.rfind("# ====", 0, sin_6ga.find("# GATE 6F-A.1"))].rstrip("\n") + "\n"
    check("video.py sin otros cambios (todo lo anterior idéntico a 6F-A)",
          sha(antes.encode("utf-8")) == "a3e1a25f805e22ab5e8f8c6ef373ac565dc4eeaba470166a444c6d2e360b6426")
    check("livekit_service.py sin cambios (tokens, Silenciar/Sacar, interacción)",
          sha(sin_gate6ga("app/services/livekit_service.py", (ROOT / "app/services/livekit_service.py").read_text(encoding="utf-8").replace("\r\n", "\n")).encode("utf-8"))
          == "cd9af7b01771f42ffaca137f9178cf01ccfa639d66325619366b302bc438961f")

    ix_p = WEB / "components/InteraccionClase.tsx"
    if not ix_p.exists():
        check(f"existe InteraccionClase.tsx en {WEB}", False)
        return
    ix = ix_p.read_text(encoding="utf-8").replace("\r\n", "\n")
    ixc = codigo(ix)
    i0 = ix.find("const referencia = useRef<Referencia | null>(null);")
    bloque = ix[i0:ix.find("return {", i0)]
    check("hora del servidor estimada + reloj monotónico (performance.now)",
          i0 > 0 and "horaServidor(referencia.current, performance.now())" in bloque
          and 'api("/video/hora", { auth: true })' in bloque and "() => performance.now()" in bloque)
    check("nunca usa la hora de los metadatos de la sala para el reloj",
          not re.search(r"\.ahora\b", re.sub(r"r\?\.ahora|r\.ahora", "", ixc.replace("typeof o.ahora", "").replace("o.ahora", ""))))
    check("sincroniza solo con temporizador, al reconectar y al volver a la pestaña (sin intervalos)",
          "if (!hayTemporizador) return;" in bloque and "room.on(RoomEvent.Reconnected, otraVez);" in bloque
          and 'addEventListener("visibilitychange", alVolver)' in bloque and "setInterval" not in bloque)
    check("no muestra un tiempo dudoso antes de tener la hora", "!!t && ix.relojListo && terminadoHace < 15000" in ix)
    check("el temporizador sigue sin paquetes por segundo (un solo publishData: reacciones)", ixc.count("publishData") == 1)
    check("CallProvider y globals.css sin cambios",
          # Actualizado en Gate 6G-B: se deshacen EXACTAMENTE sus cambios antes de comparar
          sha(__import__("_base_gate6gb").sin_gate6gb("components/CallProvider.tsx", (WEB / "components/CallProvider.tsx").read_text(encoding="utf-8").replace("\r\n", "\n")).encode("utf-8")) == "ad8e96ff15a06f111fc2502f4997b947c19542fcd64776e475d0ef7a97638737"
          and sha((WEB / "app/globals.css").read_bytes()) == "3186e5c6bb49ecf249455b2caf704bda2315d8e7d931679de18eaf650aa7ea17")
    check("manos, reacciones, turno y foco intactos",
          'void enviar({ accion: "mano", arriba: false, identity });' in ix
          and "if (previas.length >= 3) return;" in ix and "Tu turno para hablar" in ix
          and 'layout.pin.dispatch?.({ msg: "set_pin", trackReference: ref });' in ix)


def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    parte_api(check)
    parte_reloj(check)
    parte_estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
