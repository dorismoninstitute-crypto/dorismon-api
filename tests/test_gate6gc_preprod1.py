"""PREPROD-1 — Hotfix 6G-C: permisos dinámicos contra la forma REAL de LiveKit Cloud.

Defecto encontrado en LiveKit Cloud real: "Bloquear micrófono" dejaba al estudiante sin
suscripción, sin datos y sin cámara. El backend leía el ParticipantPermission en camelCase,
pero LiveKit responde en snake_case; lo no leído se reconstruía como false / [].

[UNIDAD] cambiar_fuentes() contra un LiveKit simulado LOCAL que responde EXACTAMENTE como
LiveKit Cloud (respuesta capturada en PREPROD-1, 2026-10-17, GetParticipant de un estudiante):
snake_case, TODOS los campos presentes (también los false), fuentes como texto; al recibir
acepta snake_case y camelCase (protojson) y REEMPLAZA el permiso entero (lo que no viene =
false / []). Cubre: bloquear / permitir micrófono y cámara, permitir / quitar pantalla (cada
uno cambia SOLO su fuente), permiso ilegible o incompleto (no se escribe nada), compatibilidad
camelCase y enum numérico.
[ESTÁTICO] sin lecturas camelCase ni defaults peligrosos al reconstruir el permiso.

Uso: python tests/test_gate6gc_preprod1.py [BASE_URL]   (no usa el servidor; BASE_URL se ignora)
"""
import asyncio
import copy
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Respuesta REAL de LiveKit Cloud (permission de GetParticipant, estudiante recién entrado).
PERMISO_REAL_ESTUDIANTE = {
    "can_subscribe": True, "can_publish": True, "can_publish_data": True,
    "can_publish_sources": ["CAMERA", "MICROPHONE"],
    "hidden": False, "recorder": False, "can_update_metadata": False, "agent": False,
    "can_subscribe_metrics": False, "can_manage_agent_session": False,
}
_DEFECTO = {k: ([] if k == "can_publish_sources" else False) for k in PERMISO_REAL_ESTUDIANTE}
_CAMEL = {"canSubscribe": "can_subscribe", "canPublish": "can_publish", "canPublishData": "can_publish_data",
          "canPublishSources": "can_publish_sources", "canUpdateMetadata": "can_update_metadata",
          "canSubscribeMetrics": "can_subscribe_metrics", "canManageAgentSession": "can_manage_agent_session"}
SALA_ID = "11111111-2222-3333-4444-555555555555"
SALA = f"dorismon-{SALA_ID}"
YO = "estudiante-preprod"


class _LKReal(BaseHTTPRequestHandler):
    permiso: dict = {}
    respuesta_get = None       # cuerpo crudo alternativo para GetParticipant (casos ilegibles)
    llamadas: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        cuerpo = json.loads(self.rfile.read(n) or b"{}")
        metodo = self.path.rsplit("/", 1)[-1]
        _LKReal.llamadas.append({"metodo": metodo, "cuerpo": copy.deepcopy(cuerpo)})
        if metodo == "GetParticipant":
            if _LKReal.respuesta_get is not None:
                return self._crudo(_LKReal.respuesta_get)
            return self._r({"sid": "PA_x", "identity": YO, "state": "ACTIVE", "tracks": [],
                            "permission": copy.deepcopy(_LKReal.permiso), "attributes": {}})
        if metodo == "UpdateParticipant":
            nuevo = dict(_DEFECTO)              # LiveKit reemplaza el permiso entero
            for k, v in (cuerpo.get("permission") or {}).items():
                k = _CAMEL.get(k, k)
                if k in nuevo:                  # protojson DiscardUnknown
                    nuevo[k] = v
            _LKReal.permiso = nuevo
            return self._r({"identity": YO, "permission": copy.deepcopy(nuevo)})
        return self._r({})

    def _r(self, obj):
        self._crudo(json.dumps(obj))

    def _crudo(self, texto):
        data = texto.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


