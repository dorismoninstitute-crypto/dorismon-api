"""GATE 6D — Video Dorismon 2.0: experiencia móvil.

Solo interfaz: la sala, la seguridad, la reconexión (6B) y la pantalla previa
(6C) no cambian. Se verifica en el código:
  · ventana mini: Pointer Events (dedo y ratón), siempre dentro de la pantalla,
    fuera de zonas no seguras y SIN tapar la barra inferior; se recoloca al girar;
    pasa por debajo de los diálogos de la plataforma;
  · safe areas (notch, Dynamic Island, indicador de inicio) en full/split;
  · cabecera: título sin cortar (2 líneas), controles de 44 px, orden de prioridad;
  · una sola salida visible (el "Leave" de LiveKit se oculta; 6B sigue marcándolo);
  · cambiar de cámara solo con 2+ cámaras reales y la cámara encendida, sin pedir
    permisos;
  · pantalla previa usable en móvil (44 px, sin zoom en iPhone, acostado);
  · chat con teclado: la capa se ajusta a la parte visible.

Las mediciones en navegador (320/375/390/430 px, horizontal, arrastre con ratón
y con toque, barra inferior de profesor y estudiante, diálogos, teclado) se
describen en el informe de Gate 6D y en gate6d_check_navegador.js.

Actualizado en Gate 6E: backend y localStorage vigilados salvo lo autorizado en 6E.

Uso: python tests/test_gate6d.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import datetime as dt
import hashlib
import os
import re
import sys
from pathlib import Path

import httpx

# Gate 6E (autorizado después) toca puntos concretos del backend y usa
# localStorage para la clase activa: se vigila que NADA MÁS cambie.
sys.path.insert(0, str(Path(__file__).resolve().parent))
# Actualizado en Gate 6G-A: el panel de participantes cambia con autorización; el resto de
# ClassRoomExtras (mano, heartbeat, ventana flotante) debe seguir idéntico.
from _base_gate6ga import extras_sin_6ga  # noqa: E402
from _base_gate6e import huella_sin_gate6e, delete_room_solo_en_6e, localstorage_solo_clase_activa  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}
DB_URL_ORIGINAL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dorismon.db")


def sha(b: bytes) -> str:
    return hashlib.sha256(b.replace(b"\r\n", b"\n")).hexdigest()


def codigo(txt: str) -> str:
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"\{/\*.*?\*/\}", "", txt, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", txt)


def limpiar_sesiones_locales(ids):
    import sqlite3
    if not ids or not DB_URL_ORIGINAL.startswith("sqlite"):
        return None
    ruta = Path(DB_URL_ORIGINAL.split("///", 1)[-1])
    if not ruta.is_file():
        return None
    cx = sqlite3.connect(str(ruta), timeout=30)
    try:
        marcas = ",".join("?" * len(ids))
        for (t,) in cx.execute("select name from sqlite_master where type='table'").fetchall():
            if t == "class_sessions":
                continue
            cols = {r[1] for r in cx.execute(f"pragma table_info({t})")}
            for col in ("session_id", "original_session_id", "makeup_session_id"):
                if col in cols:
                    cx.execute(f"delete from {t} where {col} in ({marcas})", ids)
        n = cx.execute(f"delete from class_sessions where id in ({marcas})", ids).rowcount
        cx.commit()
        return n
    finally:
        cx.close()


async def parte_api(check):
    print("\n[API] La sala no cambia: /join igual que antes")
    creadas = []
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        async def login(cred):
            r = await c.post("/auth/login", json=cred)
            return {"Authorization": f"Bearer {r.json()['access_token']}"}

        AH, TH = await login(ADMIN), await login(TEACHER)
        if not (await c.get("/video/status", headers=AH)).json().get("ready"):
            check("LiveKit de pruebas configurado en el servidor local (LIVEKIT_*)", False)
            return
        prof = (await c.get("/auth/me", headers=TH)).json()
        now = dt.datetime.now(dt.timezone.utc)
        r = await c.post("/admin/events", headers=AH, json={
            "title": "G6D evento", "teacher_id": prof["id"], "modality": "online", "video_provider": "dorismon",
            "starts_at_utc": (now - dt.timedelta(minutes=5)).isoformat(),
            "ends_at_utc": (now + dt.timedelta(minutes=55)).isoformat()})
        evt = (r.json().get("id") or r.json().get("session_id")) if r.status_code in (200, 201) else None
        if evt:
            creadas.append(evt)
        try:
            rj = await c.post(f"/video/sessions/{evt}/join", headers=TH)
            check("/join del profesor → 200 con el mismo contrato",
                  rj.status_code == 200 and set(rj.json()) == {
                      "late", "token", "url", "room", "is_moderator", "title", "display_name", "fallback_url",
                      "expires_in",   # Actualizado en Gate 6G-B: único campo nuevo (vida del token)
                      "grabando"})    # Actualizado en Gate 6H-A: campo aditivo (aviso de grabación)
            hb = await c.post(f"/video/sessions/{evt}/heartbeat", headers=TH)
            check("heartbeat tras entrar → 200", hb.status_code == 200)
        finally:
            n = limpiar_sesiones_locales(creadas)
            print(f"    · limpieza local: {n} sesión(es) de prueba borradas" if n is not None
                  else "    · limpieza local no aplicable")


def parte_estatica(check):
    rutas = {k: WEB / v for k, v in {
        "cp": "components/CallProvider.tsx", "pe": "components/PreEntrada.tsx", "css": "app/globals.css",
        "lay": "app/dashboard/layout.tsx", "clase": "app/clase/[id]/page.tsx",
        "extras": "components/ClassRoomExtras.tsx", "ui": "components/ui.tsx"}.items()}
    if not all(p.exists() for p in rutas.values()):
        check(f"existen los archivos del frontend en {WEB}", False)
        return
    raw = {k: p.read_bytes() for k, p in rutas.items()}
    t = {k: b.decode("utf-8").replace("\r\n", "\n") for k, b in raw.items()}
    cp, cpc, pe, css, lay = t["cp"], codigo(t["cp"]), t["pe"], t["css"], t["lay"]

    print("\n[1] Ventana mini en el móvil")
    check("arrastre con Pointer Events (dedo y ratón): down / move / up / cancel",
          all(f"{e}: (e: React.PointerEvent<HTMLElement>)" in cp for e in ("onPointerDown", "onPointerMove", "onPointerUp", "onPointerCancel")))
    check("captura del puntero al arrastrar", "e.currentTarget.setPointerCapture(e.pointerId)" in cp)
    check("la barra de arrastre no desplaza la página (touch-action: none) y mide 44 px",
          'style={{ touchAction: "none" }}' in cp and "{...arrastrar}" in cp and "min-h-[44px] bg-white/5 cursor-move" in cp)
    check("ya no se arrastra solo con eventos de ratón (mousemove/mouseup)",
          "mousemove" not in cpc and "mouseup" not in cpc and "onMouseDown" not in cpc)
    check("posición por izquierda/arriba y siempre encajada en la pantalla",
          "{ left: pos?.x ?? 0, top: pos?.y ?? 0" in cp and "function encajarMini(" in cp)
    check("no tapa la barra inferior (se mide [data-barra-inferior])",
          "Math.max(alturaBarraInferior(), s.bottom)" in cp and 'querySelectorAll<HTMLElement>("[data-barra-inferior]")' in cp)
    check("las tres barras inferiores (Dirección, profesor, estudiante) están marcadas",
          lay.count('data-barra-inferior=""') == 3 and lay.count('style={{ paddingBottom: "env(safe-area-inset-bottom)" }}') == 3)
    check("respeta zonas seguras (env(safe-area-inset-*))",
          "env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)" in cp)
    check("se recoloca al girar / cambiar de tamaño / crecer",
          'window.addEventListener("orientationchange", reencajar)' in cp and 'window.addEventListener("resize", reencajar)' in cp
          and "new ResizeObserver(reencajar)" in cp)
    check("ancho que cabe en 320 px", "w-[min(300px,calc(100vw-16px))]" in cp)
    check("en pantallas bajas (acostado) el video de la mini es más bajo", "[@media(max-height:500px)]:h-[96px]" in cp)
    check("pasa por debajo de los diálogos de la plataforma mientras están abiertos",
          'document.querySelector("div.fixed.inset-0.z-50")' in cp and '${hayDialogo ? "z-[45]" : "z-[90]"}' in cp)
    check("Salir de la mini: 44 px y separado de «Agrandar»",
          'min-w-[44px] min-h-[44px] rounded-lg transition"\n            aria-label="Salir de la clase"' in cp
          and '<span className="w-2 flex-shrink-0" aria-hidden="true" />' in cp)

    print("\n[2] Pantalla completa / dividida")
    check("safe areas en full/split", all(f'padding{l}: "env(safe-area-inset-{d})"' in cp for l, d in (
        ("Top", "top"), ("Bottom", "bottom"), ("Left", "left"), ("Right", "right"))) and ": estiloSeguro}" in cp)
    check("teclado: la capa se ajusta a la parte visible (visualViewport, sin zoom)",
          "vv.scale === 1 && vv.height < window.innerHeight - 80" in cp)
    check("cabecera: título en su propia línea en el móvil y sin cortar (2 líneas)",
          "flex flex-wrap sm:flex-nowrap" in cp and "line-clamp-2 break-words" in cp and 'truncate" data-titulo-clase' not in cp)
    i_m, i_c, i_p = cp.find("<BotonMano miMano"), cp.find("<BotonCambiarCamara />"), cp.find('title="Participantes"')
    check("prioridad: mano → (cambiar cámara) → participantes → menú → salir",
          0 < i_m < i_c < i_p < cp.find('aria-label="Más opciones"') < cp.find('title="Salir de la clase"'))
    check("objetivos táctiles de 44 px (cabecera, barra de LiveKit, participantes, chat)",
          ".ds-cabecera-llamada button { min-height: 44px; min-width: 44px; }" in css
          and ".ds-capa-llamada .lk-control-bar .lk-button { min-height: 44px; min-width: 44px; }" in css
          and ".ds-capa-llamada aside button { min-height: 44px; min-width: 44px; }" in css)

    print("\n[3] Una sola salida")
    check("el «Leave» de LiveKit no se muestra (queda «Salir» de Dorismon)",
          ".ds-capa-llamada .lk-disconnect-button { display: none !important; }" in css)
    check("6B intacto: cualquier clic en «Leave» sigue siendo salida voluntaria",
          'closest?.(".lk-disconnect-button")) marcarSalida();' in cp and "marcarSalida();  // GATE 6B" in cp)

    print("\n[4] Cambiar de cámara")
    i_b = cp.find("function BotonCambiarCamara")
    boton = cp[i_b:] if i_b > 0 else ""
    check("solo con la cámara encendida y 2+ cámaras reales", "if (!isCameraEnabled || camaras.length < 2) return null;" in boton)
    check("usa la lista real del navegador sin pedir permisos (enumerateDevices, no useMediaDevices)",
          "navigator.mediaDevices?.enumerateDevices()" in boton and "useMediaDevices" not in cpc)
    check("pasa a la siguiente cámara REAL (desde la pista en uso)",
          "getTrackPublication(Track.Source.Camera)" in boton and 'room.switchActiveDevice("videoinput", siguiente.deviceId)' in boton)
    check("no supone nombres «frontal» / «trasera»", not re.search(r"facingMode|front|back|trasera|frontal", codigo(boton)))

    print("\n[5] Pantalla previa en el móvil (reglas de 6C intactas)")
    check("selectores de 44 px y 16 px (sin zoom en iPhone)", pe.count("min-h-[44px] rounded-lg bg-white/10 text-white text-base") == 3)
    check("«Probar sonido» de 44 px", 'px-3 min-h-[44px]" data-probar-sonido' in pe)
    check("acostado: vista y controles lado a lado, sin desplazarse para entrar",
          "[@media(max-height:500px)_and_(min-width:560px)]:grid-cols-2" in pe and "[@media(max-height:500px)]:hidden" in pe)
    check("sigue empezando con cámara y micrófono apagados",
          "useState<LocalVideoTrack | null>(null)" in pe and "useState<LocalAudioTrack | null>(null)" in pe)

    print("\n[6] Chat y teclado")
    check("chat del móvil anclado a la zona de la clase (no a toda la pantalla)",
          "@media (max-width: 600px) {\n  .ds-capa-llamada .lk-chat { position: absolute;" in css)
    check("chat con letra de 16 px (sin zoom al escribir)", ".ds-capa-llamada .lk-chat textarea { font-size: 16px; }" in css)
    check("en la mini no se muestra «fijar participante» (24 px)", ".ds-mini .lk-focus-toggle-button { display: none; }" in css)

    print("\n[7] La sala no cambia")
    check("una sola sala por entrada y mismas opciones (6A)",
          "key={datos.token}" in cp and "const OPCIONES_SALA: RoomOptions = { adaptiveStream: true, dynacast: true };" in cp)
    check("sin <RoomAudioRenderer /> propio (un solo audio)", "<RoomAudioRenderer" not in cpc)
    check("un solo aviso de presencia por modo (heartbeat)", cpc.count("<AvisoDePresencia") == 2)
    check("6B y 6C intactos", "function clasificarDesconexion" in cp and ") : !eleccion ? (\n        <PreEntrada" in cp)
    check("localStorage solo para la clase activa (6E)", localstorage_solo_clase_activa(cpc))
    check("/clase/{id} sin cambios", sha(raw["clase"]) == "2f9007d7022de0401e02f4d6b8f5dd5dff3cd14ff5be4eb91a0dbe075c8ec075")
    check("heartbeat / mano / moderación sin cambios",
          extras_sin_6ga(raw["extras"]))
    ui = t["ui"]
    a = ui.find("export function JoinClassButton")
    b = ui.find("\nexport function", a + 10)
    check("enlaces externos (Zoom / Meet / Teams / fallback) sin cambios",
          sha(ui[a:b].encode("utf-8")) == "ea3b2523bdb49512bf7297da569f139a164eaee8fd0df0eb6ae84d342bfc5e51"
          and cp.count("datos.fallback_url") == 4)
    for rel, h in {
        "app/services/livekit_service.py": "6ad4bc43fb596fd27fd13f9d47264da693a4c9f1a474b07f42de7b8830355985",
        "app/routers/video.py": "6aaa7d192751c9c8c85b20ac17187e26e615c2b5b457af158cca564c68fdc6e0",
        "app/routers/teacher.py": "d91197f1d609aaeef415d5984c30b938fa6b320d588f7ee36103508a0f19f00d",
    }.items():
        check(f"backend sin cambios salvo lo autorizado en 6E: {rel}", huella_sin_gate6e(ROOT, rel) == h)


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    await parte_api(check)
    parte_estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
