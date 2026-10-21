"""Integridad de los tests antiguos tras la CONSOLIDACIÓN v3.9.76 (cambio AUTORIZADO, explícito).

Los tests de 6A/6B/6C/6D/6F-A.1 afirman que app/routers/video.py no tiene más cambios que los
autorizados. La consolidación autoriza UNO más:
  D2 — /join: Dirección (super_admin) no se autoinscribe como estudiante en un evento abierto
       (en PostgreSQL la FK event_registrations → students lo rechazaba con 500).
Este archivo es SOLO de tests: reconstruye byte a byte la versión anterior deshaciendo EXACTAMENTE ese
bloque, para que la comprobación "nada más cambió" siga siendo estricta. No hay lógica en producción
para satisfacerla. Si el bloque no está exacto, no se toca nada y la huella falla.
"""

_BLOQUES = {'app/routers/video.py': [('\n    # V3.9.43 — AUTO-REGISTRO EN EVENTOS: si es un evento abierto y no se\n    # había anotado, se anota al entrar (si hay cupo). Antes quedaba afuera\n    # aunque llegara a tiempo y hubiera lugar.\n    # v3.9.76 (D2): Dirección no es estudiante; en PostgreSQL la FK a students rechazaba su inscripción (500)\n    if s.is_open_event and not is_teacher and not is_admin:\n        ya = (await db.execute(\n            select(EventRegistration).where(\n                EventRegistration.session_id == session_id,\n                EventRegistration.student_id == user.user_id,\n', '\n    # V3.9.43 — AUTO-REGISTRO EN EVENTOS: si es un evento abierto y no se\n    # había anotado, se anota al entrar (si hay cupo). Antes quedaba afuera\n    # aunque llegara a tiempo y hubiera lugar.\n    if s.is_open_event and not is_teacher:\n        ya = (await db.execute(\n            select(EventRegistration).where(\n                EventRegistration.session_id == session_id,\n                EventRegistration.student_id == user.user_id,\n')]}


def sin_v3976_consolidacion(rel: str, txt: str) -> str:
    for clave, bl in _BLOQUES.items():
        if rel.replace("\\", "/").endswith(clave):
            if not all(txt.count(nuevo) == 1 for nuevo, _ in bl):
                return txt
            for nuevo, viejo in bl:
                txt = txt.replace(nuevo, viejo, 1)
            return txt
    return txt
