"""GATE 6G-D — Límites de frecuencia para Video Dorismon (protección contra abuso).

QUÉ PROTEGE: los endpoints HTTP de Video Dorismon (los que emiten tokens o llaman a
la API de LiveKit). NO protege lo que viaja directamente por LiveKit (reacciones,
chat, data packets): eso no pasa por este backend.

ALGORITMO: "token bucket" (cubeta de fichas) por clave.
  · Cada clave tiene `capacidad` fichas (la ráfaga legítima permitida) y recupera
    `por_segundo` fichas por segundo (el ritmo sostenido permitido).
  · Cada petición gasta 1 ficha; sin fichas → 429 con Retry-After = lo que falta
    para la próxima ficha.
  Por qué: memoria O(1) por clave (2 números), ráfagas cortas permitidas sin dejar
  pasar un ritmo sostenido de abuso, y es fácil de probar con un reloj inyectado.

CLAVE: usuario AUTENTICADO (del JWT, nunca del cuerpo) + clase + acción. Nunca la IP:
  muchas personas de una escuela o familia salen por la misma IP.

ALMACENAMIENTO: EN MEMORIA, POR PROCESO. Dorismon corre hoy en UNA instancia de
  Render (uvicorn sin workers extra): la protección es completa dentro de ella. Con N
  instancias cada una tendría sus propios contadores (NO es un límite global
  distribuido). Para escalar, se reemplaza `LimitadorEnMemoria` por uno compartido
  (Redis u otro) con la misma interfaz `consumir(clave, limite)`; los endpoints no cambian.

CONCURRENCIA: las claves se reparten en franjas, cada una con su propio cerrojo
  (nunca un cerrojo global que vuelva secuencial la app). La operación es corta y
  sin esperas de red dentro del cerrojo.

MEMORIA: una clave cuya cubeta ya está llena de nuevo no aporta nada y se borra.
  Limpieza perezosa (sin tareas de fondo): una franja se limpia al usarse si pasó 1
  minuto desde su última limpieza, cada `purgar_cada` usos, o si supera su tope. Si
  aun así no cabe, se descartan las claves menos recientes.
"""
import math
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException

MENSAJE_429 = "Has realizado demasiadas acciones en poco tiempo. Espera unos segundos e inténtalo de nuevo."


@dataclass(frozen=True)
class Limite:
    nombre: str
    capacidad: float      # ráfaga legítima (fichas)
    por_segundo: float    # recuperación sostenida (fichas / segundo)


# Valores y justificación (frecuencia normal observada en el frontend actual):
LIMITES = {
    # /join, por persona (antes de tocar la base de datos). Uso normal: entrar,
    # renovar en la pantalla previa (6G-B), "Volver a entrar" (6B), recuperación (6E),
    # algún fallo de conexión: unas pocas por clase. 30 seguidas y luego 1 cada 2 s.
    "join_usuario": Limite("join_usuario", 30, 1 / 2),
    # /join, por persona y clase, justo antes de firmar el token (el coste real).
    # 12 seguidas y luego 1 cada 10 s (6/min): nadie necesita cientos de tokens.
    "join_clase": Limite("join_clase", 12, 1 / 10),
    # /video/hora (6F-A.1): 3 muestras por sincronización; reintentos 2/5/10 s;
    # reconectar; volver a la pestaña; temporizador nuevo. 60 seguidas y 1/s sostenido
    # = 20 sincronizaciones por minuto, muy por encima de cualquier uso real.
    "hora": Limite("hora", 60, 1),
    # Heartbeat: 1 por minuto + 1 al montar (cambiar completa/mini lo vuelve a montar).
    # 10 seguidos y luego 1 cada 15 s (4/min, el cuádruple de lo normal).
    "heartbeat": Limite("heartbeat", 10, 1 / 15),
    # Interacción 6F-A del estudiante (su mano): pocas veces por clase.
    # 10 seguidas y luego 1 cada 3 s.
    "interaccion_estudiante": Limite("interaccion_estudiante", 10, 1 / 3),
    # Interacción del profesor / Dirección (turno, temporizador, foco, bajar manos):
    # puede hacer varias seguidas. 30 seguidas y 1/s sostenido.
    "interaccion_moderador": Limite("interaccion_moderador", 30, 1),
    # Moderación 6G-A (silenciar, sacar, volver a desconectar, permitir volver):
    # una clase grande (30 alumnos) se gestiona de corrido. 40 seguidas y 1/s.
    "moderacion": Limite("moderacion", 40, 1),
    # Permisos 6G-C (2 llamadas a LiveKit cada uno): mismo margen que la moderación.
    "permisos": Limite("permisos", 40, 1),
    # GET /participants (ListParticipants en LiveKit): el frontend actual no lo usa.
    "participantes": Limite("participantes", 10, 1 / 6),
    # GET /bloqueos (solo base de datos): al abrir el panel y tras cada acción.
    "bloqueos": Limite("bloqueos", 30, 1 / 2),
    # GATE 6H-A — iniciar / detener grabación: pocas veces por clase (ráfaga 20: diez
    # clics nerviosos reciben "ya_activa", no 429). La protección principal contra
    # duplicados es la IDEMPOTENCIA (una sola grabación activa por clase).
    "grabacion": Limite("grabacion", 20, 1 / 6),
    # Estado de la grabación (el panel del profesor lo consulta cada pocos segundos).
    "grabacion_estado": Limite("grabacion_estado", 60, 1),
    # GATE 6H-B — biblioteca: listar (paneles, filtros, páginas) y pedir URL firmada
    # (abrir + renovar ~1 cada 10 min). Por PERSONA (no por grabación): también frena a
    # quien prueba recording_id ajenos.
    "biblioteca": Limite("biblioteca", 60, 1),
    "reproducir": Limite("reproducir", 20, 1 / 15),
    # GATE 6F-B — contenido de clase: el profesor puede soltar varias palabras seguidas
    # (60 de golpe, luego 1/s); leer: al entrar, al reconectar, por aviso y cada minuto.
    "contenido": Limite("contenido", 60, 1),
    "contenido_lectura": Limite("contenido_lectura", 120, 2),
}


