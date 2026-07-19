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

INSERT_VLM_SQL = """
INSERT INTO SVA2.KNOWLEDGE_BASE
    (SOURCE_TYPE, SOLUTION, TITLE, CONTENT,
     VALUE_DRIVER, VALUE_LEVER, KPI_ID, KPI_CATEGORY, KPI_TARGET,
     CAPABILITY, KPI_FORMULA, KPI_MEAS_FREQ,
     SOURCE_FILE, EMBEDDING)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TO_REAL_VECTOR(?))
"""

# VLM column mapping: Excel header → internal key
# Supports both file formats (SAP_Ariba_Sourcing_VLM_Index and SAP_Ariba_VLM_Index_Complete)
VLM_COL_MAP = {
    # Common columns
    "Pain Point":                                  "pain_point",
    "Value Driver":                                "value_driver",
    "Value Driver (VLM / Signavio PI)":            "value_driver",
    "Value Driver (VLM / Signavio Pi)":            "value_driver",
    "Value Lever":                                 "value_lever",
    "KPI Name":                                    "title",
    "KPI Category":                                "kpi_category",
    # Format A (Sourcing-only legacy file)
    "KPI ID":                                      "kpi_id",
    "Formula / Measure":                           "kpi_formula",
    "Typical Target / Benchmark":                  "kpi_target",
    "Ariba Sourcing Capability / Recommendation":  "capability",
    "KPI Reference (SAP KPI Catalog)":             "kpi_target",
    # Format B (Complete multi-solution file — SAP_Ariba_VLM_Index_Complete)
    "APM KPI ID":                                  "kpi_id",
    "KPI Definition / What it Measures":           "content",
    "KPI Definition / Formula":                    "content",
    "Formula / Calculation":                       "kpi_formula",
    "Ariba Capability / Recommendation":           "capability",
    "Measurement Frequency":                       "kpi_meas_freq",
    "Measurement Freq.":                           "kpi_meas_freq",
    "KPI Catalog Link":                            "kpi_target",
}

# Sheet name → canonical solution name (for multi-solution VLM files)
VLM_SHEET_SOLUTION_MAP = {
    "ariba sourcing":           "Ariba Sourcing",
    "ariba buying & invoicing": "Ariba Buying",
    "ariba contracts":          "Ariba Contracts",
    "ariba slp":                "Ariba SLP",
    "ariba risk":               "Ariba Supplier Risk",
    # Additional modules
    "ariba catalog":            "Ariba Catalog",
    "ariba guided buying":      "Ariba Guided Buying",
    "ariba invoice":            "Ariba Invoice",
    "ariba reporting":          "Ariba Reporting",
    "ariba sipm":               "Ariba SIPM",
    "ariba supplier risk":      "Ariba Supplier Risk",
    "business network":         "Business Network",
    "commerce automation":      "Commerce Automation",
    "spend analysis":           "Spend Analysis",
}


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
def ingest_file(filepath: str, source_type: str):
    print(f"\n── Ingesting: {filepath}  [source_type={source_type}]")

    if source_type == "vlm_kpis":
        _ingest_vlm(filepath)
    else:
        _ingest_knowledge(filepath, source_type)


def _ingest_knowledge(filepath: str, source_type: str):
    """Ingest next_gen / ai_scenarios / premium_services Excel files."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = pd.read_excel(filepath, dtype=str)

    df.columns = [str(c).strip() for c in df.columns]

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


def _ingest_vlm(filepath: str):
    """
    Ingest a VLM KPI Index Excel file into SVA2.KNOWLEDGE_BASE as source_type='vlm_kpis'.

    Supports two file formats:
    - Legacy single-sheet (SAP_Ariba_Sourcing_VLM_Index): Ariba Sourcing only, header at row 1.
    - Complete multi-sheet (SAP_Ariba_VLM_Index_Complete): each sheet is a solution, header at row 0.

    Embedding per row: pain_point | value_lever | kpi_name | capability
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        xl = pd.ExcelFile(filepath)

    data_sheets = [s for s in xl.sheet_names if s.lower() != "how to use"]
    is_multi = len(data_sheets) > 1

    conn   = hana_connection()
    cursor = conn.cursor()
    inserted = skipped = 0

    for sheet_name in data_sheets:
        solution = VLM_SHEET_SOLUTION_MAP.get(sheet_name.lower(), "Ariba Sourcing") if is_multi else "Ariba Sourcing"

        # Legacy single-sheet files have a merged title row before headers
        header_row = 0 if is_multi else 1
        df = xl.parse(sheet_name, header=header_row, dtype=str)
        df.columns = [str(c).strip() for c in df.columns]
        df = df.rename(columns=VLM_COL_MAP)
        df = df.dropna(how="all")

        required = {"pain_point", "title", "value_driver", "value_lever"}
        missing = required - set(df.columns)
        if missing:
            print(f"  Warning: sheet '{sheet_name}' missing columns {missing}, skipping.")
            continue

        # content falls back to kpi_formula if not mapped directly
        if "content" not in df.columns and "kpi_formula" in df.columns:
            df["content"] = df["kpi_formula"]

        print(f"  Sheet '{sheet_name}' → {solution} ({len(df)} rows)")

        for _, raw in df.iterrows():
            pain_point    = clean_str(raw.get("pain_point"))
            title         = clean_str(raw.get("title"))
            content       = clean_str(raw.get("content"))
            value_driver  = clean_str(raw.get("value_driver"))
            value_lever   = clean_str(raw.get("value_lever"))
            kpi_id        = clean_str(raw.get("kpi_id"))
            kpi_category  = clean_str(raw.get("kpi_category"))
            kpi_target    = clean_str(raw.get("kpi_target"))
            capability    = clean_str(raw.get("capability"))
            kpi_formula   = clean_str(raw.get("kpi_formula"))
            kpi_meas_freq = clean_str(raw.get("kpi_meas_freq"))

            if not pain_point or not title:
                skipped += 1
                continue

            # Enriched embedding: pain point + lever + KPI name + capability
            embed_parts = [p for p in [pain_point, value_lever, title, capability] if p]
            embed_text_value = " | ".join(embed_parts)
            try:
                vector = embed_text(embed_text_value)
            except Exception as e:
                print(f"  Warning: embedding failed, skipping KPI '{title[:60]}': {e}")
                skipped += 1
                continue

            vector_str = "[" + ",".join(str(v) for v in vector) + "]"

            cursor.execute(INSERT_VLM_SQL, (
                "vlm_kpis",
                solution,
                title,
                content,
                value_driver,
                value_lever,
                kpi_id,
                kpi_category,
                kpi_target,
                capability,
                kpi_formula,
                kpi_meas_freq,
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
        choices=["next_gen", "ai_scenarios", "premium_services", "vlm_kpis"],
        help="Knowledge source type (default: next_gen)",
    )
    args = parser.parse_args()

    for f in args.files:
        if not pathlib.Path(f).exists():
            print(f"File not found: {f}")
            sys.exit(1)
        ingest_file(f, args.source_type)

    print("\nIngestion complete.")