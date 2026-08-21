"""
ingestion/merge_roadmap.py
--------------------------
Compare extracted roadmap features (from extract_roadmap_pdf.py or extract_roadmap_pptx.py)
against existing KNOWLEDGE_BASE records in HANA, then perform a smart merge:

- New features → INSERT with embeddings
- Existing features with changed release/flags → UPDATE in-place
- Features in HANA not in new deck:
    - If release date is PAST (already delivered) → keep, mark as delivered in report
    - If release date is FUTURE but solution NOT covered in PDF → keep (extraction gap)
    - If release date is FUTURE and solution IS covered → truly deprecated → delete (with confirmation)

Outputs:
- data/Update/roadmap_merge_report.xlsx  (diff report with 5 sheets)
- data/Update/roadmap_to_ingest.xlsx     (ready for ingest_knowledge.py)

Usage:
    python ingestion/merge_roadmap.py --dry-run              # Report only, no writes
    python ingestion/merge_roadmap.py                        # INSERT new + UPDATE changed
    python ingestion/merge_roadmap.py --force-delete         # Skip confirmation for deprecations
    python ingestion/merge_roadmap.py --full-refresh         # DELETE source file + re-ingest all
"""
import sys
import argparse
import pathlib
import warnings
from difflib import SequenceMatcher

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from shared.config import (
    clean_str, embed_text, hana_connection, release_connection,
    RELEASE_DATE_MAP, PDF_UPDATE_DATE, ROADMAP_PDF_SOURCE_FILE,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EXTRACTED_FILE   = "data/Update/roadmap_2026_extracted.xlsx"
REPORT_OUTPUT    = "data/Update/roadmap_merge_report.xlsx"
INGEST_OUTPUT    = "data/Update/roadmap_to_ingest.xlsx"

# Legacy source file (original 83 features from Excel) — never touched
ORIGINAL_SOURCE_FILE = "SAP_Ariba_NextGen_Features.xlsx"
# Source file tag for records inserted by this script
NEW_SOURCE_FILE = ROADMAP_PDF_SOURCE_FILE

TITLE_MATCH_THRESHOLD = 0.85

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

UPDATE_METADATA_SQL = """
UPDATE SVA2.KNOWLEDGE_BASE
SET RELEASE = ?, AGENT_BASED = ?, JOULE_BASED = ?
WHERE TITLE = ? AND SOLUTION = ? AND SOURCE_TYPE IN ('next_gen', 'current_gen')
"""

QUERY_EXISTING = """
SELECT TITLE, SOLUTION, CONTENT, RELEASE, AGENT_BASED, JOULE_BASED, SOURCE_TYPE, SOURCE_FILE
FROM SVA2.KNOWLEDGE_BASE
WHERE SOURCE_TYPE IN ('next_gen', 'current_gen')
"""

DELETE_BY_SOURCE = """
DELETE FROM SVA2.KNOWLEDGE_BASE WHERE SOURCE_FILE = ?
"""

DELETE_BY_TITLE_SOLUTION = """
DELETE FROM SVA2.KNOWLEDGE_BASE
WHERE TITLE = ? AND SOLUTION = ? AND SOURCE_TYPE IN ('next_gen', 'current_gen')
"""


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------
def normalize_title(t: str) -> str:
    if not t:
        return ""
    return t.strip().lower().replace("\u2013", "-").replace("\u2014", "-")


def titles_match(t1: str, t2: str) -> bool:
    n1 = normalize_title(t1)
    n2 = normalize_title(t2)
    if n1 == n2:
        return True
    return SequenceMatcher(None, n1, n2).ratio() >= TITLE_MATCH_THRESHOLD


def find_existing_match(new_title: str, new_solution: str,
                        existing: list[dict]) -> dict | None:
    for rec in existing:
        if rec["solution"] == new_solution and titles_match(new_title, rec["title"]):
            return rec
    for rec in existing:
        if titles_match(new_title, rec["title"]):
            return rec
    return None


# ---------------------------------------------------------------------------
# Deprecated classification (date-aware)
# ---------------------------------------------------------------------------
def classify_deprecated(rec: dict, solution_coverage: set[str]) -> str:
    """
    Returns:
      'delivered'  – release date is on or before PDF_UPDATE_DATE (already shipped)
      'no_coverage'– solution not covered in new PDF (extraction gap, keep safely)
      'deprecated' – future release, solution covered → truly removed from roadmap
    """
    solution = rec.get("solution", "")
    release  = (rec.get("release") or "").strip()

    if solution not in solution_coverage:
        return "no_coverage"

    release_date = RELEASE_DATE_MAP.get(release)
    # release_date == None means "Roadmap" (undated future) → treat as future
    if release_date is not None and release_date <= PDF_UPDATE_DATE:
        return "delivered"

    return "deprecated"


# ---------------------------------------------------------------------------
# Embedding helper
# ---------------------------------------------------------------------------
def generate_embedding(title: str, content: str) -> list[float] | None:
    text = f"{title} — {content}"
    try:
        return embed_text(text)
    except Exception as e:
        print(f"  Warning: embedding failed for '{title[:50]}': {e}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Merge roadmap extract with HANA KNOWLEDGE_BASE")
    parser.add_argument("--input",        default=EXTRACTED_FILE, help="Extracted Excel path")
    parser.add_argument("--dry-run",      action="store_true",    help="Report only, no writes")
    parser.add_argument("--full-refresh", action="store_true",
                        help=f"Delete all records with SOURCE_FILE='{NEW_SOURCE_FILE}' and re-ingest")
    parser.add_argument("--force-delete", action="store_true",
                        help="Skip interactive confirmation when deleting deprecated features")
    args = parser.parse_args()

    if not pathlib.Path(args.input).exists():
        print(f"Error: File not found: {args.input}")
        sys.exit(1)

    # --- Load extracted features ---
    print(f"Loading extracted features: {args.input}")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df_new = pd.read_excel(args.input, dtype=str)
    df_new = df_new.fillna("")
    print(f"  {len(df_new)} features loaded")

    # Solutions covered in the extracted file (for deprecated detection)
    solution_coverage: set[str] = set(
        df_new["SAP Ariba Solution / Product"].dropna().unique()
    )
    print(f"  Solutions covered: {sorted(solution_coverage)}")

    # --- Load existing HANA records ---
    print("\nQuerying existing KNOWLEDGE_BASE records...")
    conn = hana_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(QUERY_EXISTING)
        rows = cursor.fetchall()
    except Exception as e:
        print(f"Error querying HANA: {e}")
        cursor.close()
        release_connection(conn)
        sys.exit(1)

    existing: list[dict] = []
    for row in rows:
        existing.append({
            "title":       row[0],
            "solution":    row[1],
            "content":     row[2],
            "release":     row[3],
            "agent_based": row[4],
            "joule_based": row[5],
            "source_type": row[6],
            "source_file": row[7],
        })
    print(f"  {len(existing)} existing records found")

    # --- Compare and classify ---
    print("\nComparing features...")
    new_features:     list[dict] = []
    updated_features: list[dict] = []
    unchanged:        list[str]  = []
    matched_existing: set[int]   = set()

    for _, row in df_new.iterrows():
        title       = row.get("Feature or Functionality Announced", "")
        solution    = row.get("SAP Ariba Solution / Product", "")
        description = row.get("Description", "")
        release     = row.get("Release", "")
        agent_based = row.get("Agent-based", "No")
        joule_based = row.get("Joule-based", "No")
        generation  = row.get("Generation", "next_gen")

        if not title:
            continue

        match = find_existing_match(title, solution, existing)

        if match:
            idx = existing.index(match)
            matched_existing.add(idx)

            has_changes = False
            old_release = (match.get("release") or "").strip()
            if release and release != old_release:
                has_changes = True
            if agent_based != (match.get("agent_based") or "No"):
                has_changes = True
            if joule_based != (match.get("joule_based") or "No"):
                has_changes = True

            if has_changes:
                updated_features.append({
                    "title":           title,
                    "solution":        solution,
                    "existing_title":  match["title"],
                    "existing_content":match["content"],
                    "old_release":     old_release,
                    "release":         release,
                    "agent_based":     agent_based,
                    "joule_based":     joule_based,
                    "generation":      generation,
                    "change_type":     "metadata_update",
                })
            else:
                unchanged.append(title)
        else:
            new_features.append({
                "title":       title,
                "solution":    solution,
                "description": description,
                "release":     release,
                "agent_based": agent_based,
                "joule_based": joule_based,
                "generation":  generation,
            })

    # --- Classify deprecated ---
    deprecated_delivered: list[dict] = []
    deprecated_no_coverage: list[dict] = []
    deprecated_removed: list[dict] = []

    for i, rec in enumerate(existing):
        if i not in matched_existing:
            classification = classify_deprecated(rec, solution_coverage)
            if classification == "delivered":
                deprecated_delivered.append(rec)
            elif classification == "no_coverage":
                deprecated_no_coverage.append(rec)
            else:
                deprecated_removed.append(rec)

    # --- Report ---
    print(f"\n{'='*60}")
    print("MERGE REPORT")
    print(f"{'='*60}")
    print(f"  New features to insert:           {len(new_features)}")
    print(f"  Existing with metadata updates:   {len(updated_features)}")
    print(f"  Unchanged (already in HANA):      {len(unchanged)}")
    print(f"  Delivered (release <= {PDF_UPDATE_DATE}): {len(deprecated_delivered)}")
    print(f"  No extraction coverage:           {len(deprecated_no_coverage)}")
    print(f"  Truly deprecated (to delete):     {len(deprecated_removed)}")
    print(f"{'='*60}")

    if deprecated_removed:
        print(f"\n  Features to DELETE (future release, not in new PDF):")
        for rec in deprecated_removed[:15]:
            print(f"    - [{rec['solution']}] [{rec.get('release','')}] {rec['title'][:55]}")
        if len(deprecated_removed) > 15:
            print(f"    ... and {len(deprecated_removed)-15} more")

    # --- Save report Excel ---
    print(f"\nWriting merge report: {REPORT_OUTPUT}")
    with pd.ExcelWriter(REPORT_OUTPUT, engine="openpyxl") as writer:
        if new_features:
            pd.DataFrame(new_features).to_excel(writer, sheet_name="New", index=False)
        if updated_features:
            pd.DataFrame(updated_features).to_excel(writer, sheet_name="Updated", index=False)
        if deprecated_removed:
            pd.DataFrame(deprecated_removed).to_excel(
                writer, sheet_name="Deprecated_Removed", index=False)
        if deprecated_delivered:
            pd.DataFrame(deprecated_delivered).to_excel(
                writer, sheet_name="Deprecated_Delivered", index=False)
        if deprecated_no_coverage:
            pd.DataFrame(deprecated_no_coverage).to_excel(
                writer, sheet_name="No_Coverage", index=False)
        pd.DataFrame({"unchanged": unchanged}).to_excel(
            writer, sheet_name="Unchanged", index=False)

    # --- Save ingestion-ready Excel (new features only) ---
    if new_features:
        print(f"Writing ingestion file: {INGEST_OUTPUT}")
        ingest_rows = [{
            "SAP Ariba Solution / Product":       f["solution"],
            "Feature or Functionality Announced": f["title"],
            "Description":                        f["description"],
            "Release":                            f["release"],
            "Agent-based":                        f["agent_based"],
            "Joule-based":                        f["joule_based"],
        } for f in new_features]
        pd.DataFrame(ingest_rows).to_excel(INGEST_OUTPUT, index=False, engine="openpyxl")

    if args.dry_run:
        print("\n[DRY RUN] No changes written to HANA.")
        cursor.close()
        release_connection(conn)
        return

    # =========================================================================
    # WRITE PHASE
    # =========================================================================

    # --- UPDATE changed metadata ---
    if updated_features:
        print(f"\nUpdating {len(updated_features)} existing features...")
        updated_count = 0
        for f in updated_features:
            cursor.execute(UPDATE_METADATA_SQL, (
                f["release"],
                f["agent_based"],
                f["joule_based"],
                f["existing_title"],  # match by existing title in DB
                f["solution"],
            ))
            updated_count += 1
        conn.commit()
        print(f"  Updated {updated_count} records")

    # --- DELETE truly deprecated features (with confirmation) ---
    if deprecated_removed:
        proceed_delete = args.force_delete
        if not proceed_delete:
            print(f"\n  {len(deprecated_removed)} features are deprecated "
                  f"(future release, solution covered, not in new PDF).")
            answer = input("  Delete them from HANA? [y/N]: ").strip().lower()
            proceed_delete = (answer == "y")

        if proceed_delete:
            deleted_count = 0
            for rec in deprecated_removed:
                cursor.execute(DELETE_BY_TITLE_SOLUTION, (rec["title"], rec["solution"]))
                deleted_count += 1
            conn.commit()
            print(f"  Deleted {deleted_count} deprecated features")
        else:
            print("  Deletion skipped.")

    # --- INSERT / FULL REFRESH ---
    if args.full_refresh:
        print(f"\n[FULL REFRESH] Deleting all records with SOURCE_FILE='{NEW_SOURCE_FILE}'...")
        cursor.execute(DELETE_BY_SOURCE, (NEW_SOURCE_FILE,))
        conn.commit()
        features_to_ingest = [{
            "title":       row.get("Feature or Functionality Announced", ""),
            "solution":    row.get("SAP Ariba Solution / Product", ""),
            "description": row.get("Description", ""),
            "release":     row.get("Release", ""),
            "agent_based": row.get("Agent-based", "No"),
            "joule_based": row.get("Joule-based", "No"),
            "generation":  row.get("Generation", "next_gen"),
        } for _, row in df_new.iterrows()]
    else:
        features_to_ingest = new_features

    if not features_to_ingest:
        print("\nNo new features to ingest.")
        cursor.close()
        release_connection(conn)
        return

    print(f"\nIngesting {len(features_to_ingest)} features into HANA...")
    inserted = skipped = 0

    for f in features_to_ingest:
        title   = f["title"]
        content = f.get("description") or f.get("existing_content") or ""
        if not title:
            skipped += 1
            continue
        if not content:
            content = f"{title} capability for {f.get('solution', '')}."

        source_type = f.get("generation", "next_gen")
        solution    = f["solution"]
        release     = f.get("release", "")
        agent_based = f.get("agent_based", "No")
        joule_based = f.get("joule_based", "No")

        vector = generate_embedding(title, content)
        if vector is None:
            skipped += 1
            continue

        vector_str = "[" + ",".join(str(v) for v in vector) + "]"

        cursor.execute(INSERT_SQL, (
            source_type, solution, title, content, release,
            agent_based, joule_based, NEW_SOURCE_FILE, vector_str,
        ))

        inserted += 1
        if inserted % 10 == 0:
            conn.commit()
            print(f"  {inserted} rows committed...")

    conn.commit()
    cursor.close()
    release_connection(conn)

    print(f"\nDone — inserted: {inserted}, updated: {len(updated_features)}, skipped: {skipped}")


if __name__ == "__main__":
    main()
