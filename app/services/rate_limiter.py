"""V2.9.3 — Rate limiter en memoria para proteger el login (endurecido en v3.9.76).

Cuenta intentos fallidos y bloquea temporalmente. No requiere Redis.

DOS niveles de clave (las usa routers/auth.py) CUANDO la IP del cliente es fiable:
  · "ip:email"  → 5 fallos en 15 min bloquean ESE par: un atacante desde su IP no puede bloquear a
                  la persona dueña de la cuenta (que entra desde otra IP).
  · "ip:*"      → 60 fallos (de cualquier cuenta) en 15 min bloquean esa IP: frena el "rociado" de
                  contraseñas sobre muchas cuentas.

IP DEL CLIENTE:
  · Render: usa exclusivamente CF-Connecting-IP, que debe contener una IP válida. Si falta o es inválida,
    la identidad de IP se considera NO fiable y no se crean buckets de rate limiting basados en la IP
    compartida del proxy/socket (fail-safe contra bloqueos cruzados de usuarios).
  · No-Render: una conexión directa sin cabeceras de proxy usa la IP válida del socket. Para una
    infraestructura propia con proxies de confianza puede configurarse TRUSTED_PROXY_COUNT y se toma el
    salto correspondiente de X-Forwarded-For. Si la cadena es insuficiente o inválida, no se confía.

MEMORIA ACOTADA: como máximo MAX_ENTRIES claves. La limpieza es real: caduca por actividad (no solo al
reintentar la misma clave), se ejecuta de forma oportunista cada PURGE_EVERY llamadas y, si aun así se
supera el tope, se expulsan primero las entradas más antiguas.

LIMITACIÓN (single-instance): el estado vive en la memoria de UN proceso. Con varias instancias, o tras un
reinicio, los contadores no se comparten ni sobreviven; entonces habría que moverlo a Redis. Con la
configuración actual (una instancia en Render) basta.
"""
import ipaddress
import logging
import os
import time
from threading import Lock

# Configuración
MAX_ATTEMPTS = 5          # fallos permitidos por par ip:email
IP_MAX_ATTEMPTS = 60      # fallos permitidos por IP (todas las cuentas): frena el rociado, no a una oficina
WINDOW_SECONDS = 900      # ventana de 15 min para contar intentos
LOCKOUT_SECONDS = 900     # bloqueo de 15 min tras superar el límite
MAX_ENTRIES = 10_000      # tope de claves en memoria
PURGE_EVERY = 200         # limpieza oportunista cada N llamadas
MAX_KEY_LEN = 320         # claves más largas se recortan (el email viene del cliente)

# Estructura: { key: {"fails": [timestamps], "locked_until": ts, "last": ts} }
_store: dict[str, dict] = {}
_lock = Lock()
_calls = 0
_warned: set[str] = set()
_log = logging.getLogger(__name__)


def _now() -> float:
    return time.time()


def _k(key: str) -> str:
    return key if len(key) <= MAX_KEY_LEN else key[:MAX_KEY_LEN]


def _vigente(entry: dict, now: float) -> bool:
    """¿La entrada todavía importa? (bloqueo activo o fallos dentro de la ventana)."""
    return entry.get("locked_until", 0) > now or any(t > now - WINDOW_SECONDS for t in entry["fails"])


