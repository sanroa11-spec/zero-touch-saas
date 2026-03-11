"""
product_engine.py — Módulo 2: Motor de Producto (Gemini AI)
===========================================================
Responsabilidades:
  - Descargar y parsear un PDF desde URL pública
  - Enviar el contenido a Gemini 1.5 Flash para su análisis
  - Retornar un resumen estructurado según el plan contratado
  - Manejo robusto de errores y PDFs grandes (chunking automático)

Uso directo (para testing):
  python product_engine.py https://arxiv.org/pdf/1706.03762 basic
"""

import os
import io
import re
import sys
import logging
import requests
import google.generativeai as genai
from pypdf import PdfReader
from dotenv import load_dotenv

load_dotenv()

# ── Configuración ─────────────────────────────────────────────────────────────
genai.configure(api_key=os.environ["GEMINI_API_KEY"])
logger = logging.getLogger("product_engine")

# Límite de páginas por plan (sincronizado con stripe_module.py)
PLAN_LIMITS = {
    "basic": 50,
    "pro": 200,
    "pack": 200,
}

# Límite de tokens de texto para Gemini (caracteres aprox.)
# Gemini 1.5 Flash: 1M tokens de contexto → usamos 400k chars como límite seguro
MAX_CHARS_PER_REQUEST = 400_000

# Modelo a usar
GEMINI_MODEL = "gemini-1.5-flash"


# ── Prompts por plan ──────────────────────────────────────────────────────────

PROMPTS = {
    "basic": """
Eres un asistente experto en síntesis de documentos académicos y técnicos.
Analiza el siguiente texto extraído de un PDF y genera un resumen ejecutivo en español.

**Formato de respuesta requerido (usa exactamente estos encabezados Markdown):**

## 📄 Resumen Ejecutivo
[3-5 párrafos concisos que capturen la esencia del documento]

## 🎯 Puntos Clave
- [Punto 1: idea principal]
- [Punto 2: idea principal]
- [Punto 3: idea principal]
- [Punto 4: idea principal]
- [Punto 5: idea principal]

## 📊 Datos Importantes
[Números, estadísticas, fechas o cifras relevantes mencionadas en el documento]

---
**Texto del PDF:**
{text}
""",

    "pro": """
Eres un analista de documentos de nivel experto. Tu misión es generar un análisis profundo
del PDF proporcionado, útil para profesionales, investigadores o estudiantes avanzados.

**Formato de respuesta requerido (usa exactamente estos encabezados Markdown):**

## 📄 Resumen Ejecutivo
[3-5 párrafos que capturen el argumento central y metodología]

## 🔍 Análisis Detallado
### Contexto y Problema
[¿Qué problema aborda el documento? ¿Cuál es el contexto?]

### Metodología / Enfoque
[¿Cómo aborda el tema? ¿Qué metodología usa?]

### Hallazgos y Conclusiones
[¿Qué concluye? ¿Cuáles son los resultados principales?]

## 🎯 Puntos Clave
- [Punto 1]
- [Punto 2]
- [Punto 3]
- [Punto 4]
- [Punto 5]
- [Punto 6]
- [Punto 7]

## 📊 Datos y Estadísticas Relevantes
[Lista de cifras, estadísticas y datos cuantitativos importantes]

## ⚠️ Limitaciones y Consideraciones
[Limitaciones del estudio, sesgos potenciales, o puntos de debate]

## 💡 Aplicaciones Prácticas
[¿Cómo se puede aplicar este conocimiento en la práctica?]

---
**Texto del PDF:**
{text}
""",

    "pack": """
Eres un analista de documentos de nivel experto. Tu misión es generar un análisis profundo
del PDF proporcionado.

**Formato de respuesta requerido (usa exactamente estos encabezados Markdown):**

## 📄 Resumen Ejecutivo
[3-5 párrafos que capturen el argumento central y metodología]

## 🔍 Análisis Detallado
### Contexto y Problema
[¿Qué problema aborda el documento? ¿Cuál es el contexto?]

### Metodología / Enfoque
[¿Cómo aborda el tema? ¿Qué metodología usa?]

### Hallazgos y Conclusiones
[¿Qué concluye? ¿Cuáles son los resultados principales?]

## 🎯 Puntos Clave
- [Al menos 7-10 puntos detallados]

## 📊 Datos y Estadísticas Relevantes
[Lista de cifras, estadísticas y datos cuantitativos importantes]

## ⚠️ Limitaciones y Consideraciones
[Limitaciones del estudio, sesgos potenciales, o puntos de debate]

## 💡 Aplicaciones Prácticas
[¿Cómo se puede aplicar este conocimiento en la práctica?]

---
**Texto del PDF:**
{text}
""",
}


