"""
stripe_module.py — Módulo 1A: Motor de Transacciones (Stripe)
=============================================================
Responsabilidades:
  - Crear Stripe Payment Links para cada plan del PDF Summarizer
  - Crear Checkout Sessions dinámicas con metadata del cliente
  - Listar y gestionar productos/precios en Stripe

Uso rápido:
  python stripe_module.py create-links   → crea los Payment Links una sola vez
  python stripe_module.py list-links     → lista los Payment Links activos
"""

import os
import stripe
from dotenv import load_dotenv

load_dotenv()

# ── Configuración ────────────────────────────────────────────────────────────
stripe.api_key = os.environ["STRIPE_SECRET_KEY"]

# Planes disponibles (nombre, precio en centavos USD, descripción)
PLANS = {
    "basic": {
        "name": "PDF Summarizer — Basic",
        "amount": 499,          # $4.99
        "currency": "usd",
        "description": "1 PDF hasta 50 páginas. Resumen ejecutivo + puntos clave.",
        "pages_limit": 50,
    },
    "pro": {
        "name": "PDF Summarizer — Pro",
        "amount": 999,          # $9.99
        "currency": "usd",
        "description": "1 PDF hasta 200 páginas. Resumen profundo + análisis de argumentos.",
        "pages_limit": 200,
    },
    "pack": {
        "name": "PDF Summarizer — Pack 5",
        "amount": 1999,         # $19.99
        "currency": "usd",
        "description": "5 créditos de resumen. Válidos 30 días. Cualquier tamaño.",
        "pages_limit": 200,
    },
}


# ── Funciones principales ────────────────────────────────────────────────────

def create_stripe_products_and_prices() -> dict[str, str]:
    """
    Crea productos y precios en Stripe para cada plan.
    Idempotente: si ya existen, los reutiliza (por metadata).
    
    Returns:
        dict: {plan_key: price_id}  — guarda estos IDs en tu .env
    """
    price_ids = {}

    for plan_key, plan in PLANS.items():
        # 1. Buscar si el producto ya existe por metadata
        existing = stripe.Product.search(
            query=f"metadata['plan_key']:'{plan_key}' AND active:'true'",
            limit=1,
        )

        if existing.data:
            product = existing.data[0]
            print(f"✅ Producto existente: {product.name} (id={product.id})")
        else:
            product = stripe.Product.create(
                name=plan["name"],
                description=plan["description"],
                metadata={"plan_key": plan_key, "pages_limit": str(plan["pages_limit"])},
            )
            print(f"🆕 Producto creado: {product.name} (id={product.id})")

        # 2. Crear precio (one-time)
        price = stripe.Price.create(
            product=product.id,
            unit_amount=plan["amount"],
            currency=plan["currency"],
            metadata={"plan_key": plan_key},
        )
        price_ids[plan_key] = price.id
        print(f"   💰 Precio: ${plan['amount']/100:.2f} USD → price_id={price.id}")

    return price_ids


def create_payment_link(plan_key: str, after_completion_url: str = None) -> str:
    """
    Crea un Stripe Payment Link reutilizable para un plan.
    
    Args:
        plan_key: "basic", "pro", o "pack"
        after_completion_url: URL de redirección post-pago
    
    Returns:
        str: URL del Payment Link (ej: https://buy.stripe.com/xxx)
    """
    # Usar price_id del .env si existe, sino buscar en Stripe
    env_key = f"STRIPE_PRICE_{plan_key.upper()}"
    price_id = os.environ.get(env_key)

    if not price_id:
        # Buscar el precio más reciente para este plan
        prices = stripe.Price.search(
            query=f"metadata['plan_key']:'{plan_key}' AND active:'true'",
            limit=1,
        )
        if not prices.data:
            raise ValueError(f"No hay precio configurado para el plan '{plan_key}'. "
                           f"Ejecuta primero: python stripe_module.py create-links")
        price_id = prices.data[0].id

    # Configuración del Payment Link
    link_params = {
        "line_items": [{"price": price_id, "quantity": 1}],
        "metadata": {"plan_key": plan_key},
        # El formulario de Stripe pedirá el email del cliente automáticamente
        "billing_address_collection": "auto",
        # Campos extra: recopilar URL del PDF del cliente
        "custom_fields": [
            {
                "key": "pdf_url",
                "label": {"type": "custom", "custom": "URL de tu PDF (Google Drive, Dropbox, etc.)"},
                "type": "text",
                "optional": False,
            }
        ],
        # Mensaje post-pago
        "after_completion": {
            "type": "hosted_confirmation",
            "hosted_confirmation": {
                "custom_message": (
                    "✅ ¡Pago recibido! Procesando tu PDF... "
                    "Recibirás tu resumen en el email en los próximos 2 minutos."
                )
            },
        },
    }

    if after_completion_url:
        link_params["after_completion"] = {
            "type": "redirect",
            "redirect": {"url": after_completion_url},
        }

    payment_link = stripe.PaymentLink.create(**link_params)
    print(f"🔗 Payment Link [{plan_key}]: {payment_link.url}")
    return payment_link.url


