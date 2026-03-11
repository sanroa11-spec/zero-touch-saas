"""
wompi_webhook_server.py — Servidor de Webhooks de Wompi (Colombia)
==================================================================
Reemplaza webhook_server.py para usar Wompi en lugar de Stripe.

Diferencias clave vs Stripe:
  - Wompi envía evento 'transaction.updated' (no 'checkout.session.completed')
  - La firma usa SHA256 sobre campos específicos + events_secret
  - El cliente pega el PDF URL en la referencia de la transacción

Despliegue:
  Local:      python wompi_webhook_server.py
  Producción: gunicorn wompi_webhook_server:app -b 0.0.0.0:$PORT
"""

import os
import json
import logging
import threading
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# ── Configuración ─────────────────────────────────────────────────────────────
PORT = int(os.environ.get("PORT", 5000))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("wompi_webhook")

app = Flask(__name__)


# ── Delivery async ────────────────────────────────────────────────────────────

def _process_order_async(order_data: dict):
    try:
        from delivery import process_paid_order
        process_paid_order(order_data)
    except Exception as e:
        logger.error(f"❌ Error en process_paid_order: {e}", exc_info=True)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "gateway": "Wompi", "version": "1.0.0"}), 200


@app.route("/webhook", methods=["POST"])
def wompi_webhook():
    """
    Endpoint principal de webhook de Wompi.

    Wompi POST body (JSON):
    {
      "event": "transaction.updated",
      "data": {
        "transaction": {
          "id": "...",
          "status": "APPROVED",
          "amount_in_cents": 1990000,
          "currency": "COP",
          "customer_email": "cliente@email.com",
          "reference": "plan:basic|pdf:https://...",
          "created_at": "2026-03-11T..."
        }
      },
      "signature": {
        "properties": ["transaction.id", "transaction.status", "transaction.amount_in_cents"],
        "checksum": "sha256hex..."
      },
      "sent_at": "2026-03-11T..."
    }
    """
    try:
        event = request.get_json(force=True)
    except Exception:
        logger.warning("⚠️  Payload inválido recibido")
        return jsonify({"error": "Invalid JSON"}), 400

    if not event:
        return jsonify({"error": "Empty payload"}), 400

    # ── Verificar firma ──────────────────────────────────────────────────────
    from wompi_module import verify_webhook_signature, extract_order_data

    checksum = event.get("signature", {}).get("checksum", "")
    if checksum and not verify_webhook_signature(event, checksum):
        logger.warning("🚨 Firma de webhook inválida — posible ataque")
        return jsonify({"error": "Invalid signature"}), 401

    # ── Despachar evento ─────────────────────────────────────────────────────
    event_name = event.get("event", "")
    tx_status  = event.get("data", {}).get("transaction", {}).get("status", "")
    tx_id      = event.get("data", {}).get("transaction", {}).get("id", "")

    logger.info(f"📩 Evento recibido: {event_name} | status={tx_status} | id={tx_id}")

    if event_name == "transaction.updated" and tx_status == "APPROVED":
        order_data = extract_order_data(event)

        if order_data and order_data.get("customer_email"):
            logger.info(
                f"✅ Pago aprobado → email={order_data['customer_email']}, "
                f"plan={order_data['plan_key']}, "
                f"amount={order_data['amount_total']:.0f} COP"
            )
            thread = threading.Thread(
                target=_process_order_async,
                args=(order_data,),
                daemon=True,
            )
            thread.start()
        else:
            logger.warning(f"⚠️  Transacción aprobada pero sin email válido: {tx_id}")

    elif event_name == "transaction.updated" and tx_status == "DECLINED":
        logger.warning(f"💳 Pago rechazado: tx_id={tx_id}")

    elif event_name == "transaction.updated" and tx_status == "VOIDED":
        logger.info(f"↩️  Transacción anulada: tx_id={tx_id}")

    else:
        logger.debug(f"ℹ️  Evento ignorado: {event_name} / {tx_status}")

    # Wompi espera respuesta 200 en menos de 5 segundos
    return jsonify({"status": "received"}), 200


@app.route("/test-delivery", methods=["POST"])
def test_delivery():
    """Endpoint de prueba (solo en modo no-producción)."""
    if os.environ.get("FLASK_ENV") == "production":
        return jsonify({"error": "Disabled in production"}), 403

    data = request.get_json()
    order_data = {
        "session_id": "wompi_test_001",
        "customer_email": data.get("email", "test@example.com"),
        "plan_key": data.get("plan_key", "basic"),
        "pdf_url": data.get("pdf_url", "https://arxiv.org/pdf/1706.03762"),
        "amount_total": 19900,
        "currency": "COP",
        "payment_status": "paid",
    }

    logger.info(f"🧪 Test delivery: {json.dumps(order_data)}")
    threading.Thread(target=_process_order_async, args=(order_data,), daemon=True).start()

    return jsonify({"status": "dispatched", "order": order_data}), 202


@app.route("/wompi-redirect", methods=["GET"])
def wompi_redirect():
    """
    Wompi redirige aquí después del pago con ?id=TRANSACTION_ID.
    Muestra una página de agradecimiento mientras el webhook procesa en background.
    """
    tx_id = request.args.get("id", "")
    logger.info(f"🔄 Redirección post-pago: tx_id={tx_id}")

    html = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
      <meta charset="UTF-8">
      <title>Procesando tu resumen...</title>
      <style>
        body {{ font-family: -apple-system, sans-serif; background: #08080f; color: #e0e0e0;
                display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
        .card {{ text-align: center; background: #13132a; border: 1px solid rgba(99,102,241,0.3);
                 border-radius: 20px; padding: 48px; max-width: 500px; }}
        .icon {{ font-size: 64px; margin-bottom: 16px; }}
        h1 {{ color: #818cf8; margin: 0 0 12px; font-size: 24px; }}
        p  {{ color: #94a3b8; line-height: 1.6; }}
        .tx {{ background: #08080f; border-radius: 8px; padding: 8px 16px;
               font-family: monospace; font-size: 12px; color: #6b7280; margin-top: 24px; }}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="icon">⚡</div>
        <h1>¡Pago recibido!</h1>
        <p>Estamos procesando tu PDF con Gemini AI.<br>
           Recibirás el resumen en tu email en los próximos <strong>2 minutos</strong>.</p>
        <div class="tx">Transacción: {tx_id}</div>
      </div>
    </body>
    </html>
    """
    return html, 200


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger.info(f"🚀 Wompi Webhook Server iniciando en puerto {PORT}...")
    logger.info("📋 Endpoints:")
    logger.info("   POST /webhook          → Recibe eventos de Wompi")
    logger.info("   GET  /wompi-redirect   → Página post-pago")
    logger.info("   POST /test-delivery    → Test sin pasar por Wompi")
    app.run(host="0.0.0.0", port=PORT, debug=False)
