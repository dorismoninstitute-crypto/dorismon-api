"""GATE 6F-A.1 (cierre) — Fallback del reloj del temporizador.

Si /video/hora falla:
  · con una sincronización válida previa → se conserva esa referencia y se sigue
    con el reloj monotónico (nunca se vuelve al reloj del dispositivo);
  · sin ninguna sincronización todavía → no se muestra un tiempo, sino
    "Sincronizando temporizador…", con pocos reintentos limitados (sin sondeo).

[RELOJ] Simulación EJECUTABLE con Node del código real (lib/relojClase.ts +
    restanteMs de InteraccionClase.tsx), relojes inyectados y /video/hora caído a voluntad:
    1. sincroniza bien → el endpoint falla → el tiempo sigue correcto;
    2. el reloj del dispositivo cambia ±120 s → sin cambio;
    3. la primera sincronización falla → no hay tiempo que mostrar;
    4. el siguiente intento funciona → tiempo correcto al instante;
    5. reconexión con /video/hora caído → usa la última referencia;
    6. pausa / reanudar coherentes (con el endpoint caído);
    7. recuperación 6E (cliente nuevo): falla y luego funciona → correcto.
    + reintentos limitados: con el endpoint siempre caído, 1 + 3 intentos y se para.
[ESTÁTICO] el componente conserva la referencia, no usa Date.now para el temporizador,
    muestra "Sincronizando temporizador…" y limita los reintentos.

Uso: python tests/test_gate6fa1b.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
TOLERANCIA_MS = 250


def codigo(txt: str) -> str:
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"\{/\*.*?\*/\}", "", txt, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", txt)


SIMULACION = r"""
import { sincronizar, horaServidor, conservarReferencia, REINTENTOS_RELOJ_MS } from "./relojClase.ts";
import { restanteMs } from "./restante.ts";

let T = 1_700_000_000_000;            // hora real = reloj del servidor
const avanzar = (ms) => { T += ms; };
let caido = false;                     // /video/hora caído
let peticiones = 0;

function cliente(skew) {
  // skew: reloj de pared del dispositivo (NO lo usa el cálculo; se cambia para probarlo)
  return { skew, monoOrigen: 5_000_000 - T, ref: null, ida: 40, vuelta: 60 };
}
const mono = (c) => T + c.monoOrigen;
async function sync(c) {
  const nueva = await sincronizar(async () => {
    peticiones++;
    if (caido) throw new Error("caído");
    const s = T + c.ida; avanzar(c.ida + c.vuelta); return s;
  }, () => mono(c), 3);
  c.ref = conservarReferencia(c.ref, nueva);
  return !!nueva;
}
// Lo que muestra el componente: sin referencia → nada (null), nunca el reloj local
const mostrado = (c) => (c.ref ? restanteMs(tz, horaServidor(c.ref, mono(c))) : null);

let tz = null;
const iniciar = (s) => { tz = { duracion: s * 1000, restante: s * 1000, inicio: T, pausado: false }; };
const pausar = () => { tz = { ...tz, restante: Math.max(0, tz.restante - (T - tz.inicio)), inicio: T, pausado: true }; };
const reanudar = () => { tz = { ...tz, inicio: T, pausado: false }; };
const real = () => restanteMs(tz, T);
const R = [];
const anotar = (caso, c, extra = {}) => R.push({ caso, mostrado: mostrado(c), real: real(), ...extra });

// 1. sincroniza bien → el endpoint falla → sigue correcto
const a = cliente(0);
await sync(a); iniciar(180); avanzar(10_000);
caido = true; const ok1 = await sync(a); avanzar(60_000);
anotar("1. sincronizado y luego /video/hora falla", a, { resincronizo: ok1 });

// 2. el reloj del dispositivo cambia ±120 s → sin cambio
const antes2 = mostrado(a); a.skew = +120_000; const mas = mostrado(a); a.skew = -120_000; const menos = mostrado(a);
R.push({ caso: "2. reloj del dispositivo ±120 s", mostrado: menos, real: real(), antes: antes2, mas });

// 3 y 4. la primera sincronización falla → nada; el siguiente intento (reintento) funciona
const b = cliente(+120_000);
caido = true; await sync(b);
R.push({ caso: "3. primera sincronización falla", mostrado: mostrado(b), real: real() });
avanzar(REINTENTOS_RELOJ_MS[0]); caido = false; await sync(b);
anotar("4. el reintento funciona", b);

// 5. reconexión con /video/hora caído → última referencia
caido = true; avanzar(5_000); await sync(b); avanzar(5_000);
anotar("5. reconexión con /video/hora caído", b);

// 6. pausa / esperar / reanudar con el endpoint caído
pausar(); avanzar(20_000); anotar("6a. pausado (+20 s)", b);
reanudar(); avanzar(7_000); anotar("6b. reanudado (+7 s)", b);
iniciar(60); avanzar(3_000); anotar("6c. reiniciado (+3 s)", b);

