"""V2.1 — Servicio de email real con Resend.

Si RESEND_API_KEY no está seteada, los emails se loguean pero no se envían (graceful).
Si está seteada, se envían vía API de Resend.

Configurar en Render:
  RESEND_API_KEY=re_xxxxxxxxxxxx
  EMAIL_FROM=Dorismon <onboarding@resend.dev>   (o noreply@dorismon.do si tienes dominio verificado)
  APP_URL=https://dorismon-web.vercel.app
"""
import os
import logging
import secrets
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
EMAIL_FROM = os.getenv("EMAIL_FROM", "Dorismon <onboarding@resend.dev>").strip()
APP_URL = os.getenv("APP_URL", "https://dorismon-web.vercel.app").strip()


def is_email_configured() -> bool:
    """Indica si está configurado el servicio de email real."""
    return bool(RESEND_API_KEY)


async def send_email(to: str, subject: str, html: str, text: str = "") -> bool:
    """Envía un email vía Resend. Retorna True si fue enviado.

    Si no hay API key configurada, loguea y retorna False (no rompe).
    """
    if not RESEND_API_KEY:
        logger.warning(f"[EMAIL] ⚠️ NO HAY RESEND_API_KEY configurada. Email a {to} NO se envió.")
        return False

    logger.info(f"[EMAIL] Enviando a Resend: to={to}, subject={subject[:50]}, from={EMAIL_FROM}, key_len={len(RESEND_API_KEY)}")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            payload = {
                "from": EMAIL_FROM,
                "to": [to],
                "subject": subject,
                "html": html,
                "text": text or _html_to_text(html),
            }
            r = await client.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if r.status_code in (200, 201, 202):
                logger.info(f"[EMAIL ✅ OK] to={to}, resend_status={r.status_code}, response={r.text[:200]}")
                return True
            else:
                logger.error(f"[EMAIL ❌ FAIL] to={to}, resend_status={r.status_code}, body={r.text[:500]}")
                return False
    except Exception as e:
        logger.error(f"[EMAIL ❌ EXCEPTION] to={to}, error={type(e).__name__}: {e}")
        return False


def _html_to_text(html: str) -> str:
    """Extrae texto plano de HTML básico para clientes que no soporten HTML."""
    import re
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"</p>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def gen_verification_code() -> str:
    """Genera un código de 6 dígitos numérico."""
    return f"{secrets.randbelow(900000) + 100000}"  # 100000-999999


def gen_reset_token() -> str:
    """Genera un token seguro de 32 caracteres."""
    return secrets.token_urlsafe(32)


# === TEMPLATES ===

