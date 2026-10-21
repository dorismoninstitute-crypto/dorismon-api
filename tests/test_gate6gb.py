"""GATE 6G-B — Tokens, TTL y mínimo privilegio.

[INTEGRACIÓN] App EN ESTE PROCESO (misma base que el servidor local) contra un LiveKit
    simulado LOCAL que aplica la regla real de permisos de administración y registra
    el ORDEN de las llamadas:
    · token del navegador (estudiante, profesor, Dirección): sala correcta, identidad =
      usuario, SIN roomAdmin, solo permisos de participante; moderador = metadata.
    · vida: min(fin + 15 min, ahora + 30 min); clase larga → 30 min; clase que acaba →
      menos; nunca más allá de fin + 15 min; /join informa expires_in.
    · reentrada → token NUEVO; completed → sin token.
    · finalizar: 1) metadatos "finalizada", 2) RemoveParticipant + revokeTokenTs para
      CADA persona con token (presencia de /join) y para quien siga conectado,
      3) DeleteRoom al final; LiveKit caído → la clase queda finalizada igual;
      clase externa → sin LiveKit.
    · 6G-A (silenciar / sacar) y 6F-A (mano) siguen funcionando sin roomAdmin en el navegador.
[ESTÁTICO] frontend: token fresco al salir de la pantalla previa, fin de clase ≠ retirado,
    6E vuelve por /join, ningún uso de roomAdmin en el navegador.

Uso: python tests/test_gate6gb.py [BASE_URL] [RUTA_DORISMON_WEB]
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
TTL_MAX = 30 * 60


def codigo(txt: str) -> str:
    txt = re.sub(r"/\*.*?\*/", "", txt, flags=re.S)
    txt = re.sub(r"\{/\*.*?\*/\}", "", txt, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", txt)


def claims(tok):
    try:
        p = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    except Exception:
        return {}


class _LK(BaseHTTPRequestHandler):
    llamadas: list = []
    conectados: dict = {}
    fallar_todo = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        cuerpo = json.loads(self.rfile.read(n) or b"{}")
        metodo = self.path.rsplit("/", 1)[-1]
        v = claims((self.headers.get("Authorization") or "").replace("Bearer ", "")).get("video") or {}
        _LK.llamadas.append({"metodo": metodo, "cuerpo": cuerpo, "video": v})
        if _LK.fallar_todo:
            return self._r(503, {"code": "unavailable"})
        admin = {"ListParticipants", "MutePublishedTrack", "RemoveParticipant", "UpdateParticipant", "UpdateRoomMetadata"}
        if metodo in admin and not (v.get("roomAdmin") and v.get("room") == cuerpo.get("room")):
            return self._r(403, {"code": "permission_denied"})
        if metodo in ("DeleteRoom",) and not v.get("roomCreate"):
            return self._r(403, {"code": "permission_denied"})
        if metodo == "ListRooms":
            return self._r(200, {"rooms": [{"name": (cuerpo.get("names") or [""])[0], "metadata": ""}]})
        if metodo == "ListParticipants":
            return self._r(200, {"participants": [{"identity": i, "tracks": [{"sid": "TR", "type": "AUDIO"}]}
                                                  for i in _LK.conectados]})
        if metodo == "RemoveParticipant":
            if cuerpo.get("identity") not in _LK.conectados:
                return self._r(404, {"code": "not_found", "msg": "participant does not exist"})
            _LK.conectados.pop(cuerpo["identity"], None)
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
        cx.execute(f"delete from audit_logs where module in ('video','teacher') and target_id in ({marcas})", ids)
        n = cx.execute(f"delete from class_sessions where id in ({marcas})", ids).rowcount
        cx.commit()
        return n
    finally:
        cx.close()


async def parte_integracion(check):
    print("\n[INTEGRACIÓN] App en proceso + LiveKit simulado (regla real, orden de llamadas)")
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
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        check(f"la app se importa en proceso ({type(e).__name__}: {e})", False)
        return
    creadas = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=60) as c:
        async def login(cred):
            return {"Authorization": f"Bearer {(await c.post('/auth/login', json=cred)).json()['access_token']}"}
        AH, TH, MH, CH = [await login(x) for x in (ADMIN, TEACHER, MARIA, CARLOS)]
        ids = {k: (await c.get("/auth/me", headers=h)).json()["id"]
               for k, h in (("admin", AH), ("prof", TH), ("maria", MH), ("carlos", CH))}
        now = dt.datetime.now(dt.timezone.utc)

        async def evento(nombre, inicio_min, fin_min, **extra):
            body = {"title": nombre, "teacher_id": ids["prof"], "modality": "online", "video_provider": "dorismon",
                    "starts_at_utc": (now + dt.timedelta(minutes=inicio_min)).isoformat(),
                    "ends_at_utc": (now + dt.timedelta(minutes=fin_min)).isoformat()}
            body.update(extra)
            r = await c.post("/admin/events", headers=AH, json=body)
            i = r.json().get("id") if r.status_code in (200, 201) else None
            if i:
                creadas.append((i, now + dt.timedelta(minutes=fin_min)))
            return i

        larga = await evento("G6GB larga", -5, 180)
        acaba = await evento("G6GB acaba pronto", -50, 10)
        termino = await evento("G6GB terminó hace 10 min", -70, -10)
        ext = await evento("G6GB externa", -5, 55, video_provider="meet", meeting_url="https://meet.google.com/abc-defg-hij")
        fin_de = {i: f for i, f in creadas}
        check("se crean las clases de prueba", all((larga, acaba, termino, ext)))
        if not all((larga, acaba, termino, ext)):
            return
        try:
            J = lambda h, sid: c.post(f"/video/sessions/{sid}/join", headers=h)

            print("\n  · Token del navegador (mínimo privilegio)")
            for quien, h in (("estudiante", MH), ("profesor", TH), ("Dirección", AH)):
                r = await J(h, larga)
                d = r.json() if r.status_code == 200 else {}
                cl = claims(d.get("token", ""))
                v = cl.get("video") or {}
                check(f"{quien}: sala correcta, identidad = su usuario, SIN roomAdmin",
                      r.status_code == 200 and v.get("room") == f"dorismon-{larga}"
                      and cl.get("sub") == ids[{"estudiante": "maria", "profesor": "prof", "Dirección": "admin"}[quien]]
                      and "roomAdmin" not in v)
                check(f"{quien}: solo permisos de participante (join/publish/subscribe/data)",
                      # Actualizado en Gate 6G-C: + canPublishSources (fuentes por rol), nunca roomAdmin
                      set(v) == {"room", "roomJoin", "canPublish", "canSubscribe", "canPublishData", "canPublishSources"}
                      and all(v[k] is True for k in ("roomJoin", "canPublish", "canSubscribe", "canPublishData")))
                if quien != "estudiante":
                    check(f"{quien}: sigue marcado como moderador (metadata/is_moderator), sin permisos extra",
                          cl.get("metadata") == "teacher" and d.get("is_moderator") is True)

            print("\n  · Vida del token")
            d = (await J(MH, larga)).json()
            cl = claims(d["token"])
            vida = cl["exp"] - (cl["nbf"] + 10)
            check(f"clase larga (3 h): token de 30 min, no de 3 h (vida {vida} s, expires_in {d.get('expires_in')})",
                  vida == TTL_MAX and d.get("expires_in") == TTL_MAX)
            d = (await J(MH, acaba)).json()
            cl = claims(d["token"])
            limite = (fin_de[acaba] + dt.timedelta(minutes=15)).timestamp()
            check(f"clase que termina en 10 min: vida reducida a fin + 15 min ({d.get('expires_in')} s ≈ 1500)",
                  1400 <= d.get("expires_in", 0) <= 1500 and cl["exp"] <= limite + 1)
            r = await J(MH, termino)
            d = r.json() if r.status_code == 200 else {}
            cl = claims(d.get("token", ""))
            limite = (fin_de[termino] + dt.timedelta(minutes=15)).timestamp()
            check(f"clase que terminó hace 10 min (dentro del margen): ≈ 5 min y NUNCA más allá de fin + 15 ({d.get('expires_in')} s)",
                  r.status_code == 200 and 200 <= d.get("expires_in", 0) <= 300 and cl.get("exp", 1e18) <= limite + 1)
            a = (await J(CH, larga)).json()["token"]
            await asyncio.sleep(1.1)
            b = (await J(CH, larga)).json()["token"]
            check("reentrada (/join otra vez) → token NUEVO", a != b and claims(b)["exp"] > claims(a)["exp"])

            print("\n  · 6G-A y 6F-A siguen funcionando (por el backend)")
            _LK.conectados = {ids["maria"]: {}, ids["carlos"]: {}}
            _LK.llamadas = []
            r = await c.post(f"/video/sessions/{larga}/moderate", headers=TH, json={"action": "mute", "identity": ids["maria"]})
            check(f"silenciar (6G-A) → 200 con token de servidor de la sala ({r.status_code})",
                  r.status_code == 200 and any(x["metodo"] == "MutePublishedTrack" for x in _LK.llamadas))
            r = await c.post(f"/video/sessions/{larga}/interaccion", headers=MH, json={"accion": "mano", "arriba": True})
            check(f"levantar la mano (6F-A) → 200 ({r.status_code})", r.status_code == 200)

            print("\n  · Finalizar: tokens anteriores inútiles")
            _LK.conectados = {ids["maria"]: {}, "alguien-conectado-sin-presencia": {}}
            _LK.llamadas = []
            r = await c.post(f"/teacher/sessions/{larga}/finalize", headers=TH)
            orden = [x["metodo"] for x in _LK.llamadas]
            rem = [x for x in _LK.llamadas if x["metodo"] == "RemoveParticipant"]
            revocados = {x["cuerpo"]["identity"] for x in rem}
            check(f"finaliza (200, video_room = {r.json().get('video_room')})", r.status_code == 200)
            check("1) primero marca 'finalizada' en los metadatos de la sala",
                  "UpdateRoomMetadata" in orden and orden.index("UpdateRoomMetadata") < orden.index("RemoveParticipant")
                  and json.loads(next(x for x in _LK.llamadas if x["metodo"] == "UpdateRoomMetadata")["cuerpo"]["metadata"]).get("finalizada") is True)
            check("2) revoca a TODOS los que recibieron token (estudiantes, profesor, Dirección) y a quien siga conectado",
                  {ids["maria"], ids["carlos"], ids["prof"], ids["admin"], "alguien-conectado-sin-presencia"} <= revocados)
            check("…con revokeTokenTs y token de servidor de ESA sala",
                  bool(rem) and all(isinstance(x["cuerpo"].get("revokeTokenTs"), int) and x["video"].get("room") == f"dorismon-{larga}" for x in rem))
            check("3) DeleteRoom al FINAL", orden and orden[-1] == "DeleteRoom")
            check("después: /join no da token (completed)", (await J(MH, larga)).status_code == 400)

            print("\n  · LiveKit caído al finalizar / clase externa")
            await J(MH, acaba)
            _LK.fallar_todo = True
            r = await c.post(f"/teacher/sessions/{acaba}/finalize", headers=TH)
            _LK.fallar_todo = False
            check(f"LiveKit caído → finaliza igual (200, video_room = {r.json().get('video_room')}) y /join 400",
                  r.status_code == 200 and r.json().get("video_room") == "error" and (await J(MH, acaba)).status_code == 400)
            _LK.llamadas = []
            r = await c.post(f"/teacher/sessions/{ext}/finalize", headers=TH)
            check("clase externa → no_aplica y NINGUNA llamada a LiveKit",
                  r.status_code == 200 and r.json().get("video_room") == "no_aplica" and not _LK.llamadas)
        finally:
            srv.shutdown()
            n = limpiar([i for i, _ in creadas])
            print(f"    · limpieza local: {n} sesión(es)" if n is not None else "    · limpieza no aplicable")


def parte_estatica(check):
    print("\n[ESTÁTICO]")
    lk = (ROOT / "app/services/livekit_service.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    bt = lk[lk.find("def build_token("):lk.find("return jwt.encode", lk.find("def build_token("))]
    check("build_token: sin roomAdmin y vida por defecto de 30 min", "roomAdmin" not in bt.split('"""')[-1]
          and "ttl_seconds: int = 30 * 60," in bt)
    check("ttl_token_participante = min(fin + 15 min, ahora + 30 min)",
          "TTL_MAXIMO_TOKEN = 30 * 60" in lk and "timedelta(minutes=MINUTOS_GRACIA_FINAL) - ahora" in lk)
    v = (ROOT / "app/routers/video.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    j = v[v.find("async def join_class_video"):v.find("async def heartbeat")]
    check("/join usa la vida corta y la informa (expires_in)",
          "ttl = ttl_token_participante(ends, now)" in j and "ttl_seconds=ttl," in j and '"expires_in": ttl,' in j)
    t = (ROOT / "app/routers/teacher.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    f = t[t.find("async def finalize_session"):t.find("@router.", t.find("async def finalize_session") + 10)]
    check("finalizar: completed y auditoría ANTES; revocar ANTES de DeleteRoom",
          0 < f.find("await db.commit()") < f.find("await revocar_tokens_de_la_clase(") < f.find("video_room = await delete_room(session_id)"))

    cp_p = WEB / "components/CallProvider.tsx"
    if not cp_p.exists():
        check(f"existe CallProvider.tsx en {WEB}", False)
        return
    cp = cp_p.read_text(encoding="utf-8").replace("\r\n", "\n")
    cpc = codigo(cp)
    check("pantalla previa: si al token le quedan < 20 s, /join NUEVO antes de conectar",
          "onEntrar={entrarDesdePrevia}" in cp and "performance.now() - tokenPedidoEn.current > vida - 20_000" in cp
          and "await onReentrar(sessionId)" in cp)
    check("fin de clase ≠ retirado: PARTICIPANT_REMOVED tras 'finalizada' → «La clase ha finalizado.»",
          "if (h.claseFinalizada) {" in cp and "<VigilarFinDeClase onFinalizada={marcarClaseFinalizada} />" in cp)
    check("6E: «Volver a la clase» pasa por /join nuevo (entrar); 6B: «Volver a entrar» también",
          "entrar(c.session_id, { fin: c.fin ?? null }, c.modo);" in cp
          and "const datos = await api(`/video/sessions/${sessionId}/join`" in cp)
    check("ningún token guardado ni reutilizado (sin token en localStorage/sessionStorage)",
          not re.search(r"(localStorage|sessionStorage)\.setItem\([^)]*token", cpc))
    todo_web = "".join(p.read_text(encoding="utf-8") for p in (WEB / "components").glob("*.tsx"))
    check("el navegador no usa roomAdmin ni la API de administración de LiveKit",
          not re.search(r"roomAdmin|RoomServiceClient|twirp", todo_web))


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
