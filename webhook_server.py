"""
webhook_server.py — Módulo 1B: Servidor de Webhooks de Stripe
=============================================================
Responsabilidades:
  - Recibir y validar webhooks de Stripe (firma HMAC)
  - Procesar evento 'checkout.session.completed'
  - Delegar al módulo de entrega (delivery.py) de forma asíncrona
  - Endpoint de health-check para Railway/Render

Despliegue:
  Local:      python webhook_server.py
  Producción: gunicorn webhook_server:app -b 0.0.0.0:$PORT
  Stripe CLI: stripe listen --forward-to localhost:5000/webhook
"""

import os
import json
import logging
import threading
import stripe
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# ── Configuración ─────────────────────────────────────────────────────────────
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
PORT = int(os.environ.get("PORT", 5000))

# Logger estructurado
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("webhook_server")

app = Flask(__name__)


# ── Importación lazy de delivery para evitar imports circulares ────────────────
def _process_order_async(session_data: dict):
    """Ejecuta la entrega en un hilo separado para no bloquear el webhook."""
    try:
        from delivery import process_paid_order
        process_paid_order(session_data)
    except Exception as e:
        logger.error(f"❌ Error en process_paid_order: {e}", exc_info=True)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health_check():
    """Health check — Railway/Render lo usa para verificar que el server está vivo."""
    return jsonify({
        "status": "ok",
        "service": "PDF Summarizer — Webhook Server",
        "version": "1.0.0",
    }), 200


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    """
    Endpoint principal de webhook de Stripe.
    
    Flujo:
    1. Recibe la request raw (payload + headers)
    2. Verifica la firma HMAC con el Webhook Secret
    3. Parsea el evento de Stripe
    4. Despacha según event.type
    5. Responde 200 inmediatamente (Stripe espera < 30s)
    """
    payload = request.get_data()  # raw bytes — necesario para verificar firma
    sig_header = request.headers.get("Stripe-Signature")

    # ── 1. Verificar firma ───────────────────────────────────────────────────
    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=WEBHOOK_SECRET,
        )
    except ValueError as e:
        # Payload inválido (no es JSON válido de Stripe)
        logger.warning(f"⚠️  Payload inválido recibido: {e}")
        return jsonify({"error": "Invalid payload"}), 400
    except stripe.error.SignatureVerificationError as e:
        # Firma no coincide — posible ataque o secret equivocado
        logger.warning(f"🚨 Firma de webhook inválida: {e}")
        return jsonify({"error": "Invalid signature"}), 400

    # ── 2. Despachar eventos ────────────────────────────────────────────────
    event_type = event["type"]
    logger.info(f"📩 Evento recibido: {event_type} (id={event['id']})")

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(event["data"]["object"])

    elif event_type == "payment_intent.payment_failed":
        _handle_payment_failed(event["data"]["object"])

    elif event_type == "charge.dispute.created":
        _handle_dispute(event["data"]["object"])

    else:
        logger.debug(f"ℹ️  Evento ignorado: {event_type}")

    # Stripe requiere 2xx para considerar el webhook como entregado
    return jsonify({"status": "received"}), 200


# ── Handlers de eventos ───────────────────────────────────────────────────────

def _handle_checkout_completed(session: dict):
    """
    Se ejecuta cuando un cliente completa el pago exitosamente.
    Extrae los datos necesarios y lanza el proceso de entrega en background.
    """
    session_id = session.get("id")
    logger.info(f"✅ Pago completado — session_id={session_id}")

    # Extraer email del cliente
    customer_email = (
        session.get("customer_details", {}).get("email")
        or session.get("customer_email")
    )

    if not customer_email:
        logger.error(f"❌ No se encontró email para session {session_id}")
        return

    # Extraer metadata del pago (plan y URL del PDF)
    metadata = session.get("metadata", {})
    plan_key = metadata.get("plan_key", "basic")
    pdf_url = metadata.get("pdf_url", "")

    # Si el PDF vino como custom_field (desde Payment Link), extraerlo
    if not pdf_url:
        custom_fields = session.get("custom_fields", [])
        for field in custom_fields:
            if field.get("key") == "pdf_url":
                pdf_url = field.get("text", {}).get("value", "")
                break

    if not pdf_url:
        logger.warning(f"⚠️  No se encontró PDF URL para session {session_id}")

    # Construir el payload completo para el módulo de entrega
    session_data = {
        "session_id": session_id,
        "customer_email": customer_email,
        "plan_key": plan_key,
        "pdf_url": pdf_url,
        "amount_total": session.get("amount_total", 0) / 100,
        "currency": session.get("currency", "usd"),
        "payment_status": session.get("payment_status"),
    }

    logger.info(
        f"📦 Despachando entrega → email={customer_email}, "
        f"plan={plan_key}, pdf_url={pdf_url[:60]}..."
    )

    # Ejecutar en hilo separado para responder a Stripe de inmediato
    thread = threading.Thread(
        target=_process_order_async,
        args=(session_data,),
        daemon=True,
    )
    thread.start()


def _handle_payment_failed(payment_intent: dict):
    """Loguea los pagos fallidos para análisis."""
    logger.warning(
        f"💳 Pago fallido — payment_intent={payment_intent.get('id')}, "
        f"reason={payment_intent.get('last_payment_error', {}).get('message')}"
    )


def _handle_dispute(charge: dict):
    """Alerta de disputa/chargeback."""
    logger.error(
        f"🚨 DISPUTA RECIBIDA — charge={charge.get('id')}, "
        f"amount=${charge.get('amount', 0) / 100:.2f}"
    )
    # TODO: Notificar al operador por email/Slack si es necesario


# ── Endpoint de prueba (solo en desarrollo) ───────────────────────────────────

@app.route("/test-delivery", methods=["POST"])
def test_delivery():
    """
    Endpoint de prueba para simular un webhook sin pasar por Stripe.
    DESHABILITAR en producción cambiando FLASK_ENV=production.
    
    Body JSON:
    {
        "email": "test@example.com",
        "plan_key": "basic",
        "pdf_url": "https://arxiv.org/pdf/1706.03762"
    }
    """
    if os.environ.get("FLASK_ENV") == "production":
        return jsonify({"error": "Endpoint deshabilitado en producción"}), 403

    data = request.get_json()
    session_data = {
        "session_id": "test_session_000",
        "customer_email": data.get("email", "test@example.com"),
        "plan_key": data.get("plan_key", "basic"),
        "pdf_url": data.get("pdf_url", "https://arxiv.org/pdf/1706.03762"),
        "amount_total": 4.99,
        "currency": "usd",
        "payment_status": "paid",
    }

    logger.info(f"🧪 Test delivery triggered: {json.dumps(session_data, indent=2)}")
    _process_order_async(session_data)  # Ejecutar en background

    return jsonify({
        "status": "dispatched",
        "message": f"Procesando PDF para {session_data['customer_email']}",
        "session": session_data,
    }), 202


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(f"🚀 Webhook server iniciando en puerto {PORT}...")
    logger.info("💡 Para testing local, usa: stripe listen --forward-to localhost:5000/webhook")
    app.run(host="0.0.0.0", port=PORT, debug=False)