class LimitadorEnMemoria:
    def __init__(self, franjas: int = 64, max_claves: int = 50_000, purgar_cada: int = 512,
                 purgar_tras_segundos: float = 60, reloj=time.monotonic):
        self._franjas = [{} for _ in range(franjas)]
        self._cerrojos = [threading.Lock() for _ in range(franjas)]
        self._contador = [0] * franjas
        self._ultima_purga = [None] * franjas
        self._max_por_franja = max(16, max_claves // franjas)
        self._purgar_cada = purgar_cada
        self._purgar_tras = purgar_tras_segundos
        self.reloj = reloj

    def _franja(self, clave: str) -> int:
        return hash(clave) % len(self._franjas)

    def consumir(self, clave: str, limite: Limite) -> float:
        """0 si se permite (y gasta una ficha); si no, segundos hasta la próxima ficha."""
        i = self._franja(clave)
        with self._cerrojos[i]:
            ahora = self.reloj()
            d = self._franjas[i]
            fichas, ultimo = d[clave][:2] if clave in d else (limite.capacidad, ahora)
            fichas = min(limite.capacidad, fichas + (ahora - ultimo) * limite.por_segundo)
            if fichas >= 1:
                d[clave] = (fichas - 1, ahora, limite)
                espera = 0.0
            else:
                d[clave] = (fichas, ahora, limite)
                espera = (1 - fichas) / limite.por_segundo
            self._contador[i] += 1
            if self._ultima_purga[i] is None:
                self._ultima_purga[i] = ahora
            # Limpieza perezosa: cada N usos de la franja, si pasó 1 minuto desde la
            # última limpieza de la franja, o si supera su tope (sin tareas de fondo)
            if (self._contador[i] % self._purgar_cada == 0 or len(d) > self._max_por_franja
                    or ahora - self._ultima_purga[i] >= self._purgar_tras):
                self._purgar_franja(i, ahora)
                self._ultima_purga[i] = ahora
            return espera

    def _purgar_franja(self, i: int, ahora: float):
        """(Con el cerrojo de la franja tomado.) Borra cubetas ya llenas otra vez; si la
        franja sigue por encima del tope, descarta las menos recientes."""
        d = self._franjas[i]
        for k in [k for k, (f, u, lim) in d.items()
                  if f + (ahora - u) * lim.por_segundo >= lim.capacidad]:
            del d[k]
        if len(d) > self._max_por_franja:
            for k, _ in sorted(d.items(), key=lambda kv: kv[1][1])[:len(d) - self._max_por_franja]:
                del d[k]

    def purgar(self):
        for i in range(len(self._franjas)):
            with self._cerrojos[i]:
                ahora = self.reloj()
                self._purgar_franja(i, ahora)
                self._ultima_purga[i] = ahora

    def tamanio(self) -> int:
        return sum(len(d) for d in self._franjas)


LIMITADOR = LimitadorEnMemoria()


def comprobar(accion: str, user_id: str, session_id: str | None = None) -> None:
    """Gasta una ficha de (acción, usuario, clase) o lanza 429 con Retry-After."""
    limite = LIMITES[accion]
    espera = LIMITADOR.consumir(f"{accion}:{user_id}:{session_id or '-'}", limite)
    if espera > 0:
        raise HTTPException(429, MENSAJE_429, headers={"Retry-After": str(max(1, math.ceil(espera)))})


# ------------------------------------------------ Finalizar: resultado del cierre
# Recuerda (en memoria, por proceso, 2 h como máximo) cómo terminó el cierre de la
# sala de cada clase finalizada, para responder lo mismo a peticiones repetidas y
# permitir reintentar SOLO si falló. Acotado: se limpia al pasar de 5 000 entradas.
_CIERRES: dict = {}
_REINTENTANDO: set = set()
_CIERRES_LOCK = threading.Lock()
_CIERRE_TTL = 2 * 60 * 60
_CIERRES_MAX = 5_000


def registrar_cierre(session_id: str, resultado: str) -> None:
    with _CIERRES_LOCK:
        ahora = time.monotonic()
        _CIERRES[session_id] = (resultado, ahora)
        if len(_CIERRES) > _CIERRES_MAX:
            for k in [k for k, (_, t) in _CIERRES.items() if ahora - t > _CIERRE_TTL]:
                del _CIERRES[k]
            for k, _ in sorted(_CIERRES.items(), key=lambda kv: kv[1][1])[:max(0, len(_CIERRES) - _CIERRES_MAX)]:
                del _CIERRES[k]


def cierre_previo(session_id: str):
    with _CIERRES_LOCK:
        v = _CIERRES.get(session_id)
        if not v or time.monotonic() - v[1] > _CIERRE_TTL:
            return None
        return v[0]


def tomar_reintento_cierre(session_id: str) -> bool:
    """Solo un reintento de cierre a la vez por clase."""
    with _CIERRES_LOCK:
        if session_id in _REINTENTANDO:
            return False
        _REINTENTANDO.add(session_id)
        return True


def soltar_reintento_cierre(session_id: str) -> None:
    with _CIERRES_LOCK:
        _REINTENTANDO.discard(session_id)
