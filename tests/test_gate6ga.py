"""GATE 6G-A — Moderación real y expulsión persistente.

[INTEGRACIÓN] La app FastAPI EN ESTE PROCESO (misma base de datos que el servidor
    local) contra un LiveKit simulado LOCAL que aplica la regla REAL de LiveKit
    (EnsureAdminPermission: el token debe tener roomAdmin Y nombrar la sala pedida;
    si no, 403 permission_denied). Así un token sin sala FALLA, como en producción.
    · Silenciar: profesor y Dirección sí; estudiante no; nadie silencia al profesor
      de la clase ni a Dirección; sin micrófono / no conectado → mensaje claro.
    · Sacar: bloqueo guardado + RemoveParticipant con token de ESA sala;
      /join posterior → 403 "El profesor te retiró de esta clase." SIN token;
      heartbeat e interacción rechazados; LiveKit falla → 502 y el bloqueo SIGUE.
    · Permitir volver → /join vuelve a dar token; reexpulsar → bloqueo de nuevo
      (times_blocked = 2); doble clic / concurrencia → una sola fila activa.
    · Otra clase y otro estudiante no quedan bloqueados; completed / cancelled
      siguen rechazadas; clase externa → 400 sin llamar a LiveKit.
    · Auditoría: video_block / video_unblock / video_remove / video_mute.
[ESTÁTICO] modelo + restricción única, orden seguro, sin tokens guardados, UI.

Uso: python tests/test_gate6ga.py [BASE_URL] [RUTA_DORISMON_WEB]
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
OTRO_PROFE = {"email": "ana@dorismon.do", "password": "Prof-Test-4822!"}
MARIA = {"email": "maria.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
CARLOS = {"email": "carlos.estudiante@dorismon.do", "password": "Estu-Test-4823!"}
DB_URL = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./dorismon.db")


def codigo(txt: str) -> str:
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"\{/\*.*?\*/\}", "", txt, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", txt)


def _claims(tok):
    try:
        p = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}


# ------------------------------------------------ LiveKit simulado (regla real)
class _LK(BaseHTTPRequestHandler):
    llamadas: list = []
    conectados: dict = {}     # identidad → {"audio": bool}
    fallar: set = set()       # métodos que responden 503

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        cuerpo = json.loads(self.rfile.read(n) or b"{}")
        metodo = self.path.rsplit("/", 1)[-1]
        cl = _claims((self.headers.get("Authorization") or "").replace("Bearer ", ""))
        v = cl.get("video") or {}
        _LK.llamadas.append({"metodo": metodo, "cuerpo": cuerpo, "video": v})
        admin = {"ListParticipants", "MutePublishedTrack", "RemoveParticipant",
                 "UpdateParticipant", "UpdateRoomMetadata", "GetParticipant"}
        # EnsureAdminPermission de LiveKit: roomAdmin Y room del token == room pedida
        if metodo in admin and not (v.get("roomAdmin") and v.get("room") == cuerpo.get("room")):
            return self._r(403, {"code": "permission_denied", "msg": "permissions denied"})
        if metodo in _LK.fallar:
            return self._r(503, {"code": "unavailable"})
        if metodo == "ListParticipants":
            return self._r(200, {"participants": [
                {"identity": i, "tracks": ([{"sid": f"TR_{i[:6]}", "type": "AUDIO"}] if d.get("audio") else [])}
                for i, d in _LK.conectados.items()]})
        if metodo == "MutePublishedTrack":
            return self._r(200, {"track": {"sid": cuerpo.get("trackSid"), "muted": cuerpo.get("muted")}})
        if metodo == "RemoveParticipant":
            if cuerpo.get("identity") not in _LK.conectados:
                return self._r(404, {"code": "not_found", "msg": "participant does not exist"})
            _LK.conectados.pop(cuerpo["identity"], None)
            return self._r(200, {})
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
        if "audit_logs" in {r[0] for r in cx.execute("select name from sqlite_master")}:
            cx.execute(f"delete from audit_logs where module = 'video' and target_id in ({marcas})", ids)
        n = cx.execute(f"delete from class_sessions where id in ({marcas})", ids).rowcount
        cx.commit()
        return n
    finally:
        cx.close()


# ============================================================================ INTEGRACIÓN
async def parte_integracion(check):
    print("\n[INTEGRACIÓN] App en proceso + LiveKit simulado con la regla real de permisos")
    sys.path.insert(0, str(ROOT))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _LK)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    os.environ.update({"LIVEKIT_URL": f"ws://127.0.0.1:{srv.server_address[1]}",
                       "LIVEKIT_API_KEY": os.environ.get("LIVEKIT_API_KEY") or "APIprueba",
                       "LIVEKIT_API_SECRET": os.environ.get("LIVEKIT_API_SECRET") or "secretodepruebaparatest123456789"})
    try:
        from app.main import app
        from app.core.db import engine
        from app.models import Base
        async with engine.begin() as conn:      # crea SOLO tablas que falten (aditivo)
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        check(f"la app se importa en proceso ({type(e).__name__}: {e})", False)
        return
    creadas = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(cred):
            r = await c.post("/auth/login", json=cred)
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        AH, TH, OH, MH, CH = [await login(x) for x in (ADMIN, TEACHER, OTRO_PROFE, MARIA, CARLOS)]
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

        a, otra, ext, fin = (await evento("G6GA clase"), await evento("G6GA otra clase"),
                             await evento("G6GA externa", video_provider="meet",
                                          meeting_url="https://meet.google.com/abc-defg-hij"),
                             await evento("G6GA finalizar"))
        check("se crean las clases de prueba", all((a, otra, ext, fin)))
        if not all((a, otra, ext, fin)):
            return
        try:
            J = lambda h, sid: c.post(f"/video/sessions/{sid}/join", headers=h)
            M = lambda h, sid, **b: c.post(f"/video/sessions/{sid}/moderate", headers=h, json=b)
            for h in (TH, MH, CH):
                await J(h, a)
            sala = f"dorismon-{a}"

            print("\n  · Silenciar")
            _LK.conectados = {ids["maria"]: {"audio": True}, ids["carlos"]: {"audio": False}, ids["prof"]: {"audio": True}}
            _LK.llamadas = []
            r = await M(TH, a, action="mute", identity=ids["maria"])
            mutes = [x for x in _LK.llamadas if x["metodo"] == "MutePublishedTrack"]
            check(f"profesor silencia a la estudiante (200 → {r.status_code})", r.status_code == 200 and len(mutes) == 1)
            check("MutePublishedTrack y ListParticipants con token de ESA sala (room + roomAdmin)",
                  all(x["video"].get("room") == sala and x["video"].get("roomAdmin") for x in _LK.llamadas)
                  and mutes and mutes[0]["cuerpo"]["muted"] is True and mutes[0]["cuerpo"]["identity"] == ids["maria"])
            check("token de moderación sin permisos de participante (sin roomJoin/canPublish)",
                  all("roomJoin" not in x["video"] and "canPublish" not in x["video"] for x in _LK.llamadas))
            r = await M(AH, a, action="mute", identity=ids["maria"])
            check(f"Dirección silencia a la estudiante ({r.status_code})", r.status_code == 200)
            r = await M(MH, a, action="mute", identity=ids["carlos"])
            check("estudiante NO puede silenciar (403)", r.status_code == 403)
            r = await M(OH, a, action="mute", identity=ids["maria"])
            check("otro profesor (no es su clase) NO puede moderar (403)", r.status_code == 403)
            r = await M(AH, a, action="mute", identity=ids["prof"])
            check("nadie silencia al profesor de la clase (400)", r.status_code == 400)
            r = await M(TH, a, action="mute", identity=ids["admin"])
            check("el profesor NO silencia a Dirección (400)", r.status_code == 400 and "Dirección" in r.text)
            r = await M(TH, a, action="remove", identity=ids["admin"])
            check("el profesor NO saca a Dirección (400)", r.status_code == 400)
            r = await M(TH, a, action="mute", identity=ids["carlos"])
            check("sin micrófono publicado → mensaje claro (400)", r.status_code == 400 and "micrófono" in r.text)
            _LK.conectados.pop(ids["carlos"])
            r = await M(TH, a, action="mute", identity=ids["carlos"])
            check("no conectado → mensaje claro (400)", r.status_code == 400 and "conectada" in r.text)
            _LK.conectados[ids["carlos"]] = {"audio": True}

            print("\n  · Sacar (orden seguro y persistencia)")
            _LK.llamadas = []
            r = await M(TH, a, action="remove", identity=ids["maria"])
            jr = r.json() if r.status_code == 200 else {}
            rem = [x for x in _LK.llamadas if x["metodo"] == "RemoveParticipant"]
            check(f"profesor saca a la estudiante → bloqueada y desconectada ({r.status_code} {jr})",
                  r.status_code == 200 and jr.get("bloqueado") is True and jr.get("desconectado") is True)
            check("RemoveParticipant con token de ESA sala, su identidad y revocación de token (revokeTokenTs)",
                  len(rem) == 1 and rem[0]["video"].get("room") == sala and rem[0]["cuerpo"]["identity"] == ids["maria"]
                  and isinstance(rem[0]["cuerpo"].get("revokeTokenTs"), int))
            rj = await J(MH, a)
            check("/join posterior → 403 'El profesor te retiró de esta clase.'",
                  rj.status_code == 403 and "El profesor te retiró de esta clase." in rj.text)
            check("y NO se emite token", rj.status_code == 403 and "token" not in rj.json())
            hb = await c.post(f"/video/sessions/{a}/heartbeat", headers=MH)
            check("heartbeat del retirado rechazado (403)", hb.status_code == 403)
            it = await c.post(f"/video/sessions/{a}/interaccion", headers=MH, json={"accion": "mano", "arriba": True})
            check("interacción del retirado rechazada (403)", it.status_code == 403)
            bl = await c.get(f"/video/sessions/{a}/bloqueos", headers=TH)
            items = bl.json().get("items", []) if bl.status_code == 200 else []
            check("lista de retirados (profesor) muestra a la estudiante",
                  [i["user_id"] for i in items] == [ids["maria"]] and items[0]["times_blocked"] == 1)
            check("la lista de retirados es solo para moderadores (estudiante 403)",
                  (await c.get(f"/video/sessions/{a}/bloqueos", headers=CH)).status_code == 403)

            print("\n  · Alcance del bloqueo")
            check("otro estudiante NO queda bloqueado", (await J(CH, a)).status_code == 200)
            check("la misma estudiante en OTRA clase NO queda bloqueada", (await J(MH, otra)).status_code == 200)

            print("\n  · LiveKit falla al sacar → el bloqueo se queda")
            _LK.fallar = {"RemoveParticipant"}
            r = await M(TH, a, action="remove", identity=ids["carlos"])
            _LK.fallar = set()
            check("502 con el mensaje pedido (no dice que lo desconectó)",
                  r.status_code == 502 and "quedó bloqueado, pero no pudimos desconectarlo" in r.text)
            check("…y Carlos NO obtiene token nuevo (/join 403)", (await J(CH, a)).status_code == 403)
            r = await M(TH, a, action="remove", identity=ids["carlos"])
            check("reintentar Sacar → ya bloqueado, ahora desconectado (200)",
                  r.status_code == 200 and r.json().get("bloqueo") == "ya_estaba" and r.json().get("desconectado") is True)

            print("\n  · Permitir volver / reexpulsar / concurrencia")
            _LK.llamadas = []
            r = await M(TH, a, action="allow", identity=ids["maria"])
            check("Permitir volver (200) sin llamar a LiveKit ni emitir token",
                  r.status_code == 200 and r.json().get("estaba_bloqueado") is True and not _LK.llamadas
                  and "token" not in r.text)
            check("/join vuelve a permitirse (200 con token)",
                  (rj := await J(MH, a)).status_code == 200 and rj.json().get("token"))
            r = await M(TH, a, action="allow", identity=ids["maria"])
            check("Permitir volver otra vez → idempotente (estaba_bloqueado = false)",
                  r.status_code == 200 and r.json().get("estaba_bloqueado") is False)
            _LK.conectados[ids["maria"]] = {"audio": True}
            r = await M(AH, a, action="remove", identity=ids["maria"])
            check("reexpulsar (Dirección) → bloqueada de nuevo", r.status_code == 200 and r.json().get("bloqueo") == "reactivado")
            items = (await c.get(f"/video/sessions/{a}/bloqueos", headers=AH)).json().get("items", [])
            check("una sola fila por (clase, persona) con times_blocked = 2",
                  sum(1 for i in items if i["user_id"] == ids["maria"]) == 1
                  and next((i for i in items if i["user_id"] == ids["maria"]), {}).get("times_blocked") == 2)
            check("/join tras reexpulsar → 403", (await J(MH, a)).status_code == 403)
            await M(TH, a, action="allow", identity=ids["maria"])
            rs = await asyncio.gather(*[M(TH, a, action="remove", identity=ids["maria"]) for _ in range(4)],
                                      return_exceptions=True)
            oks = [x for x in rs if not isinstance(x, Exception) and x.status_code in (200, 502)]
            items = (await c.get(f"/video/sessions/{a}/bloqueos", headers=TH)).json().get("items", [])
            check(f"doble clic / 4 peticiones a la vez → sin errores ({[getattr(x, 'status_code', x) for x in rs]}) "
                  "y UNA fila activa", len(oks) == 4 and sum(1 for i in items if i["user_id"] == ids["maria"]) == 1)

            print("\n  · Auditoría (historia completa)")
            import sqlite3
            ruta = Path(DB_URL.split("///", 1)[-1])
            if DB_URL.startswith("sqlite") and ruta.is_file():
                cx = sqlite3.connect(str(ruta))
                acciones = [r[0] for r in cx.execute(
                    "select action from audit_logs where module='video' and target_id=? order by created_at", (a,))]
                cols = {r[1] for r in cx.execute("pragma table_info(video_access_blocks)")}
                cx.close()
                check(f"audit_logs registra bloquear / permitir / sacar / silenciar ({len(acciones)} entradas)",
                      {"video_block", "video_unblock", "video_remove", "video_mute"} <= set(acciones)
                      and acciones.count("video_unblock") >= 2)
                check("la tabla no guarda tokens ni secretos", not any(re.search(r"token|secret", c_, re.I) for c_ in cols))
            else:
                check("auditoría verificable en SQLite local", False)

            print("\n  · Reglas existentes")
            await c.post(f"/teacher/sessions/{fin}/finalize", headers=TH)
            check("clase completed → /join rechazado (400)", (await J(CH, fin)).status_code == 400)
            _LK.llamadas = []
            r = await M(TH, ext, action="remove", identity=ids["carlos"])
            check("clase externa (Meet) → 400 y SIN llamadas a LiveKit", r.status_code == 400 and not _LK.llamadas)
            check("estudiante fuera de la clase no gana nada: acción inválida → 400",
                  (await M(TH, a, action="explotar", identity=ids["carlos"])).status_code == 400)
        finally:
            srv.shutdown()
            n = limpiar(creadas)
            print(f"    · limpieza local: {n} sesión(es) de prueba borradas" if n is not None else "    · limpieza no aplicable")


# ============================================================================ API (servidor)
async def parte_api(check):
    print("\n[API] Servidor local (su LiveKit no es alcanzable: comprueba el fail-closed real)")
    creadas = []
    async with httpx.AsyncClient(base_url=BASE, timeout=60) as c:
        async def login(cred):
            r = await c.post("/auth/login", json=cred)
            return {"Authorization": f"Bearer {r.json()['access_token']}"}
        AH, TH, MH = await login(ADMIN), await login(TEACHER), await login(MARIA)
        prof = (await c.get("/auth/me", headers=TH)).json()["id"]
        maria = (await c.get("/auth/me", headers=MH)).json()["id"]
        now = dt.datetime.now(dt.timezone.utc)
        r = await c.post("/admin/events", headers=AH, json={
            "title": "G6GA servidor", "teacher_id": prof, "modality": "online", "video_provider": "dorismon",
            "starts_at_utc": (now - dt.timedelta(minutes=5)).isoformat(),
            "ends_at_utc": (now + dt.timedelta(minutes=55)).isoformat()})
        sid = r.json().get("id")
        if sid:
            creadas.append(sid)
        try:
            await c.post(f"/video/sessions/{sid}/join", headers=MH)
            r = await c.post(f"/video/sessions/{sid}/moderate", headers=TH, json={"action": "remove", "identity": maria})
            # Con LiveKit inalcanzable (suite) → 502 honesto; con uno local que responde → 200
            check(f"Sacar → bloqueo; si LiveKit no responde, mensaje honesto ({r.status_code})",
                  (r.status_code == 502 and "quedó bloqueado" in r.text)
                  or (r.status_code == 200 and r.json().get("bloqueado") is True))
            rj = await c.post(f"/video/sessions/{sid}/join", headers=MH)
            check("…/join rechazado sin token (403)", rj.status_code == 403 and "retiró" in rj.text)
            await c.post(f"/video/sessions/{sid}/moderate", headers=TH, json={"action": "allow", "identity": maria})
            check("Permitir volver → /join 200", (await c.post(f"/video/sessions/{sid}/join", headers=MH)).status_code == 200)
        finally:
            n = limpiar(creadas)
            print(f"    · limpieza local: {n} sesión(es)" if n is not None else "    · limpieza no aplicable")


# ============================================================================ ESTÁTICO
def parte_estatica(check):
    print("\n[ESTÁTICO]")
    models = (ROOT / "app/models/placement_booking.py").read_text(encoding="utf-8")
    m = models[models.find("class VideoAccessBlock"):]
    check("tabla video_access_blocks con restricción única (session_id, user_id)",
          '__tablename__ = "video_access_blocks"' in m
          and 'UniqueConstraint("session_id", "user_id", name="uq_video_access_block_session_user")' in m)
    check("campos: activo, quién/cuándo bloqueó, quién/cuándo permitió, motivo, veces",
          all(x in m for x in ("active:", "blocked_by:", "blocked_at:", "unblocked_by:", "unblocked_at:",
                               "reason:", "times_blocked:")))
    # Actualizado en Gate 6H-A: sus modelos (grabación) se añaden después, también al final
    from _base_gate6ha import sin_gate6ha
    check("modelo al FINAL del archivo (nada más del esquema cambia)", sin_gate6ha("app/models/placement_booking.py", models).rstrip().endswith(
        "times_blocked: Mapped[int] = mapped_column(Integer, default=1, nullable=False)"))
    lk = (ROOT / "app/services/livekit_service.py").read_text(encoding="utf-8")
    i = lk.find("def _admin_token(session_id: str)")
    tok = lk[i:lk.find("return jwt.encode", i)]
    check("token de moderación de SERVIDOR ligado a la sala (room + roomAdmin, sin roomList ni join)",
          i > 0 and '"video": {"room": room_name(session_id), "roomAdmin": True}' in tok)
    check("token del navegador sin cambios (TTL, roomAdmin del moderador, permisos)",
          # Actualizado en Gate 6G-B: TTL corto y sin roomAdmin en el navegador (autorizado)
          "ttl_seconds: int = 30 * 60," in lk and "roomAdmin\": bool(is_moderator)" not in lk
          # Actualizado en Gate 6G-C: fuentes por rol en el token (autorizado)
          and '"canPublishData": True,' in lk and '"canPublishSources": list(FUENTES_MODERADOR if is_moderator else FUENTES_ESTUDIANTE),' in lk)
    v = (ROOT / "app/routers/video.py").read_text(encoding="utf-8")
    mo = v[v.find('@router.post("/sessions/{session_id}/moderate")'):v.find("# GATE 6F-A —")]
    check("orden seguro: bloquear ANTES de RemoveParticipant",
          0 < mo.find("await bloquear(") < mo.find("await remove_participant("))
    check("fail-closed: si LiveKit falla, 502 con el mensaje pedido (sin deshacer el bloqueo)",
          "El acceso del estudiante quedó bloqueado, pero no pudimos desconectarlo " in mo
          and "permitir_volver" not in mo[mo.find("await remove_participant("):])
    j = v[v.find("async def join_class_video"):v.find("async def heartbeat")]
    check("/join comprueba el bloqueo ANTES de emitir el token",
          0 < j.find("bloqueo_activo(db, session_id, user.user_id)") < j.find("token = build_token("))

    print("\n  · Interfaz (panel de participantes)")
    ex = (WEB / "components/ClassRoomExtras.tsx").read_text(encoding="utf-8")
    check("lee los retirados de la clase y ofrece «Permitir volver»",
          "/bloqueos`" in ex and "Permitir volver" in ex and 'moderar(r.user_id, "allow")' in ex)
    check("al retirado no se le ofrecen Silenciar/Sacar (sin botones contradictorios)",
          "retirado ? (" in ex)
    check("al moderador (profesor/Dirección) no se le ofrecen Silenciar/Sacar",
          'const moderable = esModerador && !p.isLocal && p.metadata !== "teacher";' in ex)
    check("Sacar avisa que no podrá volver hasta «Permitir volver»",
          "No podrá volver a entrar" in ex)
    check("retirado que sigue conectado (LiveKit falló): «Volver a desconectar» sin repetir la confirmación",
          'moderar(p.identity, "remove", true)' in ex and "Volver a desconectar" in ex
          and 'if (action === "remove" && !reintento && !confirm(' in ex)


async def main():
    total = passed = 0

    def check(label, ok):
        nonlocal total, passed
        total += 1
        passed += int(bool(ok))
        print(f"  {'✓' if ok else '✗'} {label}")

    await parte_api(check)
    await parte_integracion(check)
    parte_estatica(check)
    print(f"\n{passed}/{total} tests pasaron")
    return passed == total


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(main()) else 1)
