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
)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------
INSERT_SQL = """
INSERT INTO SVA2.PAIN_POINTS
    (SOURCE_FILE, PAIN_POINT, COMMENTS, SOLUTION, AREA, CATEGORY,
     RECOMMENDATIONS, EFFORT, BENEFITS, DOCUMENTATION, TIMELINE, IMPACT,
     KEYS_TO_SUCCESS, EMBEDDING)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, TO_REAL_VECTOR(?))
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
        "source_file":    source_file,
        "pain_point":     pain_point,
        "comments":       clean_str(row.get("comments")),
        "solution":       solution_matched,
        "area":           clean_str(row.get("area")),
        "category":       clean_str(row.get("category")),
        "recommendations": clean_str(row.get("recommendations")),
        "effort":         clean_str(row.get("effort")),
        "benefits":       clean_str(row.get("benefits")),
        "documentation":  clean_str(row.get("documentation")),
        "timeline":       clean_str(row.get("timeline")),
        "impact":         clean_str(row.get("impact")),
        "keys_to_success": clean_str(row.get("keys_to_success")),
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

        # Build embedding text: pain point + comments (if available)
        embed_input = row["pain_point"]
        if row["comments"]:
            embed_input += "\n" + row["comments"]

        try:
            vector = embed_text(embed_input)
        except Exception as e:
            print(f"  ⚠ Embedding failed for row, skipping: {e}")
            skipped += 1
            continue

        # Convert vector to HANA string format: [f1,f2,...,fn]
        vector_str = "[" + ",".join(str(v) for v in vector) + "]"

        cursor.execute(INSERT_SQL, (
            row["source_file"],
            row["pain_point"],
            row["comments"],
            row["solution"],
            row["area"],
            row["category"],
            row["recommendations"],
            row["effort"],
            row["benefits"],
            row["documentation"],
            row["timeline"],
            row["impact"],
            row["keys_to_success"],
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
