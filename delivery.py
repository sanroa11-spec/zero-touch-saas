"""
delivery.py — Módulo 3: Motor de Entrega
=========================================
Responsabilidades:
  - Orquestar el flujo completo post-pago:
      1. Recibir datos del webhook de Stripe
      2. Llamar a product_engine para resumir el PDF
      3. Enviar el resumen por email (SendGrid o SMTP Gmail)
      4. Registrar la orden en Supabase
  - Manejar errores con reintentos automáticos
  - Enviar email de error al cliente si el procesamiento falla

Uso directo (testing):
  python delivery.py --test
"""

import os
import json
import logging
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("delivery")

# ── Email backend: SendGrid (preferido) o SMTP Gmail (fallback) ───────────────
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
FROM_EMAIL = os.environ.get("SENDGRID_FROM_EMAIL", "noreply@yourpdfai.com")
FROM_NAME = os.environ.get("SENDGRID_FROM_NAME", "PDF AI Summarizer")

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Número máximo de reintentos si Gemini falla
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5


# ── Email Templates ───────────────────────────────────────────────────────────

EMAIL_SUCCESS_SUBJECT = "✅ Tu resumen de PDF está listo"

EMAIL_SUCCESS_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0f0f13; color: #e0e0e0; margin: 0; padding: 20px; }}
    .container {{ max-width: 680px; margin: 0 auto; background: #1a1a2e;
                  border-radius: 16px; border: 1px solid #2d2d4e; overflow: hidden; }}
    .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
               padding: 32px; text-align: center; }}
    .header h1 {{ margin: 0; color: white; font-size: 24px; font-weight: 700; }}
    .header p {{ margin: 8px 0 0; color: rgba(255,255,255,0.8); font-size: 14px; }}
    .stats {{ display: flex; gap: 16px; padding: 24px; background: #12122a; }}
    .stat {{ flex: 1; text-align: center; padding: 16px; background: #1a1a2e;
             border-radius: 10px; border: 1px solid #2d2d4e; }}
    .stat .value {{ font-size: 24px; font-weight: 700; color: #818cf8; }}
    .stat .label {{ font-size: 12px; color: #6b7280; margin-top: 4px; }}
    .content {{ padding: 32px; }}
    .summary-box {{ background: #12122a; border-radius: 12px; padding: 24px;
                    border-left: 4px solid #818cf8; white-space: pre-wrap;
                    font-size: 14px; line-height: 1.7; color: #c0c0d0; }}
    .footer {{ padding: 24px; text-align: center; border-top: 1px solid #2d2d4e; }}
    .footer p {{ color: #4b5563; font-size: 12px; margin: 0; }}
    h2 {{ color: #818cf8; font-size: 16px; margin: 0 0 16px; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>📄 Tu resumen está listo</h1>
      <p>Plan {plan_name} · Procesado por Gemini AI</p>
    </div>

    <div class="stats">
      <div class="stat">
        <div class="value">{pages_processed}</div>
        <div class="label">Páginas procesadas</div>
      </div>
      <div class="stat">
        <div class="value">{char_count_k}K</div>
        <div class="label">Caracteres analizados</div>
      </div>
      <div class="stat">
        <div class="value">&lt;2min</div>
        <div class="label">Tiempo de entrega</div>
      </div>
    </div>

    <div class="content">
      <h2>Tu resumen</h2>
      <div class="summary-box">{summary_text}</div>
    </div>

    <div class="footer">
      <p>PDF AI Summarizer · Este resumen fue generado automáticamente con IA</p>
      <p style="margin-top:8px">¿Necesitas más resúmenes? <a href="{payment_link}" style="color:#818cf8">Compra un Pack de 5</a></p>
    </div>
  </div>
</body>
</html>
"""

EMAIL_ERROR_SUBJECT = "⚠️ Problema procesando tu PDF — Te reembolsamos"

EMAIL_ERROR_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <style>
    body {{ font-family: -apple-system, sans-serif; background:#0f0f13;
            color:#e0e0e0; margin:0; padding:20px; }}
    .container {{ max-width:600px; margin:0 auto; background:#1a1a2e;
                  border-radius:16px; border:1px solid #2d2d4e; padding:32px; }}
    h1 {{ color:#f87171; }}
    .detail {{ background:#12122a; border-radius:8px; padding:16px;
               font-family:monospace; font-size:13px; color:#9ca3af; }}
    a {{ color:#818cf8; }}
  </style>
</head>
<body>
  <div class="container">
    <h1>⚠️ Hubo un problema con tu PDF</h1>
    <p>Lamentamos los inconvenientes. No pudimos procesar tu archivo por el siguiente motivo:</p>
    <div class="detail">{error_message}</div>
    <p style="margin-top:24px"><strong>¿Qué hacer ahora?</strong></p>
    <ul>
      <li>Verifica que el enlace de tu PDF sea <strong>público</strong> (cualquiera con el link puede verlo).</li>
      <li>Si usas Google Drive, haz clic en "Compartir" → "Cualquiera con el enlace".</li>
      <li>Responde a este email con el PDF adjunto y lo procesamos manualmente.</li>
    </ul>
    <p>Si el problema persiste, procesaremos tu orden manualmente en las próximas 24h,
       o te reembolsaremos completamente. No necesitas hacer nada.</p>
    <p>— El equipo de PDF AI Summarizer</p>
  </div>
</body>
</html>
"""


# ── Envío de email ────────────────────────────────────────────────────────────

def _send_email_sendgrid(to_email: str, subject: str, html_body: str) -> bool:
    """Envía email usando la API de SendGrid."""
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail, From, To, Subject, HtmlContent

        sg = SendGridAPIClient(api_key=SENDGRID_API_KEY)
        message = Mail(
            from_email=(FROM_EMAIL, FROM_NAME),
            to_emails=to_email,
            subject=subject,
            html_content=html_body,
        )
        response = sg.send(message)
        logger.info(f"📧 Email enviado (SendGrid) → {to_email} | status={response.status_code}")
        return response.status_code in (200, 202)
    except Exception as e:
        logger.error(f"❌ SendGrid error: {e}")
        return False


def _send_email_smtp(to_email: str, subject: str, html_body: str) -> bool:
    """Fallback: envía email via SMTP (Gmail con App Password)."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASSWORD")  # App Password de Gmail

    if not smtp_user or not smtp_pass:
        logger.error("❌ SMTP no configurado. Define SMTP_USER y SMTP_PASSWORD en .env")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{FROM_NAME} <{smtp_user}>"
        msg["To"] = to_email
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_email, msg.as_string())

        logger.info(f"📧 Email enviado (SMTP) → {to_email}")
        return True
    except Exception as e:
        logger.error(f"❌ SMTP error: {e}")
        return False


def send_email(to_email: str, subject: str, html_body: str) -> bool:
    """
    Envía un email usando SendGrid (preferido) o SMTP Gmail (fallback).
    Retorna True si el envío fue exitoso.
    """
    if SENDGRID_API_KEY:
        return _send_email_sendgrid(to_email, subject, html_body)
    else:
        logger.warning("⚠️  SendGrid no configurado, usando SMTP fallback")
        return _send_email_smtp(to_email, subject, html_body)


# ── Supabase — Registro de órdenes ────────────────────────────────────────────

def _log_order_to_supabase(session_data: dict, result: dict, status: str) -> bool:
    """
    Registra la orden en la tabla 'orders' de Supabase.
    Crea la fila con todos los metadatos de la transacción.

    Esquema SQL recomendado (ejecutar en Supabase SQL Editor):
    ----------------------------------------------------------
    CREATE TABLE orders (
        id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
        session_id      TEXT UNIQUE NOT NULL,
        customer_email  TEXT NOT NULL,
        plan_key        TEXT NOT NULL,
        pdf_url         TEXT,
        amount_usd      NUMERIC(10,2),
        status          TEXT DEFAULT 'pending',  -- 'completed' | 'error' | 'pending'
        pages_processed INT,
        char_count      INT,
        error_message   TEXT,
        created_at      TIMESTAMPTZ DEFAULT NOW()
    );
    ----------------------------------------------------------
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.warning("⚠️  Supabase no configurado. Saltando registro de orden.")
        return False

    try:
        from supabase import create_client

        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

        row = {
            "session_id": session_data.get("session_id"),
            "customer_email": session_data.get("customer_email"),
            "plan_key": session_data.get("plan_key"),
            "pdf_url": session_data.get("pdf_url"),
            "amount_usd": session_data.get("amount_total", 0),
            "status": status,
            "pages_processed": result.get("pages_processed", 0),
            "char_count": result.get("char_count", 0),
            "error_message": result.get("error"),
        }

        supabase.table("orders").upsert(row, on_conflict="session_id").execute()
        logger.info(f"🗄️  Orden registrada en Supabase: {session_data.get('session_id')}")
        return True

    except Exception as e:
        logger.error(f"❌ Supabase error: {e}", exc_info=True)
        return False


# ── Función principal — Orquestador ──────────────────────────────────────────

def process_paid_order(session_data: dict) -> bool:
    """
    Orquestador principal. Llamado por webhook_server.py cuando un pago es exitoso.

    Flujo:
        1. Validar datos del session
        2. Llamar a product_engine.summarize_pdf() con reintentos
        3a. Si éxito → enviar email con resumen + registrar en Supabase
        3b. Si error → enviar email de error + registrar en Supabase

    Args:
        session_data: dict con keys:
            - session_id (str)
            - customer_email (str)
            - plan_key (str)
            - pdf_url (str)
            - amount_total (float)

    Returns:
        bool: True si la entrega fue exitosa, False si hubo error
    """
    from product_engine import summarize_pdf  # Import aquí para evitar circular

    customer_email = session_data.get("customer_email")
    plan_key = session_data.get("plan_key", "basic")
    pdf_url = session_data.get("pdf_url", "")
    session_id = session_data.get("session_id")

    logger.info(f"🚀 Procesando orden: session={session_id}, email={customer_email}, plan={plan_key}")

    # ─ Validaciones básicas ──────────────────────────────────────────────────
    if not customer_email:
        logger.error("❌ No hay email del cliente. Abortando.")
        return False

    if not pdf_url:
        error_msg = "No se recibió la URL del PDF. Responde a este email con tu PDF adjunto."
        _send_error_email(customer_email, error_msg)
        _log_order_to_supabase(session_data, {"error": error_msg}, "error")
        return False

    # ─ Procesar PDF con reintentos ───────────────────────────────────────────
    result = None
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info(f"🔄 Intento {attempt}/{MAX_RETRIES}: summarize_pdf()")
        result = summarize_pdf(pdf_url=pdf_url, plan=plan_key)

        if not result.get("error"):
            break  # Éxito

        last_error = result["error"]
        logger.warning(f"⚠️  Intento {attempt} falló: {last_error}")

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SECONDS * attempt)  # Backoff exponencial suave

    # ─ Evaluar resultado ─────────────────────────────────────────────────────
    if result and not result.get("error"):
        # ✅ ÉXITO: Enviar resumen por email
        success = _deliver_summary(customer_email, plan_key, result)
        status = "completed" if success else "email_failed"
        _log_order_to_supabase(session_data, result, status)
        return success
    else:
        # ❌ ERROR: Notificar al cliente y registrar el fallo
        error_msg = last_error or "Error desconocido al procesar el PDF."
        logger.error(f"❌ Todos los intentos fallaron: {error_msg}")
        _send_error_email(customer_email, error_msg)
        _log_order_to_supabase(session_data, result or {"error": error_msg}, "error")
        return False


def _deliver_summary(customer_email: str, plan_key: str, result: dict) -> bool:
    """Formatea y envía el email de éxito con el resumen."""
    plan_names = {"basic": "Basic ($4.99)", "pro": "Pro ($9.99)", "pack": "Pack 5 ($19.99)"}
    pack_link = os.environ.get("STRIPE_PACK_PAYMENT_LINK", "https://your-payment-link.com")

    html = EMAIL_SUCCESS_HTML.format(
        plan_name=plan_names.get(plan_key, plan_key),
        pages_processed=result.get("pages_processed", "?"),
        char_count_k=round(result.get("char_count", 0) / 1000, 1),
        summary_text=result.get("summary_markdown", "").replace("\n", "<br>"),
        payment_link=pack_link,
    )

    return send_email(customer_email, EMAIL_SUCCESS_SUBJECT, html)


def _send_error_email(customer_email: str, error_message: str) -> bool:
    """Envía email de error al cliente explicando el problema."""
    html = EMAIL_ERROR_HTML.format(error_message=error_message)
    return send_email(customer_email, EMAIL_ERROR_SUBJECT, html)


# ── CLI de testing ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    print("\n🧪 Test de entrega end-to-end\n")

    test_session = {
        "session_id": f"test_{int(time.time())}",
        "customer_email": os.environ.get("TEST_EMAIL", "test@example.com"),
        "plan_key": "basic",
        "pdf_url": "https://arxiv.org/pdf/1706.03762",  # Attention is All You Need
        "amount_total": 4.99,
        "currency": "usd",
        "payment_status": "paid",
    }

    print(f"📦 Sesión de prueba:\n{json.dumps(test_session, indent=2)}\n")
    success = process_paid_order(test_session)

    if success:
        print(f"\n✅ Entrega exitosa → {test_session['customer_email']}")
    else:
        print(f"\n❌ Entrega fallida — revisa los logs")
        sys.exit(1)
