"""
growth_bot.py — Módulo 4: El Vendedor (Reddit Growth Bot)
==========================================================
Responsabilidades:
  - Escanear subreddits en busca de personas que necesitan resumir PDFs
  - Publicar comentarios de alto valor con enlace al servicio
  - Modo dry-run para testing sin postear
  - Rate limiting estricto para no ser baneado
  - Sistema de cooldown por subreddit y tracking de posts comentados

Uso:
  python growth_bot.py                  → corre para siempre (loop)
  python growth_bot.py --run-once       → una sola pasada (GitHub Actions)
  python growth_bot.py --dry-run        → simula sin postear nada
  python growth_bot.py --stats          → muestra estadísticas
"""

import os
import sys
import json
import time
import logging
import hashlib
import argparse
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path

import praw
from dotenv import load_dotenv

load_dotenv()

# ── Configuración ─────────────────────────────────────────────────────────────
logger = logging.getLogger("growth_bot")

# Payment Links (configurar en .env o directamente aquí después de crear-links)
PAYMENT_LINK_BASIC = os.environ.get("STRIPE_BASIC_PAYMENT_LINK", "https://buy.stripe.com/BASIC")
PAYMENT_LINK_PRO   = os.environ.get("STRIPE_PRO_PAYMENT_LINK",   "https://buy.stripe.com/PRO")
PAYMENT_LINK_PACK  = os.environ.get("STRIPE_PACK_PAYMENT_LINK",  "https://buy.stripe.com/PACK")

# Archivo de estado local (tracking de posts ya comentados)
STATE_FILE = Path(__file__).parent / "bot_state.json"

# Límites de seguridad — CRÍTICOS para no ser baneado
MAX_COMMENTS_PER_RUN   = 5    # Máx comentarios por ejecución de --run-once
MAX_COMMENTS_PER_HOUR  = 5    # Límite global por hora
SUBREDDIT_COOLDOWN_HRS = 4    # Horas entre posts en el mismo subreddit
POST_MIN_SCORE         = 3    # Solo responder posts con al menos N upvotes
POST_MAX_AGE_HOURS     = 48   # Solo posts de menos de N horas de antigüedad
COMMENT_DELAY_SECONDS  = 45   # Espera entre comentarios (anti-spam)


# ── Subreddits objetivo ───────────────────────────────────────────────────────
TARGET_SUBREDDITS = [
    # Alta conversión — buscan soluciones activamente
    {"name": "college",          "weight": 3},
    {"name": "GradSchool",       "weight": 3},
    {"name": "premed",           "weight": 2},
    {"name": "lawschool",        "weight": 2},
    {"name": "productivity",     "weight": 2},
    {"name": "studytips",        "weight": 2},
    {"name": "Professors",       "weight": 1},
    {"name": "academia",         "weight": 1},
    {"name": "slatestarcodex",   "weight": 1},
    {"name": "PhD",              "weight": 2},
    {"name": "MachineLearning",  "weight": 1},
    {"name": "datascience",      "weight": 1},
    {"name": "learnprogramming", "weight": 1},
    {"name": "Entrepreneur",     "weight": 2},
    {"name": "smallbusiness",    "weight": 1},
]

# Keywords que indican alta intención de compra
TRIGGER_KEYWORDS = [
    # Dolor con PDFs
    "pdf too long",
    "summarize this pdf",
    "summarize pdf",
    "reading this paper",
    "too many pages",
    "don't have time to read",
    "haven't read the paper",
    "research paper summary",
    "tldr paper",
    "paper summary",
    "condense this",
    # Búsqueda activa de herramientas
    "ai summarizer",
    "pdf summarizer",
    "summarize papers",
    "summarize documents",
    "notes from pdf",
    "extract key points",
    "study notes from",
    # Expresiones de agobio
    "so many papers",
    "need to read",
    "stack of papers",
    "hundreds of pages",
    "dense paper",
    "overwhelming amount",
]