def _purge_locked(now: float) -> int:
    """Quita lo caducado (con _lock tomado). Devuelve cuántas entradas se borraron."""
    muertas = [k for k, v in _store.items() if not _vigente(v, now)]
    for k in muertas:
        _store.pop(k, None)
    # Tope duro: si hay demasiadas vigentes, se expulsan las de actividad más antigua
    if len(_store) >= MAX_ENTRIES:   # deja hueco para la entrada que se va a crear: nunca supera el tope
        exceso = len(_store) - MAX_ENTRIES + 1 + max(1, MAX_ENTRIES // 10)
        for k, _v in sorted(_store.items(), key=lambda kv: kv[1].get("last", 0))[:exceso]:
            _store.pop(k, None)
    return len(muertas)


def _tick(now: float):
    """Limpieza oportunista (con _lock tomado): periódica o cuando se acerca al tope."""
    global _calls
    _calls += 1
    if _calls % PURGE_EVERY == 0 or len(_store) >= MAX_ENTRIES:
        _purge_locked(now)


def check_rate_limit(key: str) -> tuple[bool, int]:
    """Verifica si la clave está bloqueada.

    Retorna (permitido, segundos_restantes_de_bloqueo).
    Si permitido=True, segundos=0.
    """
    key = _k(key)
    with _lock:
        now = _now()
        _tick(now)
        entry = _store.get(key)
        if not entry:
            return True, 0
        locked_until = entry.get("locked_until", 0)
        if locked_until > now:
            return False, max(1, int(locked_until - now))
        return True, 0


def register_failure(key: str, max_attempts: int = MAX_ATTEMPTS) -> tuple[bool, int]:
    """Registra un intento fallido. Si supera el límite, bloquea.

    Retorna (recién_bloqueado, segundos_de_bloqueo).
    """
    key = _k(key)
    with _lock:
        now = _now()
        _tick(now)
        entry = _store.setdefault(key, {"fails": [], "locked_until": 0, "last": now})
        entry["last"] = now
        entry["fails"] = [t for t in entry["fails"] if t > now - WINDOW_SECONDS]
        entry["fails"].append(now)
        if len(entry["fails"]) >= max_attempts:
            entry["locked_until"] = now + LOCKOUT_SECONDS
            entry["fails"] = []  # resetear contador tras bloquear
            return True, LOCKOUT_SECONDS
        return False, 0


def register_success(key: str):
    """Limpia el registro tras un login exitoso."""
    with _lock:
        _store.pop(_k(key), None)


def _purge_old() -> int:
    """Limpieza global de entradas caducadas. Devuelve cuántas borró (la usan los tests)."""
    with _lock:
        return _purge_locked(_now())


def stats() -> dict:
    with _lock:
        return {"entradas": len(_store), "tope": MAX_ENTRIES}


def reset():
    """Solo para tests."""
    global _calls
    with _lock:
        _store.clear()
        _calls = 0
    _warned.clear()


# ---------------------------------------------------------------------------------------------
# IP del cliente y claves del login
# ---------------------------------------------------------------------------------------------
def _proxies_de_confianza() -> int:
    try:
        return max(0, int(os.getenv("TRUSTED_PROXY_COUNT", "0")))
    except ValueError:
        return 0


def _en_render() -> bool:
    return os.getenv("RENDER", "").strip().lower() in {"1", "true", "yes"}


def _ip_valida(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _warn_once(code: str, message: str) -> None:
    if code not in _warned:
        _warned.add(code)
        _log.warning(message)


_CABECERAS_PROXY = ("x-forwarded-for", "x-real-ip", "forwarded", "cf-connecting-ip", "true-client-ip")


def resolver_ip_cliente(request) -> tuple[str, bool]:
    """Resuelve (ip, fiable) en una sola operación.

    En Render solo se confía en CF-Connecting-IP. Si ese encabezado falta o es inválido, se conserva la IP
    del socket únicamente para diagnóstico, pero se marca como NO fiable y el login no crea buckets de rate
    limiting basados en ella.

    Fuera de Render, TRUSTED_PROXY_COUNT habilita explícitamente X-Forwarded-For para una infraestructura
    propia. Sin proxies configurados y sin cabeceras de proxy, una IP válida del socket es una conexión
    directa fiable.
    """
    peer_raw = request.client.host if getattr(request, "client", None) and request.client.host else None
    peer = _ip_valida(peer_raw)
    fallback = peer or "unknown"

    if _en_render():
        cf_ip = _ip_valida(request.headers.get("cf-connecting-ip"))
        if cf_ip:
            return cf_ip, True
        _warn_once(
            "render-cf-ip-missing",
            "LIMITADOR DE LOGIN: RENDER=true pero CF-Connecting-IP falta o es inválido. "
            "La IP del cliente no es fiable y se desactivan los buckets por IP para evitar bloqueos cruzados.",
        )
        return fallback, False

    n = _proxies_de_confianza()
    if n > 0:
        partes = [p.strip() for p in (request.headers.get("x-forwarded-for") or "").split(",") if p.strip()]
        if len(partes) < n:
            _warn_once(
                "xff-insuficiente",
                "LIMITADOR DE LOGIN: TRUSTED_PROXY_COUNT está configurado pero X-Forwarded-For no contiene "
                "suficientes saltos. La IP del cliente se considera no fiable.",
            )
            return fallback, False
        ip = _ip_valida(partes[-n])
        if not ip:
            _warn_once(
                "xff-invalido",
                "LIMITADOR DE LOGIN: el salto seleccionado de X-Forwarded-For no es una IP válida. "
                "La IP del cliente se considera no fiable.",
            )
            return fallback, False
        return ip, True

    # Sin proxy administrado: si aparecen cabeceras de proxy, no podemos demostrar que el socket represente
    # al visitante final. Es preferible perder temporalmente el límite por IP que bloquear a usuarios distintos
    # bajo una IP compartida.
    if any(request.headers.get(h) for h in _CABECERAS_PROXY):
        _warn_once(
            "proxy-no-configurado",
            "LIMITADOR DE LOGIN: llegaron cabeceras de proxy fuera de Render sin TRUSTED_PROXY_COUNT. "
            "La IP del cliente se considera no fiable; configure el proxy explícitamente si corresponde.",
        )
        return fallback, False

    if peer:
        return peer, True
    return "unknown", False


def client_ip(request) -> str:
    """Compatibilidad: devuelve la IP resuelta; use resolver_ip_cliente() cuando también importe la confianza."""
    return resolver_ip_cliente(request)[0]


def ip_confiable(request) -> bool:
    """Compatibilidad: indica si la IP resuelta identifica de forma fiable al cliente."""
    return resolver_ip_cliente(request)[1]


def claves_login(request, email: str) -> tuple[str | None, str | None]:
    """Devuelve (clave ip:email, clave ip:*) solo si la identidad de IP es fiable.

    Si la IP no es fiable se devuelve (None, None): no se usa ni la IP del proxy ni el email solo como
    sustituto, porque ambas opciones podrían permitir bloqueos cruzados de terceros.
    """
    ip, fiable = resolver_ip_cliente(request)
    if not fiable:
        return None, None
    email_norm = (email or "").strip().lower()
    return f"{ip}:{email_norm}", f"{ip}:*"
