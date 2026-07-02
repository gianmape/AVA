"""
Shared configuration and helpers.

Auth is handled by gen-ai-hub-sdk automatically.
Credentials are loaded from the .env file via python-dotenv,
which sets the AICORE_* environment variables the SDK expects.
No manual OAuth or config.json needed.
"""
import os
import difflib
import unicodedata
import re
import time as _time
import requests as _requests
from dotenv import load_dotenv
from hdbcli import dbapi

from gen_ai_hub.proxy.core.proxy_clients import get_proxy_client

load_dotenv()


def _normalise_str(s: str) -> str:
    """Lowercase + remove accents for case/accent-insensitive comparisons."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


# ---------------------------------------------------------------------------
# Valid taxonomy values — used in both ingestion (metadata) and query (prompt)
# ---------------------------------------------------------------------------
VALID_SOLUTIONS = {
    "Ariba Buying", "Ariba Catalog", "Commerce Automation",
    "Ariba Contracts", "Business Network", "Ariba Guided Buying",
    "Ariba Invoice", "Ariba Reporting", "Ariba Supplier Risk",
    "Ariba Sourcing", "Spend Analysis", "Ariba SIPM",
    "Ariba SLP",
    # Extended solutions (from IDEAS.md — historical data may use these)
    "Ariba Buying and Invoicing", "Discount Management",
    "Business Network Discovery", "Ariba Overall",
    "Supply Chain Collaboration",
}

VALID_SOLUTION_AREAS = {
    "Gestión de Proyectos y Tareas",
    "Autoría de Contratos",
    "Reportes",
    "Integración",
    "Eventos de Sourcing",
    "Biblioteca de Sourcing",
    "Gestion de Plantillas",
    "Administración",
    "Formulario de Ahorros",
    "Registro de Proveedores",
    "Gestión de Proveedores",
    "Solicitud de Registro de Proveedor",
    "Sourcing Táctico",
    "Ordenes de Compra",
    "Gestión de Catálogos",
    "Solicitud de Ordenes de Compra",
}

VALID_CATEGORIES = {
    "Feature Adoption", "Innovation", "Q&A", "Process Change", "Training", "Roadmap Discussion",
}

VALID_EFFORTS   = {"Low", "Medium", "High", "Complex", "N/A"}
VALID_TIMELINES = {"Quick Win", "Short Term", "Mid Term", "Long Term"}
VALID_IMPACTS   = {"Low", "Medium", "High", "N/A"}

# Pre-computed normalised lookup maps — built once at import time from the
# constants above so normalise_solution / normalise_solution_area never
# reconstruct them on every call.
_SOLUTION_CANONICAL_MAP = {_normalise_str(v): v for v in VALID_SOLUTIONS}
_SOLUTION_AREA_CANONICAL_MAP = {_normalise_str(v): v for v in VALID_SOLUTION_AREAS}
_EXPLICIT_ALIASES = {
    # SLP — long-form names never fuzzy-match "ariba slp"
    "supplier lifecycle performance":       "Ariba SLP",
    "supplier lifecycle and performance":   "Ariba SLP",
    "supplier lifecycle management":        "Ariba SLP",
    "slp":                                  "Ariba SLP",
    # Supplier Risk
    "supplier risk":                        "Ariba Supplier Risk",
    "risk":                                 "Ariba Supplier Risk",
    # Invoice
    "invoice":                              "Ariba Invoice",
    "invoicing":                            "Ariba Invoice",
    # Sourcing
    "sourcing":                             "Ariba Sourcing",
    # Buying
    "buying":                               "Ariba Buying",
    "buying and invoicing":                 "Ariba Buying and Invoicing",
    # Contracts
    "contracts":                            "Ariba Contracts",
    # SIPM
    "sipm":                                 "Ariba SIPM",
    # Others
    "catalog":                              "Ariba Catalog",
    "guided buying":                        "Ariba Guided Buying",
    "spend control tower":                  "Spend Analysis",
    "reporting":                            "Ariba Reporting",
    # Extended solutions
    "discount management":                  "Discount Management",
    "business network discovery":           "Business Network Discovery",
    "network discovery":                    "Business Network Discovery",
    "overall":                              "Ariba Overall",
    "supply chain collaboration":           "Supply Chain Collaboration",
    "supply chain":                         "Supply Chain Collaboration",
}

# Columns to drop — row numbering/IDs from Excel that have no analytical value
IGNORE_COLUMNS = {"id", "no", "no.", "n°", "#", "num", "num.", "número", "numero"}


def drop_ignored_columns(df):
    """Drop Excel row-numbering columns that carry no analytical value."""
    to_drop = [col for col in df.columns if str(col).strip().lower() in IGNORE_COLUMNS]
    return df.drop(columns=to_drop) if to_drop else df
COLUMN_ALIASES = {
    "observation":            "pain_point",
    "observation/pain point": "pain_point",
    "pain point":             "pain_point",
    "paint point":            "pain_point",  # common typo
    "painpoint":              "pain_point",
    "solution":               "solution",
    "area":                   "solution_area",
    "solution area":          "solution_area",
    "recommendation":         "recommendation",
    "recommendations":        "recommendation",
    "category":               "category",
    "effort":                 "effort",
    "benefits":               "benefits",
    "benefit":                "benefits",
    "timeline":               "timeline",
    "impact":                 "impact",
}

# ---------------------------------------------------------------------------
# SDK proxy instances (lazy singletons)
# The SDK reads AI Core credentials from:
#   ~/.aicore/config.json  OR  AICORE_* environment variables
# ---------------------------------------------------------------------------
_embedding_url    = None
_embedding_client = None

import logging as _logging
_log = _logging.getLogger("sva2")


def _init_embedding():
    """
    Build the embedding endpoint URL and proxy client once at startup.

    Constructs the URL directly from env vars to avoid calling
    select_deployment(), which makes a blocking HTTP call to the AI Core API
    with no timeout and hangs indefinitely on Cloud Foundry.

    URL pattern: {AICORE_BASE_URL}/v2/inference/deployments/{DEPLOYMENT_ID}/models/{MODEL}:predict

    NOTE: We cache the URL and proxy client but NOT the auth headers.
    OAuth tokens expire (typically after 12 hours). Calling pc.request_header
    on each embed_text() invocation ensures we always use a fresh token.
    """
    global _embedding_url, _embedding_client
    if _embedding_url is not None:
        return

    base   = os.environ["AICORE_BASE_URL"].rstrip("/")
    dep_id = os.environ["EMBEDDING_DEPLOYMENT_ID"]
    model  = os.environ["EMBEDDING_MODEL_NAME"]
    _embedding_url    = f"{base}/v2/inference/deployments/{dep_id}/models/{model}:predict"
    _embedding_client = get_proxy_client("gen-ai-hub", deployment_id=dep_id)

    _log.info("   _init_embedding: ready — %s", _embedding_url)


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
def embed_text(text: str) -> list[float]:
    """
    Generate an embedding vector via Gemini Embedding deployed on SAP AI Core.

    Gemini Embedding uses the Vertex AI predict format:
      POST .../models/gemini-embedding:predict
      body: {"instances": [{"content": "<text>"}]}

    Retries up to 3 times with exponential backoff on 429 (rate limit) errors,
    which can occur when multiple parallel calls hit the endpoint simultaneously.
    """
    _init_embedding()
    headers = {**dict(_embedding_client.request_header), "Content-Type": "application/json"}
    body = {"instances": [{"content": text}]}

    last_exc = None
    for attempt in range(3):
        if attempt > 0:
            delay = 2 ** attempt  # 2s, 4s
            _log.warning("   embed_text: 429 rate limit — retry %d/3 in %ds", attempt + 1, delay)
            _time.sleep(delay)
        _log.info("   embed_text: POST %s", _embedding_url)
        response = _requests.post(_embedding_url, headers=headers, json=body, timeout=30)
        if response.status_code == 429:
            last_exc = response
            continue
        response.raise_for_status()
        values = response.json()["predictions"][0]["embeddings"]["values"]
        _log.info("   embed_text: OK (%d dims)", len(values))
        return values

    # All retries exhausted — raise the last 429 response as an HTTPError
    last_exc.raise_for_status()


# ---------------------------------------------------------------------------
# HANA Cloud connection pool
# ---------------------------------------------------------------------------
# hdbcli has no built-in pool. We use a queue.Queue of pre-opened connections
# so parallel threads (ThreadPoolExecutor in batch functions) reuse connections
# instead of paying TCP+TLS+HANA auth overhead (~100–300 ms) on every call.
#
# Pool size defaults to 10 — matches max_workers in batch executors (server.py).
# server_cf.py overrides this to 2 via os.environ.setdefault before first use,
# since CF runs single query mode only (sequential, never parallel).
# Override with HANA_POOL_SIZE env var in .env to tune for your deployment.

import queue as _queue
import threading as _threading

_pool: "_queue.Queue[dbapi.Connection] | None" = None
_pool_lock = _threading.Lock()
_pool_size = 0


def _make_connection() -> "dbapi.Connection":
    ssl_validate = os.environ.get("HANA_SSL_VALIDATE", "true").strip().lower() != "false"
    return dbapi.connect(
        address=os.environ["HANA_HOST"],
        port=int(os.environ.get("HANA_PORT", 443)),
        user=os.environ["HANA_USER"],
        password=os.environ["HANA_PASSWORD"],
        encrypt=True,
        sslValidateCertificate=ssl_validate,
    )


def _init_pool() -> None:
    global _pool, _pool_size
    if _pool is not None:
        return
    with _pool_lock:
        if _pool is not None:
            return
        size = int(os.environ.get("HANA_POOL_SIZE", 10))
        _pool_size = size
        q: "_queue.Queue[dbapi.Connection]" = _queue.Queue(maxsize=size)
        for _ in range(size):
            q.put(_make_connection())
        _pool = q
        _log.info("   HANA connection pool initialised — size=%d", size)


def hana_connection() -> "dbapi.Connection":
    """
    Return a pooled HANA connection.

    Usage — callers MUST return the connection when done:
        conn = hana_connection()
        try:
            ...
        finally:
            release_connection(conn)

    The pool is initialised lazily on the first call.
    Broken connections are replaced transparently.
    """
    _init_pool()
    assert _pool is not None
    conn = _pool.get()
    # Validate — replace silently if the connection was dropped
    try:
        conn.isconnected()
    except Exception:
        _log.warning("   HANA pool: stale connection detected, replacing")
        try:
            conn.close()
        except Exception:
            pass
        conn = _make_connection()
    return conn


def release_connection(conn: "dbapi.Connection") -> None:
    """Return a connection to the pool. Call in a finally block."""
    if _pool is not None and not _pool.full():
        _pool.put(conn)
    else:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def normalise_solution(value: str | None) -> str | None:
    """
    Fuzzy-match a raw solution string against VALID_SOLUTIONS.

    Handles variants like "Ariba Sourcing" -> "Sourcing" by also trying
    with the "Ariba " / "SAP Ariba " prefix stripped before fuzzy matching.
    Returns the canonical name if a match is found, otherwise None.
    """
    if not value:
        return None

    # 1. Exact match
    raw_norm = _normalise_str(value)
    if raw_norm in _SOLUTION_CANONICAL_MAP:
        return _SOLUTION_CANONICAL_MAP[raw_norm]

    # 2. Strip common prefixes and try exact match again
    stripped = re.sub(r"^(sap ariba |ariba )", "", raw_norm).strip()
    if stripped in _SOLUTION_CANONICAL_MAP:
        return _SOLUTION_CANONICAL_MAP[stripped]

    # 2b. Explicit aliases for names that fuzzy-match poorly
    if stripped in _EXPLICIT_ALIASES:
        return _EXPLICIT_ALIASES[stripped]
    if raw_norm in _EXPLICIT_ALIASES:
        return _EXPLICIT_ALIASES[raw_norm]

    # 3. Fuzzy match on original (high cutoff to avoid false positives)
    close = difflib.get_close_matches(raw_norm, _SOLUTION_CANONICAL_MAP.keys(), n=1, cutoff=0.82)
    if close:
        return _SOLUTION_CANONICAL_MAP[close[0]]

    # 4. Fuzzy match on stripped prefix version
    close = difflib.get_close_matches(stripped, _SOLUTION_CANONICAL_MAP.keys(), n=1, cutoff=0.82)
    if close:
        return _SOLUTION_CANONICAL_MAP[close[0]]

    return None


def normalise_solution_area(value: str | None) -> str | None:
    """
    Fuzzy-match a raw solution_area string against VALID_SOLUTION_AREAS.

    Matching strategy (in order):
      1. Exact match (case-insensitive, accent-insensitive after stripping)
      2. difflib close match with cutoff=0.75

    Returns the canonical name if a match is found, otherwise the raw value
    (graceful degradation — new areas added later will pass through as-is).
    """
    if not value:
        return value

    raw_norm = _normalise_str(value)

    # 1. Exact accent-insensitive match
    if raw_norm in _SOLUTION_AREA_CANONICAL_MAP:
        return _SOLUTION_AREA_CANONICAL_MAP[raw_norm]

    # 2. Fuzzy match
    close = difflib.get_close_matches(raw_norm, _SOLUTION_AREA_CANONICAL_MAP.keys(), n=1, cutoff=0.75)
    if close:
        return _SOLUTION_AREA_CANONICAL_MAP[close[0]]

    # Graceful degradation: unknown area passes through unchanged
    return value


# Effort label prefixes — maps the start of any raw value to the canonical label.
# Handles: "Low", "Low (1 – 3 Days)", "low", "LOW (anything)", etc.
_EFFORT_PREFIXES = {
    "low":     "Low",
    "medium":  "Medium",
    "high":    "High",
    "complex": "Complex",
    "n/a":     "N/A",
}

# Timeline label prefixes — same strategy.
_TIMELINE_PREFIXES = {
    "quick win":  "Quick Win",
    "short term": "Short Term",
    "mid term":   "Mid Term",
    "long term":  "Long Term",
}


def _normalise_by_prefix(value: str | None, prefix_map: dict[str, str]) -> str | None:
    """
    Strip parenthetical descriptions from Effort / Timeline raw values and return
    the canonical label.

    Strategy:
      1. Strip the string, lowercase, remove anything in parentheses.
      2. Match the result against known label prefixes.
      3. Fall back to graceful degradation if no match.

    Examples:
      "Low (1 – 3 Days)"   → "Low"
      "Quick Win (Within…)" → "Quick Win"
      "medium"              → "Medium"
    """
    if not value:
        return value

    import re
    cleaned = re.sub(r"\(.*?\)", "", value).strip().lower()

    if cleaned in prefix_map:
        return prefix_map[cleaned]

    # Graceful degradation
    return value


def normalise_effort(value: str | None) -> str | None:
    return _normalise_by_prefix(value, _EFFORT_PREFIXES)


def normalise_timeline(value: str | None) -> str | None:
    return _normalise_by_prefix(value, _TIMELINE_PREFIXES)


# Impact label prefixes — same strategy.
# Impact is not indexed in HANA but is normalised for Excel output consistency.
_IMPACT_PREFIXES = {
    "low":    "Low",
    "medium": "Medium",
    "high":   "High",
    "n/a":    "N/A",
}


def normalise_impact(value: str | None) -> str | None:
    return _normalise_by_prefix(value, _IMPACT_PREFIXES)


def normalise_columns(df):
    """Rename DataFrame columns to canonical names using COLUMN_ALIASES."""
    rename = {}
    for col in df.columns:
        key = col.strip().lower()
        if key in COLUMN_ALIASES:
            rename[col] = COLUMN_ALIASES[key]
    return df.rename(columns=rename)


def clean_str(val) -> str | None:
    """Return stripped string or None for empty/NaN values."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s and s.lower() not in ("nan", "none", "") else None
