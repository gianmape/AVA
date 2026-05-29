"""
ingestion/ingest_knowledge.py
------------------------------
Ingest internal knowledge Excel files into SVA2.KNOWLEDGE_BASE.

Embeds title + description together for richer semantic matching.
Designed to handle multiple source types (next_gen, ai_scenarios, premium_services).

Usage:
    python ingestion/ingest_knowledge.py data/legacy/SAP_Ariba_NextGen_Features.xlsx
    python ingestion/ingest_knowledge.py data/legacy/SAP_Ariba_NextGen_Features.xlsx --source-type next_gen
"""
import sys
import os
import argparse
import pathlib
import warnings
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from shared.config import clean_str, embed_text, hana_connection

# ---------------------------------------------------------------------------
# Solution normalisation — maps Next-gen file naming to VALID_SOLUTIONS
# ---------------------------------------------------------------------------
SOLUTION_MAP = {
    "sap ariba suite":                       "SAP Ariba Suite",
    "sap ariba launchpad":                   "SAP Ariba Launchpad",
    "sap ariba intake management":           "SAP Ariba Intake Management",
    "sap ariba buying":                      "Ariba Buying",
    "sap ariba sourcing":                    "Ariba Sourcing",
    "sap ariba contracts":                   "Ariba Contracts",
    "sap ariba invoicing":                   "Ariba Invoice",
    "sap ariba supplier management":         "Ariba SLP",
    "sap ariba spend analysis and insights": "Spend Analysis",
    "sap ariba category management":         "SAP Ariba Category Management",
    "sap business network":                  "Business Network",
    "sap spend control tower":               "SAP Spend Control Tower",
}


def normalise_kb_solution(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower()
    return SOLUTION_MAP.get(key, raw.strip())


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
INSERT_SQL = """
INSERT INTO SVA2.KNOWLEDGE_BASE
    (SOURCE_TYPE, SOLUTION, TITLE, CONTENT, RELEASE,
     AGENT_BASED, JOULE_BASED, SOURCE_FILE, EMBEDDING)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, TO_REAL_VECTOR(?))
"""


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def ingest_file(filepath: str, source_type: str):
    print(f"\n── Ingesting: {filepath}  [source_type={source_type}]")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_excel(filepath, dtype=str)

    # Normalise column names to lowercase stripped keys
    df.columns = [str(c).strip() for c in df.columns]

    # Map known column names
    col_map = {
        "SAP Ariba Solution / Product":       "solution",
        "Feature or Functionality Announced": "title",
        "Description":                        "content",
        "Release":                            "release",
        "Agent-based":                        "agent_based",
        "Joule-based":                        "joule_based",
    }
    df = df.rename(columns=col_map)

    required = {"solution", "title", "content"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    conn   = hana_connection()
    cursor = conn.cursor()

    inserted = skipped = 0

    for _, raw in df.iterrows():
        title   = clean_str(raw.get("title"))
        content = clean_str(raw.get("content"))

        if not title or not content:
            skipped += 1
            continue

        solution    = normalise_kb_solution(clean_str(raw.get("solution")))
        release     = clean_str(raw.get("release"))
        agent_based = clean_str(raw.get("agent_based"))
        joule_based = clean_str(raw.get("joule_based"))

        # Embed title + content together for richer semantic matching
        embed_text_value = f"{title} — {content}"
        try:
            vector = embed_text(embed_text_value)
        except Exception as e:
            print(f"  Warning: embedding failed, skipping row '{title[:60]}': {e}")
            skipped += 1
            continue

        vector_str = "[" + ",".join(str(v) for v in vector) + "]"

        cursor.execute(INSERT_SQL, (
            source_type,
            solution,
            title,
            content,
            release,
            agent_based,
            joule_based,
            os.path.basename(filepath),
            vector_str,
        ))

        inserted += 1
        if inserted % 10 == 0:
            conn.commit()
            print(f"  {inserted} rows committed…")

    conn.commit()
    cursor.close()
    conn.close()

    print(f"  Done — inserted: {inserted}, skipped: {skipped}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest knowledge Excel into SVA2.KNOWLEDGE_BASE")
    parser.add_argument("files", nargs="+", help="Excel file(s) to ingest")
    parser.add_argument(
        "--source-type",
        default="next_gen",
        choices=["next_gen", "ai_scenarios", "premium_services"],
        help="Knowledge source type (default: next_gen)",
    )
    args = parser.parse_args()

    for f in args.files:
        if not pathlib.Path(f).exists():
            print(f"File not found: {f}")
            sys.exit(1)
        ingest_file(f, args.source_type)

    print("\nIngestion complete.")