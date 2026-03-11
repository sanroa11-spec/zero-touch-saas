#!/bin/bash
# setup.sh — Script de configuración automática del sistema Zero-Touch SaaS
# Ejecutar UNA SOLA VEZ después de rellenar el .env
# Uso: bash setup.sh

set -e
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✅ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
err()  { echo -e "${RED}❌ $1${NC}"; exit 1; }
info() { echo -e "${BLUE}ℹ️  $1${NC}"; }

echo ""
echo "========================================================"
echo "   ⚡ PDF AI Summarizer — Setup Automático"
echo "========================================================"
echo ""

# ── 1. Verificar .env ────────────────────────────────────────
if [ ! -f ".env" ]; then
    cp .env.example .env
    err ".env no encontrado. He creado uno desde .env.example. Rellena las API Keys y vuelve a ejecutar."
fi

source .env 2>/dev/null || true

check_var() {
    if [ -z "${!1}" ] || [[ "${!1}" == *"XXXX"* ]]; then
        err "La variable $1 no está configurada en .env"
    fi
    ok "$1 configurada"
}

echo "── Verificando API Keys ─────────────────────────────────"
check_var "STRIPE_SECRET_KEY"
check_var "GEMINI_API_KEY"
check_var "SENDGRID_API_KEY"
check_var "SUPABASE_URL"
check_var "SUPABASE_KEY"
echo ""

# ── 2. Instalar dependencias ─────────────────────────────────
echo "── Instalando dependencias Python ───────────────────────"
pip3 install -r requirements.txt --quiet && ok "Dependencias instaladas"
echo ""

# ── 3. Crear productos y Payment Links en Stripe ─────────────
echo "── Configurando Stripe ──────────────────────────────────"
info "Creando productos y Payment Links en Stripe..."
STRIPE_OUTPUT=$(python3 stripe_module.py create-links 2>&1)
echo "$STRIPE_OUTPUT"

# Extraer los Payment Links del output
BASIC_LINK=$(echo "$STRIPE_OUTPUT" | grep -o 'https://buy.stripe.com/[a-zA-Z0-9]*' | head -1)
PRO_LINK=$(echo "$STRIPE_OUTPUT"   | grep -o 'https://buy.stripe.com/[a-zA-Z0-9]*' | sed -n '2p')
PACK_LINK=$(echo "$STRIPE_OUTPUT"  | grep -o 'https://buy.stripe.com/[a-zA-Z0-9]*' | sed -n '3p')

if [ -n "$BASIC_LINK" ]; then
    ok "Payment Links creados"
    
    # Actualizar landing/index.html con los links reales
    sed -i.bak "s|STRIPE_BASIC_LINK|${BASIC_LINK}|g" landing/index.html
    sed -i.bak "s|STRIPE_PRO_LINK|${PRO_LINK}|g"     landing/index.html
    sed -i.bak "s|STRIPE_PACK_LINK|${PACK_LINK}|g"   landing/index.html
    rm -f landing/index.html.bak
    ok "Landing page actualizada con Payment Links reales"
    
    # Guardar links en .env para el growth bot
    echo "" >> .env
    echo "# Payment Links (generados automáticamente)" >> .env
    echo "STRIPE_BASIC_PAYMENT_LINK=${BASIC_LINK}" >> .env
    echo "STRIPE_PRO_PAYMENT_LINK=${PRO_LINK}" >> .env
    echo "STRIPE_PACK_PAYMENT_LINK=${PACK_LINK}" >> .env
    ok ".env actualizado con Payment Links"
else
    warn "No se pudieron extraer los Payment Links automáticamente. Revisa el output y actualiza landing/index.html manualmente."
fi
echo ""

# ── 4. Test del motor de IA ──────────────────────────────────
echo "── Verificando Gemini API ───────────────────────────────"
info "Probando conexión con Gemini (usando un PDF de prueba)..."
TEST_RESULT=$(python3 -c "
from product_engine import summarize_pdf
r = summarize_pdf('https://arxiv.org/pdf/1706.03762', 'basic')
if r['error']:
    print('ERROR:' + r['error'])
else:
    print('OK:' + str(r['pages_processed']) + 'pages')
" 2>&1)

if echo "$TEST_RESULT" | grep -q "^OK:"; then
    PAGES=$(echo "$TEST_RESULT" | grep -o '[0-9]*pages')
    ok "Gemini API funcionando — procesó ${PAGES}"
else
    warn "Error en Gemini: $TEST_RESULT"
fi
echo ""

# ── 5. Git push a GitHub ─────────────────────────────────────
echo "── Subiendo código a GitHub ─────────────────────────────"
if git remote get-url origin &>/dev/null; then
    git add -A
    git commit -m "chore: update landing with real Payment Links" --allow-empty
    git push origin main && ok "Código subido a GitHub"
else
    warn "No hay remote de GitHub configurado. Ejecuta:"
    warn "  git remote add origin https://github.com/TU_USUARIO/zero-touch-saas.git"
    warn "  git push -u origin main"
fi
echo ""

# ── 6. Instrucciones finales ─────────────────────────────────
echo "========================================================"
echo "   ✅ Setup completado"
echo "========================================================"
echo ""
echo "Próximos pasos manuales:"
echo ""
echo "  1. RAILWAY (webhook server):"
echo "     → Ve a https://railway.app → New Project → Deploy from GitHub"
echo "     → Selecciona el repo zero-touch-saas"
echo "     → En Variables, pega el contenido de tu .env"
echo "     → Copia la URL pública de Railway"
echo ""
echo "  2. STRIPE WEBHOOK:"
echo "     → Dashboard → Webhooks → Add endpoint"
echo "     → URL: https://TU-APP.up.railway.app/webhook"
echo "     → Event: checkout.session.completed"
echo "     → Copia el Webhook Secret → agrega a Railway como STRIPE_WEBHOOK_SECRET"
echo ""
echo "  3. GITHUB ACTIONS (growth bot):"
echo "     → Settings → Secrets → Actions"
echo "     → Agrega: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET,"
echo "       REDDIT_USERNAME, REDDIT_PASSWORD, REDDIT_USER_AGENT"
echo "       STRIPE_BASIC_PAYMENT_LINK, STRIPE_PRO_PAYMENT_LINK, STRIPE_PACK_PAYMENT_LINK"
echo ""
echo "  4. GITHUB PAGES (landing):"
echo "     → Settings → Pages → Source: main branch → /landing"
echo ""
echo "  5. PRUEBA FINAL:"
echo "     stripe listen --forward-to localhost:5000/webhook"
echo "     python3 webhook_server.py"
echo "     curl -X POST localhost:5000/test-delivery \\"
echo "       -H 'Content-Type: application/json' \\"
echo "       -d '{\"email\":\"tu@email.com\",\"plan_key\":\"basic\",\"pdf_url\":\"https://arxiv.org/pdf/1706.03762\"}'"
echo ""