async def parte_unidad(check):
    print("\n[UNIDAD] cambiar_fuentes() contra la forma REAL de LiveKit Cloud")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _LKReal)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ.update({"LIVEKIT_URL": f"ws://127.0.0.1:{srv.server_address[1]}",
                       "LIVEKIT_API_KEY": "APIprueba", "LIVEKIT_API_SECRET": "secretodepruebaparatest123456789"})
    sys.path.insert(0, str(ROOT))
    from app.services import livekit_service as ls

    async def cambiar(agregar=(), quitar=(), inicial=None):
        if inicial is not None:
            _LKReal.permiso = copy.deepcopy(inicial)
        _LKReal.llamadas = []
        return await ls.cambiar_fuentes(SALA_ID, YO, agregar, quitar)

    def resto(p):
        return {k: v for k, v in p.items() if k not in ("can_publish_sources", "can_publish")}

    metodos = lambda: [x["metodo"] for x in _LKReal.llamadas]
    base = PERMISO_REAL_ESTUDIANTE
    try:
        check("fuentes_permitidas lee la respuesta REAL (snake_case, enum como texto) → cámara + micrófono",
              ls.fuentes_permitidas(copy.deepcopy(base)) == {"camera", "microphone"})

        print("\n  · Micrófono")
        r = await cambiar(quitar=("microphone",), inicial=base)
        p = _LKReal.permiso
        check(f"Bloquear micrófono → ok, solo cámara ({r})", r == ("ok", ["camera"]))
        check("…micrófono fuera; can_publish sigue true y la cámara sigue permitida",
              p["can_publish"] is True and p["can_publish_sources"] == ["CAMERA"])
        check("…can_subscribe y can_publish_data intactos (sigue viendo / oyendo y con datos)",
              p["can_subscribe"] is True and p["can_publish_data"] is True)
        check("…TODO lo demás del permiso exactamente igual", resto(p) == resto(base))
        check("…solo GetParticipant + UpdateParticipant (nada se apaga ni se enciende)",
              metodos() == ["GetParticipant", "UpdateParticipant"])
        r = await cambiar(agregar=("microphone",))
        check(f"Permitir micrófono → vuelve ({r}); permiso idéntico al inicial",
              r == ("ok", ["camera", "microphone"]) and _LKReal.permiso == base)
        check("…sin encender el dispositivo (sin MutePublishedTrack ni otra acción)",
              metodos() == ["GetParticipant", "UpdateParticipant"])

        print("\n  · Cámara")
        r = await cambiar(quitar=("camera",), inicial=base)
        p = _LKReal.permiso
        check(f"Bloquear cámara → solo micrófono ({r})",
              r == ("ok", ["microphone"]) and p["can_publish_sources"] == ["MICROPHONE"] and p["can_publish"] is True)
        check("…suscripción, datos y resto intactos", resto(p) == resto(base))
        r = await cambiar(agregar=("camera",))
        check("Permitir cámara → permiso idéntico al inicial, sin encenderla",
              r[0] == "ok" and _LKReal.permiso == base and metodos() == ["GetParticipant", "UpdateParticipant"])

        print("\n  · Pantalla")
        r = await cambiar(agregar=("screen_share", "screen_share_audio"), inicial=base)
        p = _LKReal.permiso
        check(f"Permitir pantalla → cámara, micrófono y pantalla ({r})",
              p["can_publish_sources"] == ["CAMERA", "MICROPHONE", "SCREEN_SHARE", "SCREEN_SHARE_AUDIO"])
        check("…suscripción, datos y resto intactos", resto(p) == resto(base))
        r = await cambiar(quitar=("screen_share", "screen_share_audio"))
        check("Quitar pantalla → solo desaparecen las fuentes de pantalla", r[0] == "ok" and _LKReal.permiso == base)
        con_pantalla = dict(base, can_publish_sources=["CAMERA", "MICROPHONE", "SCREEN_SHARE", "SCREEN_SHARE_AUDIO"])
        await cambiar(quitar=("microphone",), inicial=con_pantalla)
        check("con pantalla permitida, bloquear micrófono NO quita la pantalla ni la cámara",
              _LKReal.permiso["can_publish_sources"] == ["CAMERA", "SCREEN_SHARE", "SCREEN_SHARE_AUDIO"]
              and resto(_LKReal.permiso) == resto(base))

        print("\n  · Sin fuentes / campos no usados por Dorismon")
        await cambiar(quitar=("microphone",), inicial=base)
        await cambiar(quitar=("camera",))
        p = _LKReal.permiso
        check("cámara y micrófono bloqueados → can_publish false y lista vacía; suscripción y datos SIGUEN",
              p["can_publish"] is False and p["can_publish_sources"] == [] and p["can_subscribe"] is True
              and p["can_publish_data"] is True)
        r = await cambiar(agregar=("microphone",))
        check("desde can_publish false, permitir micrófono → solo micrófono (no 'todo')",
              r == ("ok", ["microphone"]) and _LKReal.permiso["can_publish_sources"] == ["MICROPHONE"])
        raro = dict(base, hidden=True, can_update_metadata=True, can_subscribe_metrics=True, recorder=True,
                    agent=True, can_manage_agent_session=True)
        await cambiar(quitar=("camera",), inicial=raro)
        check("hidden / can_update_metadata / can_subscribe_metrics / recorder / agent… se conservan",
              resto(_LKReal.permiso) == resto(raro))
        await cambiar(quitar=("camera",), inicial=dict(base, can_subscribe=False, can_publish_data=False))
        check("un false legítimo se conserva como false (no se 'arregla' a true)",
              _LKReal.permiso["can_subscribe"] is False and _LKReal.permiso["can_publish_data"] is False)

        print("\n  · Permiso ilegible / incompleto → NO se escribe nada")
        casos = {
            "sin permission": json.dumps({"identity": YO}),
            "permission null": json.dumps({"identity": YO, "permission": None}),
            "permission vacío": json.dumps({"identity": YO, "permission": {}}),
            "falta can_subscribe": json.dumps({"permission": {k: v for k, v in base.items() if k != "can_subscribe"}}),
            "falta can_publish_data": json.dumps({"permission": {k: v for k, v in base.items() if k != "can_publish_data"}}),
            "falta can_publish": json.dumps({"permission": {k: v for k, v in base.items() if k != "can_publish"}}),
            "falta can_publish_sources": json.dumps({"permission": {k: v for k, v in base.items()
                                                                    if k != "can_publish_sources"}}),
            "tipo inesperado": json.dumps({"permission": dict(base, can_subscribe="true")}),
            "fuente desconocida": json.dumps({"permission": dict(base, can_publish_sources=["CAMERA", "HOLOGRAMA"])}),
            "snake y camel en conflicto": json.dumps({"permission": dict(base, canSubscribe=False)}),
            "no es JSON": "<html>bad gateway</html>",
            "JSON que no es objeto": json.dumps(["permission"]),
        }
        for nombre, cuerpo in casos.items():
            _LKReal.permiso = copy.deepcopy(base)
            _LKReal.respuesta_get = cuerpo
            try:
                r = await cambiar(quitar=("microphone",))
            except Exception as e:
                r = ("excepción", type(e).__name__)
            finally:
                _LKReal.respuesta_get = None
            check(f"{nombre} → error ({r[0]}), sin UpdateParticipant y permiso intacto",
                  r == ("error", None) and "UpdateParticipant" not in metodos() and _LKReal.permiso == base)

        print("\n  · Compatibilidad")
        camel = {"canSubscribe": True, "canPublish": True, "canPublishData": True, "canPublishSources": ["CAMERA", "MICROPHONE"]}
        _LKReal.respuesta_get = json.dumps({"permission": camel})
        r = await cambiar(quitar=("microphone",), inicial=base)
        _LKReal.respuesta_get = None
        enviado = next(x["cuerpo"]["permission"] for x in _LKReal.llamadas if x["metodo"] == "UpdateParticipant")
        check("camelCase (compatibilidad) → mismo resultado, y se ENVÍA en snake_case",
              r == ("ok", ["camera"]) and enviado.get("can_subscribe") is True and enviado.get("can_publish_data") is True
              and enviado.get("can_publish_sources") == ["CAMERA"] and not any(c.isupper() for k in enviado for c in k))
        r = await cambiar(quitar=("camera",), inicial=dict(base, can_publish_sources=[1, 2]))
        check("enum numérico (1 = CAMERA, 2 = MICROPHONE) → se entiende", r == ("ok", ["microphone"]))
        r = await cambiar(quitar=("screen_share",), inicial=dict(base, can_publish_sources=[]))
        check("can_publish con lista vacía (= todo) → quitar pantalla deja cámara, micrófono y audio de pantalla",
              r == ("ok", ["camera", "microphone", "screen_share_audio"]))
    finally:
        srv.shutdown()


