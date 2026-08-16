"""V3.9.53 P3 — CONFIGURACIÓN ACADÉMICA.

UNA SOLA FUENTE DE VERDAD para los requisitos de promoción.

Los porcentajes viven aquí y solo aquí. Si mañana Dorismon decide que la
asistencia mínima es 85%, se cambia en un sitio y todo el sistema lo respeta:
el cálculo, la pantalla del profesor, la del estudiante y la cola de
Dirección.

PREPARADO PARA SER CONFIGURABLE:
`requisitos_de()` recibe curso, nivel y módulo. Hoy devuelve los valores por
defecto, pero la firma ya permite guardar overrides en base de datos sin
tocar a quien la llama. No se construye el panel de configuración todavía
porque nadie lo ha pedido — y una tabla vacía con un panel encima es
complejidad sin uso.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict


# ============================================================================
# VALORES POR DEFECTO
# ============================================================================
# Acordados con Dirección. Son RECOMENDACIONES: el sistema calcula, el
# profesor recomienda y Dirección decide. Nunca promueven solos.

ASISTENCIA_MINIMA = 80.0          # % de clases a las que asistió
TAREAS_MINIMAS = 75.0             # % de tareas requeridas entregadas
PROMEDIO_QUIZZES_MINIMO = 70.0    # promedio de sus quizzes

# Habilidades que deben estar evaluadas para poder cerrar un nivel
HABILIDADES_REQUERIDAS = ("speaking", "listening", "reading", "writing")

# Otras que el instituto puede evaluar, sin ser obligatorias
HABILIDADES_OPCIONALES = ("grammar", "vocabulary")

# Nota mínima por habilidad. None = basta con que esté evaluada.
# Se deja explícito para que se vea que es una decisión, no un olvido.
NOTA_MINIMA_POR_HABILIDAD = None

ESCALA_MAXIMA = 100.0


NOMBRES_HABILIDADES = {
    "speaking": "Speaking (hablar)",
    "listening": "Listening (escuchar)",
    "reading": "Reading (leer)",
    "writing": "Writing (escribir)",
    "grammar": "Gramática",
    "vocabulary": "Vocabulario",
}


@dataclass
class RequisitosAcademicos:
    """Lo que hace falta para completar un nivel."""
    asistencia_minima: float = ASISTENCIA_MINIMA
    tareas_minimas: float = TAREAS_MINIMAS
    promedio_quizzes_minimo: float = PROMEDIO_QUIZZES_MINIMO
    habilidades_requeridas: tuple = HABILIDADES_REQUERIDAS
    nota_minima_habilidad: float | None = NOTA_MINIMA_POR_HABILIDAD
    escala_maxima: float = ESCALA_MAXIMA

    def como_dict(self) -> dict:
        d = asdict(self)
        d["habilidades_requeridas"] = list(self.habilidades_requeridas)
        d["nombres_habilidades"] = NOMBRES_HABILIDADES
        return d


def requisitos_de(course_id: int | None = None,
                  level_id: int | None = None,
                  module_id: int | None = None) -> RequisitosAcademicos:
    """Los requisitos que aplican a este curso / nivel / módulo.

    Hoy devuelve los valores por defecto para todos. La firma acepta ya los
    tres identificadores para que, cuando haga falta afinar por nivel (por
    ejemplo, exigir más en C1), se resuelva aquí dentro sin cambiar ni una
    línea en el resto del sistema.
    """
    return RequisitosAcademicos()


# ============================================================================
# ESTADOS ACADÉMICOS DE UNA MATRÍCULA
# ============================================================================
#
# ⚠️ SEMÁNTICA, definida antes de implementar:
#
#   active                  → cursando con normalidad
#   completion_review       → cumple los requisitos y espera revisión
#   requires_reinforcement  → le falta algo concreto; sigue cursando
#   requires_reevaluation   → hay que volver a evaluarlo (p. ej. speaking)
#   completed               → Dirección aprobó la finalización. FINAL.
#   withdrawn               → se retiró. No da derecho a certificado.
#
# `paused` NO está aquí: la pausa es administrativa (`Student.is_paused`) y no
# cambia el punto académico en que quedó. Al volver, sigue donde estaba.
#
# `at_risk` TAMPOCO: un estudiante en riesgo sigue activo. Se calcula al vuelo
# en tracking.py con datos vivos; guardarlo como estado lo dejaría desfasado.

ESTADOS_ACADEMICOS = {
    "active": "Cursando",
    "completion_review": "Listo para revisión",
    "requires_reinforcement": "Necesita refuerzo",
    "requires_reevaluation": "Pendiente de reevaluar",
    "completed": "Nivel completado",
    "withdrawn": "Retirado",
}

# Desde qué estado se puede pasar a cuál. Evita saltos imposibles, como
# volver a "cursando" algo que ya se completó.
TRANSICIONES = {
    "active": {"completion_review", "requires_reinforcement", "withdrawn"},
    "completion_review": {"completed", "requires_reinforcement",
                          "requires_reevaluation", "active"},
    "requires_reinforcement": {"completion_review", "active", "withdrawn"},
    "requires_reevaluation": {"completion_review", "requires_reinforcement", "active"},
    "completed": set(),          # final: no se sale de aquí
    "withdrawn": {"active"},     # reingreso controlado
}


def puede_pasar_a(actual: str, nuevo: str) -> bool:
    """¿Es válido este cambio de estado?"""
    return nuevo in TRANSICIONES.get(actual or "active", set())


RECOMENDACIONES = {
    "recommend_promotion": "Recomiendo promoción",
    "requires_reinforcement": "Necesita refuerzo",
    "requires_reevaluation": "Hay que reevaluarlo",
}