# ── Funciones de soporte ──────────────────────────────────────────────────────

def _download_pdf(url: str, timeout: int = 30) -> bytes:
    """
    Descarga un PDF desde una URL pública.
    Soporta Google Drive, Dropbox, URLs directas, arXiv, etc.
    """
    # Convertir URLs de Google Drive al formato de descarga directa
    gdrive_match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", url)
    if gdrive_match:
        file_id = gdrive_match.group(1)
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        logger.info(f"📂 Google Drive URL convertida a descarga directa")

    # Convertir Dropbox: ?dl=0 → ?dl=1
    if "dropbox.com" in url and "dl=0" in url:
        url = url.replace("dl=0", "dl=1")

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; PDFSummarizer/1.0)",
        "Accept": "application/pdf,*/*",
    }

    logger.info(f"⬇️  Descargando PDF desde: {url[:80]}...")
    response = requests.get(url, headers=headers, timeout=timeout, stream=True)
    response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    if "pdf" not in content_type and not url.endswith(".pdf"):
        # Advertencia pero no error — algunos CDNs no setean bien el content-type
        logger.warning(f"⚠️  Content-Type inusual: {content_type}")

    pdf_bytes = response.content
    logger.info(f"✅ PDF descargado: {len(pdf_bytes) / 1024:.1f} KB")
    return pdf_bytes


