"""
Shared configuration and helpers.

Auth is handled by gen-ai-hub-sdk automatically.
Credentials are loaded from the .env file via python-dotenv,
which sets the AICORE_* environment variables the SDK expects.
No manual OAuth or config.json needed.
"""
import os
import difflib
import requests as _requests
from dotenv import load_dotenv
from hdbcli import dbapi

from gen_ai_hub.proxy.core.proxy_clients import get_proxy_client

load_dotenv()

# ---------------------------------------------------------------------------
# Valid taxonomy values — used in both ingestion (metadata) and query (prompt)
# ---------------------------------------------------------------------------
VALID_SOLUTIONS = {
    "Ariba Buying", "Ariba Buying and Inv", "Catalog", "Commerce Auto",
    "Contracts", "Discount Mgmnt", "Discovery", "Guided Buying",
    "Integration", "Invoice Mgmnt", "Overall", "Reporting", "Risk",
    "Sourcing", "Spend Analysis", "SIPM", "SLP", "Supply Chain Collaboration",
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
    "Feature Adoption", "Innovation", "Q&A", "Process Change", "Roadmap Discussion",
}

VALID_EFFORTS   = {"Low", "Medium", "High", "Complex", "N/A"}
VALID_TIMELINES = {"Quick Win", "Short Term", "Mid Term", "Long Term"}
VALID_IMPACTS   = {"Low", "Medium", "High", "N/A"}

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
_embedding_client = None


def _get_embedding_proxy():
    global _embedding_client
    if _embedding_client is None:
        _embedding_client = get_proxy_client(
            "gen-ai-hub",
            deployment_id=os.environ["EMBEDDING_DEPLOYMENT_ID"],
        )
    return _embedding_client


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
def embed_text(text: str) -> list[float]:
    """
    Generate an embedding vector via Gemini Embedding deployed on SAP AI Core.

    Gemini Embedding uses the Vertex AI predict format:
      POST .../models/gemini-embedding:predict
      body: {"instances": [{"content": "<text>"}]}
    """
    pc = _get_embedding_proxy()
    deployment = pc.select_deployment(deployment_id=os.environ["EMBEDDING_DEPLOYMENT_ID"])
    url = deployment.url + f"/models/{os.environ['EMBEDDING_MODEL_NAME']}:predict"
    headers = dict(pc.request_header)
    headers["Content-Type"] = "application/json"
    body = {"instances": [{"content": text}]}

    response = _requests.post(url, headers=headers, json=body)
    response.raise_for_status()
    return response.json()["predictions"][0]["embeddings"]["values"]


# ---------------------------------------------------------------------------
# HANA Cloud connection
# ---------------------------------------------------------------------------
def hana_connection():
    return dbapi.connect(
        address=os.environ["HANA_HOST"],
        port=int(os.environ.get("HANA_PORT", 443)),
        user=os.environ["HANA_USER"],
        password=os.environ["HANA_PASSWORD"],
        encrypt=True,
        sslValidateCertificate=False,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
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

    import unicodedata

    def _normalise(s: str) -> str:
        """Lowercase + remove accents for fuzzy comparison."""
        nfkd = unicodedata.normalize("NFKD", s)
        return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()

    raw_norm = _normalise(value)
    canonical_map = {_normalise(v): v for v in VALID_SOLUTION_AREAS}

    # 1. Exact accent-insensitive match
    if raw_norm in canonical_map:
        return canonical_map[raw_norm]

    # 2. Fuzzy match
    close = difflib.get_close_matches(raw_norm, canonical_map.keys(), n=1, cutoff=0.75)
    if close:
        return canonical_map[close[0]]

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
