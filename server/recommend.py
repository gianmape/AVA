"""
mcp/recommend.py
------------------
HANA vector retrieval and Excel write helpers.

Joule orchestrates the full pipeline:
  1. read_excel_painpoints  — read rows from input Excel
  2. retrieve_similar_cases — embed + HANA cosine search per row
  3. (Joule synthesizes recommendations using SAP doc knowledge)
  4. write_excel_output     — write Joule results back to Excel

These functions are exposed as MCP tools via mcp/server.py.
"""
import sys
import os
import pathlib
import re
import shutil
import warnings
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import requests
from langdetect import detect as _detect_lang, LangDetectException, DetectorFactory
import openpyxl

# Make langdetect deterministic — avoids sporadic misclassification of short Spanish text
DetectorFactory.seed = 0

log = logging.getLogger(__name__)
from openpyxl.styles import Font, Alignment
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from shared.config import (
    normalise_columns,
    drop_ignored_columns,
    clean_str,
    embed_text,
    hana_connection,
    release_connection,
    VALID_SOLUTIONS,
    normalise_solution_area,
    normalise_solution,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VECTOR_SEARCH_SQL = """
SELECT TOP {top_k}
    ID, PAIN_POINT, SOLUTION_AREA, RECOMMENDATION, CATEGORY,
    EFFORT, BENEFITS, TIMELINE, IMPACT,
    COSINE_SIMILARITY(EMBEDDING, TO_REAL_VECTOR(?)) AS SCORE,
    COALESCE(USE_COUNT, 0) AS USE_COUNT,
    COALESCE(QUALITY_SCORE, 0) AS QUALITY_SCORE
FROM SVA2.PAIN_POINTS
WHERE SOLUTION = ?
{area_filter}
ORDER BY SCORE DESC
"""

# Minimum cosine similarity threshold — cases below this are considered irrelevant
_SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.55"))

# Quality weighting: final_score = cosine_score + (quality_bonus * _QUALITY_WEIGHT)
# quality_bonus = quality_score if > 0 (from consultant feedback), else 0 (no proxy)
_QUALITY_WEIGHT = 0.15

KNOWLEDGE_SEARCH_SQL = """
SELECT TOP {top_k}
    TITLE, CONTENT, SOLUTION, RELEASE, AGENT_BASED, JOULE_BASED,
    VALUE_DRIVER, VALUE_LEVER, KPI_ID, KPI_CATEGORY, KPI_TARGET,
    CAPABILITY, KPI_FORMULA, KPI_MEAS_FREQ,
    COSINE_SIMILARITY(EMBEDDING, TO_REAL_VECTOR(?)) AS SCORE
FROM SVA2.KNOWLEDGE_BASE
WHERE SOURCE_TYPE = ?
{solution_filter}
ORDER BY SCORE DESC
"""

TARGET_COLS = {
    "recommendations":        "Recommendations",
    "category":               "Category",
    "effort":                 "Effort",
    "benefits":               "Benefits",
    "links to documentation": "Documentation",
    "documentation":          "Documentation",
    "timeline":               "Timeline",
    "impact":                 "Impact",
    "solution area":          "Solution Area",
    "ariba next-gen":         "Ariba Next-Gen",
    "value kpis":             "Value KPIs",
}

# Pre-lowercased and pre-stripped URL prefixes blocked from documentation output.
# Built once at import time — _is_blocked() does no repeated .lower() calls.
_URL_BLOCKLIST = tuple(p.lower().rstrip("/") for p in (
    "https://community.sap.com/topics/",
    "https://support.ariba.com",
    "https://support.sap.com",
    "https://learning.sap.com",         # entire domain blocked — all URLs are unreliable
    # help.sap.com /viewer/product/ roots — legacy URL format (no GUID = no article)
    "https://help.sap.com/viewer/product/",
))


def _is_blocked(url: str) -> bool:
    u = url.strip().rstrip("/").lower()
    if any(u.startswith(blocked) for blocked in _URL_BLOCKLIST):
        return True
    # Block SAP Community portal category pages: /t5/<board>/ct-p/<anything>
    if "community.sap.com/t5/" in u and "/ct-p/" in u:
        return True
    # Block help.sap.com URLs that are too shallow (product roots / category pages).
    if "help.sap.com" in u:
        try:
            path = urlparse(u).path.strip("/")
            segments = [s for s in path.split("/") if s]
            if len(segments) < 3:
                return True
        except Exception:
            pass
    return False


_URL_VALIDATE_TIMEOUT = 8  # seconds per request

# SAP Help Portal Search API — used to validate slug-based URLs that have no GUID.
_SAP_HELP_SEARCH_API = "https://help.sap.com/http.svc/search"


def _validate_help_url(url: str, title: str = "") -> str | None:
    """Validate a help.sap.com URL. Returns a canonical GUID-based URL if found, else None.

    Strategy:
    - If the URL already contains a 32-char hex GUID → accept as-is (canonical).
    - Otherwise (slug-based, likely LLM-fabricated) → query the SAP Help Search API
      using the title/slug keywords to find the real canonical URL.
      Accepts a result if its title closely matches the provided title.
    """
    path = urlparse(url).path.strip("/")

    # URLs with GUID are canonical — pass directly
    if re.search(r'[0-9a-f]{32}', path):
        return url

    # Slug-based URL: extract topic keywords from path
    segments = [s for s in path.split("/") if s]
    topic_segments = segments[2:] if len(segments) > 2 else segments
    slug_keywords = " ".join(s.replace("-", " ").replace("_", " ") for s in topic_segments)
    query = title if title else slug_keywords

    if not query or len(query) < 3:
        return None

    # Normalise title for comparison
    def _norm(s):
        return set(s.lower().replace("-", " ").replace("_", " ").split())

    query_words = _norm(query)

    try:
        resp = requests.get(
            _SAP_HELP_SEARCH_API,
            params={"q": query, "state": "PRODUCTION", "locale": "en-US", "top": "5"},
            timeout=_URL_VALIDATE_TIMEOUT,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", {})
        results = data.get("results", [])
        if not results:
            return None

        # Accept first result whose title shares significant overlap with the query.
        # This prevents returning a completely unrelated article.
        for r in results:
            r_title_words = _norm(r.get("title", ""))
            # At least 50% of query words (min 2) should appear in the result title
            overlap = query_words.intersection(r_title_words)
            threshold = max(2, len(query_words) // 2)
            if len(overlap) >= threshold:
                canonical_path = r.get("url", "")
                if canonical_path:
                    base = canonical_path.split("?")[0]
                    return f"https://help.sap.com{base}"

        # No title-matched result found — reject to avoid returning unrelated articles
        return None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Server-side documentation search (SAP Help Portal)
# ---------------------------------------------------------------------------
_SEARCH_STOP_WORDS = {
    # English
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "to", "in", "of", "for", "and", "or", "that", "this", "with", "not",
    "it", "on", "at", "by", "from", "as", "but", "no", "we", "our",
    "have", "has", "had", "do", "does", "did", "can", "could", "would",
    "should", "will", "shall", "may", "might", "very", "also", "just",
    "than", "then", "so", "if", "when", "which", "who", "how", "what",
    "there", "their", "they", "them", "its", "my", "your", "all", "each",
    "been", "being", "some", "only", "other", "into", "more", "such",
    "need", "needs", "want", "wants", "like", "make", "made", "after",
    "before", "between", "through", "during", "about", "above", "below",
    "live", "post", "goes", "going", "work", "works", "used", "using",
    # Spanish — common verbs, prepositions, connectors, filler words
    "el", "la", "los", "las", "de", "en", "que", "es", "un", "una",
    "por", "con", "para", "se", "del", "al", "su", "como", "más",
    "pero", "sus", "le", "ya", "o", "este", "esta", "ha", "me", "sin",
    "sobre", "ser", "todo", "desde", "son", "entre", "cuando", "muy",
    "tiene", "tiene", "sido", "hay", "esto", "eso", "así", "otro",
    "otra", "otros", "bien", "puede", "todos", "estas", "estos",
    "ella", "ellos", "ante", "solo", "hacia", "donde", "ahora",
    "parte", "después", "cada", "hacer", "mismo", "poder", "sino",
    "hecho", "forma", "aquí", "pues", "debe", "vez", "existe",
    "tener", "también", "fueron", "porque", "nada", "mucho",
    "tiempo", "manera", "fuera", "mejor", "gran", "sea", "dos",
    "cual", "caso", "bajo", "esos", "momento", "dicho", "sido",
    "tanto", "además", "tiene", "asociado", "asociada", "tarea",
    "tareas", "proceso", "manera", "tienen", "puede", "pueden",
    "existe", "cuando", "ejemplo", "necesario", "actualmente",
    "embargo", "realizar", "realiza", "posible", "debido",
    "través", "respecto", "algunas", "algunos", "siempre",
    "problema", "problemas", "generar", "genera", "permite",
    # Additional Spanish noise words found in pain points
    "estan", "están", "funciona", "funciono", "funcionó", "funcionar",
    "nivel", "niveles", "ambiente", "anterior", "anteriores",
    "gestionar", "gestión", "gestion", "usarse", "usando", "usan",
    "aplican", "aplica", "aplica", "eliminar", "eliminados",
    "sistema", "sistemas", "deben", "deberia", "debería",
    "activo", "activa", "activar", "activado", "activada",
    "empleando", "emplear", "emplea", "usar", "usados", "usadas",
    "copia", "copiar", "copiado", "copiada", "copian", "copio",
    "escenario", "escenarios", "ningun", "ningún", "ninguna",
    "manualmente", "manual", "manuales",
    "bases", "base", "seccion", "sección", "area", "áreas",
    "dejarlos", "dejar", "subir", "suben", "sube",
    "publicado", "publicada", "publicar",
    "lleva", "llevan", "llevar", "seguir", "siguen",
    "preguntas", "pregunta", "contestar",
    "adaptarse", "adaptar", "adaptado",
    "caracteristicas", "característica", "características",
    "resumen", "ejecutivo", "contenido",
    "estado", "estados", "dada", "dado", "dados",
    "hace", "hacen", "ariba",  # "ariba" is covered by solution name already
    "cmpc",  # client-specific name, not a SAP search term
}


def _extract_keywords(text: str, max_words: int = 8) -> str:
    """Extract meaningful keywords from pain point text for search API query.

    Strategy:
    1. Strip punctuation, split to tokens.
    2. Remove stop words (English + Spanish).
    3. Score each token:
       - Domain-mapped Spanish terms (in _SAP_TERM_MAP): score = 20 + len(translation)
         These are the most reliable signal — always prioritize them.
       - Non-mapped words ≥7 chars: score = len(word)  (long = more specific)
       - Non-mapped words 4–6 chars: score = 0  (too short/generic — skip unless nothing else)
    4. Take top-scored, deduplicate, restore original sentence order.
    """
    words = re.sub(r'[^\w\s]', ' ', text.lower()).split()
    meaningful = [w for w in words if w not in _SEARCH_STOP_WORDS and len(w) >= 4]

    scored = []
    for pos, w in enumerate(meaningful):
        translated = _SAP_TERM_MAP.get(w, w)
        if translated != w:
            # Domain-mapped Spanish term: highest priority
            score = 20 + len(translated)
        elif len(w) >= 7:
            # Long unmapped word: moderate priority
            score = len(w)
        else:
            # Short unmapped word: lowest priority (likely noise in Spanish text)
            score = 0
        scored.append((score, pos, translated))

    # Pick top-scored, then restore sentence order; deduplicate
    top_by_score = sorted(scored, key=lambda x: (-x[0], x[1]))[:max_words]
    top_by_score.sort(key=lambda x: x[1])
    seen: set[str] = set()
    top = []
    for _, _, t in top_by_score:
        if t not in seen:
            seen.add(t)
            top.append(t)

    return " ".join(top)


# Common Spanish→English SAP domain term mappings for search API
_SAP_TERM_MAP = {
    "aprobacion": "approval", "aprobación": "approval", "aprobada": "approval",
    "aprobado": "approval", "aprobar": "approval", "aprobaciones": "approvals",
    "formulario": "form", "formularios": "forms",
    "recomendaciones": "recommendations", "recomendacion": "recommendation",
    "funcionalidades": "features", "funcionalidad": "feature",
    "inteligencia": "intelligence", "artificial": "artificial",
    "manejo": "management",
    "proveedor": "supplier", "proveedores": "suppliers",
    "compra": "purchase", "compras": "purchasing",
    "contrato": "contract", "contratos": "contracts",
    "factura": "invoice", "facturas": "invoices",
    "catalogo": "catalog", "catálogo": "catalog",
    "solicitud": "requisition", "solicitudes": "requisitions",
    "subasta": "auction", "subastas": "auctions",
    "licitacion": "sourcing", "licitación": "sourcing",
    "evento": "event", "eventos": "events",
    "notificacion": "notification", "notificaciones": "notifications",
    "notificación": "notification",
    "configuracion": "configuration", "configuración": "configuration",
    "flujo": "workflow", "flujos": "workflows",
    "usuario": "user", "usuarios": "users",
    "campo": "field", "campos": "fields",
    "informe": "report", "informes": "reports",
    "reporte": "report", "reportes": "reports",
    "plantilla": "template", "plantillas": "templates",
    "evaluacion": "evaluation", "evaluación": "evaluation",
    "puntuacion": "scoring", "puntuación": "scoring",
    "adjudicacion": "award", "adjudicación": "award",
    "negociacion": "negotiation", "negociación": "negotiation",
    "gasto": "spend", "gastos": "spend",
    "presupuesto": "budget", "presupuestos": "budgets",
    "pedido": "order", "pedidos": "orders",
    "recepcion": "receipt", "recepción": "receipt",
    "pago": "payment", "pagos": "payments",
    "riesgo": "risk", "riesgos": "risks",
    "cumplimiento": "compliance",
    "desempeño": "performance", "rendimiento": "performance",
    "clasificacion": "classification", "clasificación": "classification",
    "integracion": "integration", "integración": "integration",
    "automatizacion": "automation", "automatización": "automation",
    "documento": "document", "documentos": "documents",
    "proyecto": "project", "proyectos": "projects",
    "regla": "rule", "reglas": "rules",
    "condicion": "condition", "condiciones": "conditions", "condición": "condition",
    "precio": "price", "precios": "prices",
    "oferta": "bid", "ofertas": "bids",
    "respuesta": "response", "respuestas": "responses",
    "calificacion": "qualification", "calificación": "qualification",
    "registro": "registration",
    "cuestionario": "questionnaire", "cuestionarios": "questionnaires",
    "categoria": "category", "categoría": "category", "categorias": "categories",
    "desaparece": "disappears", "desaparecen": "disappear",
    "visible": "visible", "visibilidad": "visibility",
    "permiso": "permission", "permisos": "permissions",
    "acceso": "access",
    "error": "error", "errores": "errors",
    "carga": "upload", "cargar": "upload",
    "descarga": "download", "descargar": "download",
    "archivo": "file", "archivos": "files",
    "correo": "email", "correos": "emails",
    # Batch pain points — terms not yet covered
    "adjudicacion": "award", "adjudicación": "award", "adjudicaciones": "awards",
    "replicando": "replication", "replicar": "replicate", "replica": "replication",
    "integracion": "integration", "integración": "integration",
    "guided": "guided", "guiado": "guided", "guiada": "guided",
    "clasico": "classic", "clásico": "classic",
    "ponderacion": "weighting", "ponderación": "weighting",
    "sobres": "sealed bid envelopes",
    "dashboard": "dashboard", "dashboards": "dashboards",
    "clausula": "clause", "cláusula": "clause", "clausulas": "clauses",
    "repositorio": "repository",
    "firma": "signature", "firmar": "signature",
    "adjunto": "attachment", "adjuntos": "attachments",
    "boletin": "bulletin", "boletín": "bulletin",
    "espacio": "workspace", "espacios": "workspaces",
    "encuesta": "survey", "encuestas": "surveys",
    "cotizacion": "quote", "cotización": "quote", "cotizaciones": "quotes",
    "calendario": "calendar",
    "tarea": "task", "tareas": "tasks",
    "revision": "review", "revisión": "review",
    "grupos": "groups", "grupo": "group",
    "migración": "migration", "migracion": "migration",
}


def search_documentation(
    pain_point: str,
    solution: str,
    max_results: int = 3,
    recommendation_hint: str = "",
) -> list[dict]:
    """Search SAP Help Portal for documentation relevant to a pain point.

    Queries the SAP Help Search API and returns up to max_results entries
    as [{"title": ..., "url": ...}] with canonical URLs.
    Returns [] on error or if no qualifying results found.

    Query strategy:
    - If recommendation_hint is provided: use it as the primary query.
      The hint is already in English and synthesized by Joule, so it is
      more precise than keywords extracted from a raw Spanish pain point.
    - Otherwise: fall back to _extract_keywords(pain_point) for the query.
    """
    if recommendation_hint:
        primary_query = f"{solution} {recommendation_hint.strip()[:80]}".strip()
        log.info("   search_documentation: using hint as primary query: %s", primary_query[:120])
    else:
        keywords = _extract_keywords(pain_point)
        primary_query = f"{solution} {keywords}".strip()

    if len(primary_query) < 5:
        return []

    def _run_query(query: str) -> list[dict]:
        try:
            resp = requests.get(
                _SAP_HELP_SEARCH_API,
                params={"q": query, "state": "PRODUCTION", "locale": "en-US", "top": "8"},
                timeout=_URL_VALIDATE_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code != 200:
                return []
            results = resp.json().get("data", {}).get("results", [])
            if not results:
                return []

            docs = []
            for r in results:
                raw_path = r.get("url", "")
                if not raw_path:
                    continue
                url = f"https://help.sap.com{raw_path.split('?')[0]}"
                if _is_blocked(url):
                    continue
                path = urlparse(url).path.strip("/")
                segments = [s for s in path.split("/") if s]
                if len(segments) < 3:
                    continue
                docs.append({"title": r.get("title", "SAP Documentation"), "url": url})
                if len(docs) >= max_results:
                    break
            return docs
        except Exception:
            return []

    docs = _run_query(primary_query)

    # Fallback: if hint was primary and returned nothing, retry with pain point keywords
    if not docs and recommendation_hint:
        keywords = _extract_keywords(pain_point)
        fallback_query = f"{solution} {keywords}".strip()
        log.info("   search_documentation: hint query empty, retrying with keywords: %s", fallback_query[:120])
        docs = _run_query(fallback_query)

    return docs


def _url_is_alive(url: str) -> bool:
    """Return True if the URL responds with HTTP 2xx/3xx (i.e. page exists).
    Uses GET because some sites (community.sap.com) block HEAD requests.
    help.sap.com validation is handled separately by _validate_help_url.
    """
    if "help.sap.com" in url:
        return True  # validation handled by _validate_help_url
    try:
        resp = requests.get(
            url,
            timeout=_URL_VALIDATE_TIMEOUT,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
            stream=True,  # Don't download full body
        )
        resp.close()
        return resp.status_code < 400
    except Exception:
        return False


def _validate_documentation_urls(docs: list[dict]) -> list[dict]:
    """
    Validate documentation URLs in parallel.
    - help.sap.com slug URLs → resolved to canonical GUID URLs via Search API.
    - help.sap.com GUID URLs → passed as-is (already canonical).
    - Other domains → HTTP GET check (status < 400).
    Returns only entries whose URLs are verified; may rewrite help.sap.com URLs to canonical form.
    """
    if not docs:
        return []

    def _check(entry):
        url = entry.get("url", "") if isinstance(entry, dict) else str(entry)
        title = entry.get("title", "") if isinstance(entry, dict) else ""
        if not url or not url.startswith("http"):
            return None

        if "help.sap.com" in url:
            canonical = _validate_help_url(url, title)
            if canonical:
                if isinstance(entry, dict):
                    entry = dict(entry)  # shallow copy
                    entry["url"] = canonical
                return entry
            log.info("   INVALID help.sap.com url (no match in Search API): %s", url)
            return None

        if _url_is_alive(url):
            return entry
        log.info("   DEAD url (HTTP check failed): %s", url)
        return None

    valid = []
    with ThreadPoolExecutor(max_workers=min(len(docs), 5)) as pool:
        # Map futures to original index to preserve order
        futures = {pool.submit(_check, d): i for i, d in enumerate(docs)}
        results_by_idx = {}
        for fut in as_completed(futures):
            idx = futures[fut]
            result = fut.result()
            if result is not None:
                results_by_idx[idx] = result

    # Return in original order
    return [results_by_idx[i] for i in sorted(results_by_idx)]


# ---------------------------------------------------------------------------
# Excel reader — returns rows Joule will process
# ---------------------------------------------------------------------------
PAIN_POINT_SHEET_HINTS = ("observation", "pain point", "observations", "pain points", "observaciones")


def _find_pain_point_sheet(xl: pd.ExcelFile) -> str:
    """
    Return the sheet name that contains the pain points data.
    Strategy:
      1. Sheet whose name contains a hint keyword (observations, pain points, etc.)
      2. First sheet that has both 'pain_point' and 'solution' columns after normalisation
      3. Fall back to the first sheet
    """
    from shared.config import normalise_columns

    for name in xl.sheet_names:
        if any(hint in name.lower() for hint in PAIN_POINT_SHEET_HINTS):
            return name

    for name in xl.sheet_names:
        try:
            df = xl.parse(name, dtype=str, nrows=5)
            df.columns = df.columns.astype(str)
            df = drop_ignored_columns(df)
            df = normalise_columns(df)
            if "pain_point" in df.columns and "solution" in df.columns:
                return name
            # Check if row 0 is a title row and headers are in row 1
            if not df.empty:
                first_row = df.iloc[0].astype(str).str.strip().str.lower()
                if any(v in first_row.values for v in ("solution", "pain point", "observation/pain point")):
                    df2 = xl.parse(name, dtype=str, header=1, nrows=3)
                    df2.columns = df2.columns.astype(str)
                    df2 = normalise_columns(df2)
                    if "pain_point" in df2.columns and "solution" in df2.columns:
                        return name
        except Exception:
            continue

    return xl.sheet_names[0]


def read_excel_painpoints(input_path: str) -> list[dict]:
    """
    Read an input Excel file and return all pain point rows.

    Returns a list of dicts with keys: idx, pain_point, solution, area, sheet.
    Joule uses this to know what rows to process.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        xl = pd.ExcelFile(input_path)
    sheet = _find_pain_point_sheet(xl)
    df = xl.parse(sheet, dtype=str)
    # If first row contains actual headers, re-read skipping the title row
    first_row = df.iloc[0].astype(str).str.strip().str.lower()
    if any(v in first_row.values for v in ("solution", "pain point", "observation/pain point")):
        df = xl.parse(sheet, dtype=str, header=1)
    df.columns = df.columns.astype(str)
    df = drop_ignored_columns(df)
    df = normalise_columns(df)

    if "pain_point" not in df.columns:
        raise ValueError("Input file must contain an 'Observation / Pain Point' column.")
    if "solution" not in df.columns:
        raise ValueError("Input file must contain a 'Solution' column.")

    # Pre-process all rows into dicts, filtering out blanks
    raw_rows = []
    for idx, row in df.iterrows():
        pain_point = clean_str(row.get("pain_point"))
        solution   = clean_str(row.get("solution"))
        if not pain_point or not solution:
            continue
        solution_matched = next(
            (s for s in VALID_SOLUTIONS if s.lower() == solution.lower()),
            solution,
        )
        raw_rows.append({
            "idx":           int(idx),
            "pain_point":    pain_point,
            "solution":      solution_matched,
            "solution_area": normalise_solution_area(clean_str(row.get("solution_area"))),
            "sheet":         sheet,
        })

    # Detect language for all rows in parallel
    import concurrent.futures

    # Spanish indicators for context-based fallback on short texts
    _ES_INDICATORS = {
        "gestión", "gestion", "integración", "integracion", "configuración",
        "configuracion", "aprobación", "aprobacion", "administración",
        "proveedor", "proveedores", "compras", "contrato", "contratos",
        "factura", "solicitud", "catálogo", "catalogo", "licitación",
        "autoría", "autoria", "reportes", "análisis", "analisis",
    }

    def _detect(row: dict) -> str:
        pain_point = row["pain_point"]
        solution_area = row.get("solution_area") or ""

        # For short texts, langdetect is unreliable — use context heuristics
        if len(pain_point) < 20:
            # Check if solution_area or pain_point contains Spanish indicators
            combined = (pain_point + " " + solution_area).lower()
            if any(ind in combined for ind in _ES_INDICATORS):
                return "es"
            # Check for common Spanish characters
            if any(c in pain_point for c in "áéíóúñ¿¡"):
                return "es"
            return "en"

        try:
            detected = _detect_lang(pain_point)
            # langdetect sometimes confuses short Spanish for Portuguese or Italian
            # If solution_area is clearly Spanish, trust that over langdetect
            if detected in ("pt", "it", "ca", "gl") and solution_area:
                area_lower = solution_area.lower()
                if any(ind in area_lower for ind in _ES_INDICATORS):
                    return "es"
            return detected
        except LangDetectException:
            # Fallback: check context
            if solution_area and any(ind in solution_area.lower() for ind in _ES_INDICATORS):
                return "es"
            return "en"

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(raw_rows), 8)) as pool:
        langs = list(pool.map(_detect, raw_rows))

    rows = []
    for r, lang in zip(raw_rows, langs):
        rows.append({**r, "language": lang})

    return rows


# ---------------------------------------------------------------------------
# HANA vector search
# ---------------------------------------------------------------------------
def retrieve_similar_cases(
    pain_point: str,
    solution: str,
    area: str | None = None,
    top_k: int = 3,
) -> list[dict]:
    """
    Embed pain_point and search HANA for similar historical cases.

    Returns a list of up to top_k dicts with keys:
    pain_point, comments, recommendations, category, effort, benefits, timeline, impact.
    Returns [] if nothing similar is found — Joule handles that case.
    """
    query_vector = embed_text(pain_point)
    vector_str   = "[" + ",".join(str(v) for v in query_vector) + "]"
    area_filter  = "AND SOLUTION_AREA = ?" if area else ""

    # Vector param goes first (used in SELECT COSINE_SIMILARITY), then WHERE params
    positional = [vector_str, solution]
    if area:
        positional.append(area)

    conn   = hana_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            VECTOR_SEARCH_SQL.format(top_k=top_k, area_filter=area_filter),
            positional,
        )
        cols = ["id", "pain_point", "solution_area", "recommendation", "category",
                "effort", "benefits", "timeline", "impact", "score",
                "use_count", "quality_score"]
        raw = [dict(zip(cols, row)) for row in cursor.fetchall()]

        # Filter out cases below the similarity threshold
        raw = [r for r in raw if (r.get("score") or 0) >= _SIMILARITY_THRESHOLD]

        # Compute weighted score: cosine + quality bonus
        # quality_score (0-1) takes priority if set; otherwise use log2(use_count) as proxy
        for r in raw:
            cosine = r.get("score") or 0
            qs = r.get("quality_score") or 0
            quality_bonus = qs if qs > 0 else 0.0
            r["weighted_score"] = cosine + (quality_bonus * _QUALITY_WEIGHT)

        # Re-sort by weighted score (may reorder when cosine scores are close)
        raw.sort(key=lambda r: r["weighted_score"], reverse=True)

        if raw:
            log.info("retrieve_similar_cases: %d cases above threshold %.2f (top score: %.3f, weighted: %.3f)",
                     len(raw), _SIMILARITY_THRESHOLD, raw[0]["score"], raw[0]["weighted_score"])
        else:
            log.info("retrieve_similar_cases: no cases above threshold %.2f for '%s'",
                     _SIMILARITY_THRESHOLD, pain_point[:80])

        # Increment USE_COUNT in a single bulk UPDATE instead of N individual queries
        if raw:
            ids = [r["id"] for r in raw]
            placeholders = ",".join("?" * len(ids))
            cursor.execute(
                f"UPDATE SVA2.PAIN_POINTS SET USE_COUNT = USE_COUNT + 1 WHERE ID IN ({placeholders})",
                ids,
            )
        conn.commit()
    finally:
        cursor.close()
        release_connection(conn)

    rows = [
        {
            "similar_pain_point": r["pain_point"],
            "solution_area":      r["solution_area"],
            "category":           r["category"],
            "effort":             r["effort"],
            "timeline":           r["timeline"],
            "impact":             r["impact"],
            "similarity_score":   round(r["score"], 3),
        }
        for r in raw
    ]
    return rows


def retrieve_similar_cases_batch(
    items: list[dict],
    top_k: int = 3,
) -> list[dict]:
    """
    Embed and search HANA for all pain points in parallel using a single connection.

    items: list of {idx, pain_point, solution, area (optional)}
    Returns: list of {idx, cases: [...]} in the same order as items.
    Includes circuit breaker: if >50% of tasks fail, aborts remaining and returns partial results.
    """
    if not items:
        return []

    import concurrent.futures

    def _search_one(item):
        cases = retrieve_similar_cases(
            pain_point=item["pain_point"],
            solution=item["solution"],
            area=item.get("area"),
            top_k=top_k,
        )
        return {"idx": item["idx"], "cases": cases}

    results = []
    failures = 0
    total = len(items)
    circuit_broken = False

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(total, 10)) as pool:
        future_to_item = {pool.submit(_search_one, item): item for item in items}

        for future in concurrent.futures.as_completed(future_to_item):
            item = future_to_item[future]
            try:
                result = future.result(timeout=30)
                results.append(result)
            except Exception as e:
                failures += 1
                log.warning("retrieve_similar_cases_batch: failure for idx=%s: %s",
                            item.get("idx"), e)
                results.append({"idx": item["idx"], "cases": [], "error": str(e)})

                # Circuit breaker: if >50% failed, cancel remaining futures
                if failures > total / 2 and not circuit_broken:
                    circuit_broken = True
                    log.error("CIRCUIT BREAKER: %d/%d failures — cancelling remaining tasks", failures, total)
                    for f in future_to_item:
                        f.cancel()
                    # Fill remaining with empty results
                    submitted_idxs = {r["idx"] for r in results}
                    for remaining_item in items:
                        if remaining_item["idx"] not in submitted_idxs:
                            results.append({
                                "idx": remaining_item["idx"],
                                "cases": [],
                                "error": "circuit_breaker_triggered",
                            })
                    break

    # Sort by idx to maintain consistent order
    results.sort(key=lambda r: r.get("idx", 0))

    # Telemetry: log average similarity score across the batch for corpus health monitoring
    all_scores = [c["similarity_score"] for r in results for c in r["cases"] if "similarity_score" in c]
    if all_scores:
        avg_score = sum(all_scores) / len(all_scores)
        empty_count = sum(1 for r in results if not r["cases"])
        log.info("BATCH TELEMETRY: %d items | avg_similarity=%.3f | min=%.3f | max=%.3f | empty=%d/%d",
                 len(items), avg_score, min(all_scores), max(all_scores), empty_count, len(items))
    else:
        log.warning("BATCH TELEMETRY: %d items | ALL returned empty (no cases above threshold %.2f)",
                    len(items), _SIMILARITY_THRESHOLD)

    return results


# ---------------------------------------------------------------------------
# Excel writer — persists Joule-synthesized results
# ---------------------------------------------------------------------------
def write_excel_output(
    input_path: str,
    rows: list[dict],
    output_path: str | None = None,
) -> str:
    """
    Generate a new fixed-format Excel file with Joule-synthesized recommendations.

    Does NOT modify the input file. Creates a fresh xlsx with columns:
    Pain Point | Solution | Solution Area (if present) | Recommendations |
    Category | Effort | Timeline | Benefits | Documentation |
    Impact | Ariba Next-Gen | Value KPIs

    Args:
        input_path:  Path to the original .xlsx (used only to derive output filename).
        rows:        List of row dicts from Joule. Each dict must have:
                       - idx (int): row index from read_excel_painpoints
                       - pain_point, solution (from read_excel_painpoints output)
                       - Any subset of output fields
                     Documentation should be a list of {"title": ..., "url": ...} dicts.
        output_path: Optional output path. Defaults to <input>_RECOMMENDED.xlsx in Downloads.

    Returns:
        Absolute path to the written output file.
    """
    if output_path is None:
        stem = pathlib.Path(input_path).stem
        downloads = pathlib.Path.home() / "Downloads"
        output_path = str(downloads / f"{stem}_RECOMMENDED.xlsx")
    else:
        output_path = str(pathlib.Path(output_path).expanduser().resolve())

    # Normalise row keys: lowercase + replace _ with space
    rows = [{k.lower().replace("_", " "): v for k, v in r.items()} for r in rows]
    rows = sorted(rows, key=lambda r: r.get("idx") or 0)

    # --- Backfill pain_point / solution from the source Excel when Joule omits them ---
    # After key normalisation above, pain_point becomes "pain point" (space, not underscore)
    _missing_pp  = any(not r.get("pain point") for r in rows)
    _missing_sol = any(not r.get("solution") for r in rows)
    if _missing_pp or _missing_sol:
        try:
            _source_rows = {
                int(sr["idx"]): sr
                for sr in read_excel_painpoints(input_path)
            }
            for r in rows:
                idx = r.get("idx")
                if idx is not None and int(idx) in _source_rows:
                    sr = _source_rows[int(idx)]
                    if not r.get("pain point"):
                        r["pain point"] = sr.get("pain_point", "")
                    if not r.get("solution"):
                        r["solution"] = sr.get("solution", "")
                    if not r.get("solution area") and sr.get("solution_area"):
                        r["solution area"] = sr.get("solution_area")
        except Exception as e:
            log.warning("backfill pain_point/solution failed: %s", e, exc_info=True)

    # --- Expand short Effort / Timeline labels to full descriptive strings ---
    _EFFORT_MAP = {
        "low":     "Low (1 – 3 Days)",
        "medium":  "Medium (1 – 3 Weeks)",
        "high":    "High (1 – 2 Months)",
        "complex": "Complex (3+ Months)",
        "n/a":     "N/A",
        # Spanish aliases
        "bajo":    "Low (1 – 3 Days)",
        "medio":   "Medium (1 – 3 Weeks)",
        "alta":    "High (1 – 2 Months)",
        "alto":    "High (1 – 2 Months)",
        "complejo": "Complex (3+ Months)",
        # Portuguese aliases
        "baixo":   "Low (1 – 3 Days)",
        "médio":   "Medium (1 – 3 Weeks)",
        "medio":   "Medium (1 – 3 Weeks)",
        "alto":    "High (1 – 2 Months)",
        "complexo": "Complex (3+ Months)",
    }
    _TIMELINE_MAP = {
        "quick win":   "Quick Win (Within 1 week)",
        "short term":  "Short Term (1 – 3 Weeks)",
        "mid term":    "Mid Term (1 – 3 Months)",
        "long term":   "Long Term (3+ Months)",
        # Spanish aliases
        "rapido":      "Quick Win (Within 1 week)",
        "rápido":      "Quick Win (Within 1 week)",
        "corto plazo": "Short Term (1 – 3 Weeks)",
        "mediano plazo": "Mid Term (1 – 3 Months)",
        "largo plazo": "Long Term (3+ Months)",
        # Portuguese aliases
        "rápido":      "Quick Win (Within 1 week)",
        "curto prazo": "Short Term (1 – 3 Weeks)",
        "médio prazo": "Mid Term (1 – 3 Months)",
        "medio prazo": "Mid Term (1 – 3 Months)",
        "longo prazo": "Long Term (3+ Months)",
    }
    for r in rows:
        for field, mapping in (("effort", _EFFORT_MAP), ("timeline", _TIMELINE_MAP)):
            val = r.get(field)
            if val:
                r[field] = mapping.get(val.strip().lower(), val)

    # --- Validate Effort, Timeline, Category values against valid taxonomy ---
    _VALID_EFFORTS = {
        "Low (1 – 3 Days)", "Medium (1 – 3 Weeks)", "High (1 – 2 Months)",
        "Complex (3+ Months)", "N/A",
    }
    _VALID_TIMELINES = {
        "Quick Win (Within 1 week)", "Short Term (1 – 3 Weeks)",
        "Mid Term (1 – 3 Months)", "Long Term (3+ Months)",
    }
    _VALID_CATEGORIES = {
        "Feature Adoption", "Innovation", "Q&A", "Process Change",
        "Training", "Roadmap Discussion",
    }
    # Common misspellings / case variants → canonical form
    _CATEGORY_ALIASES = {
        "feature adoption": "Feature Adoption",
        "innovation": "Innovation",
        "q&a": "Q&A",
        "qa": "Q&A",
        "process change": "Process Change",
        "training": "Training",
        "roadmap discussion": "Roadmap Discussion",
        "roadmap": "Roadmap Discussion",
        "configuration": "Feature Adoption",
        "adopción de funcionalidad": "Feature Adoption",
        "innovación": "Innovation",
        "cambio de proceso": "Process Change",
        "capacitación": "Training",
        "entrenamiento": "Training",
    }
    for r in rows:
        idx = r.get("idx", "?")
        # Validate Effort
        effort = r.get("effort")
        if effort and effort not in _VALID_EFFORTS:
            log.warning("Row %s: invalid Effort value '%s' — writing as-is", idx, effort)
        # Validate Timeline
        timeline = r.get("timeline")
        if timeline and timeline not in _VALID_TIMELINES:
            log.warning("Row %s: invalid Timeline value '%s' — writing as-is", idx, timeline)
        # Validate and correct Category
        category = r.get("category")
        if category:
            if category not in _VALID_CATEGORIES:
                corrected = _CATEGORY_ALIASES.get(category.strip().lower())
                if corrected:
                    log.info("Row %s: corrected Category '%s' → '%s'", idx, category, corrected)
                    r["category"] = corrected
                else:
                    log.warning("Row %s: invalid Category value '%s' — writing as-is", idx, category)

    # --- Strip blocked / generic documentation URLs ---
    for r in rows:
        docs = r.get("documentation")
        if docs:
            log.info("   RAW documentation (idx=%s): %r", r.get("idx"), docs)
        if isinstance(docs, list):
            kept = []
            for d in docs:
                url = d.get("url", "") if isinstance(d, dict) else d
                if _is_blocked(str(url)):
                    log.info("   BLOCKED url (list): %s", url)
                else:
                    kept.append(d)
            r["documentation"] = kept or None
        elif isinstance(docs, str):
            # String form: filter line by line
            lines = []
            for ln in docs.splitlines():
                if _is_blocked(ln):
                    log.info("   BLOCKED url (str): %s", ln)
                else:
                    lines.append(ln)
            r["documentation"] = "\n".join(lines) if lines else None

    # --- Validate documentation URLs (resolve help.sap.com, HTTP check others) ---
    for r in rows:
        docs = r.get("documentation")
        if isinstance(docs, list) and docs:
            r["documentation"] = _validate_documentation_urls(docs) or None
        elif isinstance(docs, str) and docs.strip():
            # Extract URLs from text lines, validate, keep only valid lines
            lines = docs.splitlines()
            url_lines = []
            for ln in lines:
                urls_in_line = re.findall(r'https?://[^\s\)]+', ln)
                if urls_in_line:
                    url_lines.append((ln, urls_in_line[0]))
                else:
                    url_lines.append((ln, None))
            # Validate all URLs in parallel
            urls_to_check = [u for _, u in url_lines if u]
            alive_set = set()
            if urls_to_check:
                with ThreadPoolExecutor(max_workers=min(len(urls_to_check), 5)) as pool:
                    def _check_str_url(u):
                        if "help.sap.com" in u:
                            return (u, _validate_help_url(u) is not None)
                        return (u, _url_is_alive(u))
                    futures = {pool.submit(_check_str_url, u): u for u in urls_to_check}
                    for fut in as_completed(futures):
                        url_val, is_ok = fut.result()
                        if is_ok:
                            alive_set.add(url_val)
                        else:
                            log.info("   DEAD url (validation failed): %s", url_val)
            kept_lines = []
            for ln, url in url_lines:
                if url is None or url in alive_set:
                    kept_lines.append(ln)
            r["documentation"] = "\n".join(kept_lines) if kept_lines else None

    # Determine output columns — inject Solution Area after Solution if any row has it
    # Keys must match the normalised form (lowercase, underscores → spaces)
    output_cols = [
        ("Pain Point",      "pain point"),
        ("Solution",        "solution"),
    ]
    if any(r.get("solution area") for r in rows):
        output_cols.append(("Solution Area", "solution area"))
    output_cols += [
        ("Recommendations", "recommendations"),
        ("Category",        "category"),
        ("Effort",          "effort"),
        ("Timeline",        "timeline"),
        ("Benefits",        "benefits"),
        ("Documentation",   "documentation"),
        ("Impact",          "impact"),
        ("Ariba Next-Gen",  "ariba next-gen"),
        ("Value KPIs",      "value kpis"),
    ]

    # Corporate style (Quick Reference Card spec)
    FONT = "Arial"
    hdr_font  = Font(name=FONT, size=11, bold=True, color="FFFFFF")
    hdr_fill  = openpyxl.styles.PatternFill("solid", fgColor="00144A")
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    even_font = Font(name=FONT, size=10, color="444444")
    even_fill = openpyxl.styles.PatternFill("solid", fgColor="D1EFFF")
    odd_font  = Font(name=FONT, size=10, color="444444")
    odd_fill  = openpyxl.styles.PatternFill("solid", fgColor="FFFFFF")
    cell_align = Alignment(vertical="top", wrap_text=True, horizontal="left")

    from openpyxl.styles import Border, Side
    from openpyxl.comments import Comment
    outer = Side(style="medium", color="002A86")
    inner = Side(style="thin",   color="EAECEE")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SVA Analysis"

    # Data rows (computed before _border so total_rows is accurate)
    valid_rows = [r for r in rows if r.get("idx") is not None]
    total_rows = 1 + len(valid_rows)

    n_cols = len(output_cols)

    def _border(row_i, col_i):
        return Border(
            left   = outer if col_i == 1          else inner,
            right  = outer if col_i == n_cols     else inner,
            top    = outer if row_i == 1          else inner,
            bottom = outer if row_i == total_rows else inner,
        )

    # Header row
    col_widths = {"Pain Point": 45, "Solution": 20, "Solution Area": 25,
                  "Recommendations": 55, "Category": 18, "Effort": 22,
                  "Timeline": 22, "Benefits": 45, "Documentation": 45,
                  "Impact": 14, "Ariba Next-Gen": 45, "Value KPIs": 45}

    for ci, (label, _) in enumerate(output_cols, start=1):
        cell = ws.cell(row=1, column=ci, value=label)
        cell.font      = hdr_font
        cell.fill      = hdr_fill
        cell.alignment = hdr_align
        cell.border    = _border(1, ci)
        ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = col_widths.get(label, 30)
        # Add explanatory note to Impact header
        if label == "Impact":
            cell.comment = Comment(
                "This column is intentionally left for the consultant to fill.\n"
                "Impact must be assessed based on the client's specific context,\n"
                "priorities, and business processes. Values: High / Medium / Low / N/A",
                "SVA System",
                width=280,
                height=80,
            )

    ws.row_dimensions[1].height = 30

    rows_written = 0
    for ri, row in enumerate(valid_rows, start=2):
        is_even  = (ri % 2 == 0)
        row_font = even_font if is_even else odd_font
        row_fill = even_fill if is_even else odd_fill

        for ci, (label, key) in enumerate(output_cols, start=1):
            value = row.get(key)

            # Format Documentation list into plain text
            if label == "Documentation" and isinstance(value, list):
                parts = []
                for item in value:
                    if isinstance(item, dict):
                        title = item.get("title", "").strip()
                        url   = item.get("url", "").strip()
                        block = []
                        if title: block.append(title)
                        if url:   block.append(url)
                        if block: parts.append("\n".join(block))
                    else:
                        parts.append(str(item).strip())
                value = "\n\n".join(parts) if parts else None
            elif isinstance(value, list):
                value = "\n".join(str(v) for v in value)

            cell = ws.cell(row=ri, column=ci, value=str(value) if value else "")
            cell.font      = row_font
            cell.fill      = row_fill
            cell.alignment = cell_align
            cell.border    = _border(ri, ci)

            # Strip hyperlinks from Documentation cells
            if label == "Documentation":
                cell.hyperlink = None

        rows_written += 1

    ws.freeze_panes = "A2"
    wb.save(output_path)
    return output_path, rows_written


# ---------------------------------------------------------------------------
# Knowledge base retrieval — batch variant
# ---------------------------------------------------------------------------
def retrieve_knowledge_context_batch(
    items: list[dict],
    source_types: list[str] | None = None,
    top_k: int = 5,
) -> dict:
    """
    Search SVA2.KNOWLEDGE_BASE for all items in parallel.

    items: list of {idx, pain_point, solution}
    source_types: knowledge sources to query (default ["next_gen", "vlm_kpis"])

    Returns a dict keyed by idx. Each value is the same structure as
    retrieve_knowledge_context: {source_type: [entries, ...], ...}
    """
    import concurrent.futures

    if source_types is None:
        source_types = ["next_gen", "current_gen", "vlm_kpis"]

    def _process_one(item):
        idx        = item["idx"]
        pain_point = item["pain_point"]
        solution   = item["solution"]
        results    = retrieve_knowledge_context(pain_point, solution, source_types, top_k=top_k)
        return idx, results

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(items), 10)) as pool:
        pairs = list(pool.map(_process_one, items))

    return {idx: results for idx, results in pairs}



def retrieve_knowledge_context(
    pain_point: str,
    solution: str,
    source_types: list[str],
    top_k: int = 3,
    recommendation_hint: str = "",
) -> dict:
    """
    Search SVA2.KNOWLEDGE_BASE for each source_type in parallel.

    Returns a dict keyed by source_type, each value a list of matching entries:
    [{title, content, solution, release, agent_based, joule_based}, ...]
    Empty list means no relevant entries found for that source type.
    """
    import concurrent.futures
    import logging
    log = logging.getLogger("sva2")

    # Build embedding query matching KB ingestion format (title — content)
    # When recommendation_hint is available, use it as enriched query for better semantic alignment
    embed_query = f"{pain_point} — {recommendation_hint}" if recommendation_hint else pain_point

    try:
        query_vector = embed_text(embed_query)
    except Exception as e:
        log.error("   retrieve_knowledge_context: embed_text failed: %s", e, exc_info=True)
        return {st: [] for st in source_types}

    vector_str   = "[" + ",".join(str(v) for v in query_vector) + "]"

    # Release relevance bonus: features in current/next quarter get a small score boost
    from datetime import date as _date
    _today = _date.today()
    _current_q = f"Q{(_today.month - 1) // 3 + 1}"
    _current_year = str(_today.year)
    _RELEASE_BONUS = 0.03

    def _release_bonus(release: str | None) -> float:
        """Return bonus for features releasing in the current quarter."""
        if not release:
            return 0.0
        r = release.upper()
        if _current_q in r and _current_year in r:
            return _RELEASE_BONUS
        return 0.0

    def _search_one(source_type: str) -> tuple[str, list[dict]]:
        cols = ["title", "content", "solution", "release", "agent_based", "joule_based",
                "value_driver", "value_lever", "kpi_id", "kpi_category", "kpi_target",
                "capability", "kpi_formula", "kpi_meas_freq", "score"]

        # For vlm_kpis, filter by solution strictly (no fallback).
        # For next_gen, run DUAL queries: filtered + global, merge by score.
        # For workshop, always global (85% of rows have SOLUTION=NULL).
        try:
            conn   = hana_connection()
            cursor = conn.cursor()
            try:
                if source_type == "vlm_kpis" and solution:
                    cursor.execute(
                        KNOWLEDGE_SEARCH_SQL.format(top_k=top_k, solution_filter="AND SOLUTION = ?"),
                        [vector_str, source_type, solution],
                    )
                    rows = [dict(zip(cols, row)) for row in cursor.fetchall()]

                elif source_type in ("next_gen", "current_gen") and solution:
                    # Single query: get both solution-specific AND cross-cutting (NULL) entries
                    # ranked by cosine similarity. Avoids 2 round-trips.
                    cursor.execute(
                        KNOWLEDGE_SEARCH_SQL.format(
                            top_k=top_k,
                            solution_filter="AND (SOLUTION = ? OR SOLUTION IS NULL)",
                        ),
                        [vector_str, source_type, solution],
                    )
                    rows = [dict(zip(cols, row)) for row in cursor.fetchall()]

                else:
                    # No solution or unknown source_type — global search
                    cursor.execute(
                        KNOWLEDGE_SEARCH_SQL.format(top_k=top_k, solution_filter=""),
                        [vector_str, source_type],
                    )
                    rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
            finally:
                cursor.close()
                release_connection(conn)

            # Apply release relevance bonus for next_gen/current_gen entries (current quarter features rank higher)
            if source_type in ("next_gen", "current_gen"):
                for row in rows:
                    row["score"] = (row.get("score") or 0) + _release_bonus(row.get("release"))
                rows.sort(key=lambda r: r.get("score", 0), reverse=True)

            # Remove score from final output — internal ranking only
            for row in rows:
                row.pop("score", None)

            return source_type, rows
        except Exception as e:
            log.error("   _search_one[%s] failed: %s", source_type, e, exc_info=True)
            return source_type, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(source_types)) as pool:
        results = dict(pool.map(_search_one, source_types))

    # Deduplicate entries within each source_type by title (exact match)
    for st in results:
        seen_titles: set[str] = set()
        deduped = []
        for entry in results[st]:
            title = (entry.get("title") or "").strip().lower()
            if title and title in seen_titles:
                continue
            if title:
                seen_titles.add(title)
            deduped.append(entry)
        results[st] = deduped

    return results
