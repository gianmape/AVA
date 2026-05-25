"""
Shared configuration and helpers.

Auth is handled by gen-ai-hub-sdk automatically.
Credentials are loaded from the .env file via python-dotenv,
which sets the AICORE_* environment variables the SDK expects.
No manual OAuth or config.json needed.
"""
import os
from dotenv import load_dotenv
from hdbcli import dbapi

# gen-ai-hub-sdk — install: pip install generative-ai-hub-sdk
from gen_ai_hub.proxy.native.openai import OpenAI as AIHubOpenAI
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

VALID_CATEGORIES = {
    "Feature Adoption", "Innovation", "Q&A", "Process Change", "Roadmap Discussion",
}

VALID_EFFORTS   = {"Low", "Medium", "High", "N/A"}
VALID_TIMELINES = {"Quick Win", "Short Term", "Mid Term", "Long Term", "Ongoing"}
VALID_IMPACTS   = {"Low", "Medium", "High"}

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
    "comments":               "comments",
    "comment":                "comments",
    "solution":               "solution",
    "area":                   "area",
    "recommendations":        "recommendations",
    "recommendation":         "recommendations",
    "category":               "category",
    "effort":                 "effort",
    "benefits":               "benefits",
    "benefit":                "benefits",
    "documentation":          "documentation",
    "links to documentation": "documentation",
    "link to documentation":  "documentation",
    "timeline":               "timeline",
    "impact":                 "impact",
    "12 keys to success":     "keys_to_success",
    "12 keys":                "keys_to_success",
}

# ---------------------------------------------------------------------------
# SDK proxy instances (lazy singletons)
# The SDK reads AI Core credentials from:
#   ~/.aicore/config.json  OR  AICORE_* environment variables
# ---------------------------------------------------------------------------
_embedding_client: AIHubOpenAI | None = None


def get_openai_proxy() -> AIHubOpenAI:
    """Used for text-embedding-ada-002 or text-embedding-3-* deployed on AI Core."""
    global _embedding_client
    if _embedding_client is None:
        pc = get_proxy_client("gen-ai-hub", deployment_id=os.environ["EMBEDDING_DEPLOYMENT_ID"])
        _embedding_client = AIHubOpenAI(proxy_client=pc)
    return _embedding_client


# ---------------------------------------------------------------------------
# Embeddings
# ---------------------------------------------------------------------------
def embed_text(text: str) -> list[float]:
    """Generate an embedding vector via the OpenAI-compatible embedding deployment."""
    proxy = get_openai_proxy()
    response = proxy.embeddings.create(
        model=os.environ["EMBEDDING_MODEL_NAME"],
        input=text,
    )
    return response.data[0].embedding


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