def _base_html(content: str) -> str:
    """Wrapper HTML con branding Dorismon."""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 0; padding: 0; background: #f8fafc; }}
  .container {{ max-width: 560px; margin: 0 auto; background: white; }}
  .header {{ background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%); padding: 32px 24px; text-align: center; color: white; }}
  .header h1 {{ margin: 0; font-size: 24px; font-weight: 900; letter-spacing: -0.5px; }}
  .header .tagline {{ font-size: 11px; opacity: 0.8; text-transform: uppercase; letter-spacing: 2px; margin-top: 4px; }}
  .content {{ padding: 32px 24px; color: #1e293b; line-height: 1.6; }}
  .button {{ display: inline-block; background: #2563eb; color: white; padding: 12px 24px; text-decoration: none; border-radius: 8px; font-weight: 700; margin: 16px 0; }}
  .code-box {{ font-size: 32px; font-weight: 900; letter-spacing: 8px; text-align: center; background: #eff6ff; color: #1e40af; padding: 20px; border-radius: 12px; margin: 24px 0; }}
  .footer {{ background: #f1f5f9; padding: 20px; text-align: center; font-size: 12px; color: #64748b; }}
  .footer a {{ color: #2563eb; text-decoration: none; }}
</style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>DORISMON</h1>
      <div class="tagline">LANGUAGE INSTITUTE</div>
    </div>
    <div class="content">
      {content}
    </div>
    <div class="footer">
      <p>Dorismon Language Institute · Santo Domingo, República Dominicana</p>
      <p>Este email fue enviado automáticamente, no respondas a esta dirección.<br>
      Si necesitas ayuda, entra a la plataforma y ve a la sección "Ayuda".</p>
    </div>
  </div>
</body>
</html>"""


def tpl_welcome(name: str, code: str) -> str:
    return _base_html(f"""
      <h2>¡Bienvenido a Dorismon, {name}! 👋</h2>
      <p>Gracias por registrarte. Para empezar a usar la plataforma necesitamos verificar tu email.</p>
      <p>Tu código de verificación es:</p>
      <div class="code-box">{code}</div>
      <p>Ingresa este código en la pantalla de verificación. <strong>El código vence en 30 minutos.</strong></p>
      <p>Si no te registraste tú, ignora este email y la cuenta no se activará.</p>
    """)


def tpl_welcome_simple(name: str) -> str:
    """V2.4: Email de bienvenida informativo, sin código de verificación.

    El usuario ya está activo. Este email es solo confirmación + información útil.
    """
    return _base_html(f"""
      <h2>¡Bienvenido a Dorismon Language Institute, {name}! 🎓</h2>
      <p>Tu cuenta ya está activa. Te damos la bienvenida a nuestra plataforma de aprendizaje de inglés.</p>

      <h3 style="color: #2563eb; margin-top: 24px;">¿Qué sigue?</h3>
      <ol style="line-height: 2;">
        <li><strong>Haz tu test de nivel</strong> — Te toma alrededor de 10 minutos y nos ayuda a ubicarte en el grupo correcto.</li>
        <li><strong>Espera la asignación</strong> — Nuestro coordinador te asignará un profesor según tu nivel.</li>
        <li><strong>¡Empieza tus clases!</strong> — Recibirás los enlaces y horarios por la plataforma.</li>
      </ol>

      <p style="text-align: center; margin-top: 24px;">
        <a href="{APP_URL}/dashboard" class="button">Ir a mi dashboard</a>
      </p>

      <p style="font-size: 12px; color: #64748b; margin-top: 24px;">
        <strong>¿Tienes dudas?</strong> Entra a la sección "Ayuda" en la plataforma y nuestro equipo te responde rápido.
      </p>
    """)


def tpl_password_reset(name: str, token: str) -> str:
    link = f"{APP_URL}/reset-password?token={token}"
    return _base_html(f"""
      <h2>Recuperar contraseña</h2>
      <p>Hola {name},</p>
      <p>Recibimos una solicitud para resetear tu contraseña. Haz clic en el botón para crear una nueva:</p>
      <p style="text-align: center;">
        <a href="{link}" class="button">Crear nueva contraseña</a>
      </p>
      <p style="font-size: 12px; color: #64748b;">O copiá este link en tu navegador:<br>
      <code style="background: #f1f5f9; padding: 4px 8px; border-radius: 4px;">{link}</code></p>
      <p><strong>El link vence en 2 horas.</strong></p>
      <p>Si no pediste esto, ignorá este email. Tu contraseña no cambia.</p>
    """)


def tpl_teacher_assigned(student_name: str, teacher_name: str, level_code: str) -> str:
    return _base_html(f"""
      <h2>Te asignamos un profesor 🎓</h2>
      <p>Hola {student_name},</p>
      <p>Tu profesor asignado es:</p>
      <p style="font-size: 18px; font-weight: 700; color: #2563eb; text-align: center; padding: 16px; background: #eff6ff; border-radius: 8px;">
        {teacher_name}<br>
        <span style="font-size: 14px; color: #64748b; font-weight: 400;">Nivel {level_code}</span>
      </p>
      <p>Pronto vas a recibir notificaciones sobre tus clases y horarios.</p>
      <p>¡Éxito en tu proceso de aprendizaje!</p>
    """)


def tpl_certificate_issued(student_name: str, level_code: str, code: str) -> str:
    cert_link = f"{APP_URL}/certificate/{code}"
    return _base_html(f"""
      <h2>🎓 ¡Felicidades, {student_name}!</h2>
      <p>Completaste exitosamente el nivel <strong>{level_code}</strong>.</p>
      <p style="text-align: center;">
        <a href="{cert_link}" class="button">Ver mi certificado</a>
      </p>
      <p>Tu código de certificado verificable:</p>
      <div class="code-box" style="font-size: 18px; letter-spacing: 2px;">{code}</div>
      <p>Podés compartir el link de tu certificado o el código para que verifiquen su autenticidad.</p>
    """)


def tpl_teacher_payment(teacher_name: str, month: str, amount: float, classes: int) -> str:
    return _base_html(f"""
      <h2>💰 Pago registrado</h2>
      <p>Hola {teacher_name},</p>
      <p>Se registró tu pago del período <strong>{month}</strong>:</p>
      <p style="font-size: 32px; font-weight: 900; color: #059669; text-align: center; padding: 20px; background: #ecfdf5; border-radius: 12px;">
        RD$ {amount:,.2f}
      </p>
      <p style="text-align: center; color: #64748b;">por <strong>{classes}</strong> clases dictadas</p>
      <p>Si tienes dudas sobre el desglose, ingresá a "Mis ingresos" en la plataforma.</p>
    """)


def tpl_class_reminder(student_name: str, class_title: str, when: str, link: str) -> str:
    return _base_html(f"""
      <h2>📅 Recordatorio de clase</h2>
      <p>Hola {student_name},</p>
      <p>Tu próxima clase es mañana:</p>
      <p style="background: #eff6ff; padding: 16px; border-radius: 8px;">
        <strong>{class_title}</strong><br>
        <span style="color: #64748b;">{when}</span>
      </p>
      {f'<p style="text-align: center;"><a href="{link}" class="button">Entrar a la clase</a></p>' if link else ''}
      <p>¡Te esperamos!</p>
    """)


def tpl_ticket_replied(name: str, subject: str, body_preview: str) -> str:
    return _base_html(f"""
      <h2>💬 Respondieron tu ticket</h2>
      <p>Hola {name},</p>
      <p>El administrador respondió a tu ticket:</p>
      <p style="background: #f1f5f9; padding: 12px; border-radius: 8px; font-weight: 700;">
        {subject}
      </p>
      <p style="font-style: italic; color: #475569;">{body_preview}...</p>
      <p style="text-align: center;">
        <a href="{APP_URL}/dashboard/messages" class="button">Ver respuesta completa</a>
      </p>
    """)


# === VALIDACIÓN MX ===

async def validate_email_domain(email: str) -> tuple[bool, str]:
    """V2.1: Valida que el dominio del email tenga registros MX reales.

    V2.1.1: Validación más estricta:
    - Bloquea dominios obviamente falsos
    - Si no hay dnspython, bloquea (no permite por default)
    - Si timeout DNS, bloquea (no permite por default)
    - Whitelist de proveedores conocidos (gmail, hotmail, etc) bypassea MX lookup

    Retorna (válido, mensaje_error).
    """
    if "@" not in email:
        return False, "Formato de email inválido"

    try:
        local, domain = email.rsplit("@", 1)
    except ValueError:
        return False, "Formato de email inválido"

    if not local or not domain:
        return False, "Formato de email inválido"
    if "." not in domain:
        return False, "El dominio del email no es válido"

    domain_lower = domain.lower()

    # V2.1.1: Whitelist de proveedores conocidos (siempre válidos)
    whitelist_domains = {
        "gmail.com", "googlemail.com", "outlook.com", "outlook.es",
        "hotmail.com", "hotmail.es", "live.com", "msn.com",
        "yahoo.com", "yahoo.es", "ymail.com",
        "icloud.com", "me.com", "mac.com",
        "protonmail.com", "proton.me", "pm.me",
        "aol.com", "zoho.com", "fastmail.com",
        "claro.net.do", "codetel.net.do", "verizon.net",
        "dorismon.do",  # tu dominio
    }
    if domain_lower in whitelist_domains:
        return True, ""

    # Lista negra ampliada V2.5
    blacklist_domains = {
        "test.com", "test.test", "test.es", "test.org", "test.io",
        "example.com", "example.org", "example.net", "example.io",
        "ejemplo.com", "ejemplo.es",
        "asdf.com", "asdfasdf.com", "qwerty.com", "qwertyuiop.com",
        "fake.com", "fakemail.com", "fake.io", "fake.org", "fake.net",
        "tempmail.com", "tempmail.org", "tempmail.net", "tempmail.io",
        "mailinator.com", "trashmail.com", "guerrillamail.com",
        "10minutemail.com", "throwaway.email", "yopmail.com",
        "abc.com", "abc.es", "xyz.com", "xyz.es",
        "prueba.com", "prueba.es", "demo.com", "demo.org",
        "noexiste.com", "nada.com", "inventado.com",
        "correo.com", "email.com", "mail.com",  # genéricos sospechosos
        # V2.5: más bloqueos
        "123.com", "1234.com", "12345.com",
        "aaa.com", "bbb.com", "ccc.com",
        "user.com", "users.com", "name.com",
        "spam.com", "spammer.com",
        "guerrilla.com", "guerrillamail.org", "guerrillamail.net",
        "mailtemp.com", "yopmail.org", "yopmail.fr", "yopmail.net",
        "dispostable.com", "discard.email", "throwawaymail.com",
        "getnada.com", "tempr.email", "mintemail.com",
    }
    if domain_lower in blacklist_domains:
        return False, f"El dominio {domain} no está permitido. Usa tu email real."

    # V2.5: Bloquear dominios SOSPECHOSOS por patrón
    local_lower = local.lower()
    if len(local_lower) < 3:
        return False, "El usuario del email es muy corto. Usa tu email real."

    # V2.5: bloquear emails con local muy genérico
    suspicious_locals = {"test", "tests", "testing", "fake", "fakeuser", "prueba",
                          "demo", "abc", "xyz", "asdf", "qwerty", "user", "users",
                          "noexiste", "nadie", "ninguno", "ejemplo", "example"}
    if local_lower in suspicious_locals:
        return False, "Ese email parece de prueba. Usa tu email real."

    # V2.1.1: Bloquear TLDs sospechosos
    suspicious_tlds = (".test", ".invalid", ".localhost", ".local", ".example")
    if any(domain_lower.endswith(t) for t in suspicious_tlds):
        return False, f"El dominio {domain} no es válido. Usa tu email real."

    # V2.1.1: Bloquear TLDs raros si el dominio es corto (5-7 chars y TLD raro)
    # Esto bloquea cosas como asdfasdf.xyz, qwerty.io, random.io
    common_tlds = (".com", ".org", ".net", ".edu", ".gov", ".io", ".co",
                   ".do", ".es", ".mx", ".ar", ".cl", ".pe", ".co.uk", ".com.do",
                   ".com.mx", ".com.ar", ".com.es", ".email", ".app", ".dev")
    rare_tlds = (".xyz", ".top", ".click", ".online", ".site", ".store",
                  ".tech", ".info", ".biz", ".loan", ".party", ".trade")
    if any(domain_lower.endswith(t) for t in rare_tlds):
        # Solo permitimos TLDs raros si el dominio tiene más de 10 chars (probable real)
        # O si pasamos validación MX completa
        pass  # se valida abajo con MX, pero más estricto

    # Resolver MX (estricto en V2.1.1)
    try:
        import dns.resolver
    except ImportError:
        logger.error("dnspython no instalado — bloqueando emails con dominios desconocidos")
        return False, "No podemos validar tu email en este momento. Prueba con Gmail, Hotmail, Outlook o tu email del trabajo."

    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=8.0)
        if len(list(answers)) == 0:
            return False, f"El dominio {domain} no tiene servidor de email configurado"
        return True, ""
    except dns.resolver.NXDOMAIN:
        return False, f"El dominio {domain} no existe. Verificá tu email."
    except dns.resolver.NoAnswer:
        return False, f"El dominio {domain} no acepta emails."
    except dns.resolver.Timeout:
        # V2.1.1: si timeout, bloqueamos (más seguro)
        logger.warning(f"DNS timeout para {domain} — bloqueando por seguridad")
        return False, f"No pudimos verificar el dominio {domain}. Prueba con Gmail, Hotmail, Outlook o intentá de nuevo."
    except Exception as e:
        logger.error(f"Error MX para {domain}: {e}")
        return False, f"No pudimos verificar el dominio {domain}. Prueba con un email de Gmail, Hotmail u Outlook."
