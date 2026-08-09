"""V3.9.31 — Fábrica de contenido con IA (Gemini).

QUÉ RESUELVE: los quizzes y las lecciones están vacíos porque escribirlos
lleva horas. Aquí eliges tema y nivel, la IA propone el contenido completo,
y TÚ revisas, editas y publicas. Nunca se publica solo.

CONFIGURACIÓN (variable de entorno en Render):
    GEMINI_API_KEY   la clave de Google AI Studio (gratis, sin tarjeta)
    GEMINI_MODEL     opcional; por defecto un modelo Flash

Si no está configurada, la plataforma funciona igual: la sección de generar
contenido avisa que falta la clave. Nada se rompe.

POR QUÉ ASÍ: el proveedor está aislado en este archivo. Si mañana quieres
usar otro (OpenAI, Claude), se cambia aquí y el resto del sistema no se toca.
"""
import os
import json
import logging

log = logging.getLogger(__name__)

BASE = "https://generativelanguage.googleapis.com/v1beta/models"
MODELO_POR_DEFECTO = "gemini-2.5-flash"


def ai_ready() -> bool:
    """¿Está puesta la clave?"""
    return bool(_api_key())


def _api_key() -> str:
    k = (os.getenv("GEMINI_API_KEY") or "").strip().strip('"').strip("'")
    # Por si pegaron la línea completa "GEMINI_API_KEY=AIza..."
    if k.upper().startswith("GEMINI_API_KEY="):
        k = k.split("=", 1)[1].strip()
    return k


def _modelo() -> str:
    return (os.getenv("GEMINI_MODEL") or "").strip() or MODELO_POR_DEFECTO