// 7. recuperación 6E: cliente nuevo; falla la primera vez y luego funciona
const c = cliente(-120_000);
await sync(c); R.push({ caso: "7a. recuperación 6E: primera hora falla", mostrado: mostrado(c), real: real() });
avanzar(REINTENTOS_RELOJ_MS[0]); caido = false; await sync(c); anotar("7b. recuperación 6E: reintento funciona", c);

// Reintentos limitados: endpoint siempre caído → 1 intento inicial + reintentos y se para
caido = true; peticiones = 0;
const d = cliente(0); let intentos = 0, reintentos = 0;
const intento = async () => { intentos++; const ok = await sync(d); return ok; };
let ok = await intento();
while (!ok && !d.ref && reintentos < REINTENTOS_RELOJ_MS.length) { avanzar(REINTENTOS_RELOJ_MS[reintentos++]); ok = await intento(); }
avanzar(10 * 60_000);   // 10 minutos después: no hubo más peticiones
console.log(JSON.stringify({ R, limite: { intentos, peticiones, reintentosMs: REINTENTOS_RELOJ_MS, mostrado: mostrado(d) } }));
"""


def parte_reloj(check):
    print("\n[RELOJ] Fallback con /video/hora caído (código real del frontend, en Node)")
    reloj_ts, ix_p, node = WEB / "lib/relojClase.ts", WEB / "components/InteraccionClase.tsx", shutil.which("node")
    if not (reloj_ts.exists() and ix_p.exists() and node):
        check("existen lib/relojClase.ts, InteraccionClase.tsx y Node", False)
        return
    ix = ix_p.read_text(encoding="utf-8").replace("\r\n", "\n")
    m = re.search(r"export function restanteMs\(.*?\n\}\n", ix, re.S)
    if not m or "conservarReferencia" not in reloj_ts.read_text(encoding="utf-8"):
        check("existen restanteMs y conservarReferencia", False)
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
    d = json.loads(p.stdout.strip().splitlines()[-1])
    R = {r["caso"]: r for r in d["R"]}
    cerca = lambda r: r["mostrado"] is not None and abs(r["mostrado"] - r["real"]) <= TOLERANCIA_MS
    seg = lambda r: "—" if r["mostrado"] is None else f"{r['mostrado'] / 1000:.2f} s"
    for caso, r in R.items():
        if caso.startswith(("3.", "7a.")):
            check(f"{caso}: no se muestra ningún tiempo (real {r['real'] / 1000:.2f} s)", r["mostrado"] is None)
        elif caso.startswith("2."):
            check(f"{caso}: sin cambio ({seg(r)})", r["mostrado"] == r["antes"] == r["mas"] and cerca(r))
        else:
            check(f"{caso}: {seg(r)} vs real {r['real'] / 1000:.2f} s", cerca(r)
                  and (r.get("resincronizo") is not True))
    lim = d["limite"]
    check(f"endpoint siempre caído: {lim['intentos']} intentos ({lim['peticiones']} peticiones) y se para; nunca muestra tiempo",
          lim["intentos"] == 1 + len(lim["reintentosMs"]) and lim["peticiones"] == 3 * lim["intentos"] and lim["mostrado"] is None
          and sum(lim["reintentosMs"]) <= 30_000)


def parte_estatica(check):
    print("\n[ESTÁTICO] Componente")
    ix_p = WEB / "components/InteraccionClase.tsx"
    if not ix_p.exists():
        check("existe InteraccionClase.tsx", False)
        return
    ix = ix_p.read_text(encoding="utf-8").replace("\r\n", "\n")
    i0 = ix.find("const referencia = useRef<Referencia | null>(null);")
    bloque = codigo(ix[i0:ix.find("return {", i0)])
    check("si falla, se conserva la última referencia válida",
          "referencia.current = conservarReferencia(referencia.current, ref);" in bloque)
    check("el temporizador no usa el reloj del dispositivo (sin Date.now; NaN sin hora)",
          "Date.now" not in bloque and "horaServidor(referencia.current, performance.now()) : NaN" in bloque)
    check("'listo' solo con una hora real del servidor", "const listo = !!referencia.current;" in bloque
          and "setRelojListo(listo);" in bloque and "setRelojListo(true)" not in bloque)
    check("reintentos limitados (solo si nunca hubo hora) y sin intervalos",
          "reintentosReloj.current < REINTENTOS_RELOJ_MS.length" in bloque and "!reintentoPendiente.current" in bloque
          and "setInterval" not in bloque and "clearTimeout(reintentoPendiente.current)" in bloque)
    check("sin hora: «Sincronizando temporizador…» (y nunca un tiempo)",
          "Sincronizando temporizador…" in ix and "const sincronizandoTemporizador = !!t && !ix.relojListo;" in ix
          and "!!t && ix.relojListo && terminadoHace < 15000" in ix and "{t && ix.relojListo && <span" in ix)


def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    parte_reloj(check)
    parte_estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
