"""Ayuda para los tests anteriores tras Gate 6H-A.1 (autorizado después).

Gate 6H-A.1 cambia, con autorización, UNA línea de video.py: al detener la grabación la
audiencia solo suma a quien entró (solo_asistencia=True; el derecho quedó fijado al iniciar).
Aquí se deshace EXACTAMENTE ese bloque. Si no está exacto, no se toca nada y la huella falla.
"""

_BLOQUES = {'app/routers/video.py': [('        raise HTTPException(502, "Se pidió detener la grabación, pero LiveKit no respondió. Inténtalo de nuevo.")\n    if rec is not None:\n        s = await db.get(ClassSession, session_id)\n        # GATE 6H-A.1: solo quien ENTRÓ durante la grabación; el derecho quedó fijado al iniciarla\n        await congelar_audiencia(db, s, rec.id, solo_asistencia=True)\n    return {"ok": True, "resultado": resultado, "grabacion": resumen(rec)}\n\n\n', '        raise HTTPException(502, "Se pidió detener la grabación, pero LiveKit no respondió. Inténtalo de nuevo.")\n    if rec is not None:\n        s = await db.get(ClassSession, session_id)\n        await congelar_audiencia(db, s, rec.id)   # quien se sumó durante la grabación\n    return {"ok": True, "resultado": resultado, "grabacion": resumen(rec)}\n\n\n')]}


def sin_gate6ha1(rel: str, txt: str) -> str:
    # Actualizado en Gate 6F-B: primero se deshacen EXACTAMENTE sus cambios
    from _base_gate6fb import sin_gate6fb
    txt = sin_gate6fb(rel, txt)
    for clave, bl in _BLOQUES.items():
        if rel.replace("\\", "/").endswith(clave):
            if not all(txt.count(nuevo) == 1 for nuevo, _ in bl):
                return txt
            for nuevo, viejo in bl:
                txt = txt.replace(nuevo, viejo, 1)
            return txt
    return txt