# ── Plantillas de comentarios ─────────────────────────────────────────────────
# Variadas para evitar spam detection. El bot elige una aleatoriamente.

COMMENT_TEMPLATES = [
    # Template 1: Empático + solución + prueba social
    """I was in the exact same situation a few weeks ago during finals — 300-page dissertations are brutal.

I ended up using an AI PDF summarizer that uses Gemini to extract the key points and give you a structured breakdown in under 2 minutes. Game changer for long academic papers.

It handles Google Drive links, Dropbox, arXiv — basically any public URL. The basic plan ($4.99) covers up to 50 pages, and there's a Pro version for up to 200.

→ [Try it here]({link_basic}) — no subscription, pay per summary.

Good luck with your readings! 📚""",

    # Template 2: Directo al punto, tono casual
    """Not sure if this is allowed, but since you're asking — I built an AI tool that does exactly this.

You paste the PDF URL and it uses Gemini AI to return a full structured summary: executive overview, key points, important data, and methodology breakdown (on the Pro plan).

Turnaround is under 2 minutes. Works on papers, reports, legal docs, textbooks.

Basic (up to 50 pages): {link_basic}
Pro (up to 200 pages): {link_pro}

Happy to answer questions about how it works!""",

    # Template 3: Respuesta de valor primero, venta al final
    """A few strategies that work for dense academic papers:

1. **Read the abstract + conclusion first** — gives you 60% of the information
2. **Skim the section headers** — maps out the argument structure
3. **Focus on figures/tables** — usually contain the most important data
4. **Use an AI summarizer** for the heavy lifting — I've been using one built on Gemini that gives you a structured markdown summary with key points and methodology breakdown

If you want to try the AI route: {link_basic} (starts at $4.99 per summary, no subscription).

The combination of strategy + AI tool has literally cut my reading time by 70%.""",

    # Template 4: Más breve, comunidad-friendly
    """This AI PDF summarizer might save you hours: {link_basic}

Paste any PDF URL → get a full structured summary in ~90 seconds via Gemini AI. Covers key points, methodology, data highlights. Works for academic papers, reports, etc.

Basic plan is $4.99/summary (no recurring charges).""",
]


# ── Estado del bot ────────────────────────────────────────────────────────────

def _load_state() -> dict:
    """Carga el estado de tracking del bot desde disco."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "commented_posts": [],       # IDs de posts donde ya comentamos
        "subreddit_last_post": {},   # {subreddit: ISO timestamp del último comentario}
        "comments_this_hour": [],    # Timestamps de comentarios en la última hora
        "total_comments": 0,
        "total_scans": 0,
    }


def _save_state(state: dict):
    """Guarda el estado del bot a disco."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _prune_old_state(state: dict) -> dict:
    """Limpia entradas antiguas para que el state file no crezca infinitamente."""
    now = datetime.now(timezone.utc)
    cutoff_posts = (now - timedelta(days=7)).isoformat()

    # Mantener solo posts de los últimos 7 días
    state["commented_posts"] = [
        p for p in state["commented_posts"]
        if p.get("timestamp", "0") > cutoff_posts
    ]

    # Limpiar timestamps de esta hora
    one_hour_ago = (now - timedelta(hours=1)).isoformat()
    state["comments_this_hour"] = [
        t for t in state["comments_this_hour"] if t > one_hour_ago
    ]

    return state


# ── Lógica del bot ────────────────────────────────────────────────────────────

def _can_comment_in_subreddit(state: dict, subreddit: str) -> bool:
    """Verifica si el cooldown del subreddit ha expirado."""
    last_post_str = state["subreddit_last_post"].get(subreddit)
    if not last_post_str:
        return True
    last_post = datetime.fromisoformat(last_post_str)
    cooldown = timedelta(hours=SUBREDDIT_COOLDOWN_HRS)
    return datetime.now(timezone.utc) > last_post + cooldown


def _under_hourly_limit(state: dict) -> bool:
    """Verifica que no hayamos excedido el límite de comentarios por hora."""
    return len(state["comments_this_hour"]) < MAX_COMMENTS_PER_HOUR


