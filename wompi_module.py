"""
wompi_module.py — Motor de Transacciones (Wompi — Colombia)
===========================================================
Reemplaza stripe_module.py para operar desde Colombia.

Wompi docs: https://docs.wompi.co
Sandbox: https://sandbox.wompi.co

Uso:
  python wompi_module.py list-links   → lista Payment Links activos
  python wompi_module.py test-link    → abre el link de prueba en consola
"""

import os
import hashlib
import hmac
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Configuración ─────────────────────────────────────────────────────────────
WOMPI_PUBLIC_KEY   = os.environ.get("WOMPI_PUBLIC_KEY", "")
WOMPI_PRIVATE_KEY  = os.environ.get("WOMPI_PRIVATE_KEY", "")
WOMPI_EVENTS_KEY   = os.environ.get("WOMPI_EVENTS_KEY", "")   # Para validar webhooks
WOMPI_INTEGRITY_KEY = os.environ.get("WOMPI_INTEGRITY_KEY", "") # Para firmar transacciones

# En producción: https://production.wompi.co/v1
# En sandbox:    https://sandbox.wompi.co/v1
IS_SANDBOX = WOMPI_PUBLIC_KEY.startswith("pub_test") or WOMPI_PUBLIC_KEY.startswith("pub_stagtest")
API_BASE = "https://sandbox.wompi.co/v1" if IS_SANDBOX else "https://production.wompi.co/v1"

# Payment Links existentes (de tu dashboard de Wompi)
# Formato: https://checkout.wompi.co/l/REFERENCIA
PAYMENT_LINKS = {
    "basic": os.environ.get("WOMPI_LINK_BASIC", "https://checkout.wompi.co/l/test_VPOS_iE3pq2"),
    "pro":   os.environ.get("WOMPI_LINK_PRO",   "https://checkout.wompi.co/l/test_VPOS_iE3pq2"),
    "pack":  os.environ.get("WOMPI_LINK_PACK",  "https://checkout.wompi.co/l/test_VPOS_iE3pq2"),
}

PLAN_AMOUNTS = {
    "basic": 19_900_00,   # $19,900 COP ≈ $4.99 USD
    "pro":   39_900_00,   # $39,900 COP ≈ $9.99 USD
    "pack":  79_900_00,   # $79,900 COP ≈ $19.99 USD
}


# ── API helpers ───────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {WOMPI_PRIVATE_KEY}",
        "Content-Type": "application/json",
    }


def get_payment_link(plan_key: str) -> str:
    """Retorna la URL del Payment Link de Wompi para un plan."""
    url = PAYMENT_LINKS.get(plan_key, PAYMENT_LINKS["basic"])
    print(f"🔗 Payment Link [{plan_key}]: {url}")
    return url


def get_transaction(transaction_id: str) -> dict:
    """
    Recupera los detalles completos de una transacción de Wompi.
    Usado para enriquecer los datos del webhook.
    """
    resp = requests.get(
        f"{API_BASE}/transactions/{transaction_id}",
        headers=_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("data", {})


# ── Verificación de firma de webhook ─────────────────────────────────────────

def verify_webhook_signature(event_data: dict, received_checksum: str) -> bool:
    """
    Verifica la firma del webhook de Wompi.

    Wompi calcula el checksum así:
      SHA256( transaction_id + status + amount_in_cents + currency + created_at + events_secret )

    Args:
        event_data:         El objeto completo del evento recibido (JSON parseado)
        received_checksum:  El valor de event_data['signature']['checksum']

    Returns:
        bool — True si la firma es válida
    """
    if not WOMPI_EVENTS_KEY:
        # Si no hay key configurada, en desarrollo dejamos pasar
        return True

    try:
        tx = event_data.get("data", {}).get("transaction", {})
        props = event_data.get("signature", {}).get("properties", [])

        # Construir la cadena a hashear siguiendo el orden de `properties`
        values = []
        for prop in props:
            # Navegar el objeto anidado: "transaction.id" → tx["id"]
            parts = prop.split(".")
            obj = event_data.get("data", {})
            for part in parts:
                obj = obj.get(part, "") if isinstance(obj, dict) else ""
            values.append(str(obj))

        values.append(WOMPI_EVENTS_KEY)
        payload = "".join(values)

        expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        valid = hmac.compare_digest(expected, received_checksum)

        if not valid:
            import logging
            logging.getLogger("wompi").warning(
                f"❌ Firma inválida. Expected={expected[:20]}... Got={received_checksum[:20]}..."
            )
        return valid

    except Exception as e:
        import logging
        logging.getLogger("wompi").error(f"Error verificando firma: {e}")
        return False


# ── Extractor de datos del evento ─────────────────────────────────────────────

def extract_order_data(event: dict) -> dict | None:
    """
    Extrae los datos relevantes de un evento webhook de Wompi.
    Retorna dict listo para pasar a delivery.process_paid_order(), o None si no aplica.

    Wompi envía el evento 'transaction.updated' con status 'APPROVED' cuando el pago es exitoso.
    """
    event_name = event.get("event", "")
    tx = event.get("data", {}).get("transaction", {})
    status = tx.get("status", "")

    if event_name != "transaction.updated" or status != "APPROVED":
        return None  # Ignorar eventos que no sean pagos exitosos

    # Extraer email del cliente
    customer_data = tx.get("customer_data", {})
    email = customer_data.get("legal_id_type", "")  # Wompi usa customer_email
    email = tx.get("customer_email", "") or customer_data.get("email", "")

    # Extraer metadata del pago (PDF URL y plan se pasan como 'reference')
    reference = tx.get("reference", "")
    # Formato de reference que usamos: "plan:basic|pdf:https://..."
    plan_key = "basic"
    pdf_url = ""

    if "|" in reference:
        parts = dict(p.split(":", 1) for p in reference.split("|") if ":" in p)
        plan_key = parts.get("plan", "basic")
        pdf_url = parts.get("pdf", "")

    amount_cents = tx.get("amount_in_cents", 0)

    return {
        "session_id":      tx.get("id", ""),
        "customer_email":  email,
        "plan_key":        plan_key,
        "pdf_url":         pdf_url,
        "amount_total":    amount_cents / 100,
        "currency":        tx.get("currency", "COP"),
        "payment_status":  "paid",
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "list-links":
        print("\n📋 Payment Links configurados:\n")
        for plan, url in PAYMENT_LINKS.items():
            print(f"  [{plan}] {url}")

    elif cmd == "test-link":
        url = get_payment_link("basic")
        print(f"\n🧪 Abre este URL para probar el pago:\n{url}\n")

    elif cmd == "check-tx":
        tx_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not tx_id:
            print("Uso: python wompi_module.py check-tx TU_TRANSACTION_ID")
        else:
            import json
            tx = get_transaction(tx_id)
            print(json.dumps(tx, indent=2))

    else:
        print("""
Uso: python wompi_module.py <comando>

Comandos:
  list-links        Muestra los Payment Links configurados
  test-link         Muestra el link de basic para prueba
  check-tx <id>     Consulta una transacción por ID
        """)
