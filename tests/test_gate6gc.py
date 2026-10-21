"""GATE 6G-C — Permisos dinámicos de cámara, micrófono y compartir pantalla.

[INTEGRACIÓN] App EN ESTE PROCESO contra un LiveKit simulado LOCAL que:
    · aplica la regla real de permisos de administración (roomAdmin + sala);
    · guarda el ParticipantPermission de cada persona (GetParticipant / UpdateParticipant,
      reemplazo COMPLETO como en LiveKit);
    · responde a "publicar una fuente" con la regla real de LiveKit (GetCanPublishSource:
      sin canPublish nada; con lista vacía todo; si no, solo lo listado) → NOT_ALLOWED.
    Cubre: fuentes iniciales por rol (token), permitir / quitar pantalla, bloquear /
    permitir micrófono y cámara (sin tocar las otras fuentes ni el resto del permiso,
    sin encender nada), silenciar ≠ bloquear, seguridad (estudiante, moderadores
    protegidos, otra sala), concurrencia, fallo de LiveKit, reentrada con permisos por
    defecto y datos (canPublishData) intactos.
[ESTÁTICO] barra de LiveKit oculta lo no permitido; avisos al estudiante; panel del profesor.

Uso: python tests/test_gate6gc.py [BASE_URL] [RUTA_DORISMON_WEB]
"""
import asyncio
import base64
import datetime as dt
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
WEB = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT.parent / "dorismon-web"
ADMIN = {"email": "admin@dorismon.do", "password": "Adm-Test-4821!"}
TEACHER = {"email": "luis@dorismon.do", "password": "Prof-Test-4822!"}
MARIA = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
CARLOS = {"email": "carlos.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
DB_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dorismon.db")
TOKEN_A_ENUM = {"camera": "CAMERA", "microphone": "MICROPHONE", "screen_share": "SCREEN_SHARE",
                "screen_share_audio": "SCREEN_SHARE_AUDIO"}


def claims(tok):
    try:
        p = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}


# PREPROD-1 (hotfix 6G-C) — En el cable, como LiveKit Cloud REAL: GetParticipant / UpdateParticipant
# responden el ParticipantPermission en snake_case, con TODOS los campos (también los false) y las
# fuentes como texto. Al recibir, LiveKit (protojson) acepta snake_case y camelCase, y lo que no
# viene queda en false / []. El modelo interno del simulador (_LK.permisos) sigue igual.
_CAMPOS_PERMISO = {"canSubscribe": "can_subscribe", "canPublish": "can_publish", "canPublishData": "can_publish_data",
                   "canPublishSources": "can_publish_sources", "hidden": "hidden", "recorder": "recorder",
                   "canUpdateMetadata": "can_update_metadata", "agent": "agent",
                   "canSubscribeMetrics": "can_subscribe_metrics", "canManageAgentSession": "can_manage_agent_session"}


def permiso_a_livekit(p: dict) -> dict:
    return {s: p.get(c, [] if c == "canPublishSources" else False) for c, s in _CAMPOS_PERMISO.items()}


def permiso_de_livekit(j: dict) -> dict:
    inv = {s: c for c, s in _CAMPOS_PERMISO.items()}
    return {inv.get(k, k): v for k, v in j.items()}


def puede_publicar(permiso: dict, fuente: str) -> bool:
    """Regla de LiveKit (auth.VideoGrant.GetCanPublishSource / LocalParticipant)."""
    if not permiso.get("canPublish"):
        return False
    lista = permiso.get("canPublishSources") or []
    return not lista or fuente in lista


