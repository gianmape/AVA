"""
ingestion/ingest.py
-------------------
Parse one or more legacy Excel files and index them into HANA Cloud Vector Engine.

Usage:
    python ingestion/ingest.py path/to/file.xlsx [path/to/more.xlsx ...]
    python ingestion/ingest.py data/legacy/          # processes all .xlsx in folder
"""
import sys
import os
import pathlib
import warnings
import pandas as pd

# Project root on path
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from shared.config import (
    normalise_columns,
    drop_ignored_columns,
    clean_str,
    embed_text,
    hana_connection,
    VALID_SOLUTIONS,
    normalise_solution_area,
    normalise_effort,
    normalise_timeline,
    normalise_impact,
)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
INSERT_SQL = """
INSERT INTO SVA2.PAIN_POINTS
    (SOURCE_FILE, PAIN_POINT, SOLUTION, SOLUTION_AREA, CATEGORY,
     RECOMMENDATION, EFFORT, TIMELINE, BENEFITS, IMPACT, SOURCE_TYPE,
     EMBEDDING)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TO_REAL_VECTOR(?))
"""


# ---------------------------------------------------------------------------
# Row processing
# ---------------------------------------------------------------------------
def process_row(row: dict, source_file: str) -> dict | None:
    """
    Validate and clean a single row from a legacy Excel file.
    Returns None if the row is missing required fields.
    """
    pain_point = clean_str(row.get("pain_point"))
    solution   = clean_str(row.get("solution"))

    # Both required — skip row if absent
    if not pain_point or not solution:
        return None

    # Normalise Solution against known values (case-insensitive match)
    solution_matched = next(
        (s for s in VALID_SOLUTIONS if s.lower() == solution.lower()),
        solution,  # keep raw value if not in list (graceful degradation)
    )

    return {
        "source_file":   source_file,
        "pain_point":    pain_point,
        "solution":      solution_matched,
        "solution_area": normalise_solution_area(clean_str(row.get("solution_area"))),
        "category":      clean_str(row.get("category")),
        "recommendation": clean_str(row.get("recommendation")),
        "effort":        normalise_effort(clean_str(row.get("effort"))),
        "timeline":      normalise_timeline(clean_str(row.get("timeline"))),
        "benefits":      clean_str(row.get("benefits")),
        "impact":        normalise_impact(clean_str(row.get("impact"))),
    }


# ---------------------------------------------------------------------------
# Main ingestion function
# ---------------------------------------------------------------------------
def ingest_file(filepath: str):
    print(f"\n── Ingesting: {filepath}")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        df = pd.read_excel(filepath, dtype=str)
        first_row = df.iloc[0].astype(str).str.strip().str.lower()
        if any(v in first_row.values for v in ("solution", "pain point", "observation/pain point")):
            df = pd.read_excel(filepath, dtype=str, header=1)
    df.columns = df.columns.astype(str)
    df = drop_ignored_columns(df)
    df = normalise_columns(df)

    conn   = hana_connection()
    cursor = conn.cursor()

    inserted = skipped = 0

    for _, raw_row in df.iterrows():
        row = process_row(raw_row.to_dict(), os.path.basename(filepath))
        if row is None:
            skipped += 1
            continue

        # Embed only the pain point — maximises similarity precision
        try:
            vector = embed_text(row["pain_point"])
        except Exception as e:
            print(f"  ⚠ Embedding failed for row, skipping: {e}")
            skipped += 1
            continue

        # Convert vector to HANA string format: [f1,f2,...,fn]
        vector_str = "[" + ",".join(str(v) for v in vector) + "]"

        cursor.execute(INSERT_SQL, (
            row["source_file"],
            row["pain_point"],
            row["solution"],
            row["solution_area"],
            row["category"],
            row["recommendation"],
            row["effort"],
            row["timeline"],
            row["benefits"],
            row["impact"],
            "historical_excel",
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
def collect_files(paths: list[str]) -> list[str]:
    files = []
    for p in paths:
        path = pathlib.Path(p)
        if path.is_dir():
            files.extend(str(f) for f in path.glob("**/*.xlsx"))
        elif path.suffix.lower() == ".xlsx":
            files.append(str(path))
        else:
            print(f"Skipping (not .xlsx): {p}")
    return files


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python ingestion/ingest.py <file.xlsx | folder> [...]")
        sys.exit(1)

    all_files = collect_files(sys.argv[1:])
    if not all_files:
        print("No .xlsx files found.")
        sys.exit(1)

    print(f"Found {len(all_files)} file(s) to process.")
    for f in all_files:
        ingest_file(f)

    print("\n✓ Ingestion complete.")
