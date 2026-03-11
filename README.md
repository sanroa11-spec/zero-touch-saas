# ⚡ PDF AI Summarizer — Zero-Touch SaaS

Un negocio digital 100% autónomo: el sistema cobra, genera el producto con IA y lo entrega solo, sin intervención humana.

---

## Arquitectura

```
Reddit Bot → Landing Page → Stripe Checkout → Webhook Server → Gemini AI → Email
```

## Estructura del proyecto

```
zero_touch_saas/
├── .env.example          # Variables de entorno (copiar a .env)
├── requirements.txt      # Dependencias Python
├── Procfile              # Railway/Render deployment
│
├── stripe_module.py      # Módulo 1A: Crear productos y Payment Links
├── webhook_server.py     # Módulo 1B: Flask webhook + validación Stripe
├── product_engine.py     # Módulo 2: Gemini AI PDF Summarizer
├── delivery.py           # Módulo 3: Orquestador Stripe→AI→Email→Supabase
├── growth_bot.py         # Módulo 4: Reddit bot de adquisición
│
├── landing/
│   ├── index.html        # Landing page (deploy en GitHub Pages)
│   └── style.css         # Design system dark mode
│
└── .github/workflows/
    └── deploy.yml        # Auto-deploy + cron del growth bot
```

---

## Setup en 10 pasos

### 1. Clonar y configurar entorno

```bash
git clone https://github.com/TU_USUARIO/zero-touch-saas.git
cd zero_touch_saas
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### 2. Configurar API Keys en `.env`

| Variable | Dónde obtenerla |
|----------|----------------|
| `STRIPE_SECRET_KEY` | [dashboard.stripe.com](https://dashboard.stripe.com) → Developers → API Keys |
| `STRIPE_WEBHOOK_SECRET` | Stripe CLI: `stripe listen` o Stripe Dashboard → Webhooks |
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com) → Get API Key |
| `SENDGRID_API_KEY` | [app.sendgrid.com](https://app.sendgrid.com) → Settings → API Keys |
| `SUPABASE_URL/KEY` | [supabase.com](https://supabase.com) → tu proyecto → Settings → API |
| `REDDIT_*` | [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) → Create App |

### 3. Crear tabla en Supabase

En el SQL Editor de tu proyecto Supabase, ejecuta:

```sql
CREATE TABLE orders (
    id              UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    session_id      TEXT UNIQUE NOT NULL,
    customer_email  TEXT NOT NULL,
    plan_key        TEXT NOT NULL,
    pdf_url         TEXT,
    amount_usd      NUMERIC(10,2),
    status          TEXT DEFAULT 'pending',
    pages_processed INT,
    char_count      INT,
    error_message   TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### 4. Crear productos y Payment Links en Stripe

```bash
python stripe_module.py create-links
```

Copia los Price IDs y URLs que imprime en consola a tu `.env`:
```
STRIPE_PRICE_BASIC=price_xxx
STRIPE_BASIC_PAYMENT_LINK=https://buy.stripe.com/xxx
```

Luego actualiza `landing/index.html` reemplazando los 3 placeholders:
- `STRIPE_BASIC_LINK` → tu Payment Link de Basic
- `STRIPE_PRO_LINK` → tu Payment Link de Pro  
- `STRIPE_PACK_LINK` → tu Payment Link de Pack 5

### 5. Testing local — Webhook

```bash
# Terminal 1: Servidor Flask
python webhook_server.py

# Terminal 2: Stripe CLI (reenvía eventos reales a localhost)
stripe listen --forward-to localhost:5000/webhook

# Terminal 3: Simular pago exitoso
stripe trigger checkout.session.completed
```

### 6. Test de entrega end-to-end

```bash
TEST_EMAIL=tu@email.com python delivery.py
```

Deberías recibir el resumen del paper "Attention is All You Need" en tu email.

### 7. Test del Growth Bot (sin postear)

```bash
python growth_bot.py --dry-run
python growth_bot.py --stats
```

### 8. Deploy del Webhook Server (Railway)

1. Crea cuenta en [railway.app](https://railway.app)
2. Nuevo proyecto → Deploy from GitHub
3. Selecciona el repo → directorio `zero_touch_saas/`
4. Agrega todas las variables del `.env` en Railway → Variables
5. Copia la URL pública que Railway asigna (ej: `https://pdf-ai.up.railway.app`)
6. En Stripe Dashboard → Webhooks → Agregar endpoint:
   - URL: `https://pdf-ai.up.railway.app/webhook`
   - Events: `checkout.session.completed`
7. Copia el Webhook Secret a Railway como `STRIPE_WEBHOOK_SECRET`

### 9. Deploy de la Landing Page (GitHub Pages)

```bash
# En Settings de tu repo GitHub:
# Pages → Source → Deploy from branch → main → /landing
```

Tu landing estará disponible en `https://TU_USUARIO.github.io/zero-touch-saas/`

### 10. Activar el Growth Bot en GitHub Actions

1. En tu repo → Settings → Secrets → Actions secrets, agrega:
   - `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`, `REDDIT_USER_AGENT`
   - `STRIPE_BASIC_PAYMENT_LINK`, `STRIPE_PRO_PAYMENT_LINK`, `STRIPE_PACK_PAYMENT_LINK`
2. El workflow `.github/workflows/deploy.yml` lo ejecutará automáticamente cada hora.

---

## Flujo autónomo completo

```
1. Bot de Reddit detecta: "need to summarize this PDF"
2. Comenta con enlace a la landing page / Payment Link de Stripe
3. Cliente paga → Stripe envía webhook a Railway
4. webhook_server.py valida la firma y extrae: email + PDF URL + plan
5. delivery.py llama a product_engine.summarize_pdf()
6. Gemini 1.5 Flash procesa el PDF → resumen estructurado
7. SendGrid envía el email al cliente en <2 minutos
8. Supabase registra la transacción
```

**Intervención humana requerida: 0%**

---

## Comandos útiles

```bash
# Ver estadísticas del bot
python growth_bot.py --stats

# Listar Payment Links activos
python stripe_module.py list-links

# Test de una Checkout Session
python stripe_module.py test-session

# Probar resumen de un PDF específico
python product_engine.py https://arxiv.org/pdf/1706.03762 pro
```

---

## Costos estimados (tier gratuita)

| Servicio | Límite free | Costo al escalar |
|----------|-------------|-----------------|
| Gemini API | 1,500 req/día | $0.075/1M tokens |
| SendGrid | 100 emails/día | $19.95/mes (50k) |
| Supabase | 500MB / 50k req | $25/mes |
| Railway | $5 crédito inicial | ~$5/mes |
| Stripe | Sin mensualidad | 2.9% + $0.30/tx |

**Break-even:** ~20 ventas del plan Basic = $100 MRR

---

## Licencia

MIT — úsalo, modifícalo, escálalo.
