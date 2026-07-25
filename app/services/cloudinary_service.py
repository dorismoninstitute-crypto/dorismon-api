"""V3.9.23 — Subida de imágenes a Cloudinary.

Cloudinary es el "depósito" donde viven las fotos de la página pública.
El admin sube desde el panel de Dorismon; esto se encarga del resto.

CONFIGURACIÓN (variables de entorno en Render):
  Opción A (recomendada, una sola variable):
      CLOUDINARY_URL=cloudinary://<api_key>:<api_secret>@<cloud_name>
  Opción B (tres variables sueltas):
      CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET

Si no hay configuración, la plataforma sigue funcionando normal: la sección
de imágenes avisa que falta configurar y nada más se rompe.
"""
import os
import logging

log = logging.getLogger(__name__)

_configured = False


def cloudinary_ready() -> bool:
    """¿Están las credenciales puestas? (acepta cualquiera de las dos formas)"""
    if os.getenv("CLOUDINARY_URL"):
        return True
    return bool(
        os.getenv("CLOUDINARY_CLOUD_NAME")
        and os.getenv("CLOUDINARY_API_KEY")
        and os.getenv("CLOUDINARY_API_SECRET")
    )


def _ensure_config():
    """Configura la librería una sola vez."""
    global _configured
    if _configured:
        return
    import cloudinary

    if os.getenv("CLOUDINARY_URL"):
        # La librería lee CLOUDINARY_URL sola
        cloudinary.config(secure=True)
    else:
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET"),
            secure=True,
        )
    _configured = True


def upload_image_sync(file_bytes: bytes, public_id: str, folder: str = "dorismon") -> dict:
    """Sube una imagen y devuelve {url, public_id}.

    Cloudinary optimiza sola: convierte al mejor formato para cada navegador
    y ajusta la calidad, así la página carga rápido aunque suban una foto
    pesada del celular.
    """
    _ensure_config()
    import cloudinary.uploader

    res = cloudinary.uploader.upload(
        file_bytes,
        public_id=public_id,
        folder=folder,
        overwrite=True,
        invalidate=True,
        resource_type="image",
        # Optimización automática de formato y calidad
        fetch_format="auto",
        quality="auto:good",
    )
    return {"url": res.get("secure_url"), "public_id": res.get("public_id")}


def delete_image_sync(public_id: str) -> bool:
    """Borra una imagen de Cloudinary. Si falla, no rompe nada."""
    try:
        _ensure_config()
        import cloudinary.uploader

        cloudinary.uploader.destroy(public_id, invalidate=True)
        return True
    except Exception as e:
        log.warning("No se pudo borrar la imagen %s: %s", public_id, e)
        return False


# Espacios de imagen de la página pública.
# El admin ve estos nombres en español con el tamaño recomendado al lado.
SITE_IMAGE_SLOTS = [
    {
        "slot": "hero",
        "label": "Foto principal",
        "hint": "1200 × 800 px · JPG · menos de 300 KB",
        "description": "La foto grande de arriba, al lado del titular. Una persona real conecta mucho más que un dibujo.",
        "priority": "imprescindible",
        "has_drawing": False,
    },
    {
        "slot": "og",
        "label": "Imagen para compartir por WhatsApp",
        "hint": "1200 × 630 px · JPG · menos de 200 KB",
        "description": "Lo que se ve cuando alguien manda el enlace de dorismon.com por WhatsApp.",
        "priority": "imprescindible",
        "has_drawing": False,
    },
    {
        "slot": "platform",
        "label": "Captura grande del panel",
        "hint": "1600 × 1000 px · PNG · menos de 400 KB",
        "description": "Entra como estudiante y toma una captura del dashboard. Es lo que demuestra que la plataforma es real.",
        "priority": "recomendada",
        "has_drawing": True,
    },
    {
        "slot": "mini_class",
        "label": "Mini: entrar a clase",
        "hint": "600 × 400 px · PNG",
        "description": "Recorte del calendario o de la próxima clase. Mientras no la subas, se muestra un dibujo.",
        "priority": "opcional",
        "has_drawing": True,
    },
    {
        "slot": "mini_progress",
        "label": "Mini: tu progreso",
        "hint": "600 × 400 px · PNG",
        "description": "Recorte de la barra de avance del estudiante. Mientras no la subas, se muestra un dibujo.",
        "priority": "opcional",
        "has_drawing": True,
    },
    {
        "slot": "mini_certificate",
        "label": "Mini: certificado",
        "hint": "600 × 400 px · PNG",
        "description": "Recorte de un certificado. Mientras no la subas, se muestra un dibujo.",
        "priority": "opcional",
        "has_drawing": True,
    },
    {
        "slot": "group",
        "label": "Foto de grupo",
        "hint": "1000 × 700 px · JPG · menos de 250 KB",
        "description": "Una clase real con varios estudiantes (con su permiso). Refuerza los grupos pequeños.",
        "priority": "recomendada",
        "has_drawing": False,
    },
    {
        "slot": "cta",
        "label": "Ilustración del cierre",
        "hint": "800 × 600 px · PNG con fondo transparente",
        "description": "El bloque final antes del pie de página. Mientras no la subas, se muestra un dibujo.",
        "priority": "opcional",
        "has_drawing": True,
    },
]

SLOT_KEYS = {s["slot"] for s in SITE_IMAGE_SLOTS}
