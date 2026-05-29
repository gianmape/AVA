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
import shutil
import warnings
from langdetect import detect as _detect_lang, LangDetectException
import openpyxl
from openpyxl.styles import Font, Alignment
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from shared.config import (
    normalise_columns,
    drop_ignored_columns,
    clean_str,
    embed_text,
    hana_connection,
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
    EFFORT, BENEFITS, TIMELINE, IMPACT
FROM SVA2.PAIN_POINTS
WHERE SOLUTION = ?
{area_filter}
ORDER BY COSINE_SIMILARITY(EMBEDDING, TO_REAL_VECTOR(?)) DESC
"""

UPDATE_USE_COUNT_SQL = "UPDATE SVA2.PAIN_POINTS SET USE_COUNT = USE_COUNT + 1 WHERE ID = ?"

KNOWLEDGE_SEARCH_SQL = """
SELECT TOP {top_k}
    TITLE, CONTENT, SOLUTION, RELEASE, AGENT_BASED, JOULE_BASED,
    VALUE_DRIVER, VALUE_LEVER, KPI_ID, KPI_CATEGORY, KPI_TARGET
FROM SVA2.KNOWLEDGE_BASE
WHERE SOURCE_TYPE = ?
ORDER BY COSINE_SIMILARITY(EMBEDDING, TO_REAL_VECTOR(?)) DESC
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
    "ariba next-gen":         "Ariba Next-Gen",
    "value kpis":             "Value KPIs",
}


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

    rows = []
    for idx, row in df.iterrows():
        pain_point = clean_str(row.get("pain_point"))
        solution   = clean_str(row.get("solution"))
        if not pain_point or not solution:
            continue
        solution_matched = next(
            (s for s in VALID_SOLUTIONS if s.lower() == solution.lower()),
            solution,
        )
        try:
            lang = _detect_lang(pain_point)
        except LangDetectException:
            lang = "en"
        rows.append({
            "idx":          int(idx),
            "pain_point":   pain_point,
            "solution":     solution_matched,
            "solution_area": normalise_solution_area(clean_str(row.get("solution_area"))),
            "sheet":        sheet,
            "language":     lang,
        })

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

    positional = [solution]
    if area:
        positional.append(area)
    positional.append(vector_str)

    conn   = hana_connection()
    cursor = conn.cursor()
    cursor.execute(
        VECTOR_SEARCH_SQL.format(top_k=top_k, area_filter=area_filter),
        positional,
    )
    cols = ["id", "pain_point", "solution_area", "recommendation", "category",
            "effort", "benefits", "timeline", "impact"]
    raw = [dict(zip(cols, row)) for row in cursor.fetchall()]

    # Increment USE_COUNT for every retrieved case
    for r in raw:
        cursor.execute(UPDATE_USE_COUNT_SQL, (r["id"],))
    conn.commit()

    cursor.close()
    conn.close()

    rows = [
        {
            "similar_pain_point": r["pain_point"],
            "solution_area":      r["solution_area"],
            "category":           r["category"],
            "effort":             r["effort"],
            "timeline":           r["timeline"],
            "impact":             r["impact"],
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
    """
    import concurrent.futures

    def _search_one(item):
        idx        = item["idx"]
        pain_point = item["pain_point"]
        solution   = item["solution"]
        area       = item.get("area")

        query_vector = embed_text(pain_point)
        vector_str   = "[" + ",".join(str(v) for v in query_vector) + "]"
        area_filter  = "AND SOLUTION_AREA = ?" if area else ""

        positional = [solution]
        if area:
            positional.append(area)
        positional.append(vector_str)

        conn   = hana_connection()
        cursor = conn.cursor()
        cursor.execute(
            VECTOR_SEARCH_SQL.format(top_k=top_k, area_filter=area_filter),
            positional,
        )
        cols = ["id", "pain_point", "solution_area", "recommendation", "category",
                "effort", "benefits", "timeline", "impact"]
        raw = [dict(zip(cols, row)) for row in cursor.fetchall()]

        # Increment USE_COUNT for every retrieved case
        for r in raw:
            cursor.execute(UPDATE_USE_COUNT_SQL, (r["id"],))
        conn.commit()

        cursor.close()
        conn.close()

        cases = [
            {
                "similar_pain_point": r["pain_point"],
                "solution_area":      r["solution_area"],
                "category":           r["category"],
                "effort":             r["effort"],
                "timeline":           r["timeline"],
                "impact":             r["impact"],
            }
            for r in raw
        ]
        return {"idx": idx, "cases": cases}

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(items), 10)) as pool:
        results = list(pool.map(_search_one, items))

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
    Copy input Excel and write Joule-synthesized recommendations into target columns.

    Args:
        input_path:  Path to the original .xlsx file.
        rows:        List of row dicts from Joule. Each dict must have:
                       - idx (int): 0-based row index matching read_excel_painpoints output
                       - Any subset of: Recommendations, Category, Effort, Benefits,
                         Documentation, Timeline, Impact
                     Documentation should be a list of {"title": ..., "url": ...} dicts
                     or a plain string.
        output_path: Optional output path; defaults to <input>_RECOMMENDED.xlsx.

    Returns:
        Absolute path to the written output file.
    """
    if output_path is None:
        stem = pathlib.Path(input_path).stem
        downloads = pathlib.Path.home() / "Downloads"
        output_path = str(downloads / f"{stem}_RECOMMENDED.xlsx")
    else:
        output_path = str(pathlib.Path(output_path).expanduser().resolve())

    shutil.copy2(input_path, output_path)

    # Determine the correct sheet
    xl = pd.ExcelFile(input_path)
    sheet = next((r.get("sheet") for r in rows if r.get("sheet")), None) or _find_pain_point_sheet(xl)

    # Detect header row offset (same logic as reader)
    df = xl.parse(sheet, dtype=str)
    first_row = df.iloc[0].astype(str).str.strip().str.lower()
    header_row_offset = 0
    if any(v in first_row.values for v in ("solution", "pain point", "observation/pain point")):
        header_row_offset = 1

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        wb = openpyxl.load_workbook(output_path, keep_vba=False)
    # Remove data validation extensions that openpyxl cannot round-trip
    ws = wb[sheet]
    ws.data_validations.dataValidation = []

    header_excel_row = header_row_offset + 1
    col_letter = {}
    for cell in ws[header_excel_row]:
        if cell.value:
            key = str(cell.value).strip().lower()
            col_letter[key] = cell.column_letter

    # Auto-create any output columns that are missing from the header row.
    # Track canonical names already mapped to avoid duplicates (e.g. "Documentation"
    # can be reached via both "documentation" and "links to documentation").
    next_col = ws.max_column + 1
    already_mapped = set(col_letter.values())  # column letters already assigned
    canonical_created = set()                   # result_key values already auto-created

    for header_key, result_key in TARGET_COLS.items():
        if header_key in col_letter:
            continue  # column already exists under this key
        if result_key in canonical_created:
            # Another alias for this result_key was already handled — just point to same letter
            existing = next(
                col_letter[k] for k, v in TARGET_COLS.items()
                if v == result_key and k in col_letter
            )
            col_letter[header_key] = existing
            continue
        has_value = any(row.get(result_key) for row in rows)
        if has_value:
            cell = ws.cell(row=header_excel_row, column=next_col, value=result_key)
            cell.font = Font(name="Calibri", size=11, bold=True)
            col_letter[header_key] = cell.column_letter
            canonical_created.add(result_key)
            next_col += 1

    data_start_excel_row = header_excel_row + 1

    rows_written = 0
    for row in rows:
        idx = row.get("idx")
        if idx is None:
            continue
        # Skip rows with no synthesized output
        has_output = any(row.get(result_key) for result_key in TARGET_COLS.values())
        if not has_output:
            continue
        excel_row  = data_start_excel_row + idx

        wrote_any = False
        written_letters = set()  # avoid writing the same column twice (alias deduplication)
        for header_key, result_key in TARGET_COLS.items():
            letter = col_letter.get(header_key)
            if not letter:
                continue
            if letter in written_letters:
                continue
            value = row.get(result_key)
            if not value:
                continue

            if result_key == "Documentation":
                if isinstance(value, list):
                    parts = []
                    for item in value:
                        if isinstance(item, dict):
                            title = item.get("title", "").strip()
                            url   = item.get("url", "").strip()
                            block = []
                            if title:
                                block.append(title)
                            if url:
                                block.append(url)
                            if block:
                                parts.append("\n".join(block))
                        else:
                            parts.append(str(item).strip())
                    value = "\n\n".join(parts)
                cell = ws[f"{letter}{excel_row}"]
                cell.value = str(value)
                cell.hyperlink = None
                cell.style = "Normal"
                cell.font = Font(name="Calibri", size=11, color="000000", underline="none", bold=False, italic=False)
                cell.alignment = Alignment(wrap_text=True)
            elif isinstance(value, list):
                value = "\n".join(value)
                cell = ws[f"{letter}{excel_row}"]
                cell.value = str(value)
            else:
                cell = ws[f"{letter}{excel_row}"]
                cell.value = str(value)
            written_letters.add(letter)
            wrote_any = True

        if wrote_any:
            rows_written += 1

    wb.save(output_path)
    return output_path, rows_written


# ---------------------------------------------------------------------------
# Knowledge base retrieval
# ---------------------------------------------------------------------------
def retrieve_knowledge_context(
    pain_point: str,
    solution: str,
    source_types: list[str],
    top_k: int = 3,
) -> dict:
    """
    Search SVA2.KNOWLEDGE_BASE for each source_type in parallel.

    Returns a dict keyed by source_type, each value a list of matching entries:
    [{title, content, solution, release, agent_based, joule_based}, ...]
    Empty list means no relevant entries found for that source type.
    """
    import concurrent.futures

    query_vector = embed_text(pain_point)
    vector_str   = "[" + ",".join(str(v) for v in query_vector) + "]"

    def _search_one(source_type: str) -> tuple[str, list[dict]]:
        positional = [source_type, vector_str]

        conn   = hana_connection()
        cursor = conn.cursor()
        cursor.execute(
            KNOWLEDGE_SEARCH_SQL.format(top_k=top_k),
            positional,
        )
        cols = ["title", "content", "solution", "release", "agent_based", "joule_based",
                "value_driver", "value_lever", "kpi_id", "kpi_category", "kpi_target"]
        rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
        cursor.close()
        conn.close()
        return source_type, rows

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(source_types)) as pool:
        results = dict(pool.map(_search_one, source_types))

    return results