def create_checkout_session(
    plan_key: str,
    customer_email: str,
    pdf_url: str,
    success_url: str = "https://yoursite.com/success",
    cancel_url: str = "https://yoursite.com/",
) -> str:
    """
    Crea una Checkout Session (alternativa programática a Payment Links).
    Permite pre-rellenar el email y pasar metadata personalizada.
    
    Args:
        plan_key: "basic", "pro", o "pack"
        customer_email: email del comprador
        pdf_url: URL del PDF a procesar
        success_url: URL post-pago exitoso
        cancel_url: URL si el cliente cancela
    
    Returns:
        str: URL de la Checkout Session (expira en 24h)
    """
    env_key = f"STRIPE_PRICE_{plan_key.upper()}"
    price_id = os.environ.get(env_key)

    if not price_id:
        raise ValueError(f"Configura {env_key} en tu .env con el Price ID de Stripe")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{"price": price_id, "quantity": 1}],
        mode="payment",
        customer_email=customer_email,
        success_url=success_url + "?session_id={CHECKOUT_SESSION_ID}",
        cancel_url=cancel_url,
        metadata={
            "plan_key": plan_key,
            "pdf_url": pdf_url,
            "pages_limit": str(PLANS[plan_key]["pages_limit"]),
        },
    )

    print(f"🛒 Checkout Session creada: {session.url}")
    return session.url


def get_session_details(session_id: str) -> dict:
    """
    Recupera los detalles completos de una Checkout Session completada.
    Útil para el webhook para extraer email + metadata.
    
    Returns:
        dict con: customer_email, plan_key, pdf_url, amount_total
    """
    session = stripe.checkout.Session.retrieve(
        session_id,
        expand=["customer_details", "line_items"],
    )

    return {
        "session_id": session.id,
        "customer_email": session.customer_details.email if session.customer_details else None,
        "plan_key": session.metadata.get("plan_key"),
        "pdf_url": session.metadata.get("pdf_url"),
        "amount_total": session.amount_total / 100,  # en USD
        "currency": session.currency,
        "payment_status": session.payment_status,
    }


# ── CLI rápido ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"

    if cmd == "create-links":
        print("\n📦 Creando productos y precios en Stripe...\n")
        prices = create_stripe_products_and_prices()
        print("\n🔗 Creando Payment Links...\n")
        for plan_key in PLANS:
            url = create_payment_link(plan_key)
        print("\n✅ Listo. Copia los Price IDs a tu .env\n")
        for k, v in prices.items():
            print(f"  STRIPE_PRICE_{k.upper()}={v}")

    elif cmd == "list-links":
        print("\n📋 Payment Links activos:\n")
        links = stripe.PaymentLink.list(active=True, limit=10)
        for link in links.data:
            print(f"  {link.id}: {link.url}")

    elif cmd == "test-session":
        # Prueba rápida de Checkout Session (necesita price IDs en .env)
        url = create_checkout_session(
            plan_key="basic",
            customer_email="test@example.com",
            pdf_url="https://arxiv.org/pdf/1706.03762",  # Attention is All You Need
        )
        print(f"\n🧪 Abre esta URL para probar el checkout:\n{url}\n")

    else:
        print("""
Uso: python stripe_module.py <comando>

Comandos:
  create-links    Crea productos, precios y Payment Links en Stripe
  list-links      Lista los Payment Links activos
  test-session    Crea una Checkout Session de prueba
        """)