def _extract_text_from_pdf(pdf_bytes: bytes, max_pages: int) -> tuple[str, int]:
    """
    Extrae texto de un PDF usando pypdf.

    Returns:
        tuple: (texto_extraído, total_páginas)
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total_pages = len(reader.pages)
    pages_to_process = min(total_pages, max_pages)

    logger.info(f"📑 PDF: {total_pages} páginas totales → procesando {pages_to_process}")

    text_parts = []
    for i, page in enumerate(reader.pages[:pages_to_process]):
        try:
            page_text = page.extract_text() or ""
            if page_text.strip():
                text_parts.append(f"[Página {i+1}]\n{page_text}")
        except Exception as e:
            logger.warning(f"⚠️  Error extrayendo página {i+1}: {e}")

    full_text = "\n\n".join(text_parts)
    logger.info(f"📝 Texto extraído: {len(full_text):,} caracteres")

    return full_text, total_pages


def _truncate_intelligently(text: str, max_chars: int) -> str:
    """
    Trunca el texto en el último párrafo completo antes del límite.
    Evita cortar en medio de una oración.
    """
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]
    # Buscar el último punto antes del límite
    last_period = truncated.rfind(". ")
    if last_period > max_chars * 0.8:  # Solo si hay suficiente contenido
        truncated = truncated[:last_period + 1]

    logger.warning(
        f"⚠️  Texto truncado: {len(text):,} → {len(truncated):,} caracteres "
        f"(límite del modelo)"
    )
    return truncated + "\n\n[... Documento truncado por longitud. Se procesó la sección principal ...]"


def _call_gemini(prompt: str) -> str:
    """
    Llama a Gemini 1.5 Flash y retorna el texto generado.
    Configurado para máxima calidad de síntesis.
    """
    model = genai.GenerativeModel(
        model_name=GEMINI_MODEL,
        generation_config={
            "temperature": 0.3,       # Bajo para más precisión, menos alucinación
            "top_p": 0.8,
            "top_k": 40,
            "max_output_tokens": 4096,
        },
        safety_settings=[
            # Desactivar filtros que podrían bloquear contenido académico legítimo
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_ONLY_HIGH"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_ONLY_HIGH"},
        ],
    )

    logger.info(f"🤖 Llamando a {GEMINI_MODEL}... (prompt: {len(prompt):,} chars)")
    response = model.generate_content(prompt)

    if not response.text:
        raise ValueError("Gemini retornó una respuesta vacía. "
                        "El PDF puede contener contenido bloqueado por los filtros de seguridad.")

    logger.info(f"✅ Respuesta de Gemini: {len(response.text):,} caracteres")
    return response.text


# ── Función principal ─────────────────────────────────────────────────────────

def summarize_pdf(pdf_url: str, plan: str = "basic") -> dict:
    """
    Función principal del motor de producto.
    Descarga, parsea y resume un PDF usando Gemini AI.

    Args:
        pdf_url: URL pública del PDF (Google Drive, Dropbox, arXiv, URL directa)
        plan: "basic", "pro", o "pack"

    Returns:
        dict con las siguientes claves:
          - summary_markdown: El resumen completo en formato Markdown
          - total_pages: Total de páginas del PDF original
          - pages_processed: Páginas efectivamente procesadas
          - char_count: Caracteres de texto procesados
          - plan: Plan utilizado
          - model: Modelo de IA utilizado
          - pdf_url: URL del PDF procesado
          - error: None si exitoso, mensaje de error si falló

    Raises:
        ValueError: Si el plan es inválido o el PDF no se puede procesar
        requests.HTTPError: Si la URL del PDF no es accesible
    """
    if plan not in PLAN_LIMITS:
        raise ValueError(f"Plan '{plan}' inválido. Opciones: {list(PLAN_LIMITS.keys())}")

    max_pages = PLAN_LIMITS[plan]
    result = {
        "summary_markdown": None,
        "total_pages": 0,
        "pages_processed": 0,
        "char_count": 0,
        "plan": plan,
        "model": GEMINI_MODEL,
        "pdf_url": pdf_url,
        "error": None,
    }

    try:
        # 1. Descargar PDF
        pdf_bytes = _download_pdf(pdf_url)

        # 2. Extraer texto
        raw_text, total_pages = _extract_text_from_pdf(pdf_bytes, max_pages)
        result["total_pages"] = total_pages
        result["pages_processed"] = min(total_pages, max_pages)

        if not raw_text.strip():
            raise ValueError(
                "No se pudo extraer texto del PDF. "
                "El archivo puede estar escaneado (imágenes sin OCR) o protegido con contraseña."
            )

        # 3. Truncar si es necesario (PDFs muy largos)
        processed_text = _truncate_intelligently(raw_text, MAX_CHARS_PER_REQUEST)
        result["char_count"] = len(processed_text)

        # 4. Construir prompt y llamar a Gemini
        prompt_template = PROMPTS.get(plan, PROMPTS["basic"])
        final_prompt = prompt_template.format(text=processed_text)

        summary = _call_gemini(final_prompt)
        result["summary_markdown"] = summary

        logger.info(
            f"🎉 Resumen generado exitosamente: "
            f"{total_pages} páginas, plan={plan}, {len(summary):,} chars de output"
        )

    except requests.HTTPError as e:
        error_msg = f"No se pudo descargar el PDF: {e}. Verifica que la URL sea pública."
        logger.error(f"❌ {error_msg}")
        result["error"] = error_msg

    except Exception as e:
        error_msg = f"Error al procesar el PDF: {str(e)}"
        logger.error(f"❌ {error_msg}", exc_info=True)
        result["error"] = error_msg

    return result


# ── CLI de testing ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if len(sys.argv) < 2:
        print("""
Uso: python product_engine.py <pdf_url> [plan]

Ejemplos:
  python product_engine.py https://arxiv.org/pdf/1706.03762 basic
  python product_engine.py https://arxiv.org/pdf/1706.03762 pro
  python product_engine.py "https://drive.google.com/file/d/YOUR_ID/view" pro
        """)
        sys.exit(1)

    test_url = sys.argv[1]
    test_plan = sys.argv[2] if len(sys.argv) > 2 else "basic"

    print(f"\n🚀 Procesando PDF con plan '{test_plan}'...")
    print(f"   URL: {test_url}\n")

    result = summarize_pdf(test_url, test_plan)

    if result["error"]:
        print(f"\n❌ ERROR: {result['error']}")
        sys.exit(1)

    print("=" * 60)
    print(result["summary_markdown"])
    print("=" * 60)
    print(f"\n📊 Stats:")
    print(f"   Páginas totales: {result['total_pages']}")
    print(f"   Páginas procesadas: {result['pages_processed']}")
    print(f"   Texto procesado: {result['char_count']:,} caracteres")
    print(f"   Modelo: {result['model']}")