def parte_estatica(check):
    print("\n[ESTÁTICO]")
    lk = (ROOT / "app/services/livekit_service.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    ini = lk.index("def fuentes_permitidas")
    fin = lk.index("r = await _sala(session_id, \"UpdateParticipant\"", ini)
    bloque = lk[ini:fin]
    check("fuentes_permitidas / cambiar_fuentes no leen claves camelCase (.get(\"canX\"))",
          ".get(\"can" not in bloque.replace(".get(\"can_", ""))
    check("sin defaults peligrosos al reconstruir el permiso (bool(actual.get(…)), get(…, False), get(…, []))",
          "bool(actual.get(" not in bloque and ", False)" not in bloque and ", [])" not in bloque)
    check("permiso ilegible → no se escribe (return \"error\" antes de UpdateParticipant)",
          "if actual is None:" in bloque and "return \"error\", None" in bloque)
    check("se parte del permiso vigente COMPLETO y solo se cambian las fuentes",
          "permiso = dict(actual)" in bloque and 'permiso["can_publish"] = bool(orden)' in bloque)


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    try:
        await parte_unidad(check)
    except Exception as e:
        check(f"la parte de unidad se ejecuta ({type(e).__name__}: {e})", False)
    try:
        parte_estatica(check)
    except Exception as e:
        check(f"la parte estática se ejecuta ({type(e).__name__}: {e})", False)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