class _LK(BaseHTTPRequestHandler):
    permisos: dict = {}       # (sala, identidad) → ParticipantPermission (JSON)
    llamadas: list = []
    fallar: set = set()

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        cuerpo = json.loads(self.rfile.read(n) or b"{}")
        metodo = self.path.rsplit("/", 1)[-1]
        v = claims((self.headers.get("Authorization") or "").replace("Bearer ", "")).get("video") or {}
        _LK.llamadas.append({"metodo": metodo, "cuerpo": cuerpo, "video": v})
        clave = (cuerpo.get("room"), cuerpo.get("identity"))
        if metodo == "_publicar":     # "el cliente intenta publicar": lo decide el servidor
            ok = puede_publicar(_LK.permisos.get(clave, {}), cuerpo.get("source"))
            return self._r(200 if ok else 403, {"reason": "OK" if ok else "NOT_ALLOWED"})
        admin = {"ListParticipants", "MutePublishedTrack", "RemoveParticipant", "UpdateParticipant",
                 "UpdateRoomMetadata", "GetParticipant"}
        if metodo in admin and not (v.get("roomAdmin") and v.get("room") == cuerpo.get("room")):
            return self._r(403, {"code": "permission_denied"})
        if metodo in _LK.fallar:
            return self._r(503, {"code": "unavailable"})
        if metodo == "GetParticipant":
            if clave not in _LK.permisos:
                return self._r(404, {"code": "not_found", "msg": "participant not found"})
            return self._r(200, {"identity": clave[1], "permission": permiso_a_livekit(_LK.permisos[clave])})
        if metodo == "UpdateParticipant":
            if clave not in _LK.permisos:
                return self._r(404, {"code": "not_found"})
            if "permission" in cuerpo:
                # LiveKit REEMPLAZA el permiso entero (protojson: lo que no viene = false/vacío)
                _LK.permisos[clave] = {k: val for k, val in permiso_de_livekit(cuerpo["permission"]).items()
                                       if val not in (False, [], None)}
            return self._r(200, {"identity": clave[1], "permission": permiso_a_livekit(_LK.permisos[clave])})
        if metodo == "ListParticipants":
            sala = cuerpo.get("room")
            return self._r(200, {"participants": [
                {"identity": i, "tracks": [{"sid": "TR_" + i[:4], "type": "AUDIO"}]}
                for (s, i) in _LK.permisos if s == sala]})
        if metodo == "ListRooms":
            return self._r(200, {"rooms": [{"name": (cuerpo.get("names") or [""])[0], "metadata": ""}]})
        return self._r(200, {})

    def _r(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def limpiar(ids):
    import sqlite3
    if not ids or not DB_URL.startswith("sqlite"):
        return None
    ruta = Path(DB_URL.split("///", 1)[-1])
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
        cx.execute(f"delete from audit_logs where module = 'video' and target_id in ({marcas})", ids)
        n = cx.execute(f"delete from class_sessions where id in ({marcas})", ids).rowcount
        cx.commit()
        return n
    finally:
        cx.close()


async def parte_integracion(check):
    print("\n[INTEGRACIÓN] App en proceso + LiveKit simulado (permisos reales de publicación)")
    sys.path.insert(0, str(ROOT))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _LK)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    lk_url = f"http://127.0.0.1:{srv.server_address[1]}"
    os.environ.update({"LIVEKIT_URL": lk_url.replace("http://", "ws://"),
                       "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY") or "APIprueba",
                       "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET") or "secretodepruebaparatest123456789"})
    try:
        from app.main import app
        from app.core.db import engine
        from app.models import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        check(f"la app se importa en proceso ({type(e).__name__}: {e})", False)
        return
    creadas = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c, \
            httpx.AsyncClient(base_url=lk_url, timeout=10) as lk:
        async def login(cred):
            return {"Authorization": f"Bearer {(await c.post('/auth/login', json=cred)).json()['access_token']}"}
        AH, TH, MH, CH = [await login(x) for x in (ADMIN, TEACHER, MARIA, CARLOS)]
        ids = {k: (await c.get("/auth/me", headers=h)).json()["id"]
               for k, h in (("admin", AH), ("prof", TH), ("maria", MH), ("carlos", CH))}
        now = dt.datetime.now(dt.timezone.utc)

        async def evento(nombre, **extra):
            body = {"title": nombre, "teacher_id": ids["prof"], "modality": "online", "video_provider": "dorismon",
                    "starts_at_utc": (now - dt.timedelta(minutes=5)).isoformat(),
                    "ends_at_utc": (now + dt.timedelta(minutes=55)).isoformat()}
            body.update(extra)
            r = await c.post("/admin/events", headers=AH, json=body)
            i = r.json().get("id") if r.status_code in (200, 201) else None
            if i:
                creadas.append(i)
            return i

        a, otra = await evento("G6GC clase"), await evento("G6GC otra clase")
        check("se crean las clases de prueba", bool(a and otra))
        if not (a and otra):
            return
        sala, sala2 = f"dorismon-{a}", f"dorismon-{otra}"
        try:
            async def entrar(h, quien, sid=a):
                """/join real → el simulador registra el permiso que LiveKit derivaría del token."""
                d = (await c.post(f"/video/sessions/{sid}/join", headers=h)).json()
                v = claims(d["token"])["video"]
                _LK.permisos[(f"dorismon-{sid}", ids[quien])] = {
                    "canSubscribe": v.get("canSubscribe"), "canPublish": v.get("canPublish"),
                    "canPublishData": v.get("canPublishData"),
                    "canPublishSources": [TOKEN_A_ENUM[x] for x in v.get("canPublishSources", [])]}
                return v

            async def publica(quien, fuente, s=None):
                r = await lk.post("/twirp/livekit.RoomService/_publicar",
                                  json={"room": s or sala, "identity": ids[quien], "source": fuente})
                return r.status_code == 200

            perm = lambda quien, s=None: _LK.permisos[(s or sala, ids[quien])]
            M = lambda h, accion, quien: c.post(f"/video/sessions/{a}/moderate", headers=h,
                                                json={"action": accion, "identity": ids[quien]})

            print("\n  · Token inicial (fuentes por rol)")
            v = await entrar(MH, "maria")
            check("estudiante: canPublishSources = cámara + micrófono (sin pantalla), datos sí, sin roomAdmin",
                  v.get("canPublishSources") == ["camera", "microphone"] and v.get("canPublishData") is True
                  and "roomAdmin" not in v)
            check("estudiante: cámara y micrófono permitidos por LiveKit",
                  await publica("maria", "CAMERA") and await publica("maria", "MICROPHONE"))
            check("estudiante: compartir pantalla RECHAZADO por LiveKit (NOT_ALLOWED), no solo oculto",
                  not await publica("maria", "SCREEN_SHARE") and not await publica("maria", "SCREEN_SHARE_AUDIO"))
            v = await entrar(TH, "prof")
            check("profesor: cámara, micrófono, pantalla y audio de pantalla",
                  v.get("canPublishSources") == ["camera", "microphone", "screen_share", "screen_share_audio"]
                  and all([await publica("prof", f) for f in TOKEN_A_ENUM.values()]))
            v = await entrar(AH, "admin")
            check("Dirección: igual que el profesor (sin roomAdmin)",
                  v.get("canPublishSources") == ["camera", "microphone", "screen_share", "screen_share_audio"]
                  and "roomAdmin" not in v)
            await entrar(CH, "carlos")

            print("\n  · Compartir pantalla con autorización")
            _LK.llamadas = []
            r = await M(TH, "permitir_pantalla", "maria")
            up = [x for x in _LK.llamadas if x["metodo"] == "UpdateParticipant"]
            check(f"profesor permite pantalla (200 {r.json().get('fuentes') if r.status_code == 200 else r.text[:60]})",
                  r.status_code == 200)
            check("primero LEE el permiso vigente (GetParticipant) y luego lo escribe COMPLETO, con token de la sala",
                  [x["metodo"] for x in _LK.llamadas] == ["GetParticipant", "UpdateParticipant"]
                  and up and up[0]["video"].get("room") == sala and up[0]["video"].get("roomAdmin"))
            check("la estudiante ya puede publicar pantalla (y su audio)",
                  await publica("maria", "SCREEN_SHARE") and await publica("maria", "SCREEN_SHARE_AUDIO"))
            check("cámara, micrófono, datos y suscripción intactos",
                  await publica("maria", "CAMERA") and await publica("maria", "MICROPHONE")
                  and perm("maria").get("canPublishData") is True and perm("maria").get("canSubscribe") is True)
            check("no se enciende nada solo (ninguna llamada de publicar/activar desde el servidor)",
                  not any(x["metodo"] in ("MutePublishedTrack",) for x in _LK.llamadas))
            r = await M(TH, "quitar_pantalla", "maria")
            check("profesor quita pantalla → LiveKit ya no la deja publicar",
                  r.status_code == 200 and not await publica("maria", "SCREEN_SHARE"))
            check("…y cámara / micrófono siguen permitidos",
                  await publica("maria", "CAMERA") and await publica("maria", "MICROPHONE"))

            print("\n  · Micrófono: silenciar ≠ bloquear")
            _LK.llamadas = []
            r = await M(TH, "mute", "maria")
            check("Silenciar (6G-A): solo MutePublishedTrack; el permiso de micrófono NO cambia (puede volver a activarlo)",
                  r.status_code == 200 and "UpdateParticipant" not in [x["metodo"] for x in _LK.llamadas]
                  and await publica("maria", "MICROPHONE"))
            r = await M(TH, "bloquear_microfono", "maria")
            check("Bloquear micrófono → LiveKit ya no deja publicarlo",
                  r.status_code == 200 and not await publica("maria", "MICROPHONE"))
            check("…sin tocar cámara ni datos", await publica("maria", "CAMERA") and perm("maria").get("canPublishData") is True)
            _LK.llamadas = []
            r = await M(TH, "permitir_microfono", "maria")
            check("Permitir micrófono → vuelve el permiso", r.status_code == 200 and await publica("maria", "MICROPHONE"))
            check("…pero NO lo enciende (ni MutePublishedTrack muted=false ni otra acción)",
                  [x["metodo"] for x in _LK.llamadas if x["metodo"] != "_publicar"] == ["GetParticipant", "UpdateParticipant"])

            print("\n  · Cámara")
            r = await M(AH, "bloquear_camara", "maria")
            check("Dirección bloquea la cámara → no puede publicarla; micrófono intacto",
                  r.status_code == 200 and not await publica("maria", "CAMERA") and await publica("maria", "MICROPHONE"))
            _LK.llamadas = []
            r = await M(TH, "permitir_camara", "maria")
            check("Permitir cámara → permiso de vuelta, sin encenderla",
                  r.status_code == 200 and await publica("maria", "CAMERA")
                  and [x["metodo"] for x in _LK.llamadas if x["metodo"] != "_publicar"] == ["GetParticipant", "UpdateParticipant"])

            print("\n  · Sin fuentes nunca significa 'todas'")
            await M(TH, "bloquear_microfono", "maria")
            r = await M(TH, "bloquear_camara", "maria")
            p = perm("maria")
            check("bloquear cámara y micrófono → canPublish = false (una lista vacía NO abre todo)",
                  r.status_code == 200 and not p.get("canPublish")
                  and not any([await publica("maria", f) for f in TOKEN_A_ENUM.values()]))
            r = await M(TH, "permitir_microfono", "maria")
            check("permitir solo micrófono → solo micrófono",
                  await publica("maria", "MICROPHONE") and not await publica("maria", "CAMERA")
                  and not await publica("maria", "SCREEN_SHARE"))
            await M(TH, "permitir_camara", "maria")

            print("\n  · Seguridad")
            check("la estudiante NO puede cambiar sus propios permisos (403)",
                  (await M(MH, "permitir_pantalla", "maria")).status_code == 403 and not await publica("maria", "SCREEN_SHARE"))
            check("la estudiante NO puede cambiar los de otro (403)",
                  (await M(MH, "bloquear_microfono", "carlos")).status_code == 403 and await publica("carlos", "MICROPHONE"))
            check("el profesor NO puede tocar a Dirección (400)",
                  (await M(TH, "bloquear_microfono", "admin")).status_code == 400 and await publica("admin", "MICROPHONE"))
            check("Dirección NO puede tocar al profesor de la clase (400)",
                  (await M(AH, "quitar_pantalla", "prof")).status_code == 400 and await publica("prof", "SCREEN_SHARE"))
            await entrar(MH, "maria", otra)
            antes_otra = dict(perm("maria", sala2))
            await M(TH, "bloquear_microfono", "maria")
            check("cambiar permisos en ESTA clase no afecta a OTRA clase", perm("maria", sala2) == antes_otra)
            await M(TH, "permitir_microfono", "maria")

            print("\n  · Concurrencia y fallos")
            rs = await asyncio.gather(M(TH, "bloquear_camara", "carlos"), M(AH, "permitir_pantalla", "carlos"),
                                      M(TH, "bloquear_microfono", "carlos"))
            check(f"profesor y Dirección a la vez sobre la misma persona ({[r.status_code for r in rs]}): "
                  "se aplican LOS TRES cambios (ninguno pisa a otro)",
                  all(r.status_code == 200 for r in rs) and not await publica("carlos", "CAMERA")
                  and not await publica("carlos", "MICROPHONE") and await publica("carlos", "SCREEN_SHARE"))
            antes = dict(perm("carlos"))
            _LK.fallar = {"UpdateParticipant"}
            r = await M(TH, "permitir_camara", "carlos")
            _LK.fallar = set()
            check("LiveKit falla → 502 con mensaje claro y el permiso NO cambia (sin éxito falso)",
                  r.status_code == 502 and "No se pudo cambiar el permiso" in r.text and perm("carlos") == antes)
            _LK.permisos.pop((sala, ids["carlos"]))
            r = await M(TH, "permitir_camara", "carlos")
            check("persona no conectada → 400 claro", r.status_code == 400 and "conectada" in r.text)

            print("\n  · Reentrada y datos")
            await M(TH, "permitir_pantalla", "maria")
            v = await entrar(MH, "maria")
            check("nuevo /join → permisos POR DEFECTO (la autorización de pantalla era de esa conexión)",
                  v.get("canPublishSources") == ["camera", "microphone"] and not await publica("maria", "SCREEN_SHARE"))
            r = await c.post(f"/video/sessions/{a}/interaccion", headers=MH, json={"accion": "mano", "arriba": True})
            check(f"datos / interacción (mano 6F-A) siguen funcionando ({r.status_code}) y canPublishData intacto",
                  r.status_code == 200 and perm("maria").get("canPublishData") is True)
        finally:
            srv.shutdown()
            n = limpiar(creadas)
            print(f"    · limpieza local: {n} sesión(es)" if n is not None else "    · limpieza no aplicable")


