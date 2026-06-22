"""V2.9.3 — Rate limiter en memoria para proteger el login.

Cuenta intentos fallidos por clave (IP + email) y bloquea temporalmente.
No requiere Redis: usa un diccionario en memoria con limpieza automática.

Para la escala actual (cientos de usuarios) es suficiente. Si en el futuro
hay múltiples instancias del backend, se migraría a Redis.
"""
import time
from threading import Lock

# Configuración
MAX_ATTEMPTS = 5          # intentos fallidos permitidos
WINDOW_SECONDS = 900      # ventana de 15 min para contar intentos
LOCKOUT_SECONDS = 900     # bloqueo de 15 min tras superar el límite

# Estructura: { key: {"fails": [timestamps], "locked_until": ts} }
_store: dict[str, dict] = {}
_lock = Lock()


def _now() -> float:
    return time.time()


def _cleanup(key: str):
    """Quita intentos viejos fuera de la ventana."""
    entry = _store.get(key)
    if not entry:
        return
    cutoff = _now() - WINDOW_SECONDS
    entry["fails"] = [t for t in entry["fails"] if t > cutoff]


def check_rate_limit(key: str) -> tuple[bool, int]:
    """Verifica si la clave está bloqueada.

    Retorna (permitido, segundos_restantes_de_bloqueo).
    Si permitido=True, segundos=0.
    """
    with _lock:
        entry = _store.get(key)
        if not entry:
            return True, 0
        locked_until = entry.get("locked_until", 0)
        if locked_until > _now():
            return False, int(locked_until - _now())
        return True, 0


def register_failure(key: str) -> tuple[bool, int]:
    """Registra un intento fallido. Si supera el límite, bloquea.

    Retorna (recién_bloqueado, segundos_de_bloqueo).
    """
    with _lock:
        entry = _store.setdefault(key, {"fails": [], "locked_until": 0})
        _cleanup(key)
        entry["fails"].append(_now())
        if len(entry["fails"]) >= MAX_ATTEMPTS:
            entry["locked_until"] = _now() + LOCKOUT_SECONDS
            entry["fails"] = []  # resetear contador tras bloquear
            return True, LOCKOUT_SECONDS
        return False, 0


def register_success(key: str):
    """Limpia el registro tras un login exitoso."""
    with _lock:
        _store.pop(key, None)


def _purge_old():
    """Limpieza global de entradas viejas (llamar ocasionalmente)."""
    with _lock:
        cutoff = _now() - max(WINDOW_SECONDS, LOCKOUT_SECONDS) - 60
        to_delete = [
            k for k, v in _store.items()
            if v.get("locked_until", 0) < _now() and not v.get("fails")
        ]
        for k in to_delete:
            _store.pop(k, None)