def _post_is_relevant(post, state: dict) -> tuple[bool, str]:
    """
    Determina si un post es relevante para comentar.
    Retorna (es_relevante, razón).
    """
    post_id = post.id

    # Ya comentamos este post
    if any(p["id"] == post_id for p in state["commented_posts"]):
        return False, "ya comentado"

    # Score muy bajo
    if post.score < POST_MIN_SCORE:
        return False, f"score muy bajo ({post.score})"

    # Post muy antiguo
    created = datetime.fromtimestamp(post.created_utc, tz=timezone.utc)
    age = datetime.now(timezone.utc) - created
    if age > timedelta(hours=POST_MAX_AGE_HOURS):
        return False, f"post muy antiguo ({age.days}d)"

    # Post bloqueado
    if post.locked or post.archived:
        return False, "post bloqueado/archivado"

    # Verificar keywords en título + selftext
    text_to_check = (post.title + " " + (post.selftext or "")).lower()
    matched_keyword = next(
        (kw for kw in TRIGGER_KEYWORDS if kw in text_to_check), None
    )
    if not matched_keyword:
        return False, "no hay keywords relevantes"

    return True, matched_keyword


def _select_comment_template(plan_preference: str = "basic") -> str:
    """Selecciona una plantilla aleatoriamente y la formatea con los payment links."""
    template = random.choice(COMMENT_TEMPLATES)
    return template.format(
        link_basic=PAYMENT_LINK_BASIC,
        link_pro=PAYMENT_LINK_PRO,
        link_pack=PAYMENT_LINK_PACK,
    )


