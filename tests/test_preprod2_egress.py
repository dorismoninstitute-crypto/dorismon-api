"""PREPROD-2 — Respuestas REALES de LiveKit Egress (forma snake_case) en grabaciones.py.

Defecto encontrado contra LiveKit Cloud real (2026-10-18): StartRoomCompositeEgress responde
EgressInfo en snake_case (egress_id, file_results, started_at…), pero grabaciones.py leía
camelCase (egressId, fileResults, startedAt…): la grabación quedaba en "starting" SIN egress_id,
con el slot de la clase ocupado, sin poder detenerse ni reconciliarse (Egress huérfana).

[UNIDAD] con las respuestas capturadas en LiveKit Cloud (credenciales del eco de la petición
quitadas): iniciar guarda egress_id; STARTING → ACTIVE → ENDING → FAILED (error y código);
int64 como TEXTO en ns; ended_at "0" o NEGATIVO no se toma como fecha; file_results vacío con el
archivo en `file`; COMPLETE con tamaño → ready (forma DEDUCIDA de ENDING real: COMPLETE real
necesita almacenamiento real); webhooks en camelCase (forma de protojson); sin egress_id → slot
liberado con error claro; COMPLETE ilegible → no decide (ni ready ni failed).
[ESTÁTICO] sin lecturas camelCase sueltas de EgressInfo.

Uso: python tests/test_preprod2_egress.py [BASE_URL]   (no usa el servidor; BASE_URL se ignora)
"""
import asyncio
import copy
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_TMP = tempfile.mkdtemp(prefix="pp2_egress_")
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + Path(_TMP, "t.db").as_posix()
os.environ.setdefault("SECRET_KEY", "solo-pruebas-locales-pp2-egress-0123456789")
os.environ.update({"LIVEKIT_URL": "ws://127.0.0.1:9", "LIVEKIT_API_KEY": "APIprueba",
                   "LIVEKIT_API_SECRET": "secretodepruebaparatest123456789",
                   "RECORDINGS_R2_BUCKET": "b", "R2_ACCESS_KEY": "k", "R2_SECRET_KEY": "s",
                   "RECORDINGS_S3_ENDPOINT": "https://preprod-sin-destino.invalid"})
SID = "84646ad9-5ced-4f78-a1e5-416596c7eb9b"
SALA = f"dorismon-{SID}"
EID = "EG_YMsoVMoPREPROD"
ARCH = f"grabaciones/{SID}/7ac3d780.mp4"

# --- Respuestas REALES (LiveKit Cloud, 2026-10-18), recortadas a los campos de EgressInfo ---
START_REAL = {"egress_id": EID, "room_id": "RM_x", "room_name": SALA, "source_type": "EGRESS_SOURCE_TYPE_WEB",
              "status": "EGRESS_STARTING", "started_at": "1792286875661886137", "ended_at": "0",
              "updated_at": "1792286875661886137", "details": "", "error": "", "error_code": 0,
              "file": {"filename": ARCH, "started_at": "0", "ended_at": "0", "duration": "0", "size": "0", "location": ""},
              "stream_results": [], "segment_results": [], "image_results": [], "manifest_location": "",
              "backup_storage_used": False, "retry_count": 0,
              "file_results": [{"filename": ARCH, "started_at": "0", "ended_at": "0", "duration": "0", "size": "0", "location": ""}]}
LIST_ACTIVE_REAL = dict(START_REAL, status="EGRESS_ACTIVE", ended_at="-6795364578871345152", file_results=[],
                        file={"filename": ARCH, "started_at": "1792286879032663130", "ended_at": "0", "duration": "0",
                              "size": "0", "location": ""}, updated_at="1792286879032664000")
STOP_ENDING_REAL = dict(START_REAL, status="EGRESS_ENDING", ended_at="0",
                        file_results=[{"filename": ARCH, "started_at": "1792286879032663130",
                                       "ended_at": "1792286905674471034", "duration": "26641807904", "size": "0", "location": ""}])
LIST_FAILED_REAL = dict(START_REAL, status="EGRESS_FAILED", ended_at="1792286905759198000", file_results=[],
                        error="S3 upload failed: err: operation error S3: PutObject, https response error StatusCode: 0",
                        error_code=400)