async def _generar(prompt: str, instruccion: str, json_esperado: bool = True) -> dict:
    """Le pide algo a la IA y devuelve la respuesta.

    Mensajes de error en español y útiles: si se acabó la cuota diaria del
    plan gratis, lo dice claro en vez de fallar en silencio.
    """
    import httpx

    if not ai_ready():
        raise RuntimeError(
            "Falta configurar la clave de la IA. Agrega GEMINI_API_KEY en Render."
        )

    cuerpo = {
        "system_instruction": {"parts": [{"text": instruccion}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 4096,
        },
    }
    if json_esperado:
        cuerpo["generationConfig"]["response_mime_type"] = "application/json"

    url = f"{BASE}/{_modelo()}:generateContent"
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(
            url,
            headers={"x-goog-api-key": _api_key(), "Content-Type": "application/json"},
            json=cuerpo,
        )

    if r.status_code == 429:
        raise RuntimeError(
            "Se acabó la cuota de la IA por hoy. El plan gratis tiene un límite "
            "diario; vuelve a intentar mañana o pasa al plan de pago."
        )
    if r.status_code in (401, 403):
        raise RuntimeError(
            "La clave de la IA no es válida o no tiene permiso. Revisa "
            "GEMINI_API_KEY en Render."
        )
    if r.status_code != 200:
        raise RuntimeError(f"La IA respondió con un error ({r.status_code}).")

    data = r.json()
    try:
        texto = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        motivo = (data.get("candidates") or [{}])[0].get("finishReason", "")
        if motivo == "SAFETY":
            raise RuntimeError(
                "La IA rechazó el tema por seguridad. Prueba con otro enunciado."
            )
        raise RuntimeError("La IA devolvió una respuesta vacía. Intenta de nuevo.")

    if not json_esperado:
        return {"text": texto}

    # A veces envuelve el JSON en ```json ... ```
    limpio = texto.strip()
    if limpio.startswith("```"):
        limpio = limpio.split("```")[1]
        if limpio.startswith("json"):
            limpio = limpio[4:]
    try:
        return json.loads(limpio.strip())
    except json.JSONDecodeError:
        raise RuntimeError(
            "La IA devolvió algo que no se pudo leer. Intenta generar de nuevo."
        )


# ============================================================================
# QUIZZES
# ============================================================================

INSTRUCCION_QUIZ = """Eres un profesor de inglés experto que crea material para
un instituto de idiomas en República Dominicana.

Reglas:
- Las preguntas y opciones van EN INGLÉS (es un curso de inglés).
- La explicación de cada respuesta va EN ESPAÑOL, clara y breve.
- Ajusta la dificultad EXACTAMENTE al nivel pedido del Marco Común Europeo.
- Cada pregunta tiene 4 opciones y una sola correcta.
- Las opciones incorrectas deben ser errores creíbles, no absurdos.
- No repitas estructuras entre preguntas.

Responde SOLO con JSON válido, sin texto adicional, con esta forma:
{"title": "...", "description": "...", "questions": [
  {"text": "...", "options": ["a","b","c","d"], "correct_index": 0,
   "explanation": "..."}
]}"""


async def generar_quiz(tema: str, nivel: str, cantidad: int = 10) -> dict:
    prompt = (
        f"Crea un quiz de {cantidad} preguntas de opción múltiple sobre "
        f"'{tema}' para estudiantes de nivel {nivel}. "
        f"El título y la descripción van en español."
    )
    data = await _generar(prompt, INSTRUCCION_QUIZ)

    # Validación: mejor rechazar que guardar algo roto
    preguntas = data.get("questions") or []
    limpias = []
    for q in preguntas:
        opciones = q.get("options") or []
        idx = q.get("correct_index")
        if not q.get("text") or len(opciones) != 4:
            continue
        if not isinstance(idx, int) or not (0 <= idx < 4):
            continue
        limpias.append({
            "text": str(q["text"])[:500],
            "options": [str(o)[:200] for o in opciones],
            "correct_index": idx,
            "explanation": str(q.get("explanation") or "")[:500],
        })
    if not limpias:
        raise RuntimeError("La IA no generó preguntas válidas. Intenta de nuevo.")

    return {
        "title": str(data.get("title") or f"Quiz de {tema}")[:150],
        "description": str(data.get("description") or "")[:400],
        "questions": limpias,
    }


# ============================================================================
# LECCIONES DE GRAMÁTICA
# ============================================================================

INSTRUCCION_LECCION = """Eres un profesor de inglés experto que escribe
lecciones para hispanohablantes de República Dominicana.

Reglas:
- La explicación va EN ESPAÑOL; los ejemplos EN INGLÉS con su traducción.
- Ajusta la dificultad al nivel pedido del Marco Común Europeo.
- Sé claro y práctico: nada de teoría innecesaria.
- Incluye los errores típicos que comete un hispanohablante con ese tema.

Responde SOLO con JSON válido, sin texto adicional, con esta forma:
{"title": "...", "summary": "...", "explanation": "...",
 "examples": [{"en": "...", "es": "..."}],
 "common_mistakes": [{"wrong": "...", "right": "...", "why": "..."}],
 "practice": ["...", "..."]}"""


async def generar_leccion(tema: str, nivel: str) -> dict:
    prompt = (
        f"Escribe una lección sobre '{tema}' para estudiantes de nivel {nivel}. "
        f"Incluye 5 ejemplos, 3 errores comunes y 5 ejercicios de práctica."
    )
    data = await _generar(prompt, INSTRUCCION_LECCION)
    return {
        "title": str(data.get("title") or tema)[:150],
        "summary": str(data.get("summary") or "")[:400],
        "explanation": str(data.get("explanation") or "")[:6000],
        "examples": (data.get("examples") or [])[:10],
        "common_mistakes": (data.get("common_mistakes") or [])[:6],
        "practice": (data.get("practice") or [])[:10],
    }


# ============================================================================
# TAREAS
# ============================================================================

INSTRUCCION_TAREA = """Eres un profesor de inglés que diseña tareas para
estudiantes hispanohablantes.

Reglas:
- El enunciado va EN ESPAÑOL; lo que el estudiante produce, EN INGLÉS.
- La tarea debe poder hacerse en 20-30 minutos.
- Debe ser concreta: nada de "practica inglés".

Responde SOLO con JSON válido:
{"title": "...", "instructions": "...", "max_score": 100}"""


async def generar_tarea(tema: str, nivel: str) -> dict:
    prompt = f"Crea una tarea sobre '{tema}' para estudiantes de nivel {nivel}."
    data = await _generar(prompt, INSTRUCCION_TAREA)
    puntaje = data.get("max_score", 100)
    try:
        puntaje = max(10, min(100, int(puntaje)))
    except (TypeError, ValueError):
        puntaje = 100
    return {
        "title": str(data.get("title") or tema)[:150],
        "instructions": str(data.get("instructions") or "")[:4000],
        "max_score": puntaje,
    }