def _scan_subreddit(reddit, subreddit_name: str, state: dict, dry_run: bool) -> int:
    """
    Escanea un subreddit y comenta en posts relevantes.
    Retorna el número de comentarios realizados.
    """
    comments_made = 0

    if not _can_comment_in_subreddit(state, subreddit_name):
        logger.info(f"⏳ r/{subreddit_name}: en cooldown, saltando")
        return 0

    try:
        subreddit = reddit.subreddit(subreddit_name)
        # Buscar en 'hot' y 'new' para máxima cobertura
        posts_to_check = list(subreddit.hot(limit=20)) + list(subreddit.new(limit=15))

        logger.info(f"🔍 r/{subreddit_name}: analizando {len(posts_to_check)} posts...")

        for post in posts_to_check:
            # Respetar límites globales
            if not _under_hourly_limit(state):
                logger.warning("⚠️  Límite horario alcanzado. Pausando.")
                break

            is_relevant, reason = _post_is_relevant(post, state)

            if not is_relevant:
                logger.debug(f"   ↩️  [{post.id}] Saltado: {reason}")
                continue

            # ¡Post relevante encontrado!
            logger.info(f"   🎯 Post relevante: [{reason}] \"{post.title[:60]}...\"")
            logger.info(f"      URL: https://reddit.com{post.permalink}")

            comment_text = _select_comment_template()

            if dry_run:
                logger.info(f"   🧪 DRY RUN — comentario que se publicaría:\n{'─'*40}\n{comment_text}\n{'─'*40}")
            else:
                try:
                    post.reply(comment_text)
                    logger.info(f"   ✅ Comentario publicado en r/{subreddit_name}")
                    time.sleep(COMMENT_DELAY_SECONDS)  # Anti-spam delay
                except praw.exceptions.APIException as e:
                    if "RATELIMIT" in str(e):
                        wait_time = 300  # 5 minutos
                        logger.warning(f"🚦 Rate limit de Reddit. Esperando {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"   ❌ Reddit API error: {e}")
                    continue

            # Actualizar estado (también en dry-run para tracking)
            now_iso = datetime.now(timezone.utc).isoformat()
            state["commented_posts"].append({"id": post.id, "timestamp": now_iso})
            state["subreddit_last_post"][subreddit_name] = now_iso
            state["comments_this_hour"].append(now_iso)
            state["total_comments"] += 1
            comments_made += 1

            if comments_made >= MAX_COMMENTS_PER_RUN:
                logger.info(f"📊 Límite por ejecución alcanzado ({MAX_COMMENTS_PER_RUN})")
                break

    except praw.exceptions.PRAWException as e:
        logger.error(f"❌ Error en r/{subreddit_name}: {e}")
    except Exception as e:
        logger.error(f"❌ Error inesperado en r/{subreddit_name}: {e}", exc_info=True)

    return comments_made


# ── Entry Points ──────────────────────────────────────────────────────────────

def run_bot(run_once: bool = False, dry_run: bool = False):
    """
    Función principal del Growth Bot.

    Args:
        run_once: Si True, hace una sola pasada y termina (para GitHub Actions)
        dry_run: Si True, simula sin postear (para testing)
    """
    # Inicializar Reddit client
    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
    )

    mode_label = "DRY RUN 🧪" if dry_run else "PRODUCCIÓN 🚀"
    logger.info(f"🤖 Growth Bot iniciado — Modo: {mode_label}")
    logger.info(f"   Usuario Reddit: u/{os.environ.get('REDDIT_USERNAME')}")
    logger.info(f"   Subreddits objetivo: {len(TARGET_SUBREDDITS)}")

    # Asegurarse de que el bot puede autenticarse
    try:
        logger.info(f"   Autenticado como: u/{reddit.user.me()}")
    except Exception as e:
        logger.error(f"❌ Error de autenticación Reddit: {e}")
        return

    # Cargar y limpiar estado
    state = _load_state()
    state = _prune_old_state(state)
    state["total_scans"] += 1

    # Ordenar subreddits por peso (mayor prioridad primero) con algo de aleatoriedad
    subreddits = sorted(
        TARGET_SUBREDDITS,
        key=lambda x: x["weight"] + random.uniform(0, 0.5),
        reverse=True,
    )

    total_comments = 0

    for sub_config in subreddits:
        sub_name = sub_config["name"]

        if not _under_hourly_limit(state):
            logger.info("🛑 Límite horario global alcanzado. Terminando scan.")
            break

        comments = _scan_subreddit(reddit, sub_name, state, dry_run)
        total_comments += comments

        if total_comments >= MAX_COMMENTS_PER_RUN:
            break

        # Pequeña pausa entre subreddits
        if not dry_run:
            time.sleep(random.uniform(3, 8))

    _save_state(state)
    logger.info(f"✅ Scan completado: {total_comments} comentarios, "
                f"{state['total_comments']} total histórico")

    if not run_once:
        # En modo continuo, esperar 60 min antes del siguiente scan
        next_run = 60 * 60
        logger.info(f"⏰ Próximo scan en {next_run//60} minutos...")
        time.sleep(next_run)
        run_bot(run_once=False, dry_run=dry_run)


def print_stats():
    """Muestra estadísticas del bot."""
    state = _load_state()
    print(f"""
📊 Growth Bot — Estadísticas
{"─"*40}
  Total scans:           {state.get("total_scans", 0)}
  Total comentarios:     {state.get("total_comments", 0)}
  Posts rastreados:      {len(state.get("commented_posts", []))}
  Subreddits activos:    {len(state.get("subreddit_last_post", {}))}
  Comentarios esta hora: {len(state.get("comments_this_hour", []))}

  Últimos subreddits comentados:
""")
    for sub, ts in state.get("subreddit_last_post", {}).items():
        print(f"    r/{sub}: {ts}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="PDF Summarizer — Reddit Growth Bot")
    parser.add_argument("--run-once", action="store_true",
                        help="Una sola pasada (para GitHub Actions cron)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simula sin postear nada")
    parser.add_argument("--stats", action="store_true",
                        help="Muestra estadísticas y sale")
    args = parser.parse_args()

    if args.stats:
        print_stats()
        sys.exit(0)

    run_bot(run_once=args.run_once, dry_run=args.dry_run)
