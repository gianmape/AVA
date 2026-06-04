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
import logging
from langdetect import detect as _detect_lang, LangDetectException
import openpyxl

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
    VALUE_DRIVER, VALUE_LEVER, KPI_ID, KPI_CATEGORY, KPI_TARGET,
    CAPABILITY, KPI_FORMULA, KPI_MEAS_FREQ
FROM SVA2.KNOWLEDGE_BASE
WHERE SOURCE_TYPE = ?
{solution_filter}
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
    "solution area":          "Solution Area",
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
    try:
        cursor.execute(
            VECTOR_SEARCH_SQL.format(top_k=top_k, area_filter=area_filter),
            positional,
        )
        cols = ["id", "pain_point", "solution_area", "recommendation", "category",
                "effort", "benefits", "timeline", "impact"]
        raw = [dict(zip(cols, row)) for row in cursor.fetchall()]

        for r in raw:
            cursor.execute(UPDATE_USE_COUNT_SQL, (r["id"],))
        conn.commit()
    finally:
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
        cases = retrieve_similar_cases(
            pain_point=item["pain_point"],
            solution=item["solution"],
            area=item.get("area"),
            top_k=top_k,
        )
        return {"idx": item["idx"], "cases": cases}

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
    }
    _TIMELINE_MAP = {
        "quick win":   "Quick Win (Within 1 week)",
        "short term":  "Short Term (1 – 3 Weeks)",
        "mid term":    "Mid Term (1 – 3 Months)",
        "long term":   "Long Term (3+ Months)",
    }
    for r in rows:
        for field, mapping in (("effort", _EFFORT_MAP), ("timeline", _TIMELINE_MAP)):
            val = r.get(field)
            if val:
                r[field] = mapping.get(val.strip().lower(), val)

    # --- Strip blocked / generic documentation URLs ---
    # These are root/category pages that return 404 or are not specific articles.
    # Any URL that starts with one of these prefixes is blocked unconditionally.
    _URL_BLOCKLIST = (
        "https://community.sap.com/topics/ariba",
        "https://community.sap.com/t5/spend-management",
        "https://community.sap.com/t5/ariba",
        "https://support.ariba.com",
        "https://support.sap.com",
        "https://learning.sap.com/learning-journeys",  # model hallucinates these — always 404
        # help.sap.com/docs/<PRODUCT> roots — blocked regardless of sub-path (model hallucinates sub-paths)
        "https://help.sap.com/docs/ARIBA_SOURCING",
        "https://help.sap.com/docs/ariba_sourcing",
        "https://help.sap.com/docs/ariba-sourcing",
        "https://help.sap.com/docs/ARIBA_CONTRACTS",
        "https://help.sap.com/docs/ariba_contracts",
        "https://help.sap.com/docs/ariba-contracts",
        "https://help.sap.com/docs/ARIBA_BUYING",
        "https://help.sap.com/docs/ariba_buying",
        "https://help.sap.com/docs/ariba-buying",
        "https://help.sap.com/docs/ARIBA_INVOICE",
        "https://help.sap.com/docs/ariba_invoice",
        "https://help.sap.com/docs/ariba-invoice",
        "https://help.sap.com/docs/ARIBA_GUIDED_BUYING",
        "https://help.sap.com/docs/ariba_guided_buying",
        "https://help.sap.com/docs/ariba-guided-buying",
        "https://help.sap.com/docs/ARIBA_SUPPLIER_LIFECYCLE_AND_PERFORMANCE",
        "https://help.sap.com/docs/ariba_supplier_lifecycle_and_performance",
        "https://help.sap.com/docs/ariba-supplier-lifecycle-and-performance",
        "https://help.sap.com/docs/SAP_ARIBA",
        "https://help.sap.com/docs/sap_ariba",
        "https://help.sap.com/docs/sap-ariba",
        "https://help.sap.com/docs/SAP_ANALYTICS_CLOUD",
        "https://help.sap.com/docs/sap_analytics_cloud",
        "https://help.sap.com/docs/sap-analytics-cloud",
    )
    def _is_blocked(url: str) -> bool:
        u = url.strip().rstrip("/").lower()
        for blocked in _URL_BLOCKLIST:
            if u.startswith(blocked.lower()):
                return True
        return False

    for r in rows:
        docs = r.get("documentation")
        if isinstance(docs, list):
            r["documentation"] = [
                d for d in docs
                if not (isinstance(d, dict) and _is_blocked(d.get("url", "")))
                and not (isinstance(d, str) and _is_blocked(d))
            ] or None
        elif isinstance(docs, str):
            # String form: filter line by line
            lines = [ln for ln in docs.splitlines() if not _is_blocked(ln)]
            r["documentation"] = "\n".join(lines) if lines else None

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
    import logging
    log = logging.getLogger("sva2")

    try:
        query_vector = embed_text(pain_point)
    except Exception as e:
        log.error("   retrieve_knowledge_context: embed_text failed: %s", e, exc_info=True)
        return {st: [] for st in source_types}

    vector_str   = "[" + ",".join(str(v) for v in query_vector) + "]"

    def _search_one(source_type: str) -> tuple[str, list[dict]]:
        # For vlm_kpis, filter by solution so KPIs from other solutions don't
        # crowd out the top_k results (e.g. SLP's 16 rows vs Buying's 29 rows).
        # next_gen has no solution filter — global search is intentional there.
        if source_type == "vlm_kpis" and solution:
            solution_filter = "AND SOLUTION = ?"
            positional = [source_type, solution, vector_str]
        else:
            solution_filter = ""
            positional = [source_type, vector_str]

        try:
            conn   = hana_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    KNOWLEDGE_SEARCH_SQL.format(top_k=top_k, solution_filter=solution_filter),
                    positional,
                )
                cols = ["title", "content", "solution", "release", "agent_based", "joule_based",
                        "value_driver", "value_lever", "kpi_id", "kpi_category", "kpi_target",
                        "capability", "kpi_formula", "kpi_meas_freq"]
                rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
            finally:
                cursor.close()
                conn.close()
            return source_type, rows
        except Exception as e:
            log.error("   _search_one[%s] failed: %s", source_type, e, exc_info=True)
            return source_type, []

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(source_types)) as pool:
        results = dict(pool.map(_search_one, source_types))

    return results