def parte_estatica(check):
    print("\n[ESTÁTICO]")
    lk = (ROOT / "app/services/livekit_service.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    check("token: canPublishSources por rol (moderador / estudiante)",
          '"canPublishSources": list(FUENTES_MODERADOR if is_moderator else FUENTES_ESTUDIANTE),' in lk
          and 'FUENTES_ESTUDIANTE = ("camera", "microphone")' in lk)
    check("cambio de permisos: lee el vigente, conserva el resto, nunca lista vacía con canPublish",
          '"GetParticipant"' in lk and 'permiso["can_publish"] = bool(orden)' in lk and "asyncio.Lock()" in lk)
    ex = (WEB / "components/ClassRoomExtras.tsx").read_text(encoding="utf-8")
    check("panel del profesor: permitir/quitar pantalla, bloquear/permitir micrófono y cámara",
          all(x in ex for x in ("Permitir pantalla", "Quitar pantalla", "Bloquear micrófono", "Permitir micrófono",
                                "Bloquear cámara", "Permitir cámara")))
    check("el estado mostrado sale de LiveKit (permisos reales) y se repinta al cambiar",
          "puedePublicar(p, fuente)" in ex and "RoomEvent.ParticipantPermissionsChanged" in ex)
    ix = (WEB / "components/InteraccionClase.tsx").read_text(encoding="utf-8")
    check("aviso al estudiante: micrófono / cámara bloqueados y cambio de permiso de pantalla",
          "El profesor bloqueó temporalmente tu micrófono." in ix and "El profesor bloqueó temporalmente tu cámara." in ix
          and "El profesor te permitió compartir pantalla." in ix and "useLocalParticipantPermissions" in ix)
    cp = (WEB / "components/CallProvider.tsx").read_text(encoding="utf-8")
    check("avisos en completa y en mini; la barra de LiveKit oculta lo no permitido (sin reescribir VideoConference)",
          "<AvisoPermisos />" in cp and "<AvisoPermisos compacto />" in cp
          and cp.count("<VideoConference SettingsComponent={AplicarFoco} />") == 2)
    check("pantalla previa intacta: cámara y micrófono apagados por defecto",
          "        connect\n        video={opcionVideo}\n        audio={opcionAudio}\n" in cp.replace("\r\n", "\n"))


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    await parte_integracion(check)
    parte_estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
