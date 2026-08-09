"""V3.9.29 — Genera las claves para los avisos al teléfono.

CÓMO USARLO (una sola vez):
    python generar_claves_push.py

Te imprime dos claves. Las copias a Render → dorismon-api → Environment:

    VAPID_PUBLIC_KEY    (la pública)
    VAPID_PRIVATE_KEY   (la privada — es una contraseña, no la compartas)
    VAPID_SUBJECT       mailto:dorismoninstitute@gmail.com

Guarda las dos en un lugar seguro. Si las pierdes y generas otras, todos los
que ya activaron los avisos tendrán que volver a activarlos.
"""
import base64

from py_vapid import Vapid01
from cryptography.hazmat.primitives import serialization


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main():
    v = Vapid01()
    v.generate_keys()

    publica = v.public_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    privada = v.private_key.private_numbers().private_value.to_bytes(32, "big")

    print()
    print("=" * 62)
    print("  CLAVES PARA LOS AVISOS AL TELÉFONO")
    print("=" * 62)
    print()
    print("Copia estas tres líneas a Render (dorismon-api → Environment):")
    print()
    print("  VAPID_PUBLIC_KEY")
    print(" ", b64(publica))
    print()
    print("  VAPID_PRIVATE_KEY")
    print(" ", b64(privada))
    print()
    print("  VAPID_SUBJECT")
    print("  mailto:dorismoninstitute@gmail.com")
    print()
    print("=" * 62)
    print("  ⚠️  La clave PRIVADA es una contraseña. No la compartas ni la")
    print("      pegues en un chat. Guárdala en un lugar seguro.")
    print("=" * 62)
    print()


if __name__ == "__main__":
    main()