# COMPLETE: forma DEDUCIDA de la real (igual que ENDING, con tamaño y ubicación). No observada: sin R2.
COMPLETE_DEDUCIDA = dict(STOP_ENDING_REAL, status="EGRESS_COMPLETE", ended_at="1792286906000000000",
                         file_results=[dict(STOP_ENDING_REAL["file_results"][0], size="7340032", location="r2://b/x")])


def _camel(x):
    if isinstance(x, dict):
        return {(k.split("_")[0] + "".join(p.title() for p in k.split("_")[1:])): _camel(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_camel(v) for v in x]
    return x


class _Resp:
    def __init__(self, cuerpo, code=200):
        self.status_code, self._c = code, cuerpo

    def json(self):
        if isinstance(self._c, Exception):
            raise self._c
        return copy.deepcopy(self._c)


async def parte_unidad(check):
    print("\n[UNIDAD] grabaciones.py con las respuestas REALES de LiveKit Egress")
    sys.path.insert(0, str(ROOT))
    from app.core.db import engine, SessionLocal
    from app.models import Base, VideoRecording
    import app.services.grabaciones as gr
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    def nueva(status="starting"):
        return VideoRecording(session_id=SID, activa=SID if status in gr.ACTIVOS else None, status=status,
                              egress_id=EID, storage_provider="r2", storage_bucket="b", mime_type="video/mp4")

    r = nueva()
    gr.aplicar_info(r, copy.deepcopy(START_REAL))
    check("STARTING real → sigue starting, slot ocupado", r.status == "starting" and r.activa == SID)
    gr.aplicar_info(r, copy.deepcopy(LIST_ACTIVE_REAL))
    check("ACTIVE real (ListEgress) → recording; started_at desde el texto en ns",
          r.status == "recording" and r.started_at is not None and r.started_at.year >= 2026)
    check("ended_at NEGATIVO del proveedor NO se toma como fecha", r.ended_at is None)
    gr.aplicar_info(r, copy.deepcopy(STOP_ENDING_REAL))
    check("ENDING real (StopEgress) → processing; slot liberado", r.status == "processing" and r.activa is None)
    gr.aplicar_info(r, copy.deepcopy(LIST_FAILED_REAL))
    check(f"FAILED real → failed con código {r.error_code} y error saneado",
          r.status == "failed" and r.error_code == 400 and "S3 upload failed" in (r.error_message or "")
          and r.ended_at is not None)

    r = nueva("processing")
    gr.aplicar_info(r, copy.deepcopy(COMPLETE_DEDUCIDA))
    check(f"COMPLETE (forma deducida) → ready con tamaño {r.size_bytes} y duración {r.duration_seconds}s",
          r.status == "ready" and r.size_bytes == 7340032 and r.duration_seconds == 26)
    r = nueva("processing")
    gr.aplicar_info(r, dict(COMPLETE_DEDUCIDA, file_results=[], file=dict(COMPLETE_DEDUCIDA["file_results"][0])))
    check("COMPLETE con file_results vacío y el archivo en `file` (como el ListEgress real) → ready",
          r.status == "ready" and r.size_bytes == 7340032)
    r = nueva("processing")
    gr.aplicar_info(r, dict(COMPLETE_DEDUCIDA, file_results=[dict(COMPLETE_DEDUCIDA["file_results"][0], size="0")],
                            file=None))
    check("COMPLETE con tamaño 0 → failed 'sin archivo' (nunca ready falso)", r.status == "failed")
    r = nueva("processing")
    ilegible = {k: v for k, v in COMPLETE_DEDUCIDA.items() if k not in ("file_results", "file")}
    cambio = gr.aplicar_info(r, ilegible)
    check("COMPLETE sin datos de archivo legibles → NO decide (ni ready ni failed)", not cambio and r.status == "processing")
    r = nueva("processing")
    cambio = gr.aplicar_info(r, dict(COMPLETE_DEDUCIDA, file_results=[dict(COMPLETE_DEDUCIDA["file_results"][0], size="??")]))
    check("COMPLETE con tamaño no numérico → NO decide", not cambio and r.status == "processing")
    r = nueva("processing")
    gr.aplicar_info(r, _camel(COMPLETE_DEDUCIDA))
    check("camelCase (forma de los webhooks) → mismo resultado", r.status == "ready" and r.size_bytes == 7340032)

    print("\n  · iniciar / sincronizar / webhook")
    from app.models import ClassSession

    class _S:
        id = SID
    orig = (gr._egress, gr.congelar_audiencia)

    async def sin_audiencia(*a, **k):
        return None
    gr.congelar_audiencia = sin_audiencia
    try:
        peticiones = []

        async def con(respuesta):
            async def falso(metodo, cuerpo):
                peticiones.append((metodo, copy.deepcopy(cuerpo)))
                return respuesta
            gr._egress = falso
            async with SessionLocal() as db:
                rec, res = await gr.iniciar(db, _S(), "actor-prueba")
                return rec.id, rec.status, rec.egress_id, rec.activa, rec.error_message, res

        _id, st, eid, act, _, res = await con(_Resp(copy.deepcopy(START_REAL)))
        check(f"iniciar con la respuesta REAL → egress_id guardado ({eid}), starting, slot ocupado",
              res == "iniciada" and eid == EID and st == "starting" and act == SID)
        met, cuerpo = peticiones[-1]
        salidas = cuerpo.get("fileOutputs") or []
        o = salidas[0] if salidas else {}
        check("StartRoomCompositeEgress: UNA salida MP4 con disableManifest = true (sin .json huérfano)",
              met == "StartRoomCompositeEgress" and len(salidas) == 1 and o.get("disableManifest") is True)
        check("…tipo, ruta y destino S3 sin cambios (MP4, grabaciones/<clase>/<id>.mp4, bucket configurado)",
              o.get("fileType") == "MP4" and o.get("filepath") == f"grabaciones/{SID}/{_id}.mp4"
              and (o.get("s3") or {}).get("bucket") == "b" and (o.get("s3") or {}).get("forcePathStyle") is True
              and cuerpo.get("roomName") == SALA and cuerpo.get("layout") == "speaker")
        async with SessionLocal() as db:
            rec = await db.get(VideoRecording, _id)
            gr._egress = lambda m, c: _ok({"items": [copy.deepcopy(LIST_ACTIVE_REAL)]})
            await gr.sincronizar(db, rec)
            check("sincronizar (ListEgress real) encuentra SU egress y avanza a recording", rec.status == "recording")
            gr._egress = lambda m, c: _ok(copy.deepcopy(STOP_ENDING_REAL))
            rec2, res2 = await gr.detener(db, SID, "actor-prueba")
            check(f"detener (StopEgress real) → {res2}, processing", res2 == "detenida" and rec2.status == "processing")
            ev = {"event": "egress_ended", "id": "EV1", "egressInfo": _camel(LIST_FAILED_REAL)}
            check("webhook (camelCase) de SU egress → aplicado", await gr.recibir_evento(db, ev) == "aplicado"
                  and rec2.status == "failed")
            otro = {"event": "egress_ended", "egressInfo": _camel(dict(LIST_FAILED_REAL, egress_id="EG_ajeno"))}
            check("webhook de un egress ajeno → desconocido", await gr.recibir_evento(db, otro) == "desconocido")

        _id, st, eid, act, msg, res = await con(_Resp({k: v for k, v in START_REAL.items() if k != "egress_id"}))
        check(f"respuesta 200 SIN egress_id → error, slot LIBERADO, mensaje claro ({msg})",
              res == "error" and st == "failed" and act is None and eid is None
              and msg == "LiveKit no devolvió el identificador de la grabación")
        _id, st, eid, act, msg, res = await con(_Resp(ValueError("no es JSON")))
        check("respuesta 200 que no es JSON → error y slot liberado", res == "error" and act is None)
        async with SessionLocal() as db:
            rec = await db.get(VideoRecording, _id)
            check("el mensaje propio se muestra tal cual al moderador",
                  gr.resumen(rec)["error"] == "LiveKit no devolvió el identificador de la grabación")
    finally:
        gr._egress, gr.congelar_audiencia = orig


async def _ok(cuerpo):
    return _Resp(cuerpo)


def parte_estatica(check):
    print("\n[ESTÁTICO]")
    g = (ROOT / "app/services/grabaciones.py").read_text(encoding="utf-8").replace("\r\n", "\n")
    import re
    sueltas = re.findall(r'\.get\("(egressId|fileResults|startedAt|endedAt|errorCode|roomName|egressInfo)"\)', g)
    check(f"sin lecturas camelCase sueltas de EgressInfo ({sueltas})", not sueltas)
    check("lectura de EgressInfo por un único helper (snake primero, camel compatibilidad)",
          "def _c(d, snake: str):" in g and 'rec.egress_id = _c(info, "egress_id")' in g)


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
