"""
mcp/server.py
-------------
MCP server exposing pain point tools for Joule Desktop.

Joule orchestrates the full pipeline using these tools:
  1. read_excel_painpoints    — read all pain point rows from an Excel file
  2. retrieve_similar_cases   — HANA vector search for similar historical cases
  3. write_excel_output       — write Joule-synthesized results back to Excel
  4. list_ingested_solutions  — inspect what historical data is available

Run (Joule Desktop via HTTP):
    python mcp/server.py --http --port 8000

Run (stdio, for Claude Desktop):
    python mcp/server.py
"""
import json
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MCP] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(pathlib.Path(__file__).parent.parent / "mcp_server.log"),
    ],
)
log = logging.getLogger("sva2")

from dotenv import load_dotenv
load_dotenv()

from mcp.server.fastmcp import FastMCP
from server.recommend import (
    read_excel_painpoints as _read_excel,
    retrieve_similar_cases as _retrieve,
    retrieve_similar_cases_batch as _retrieve_batch,
    write_excel_output as _write_excel,
)
from shared.config import hana_connection

mcp = FastMCP(
    "painpoints",
    stateless_http=True,
    instructions="""
When a user says "Run SVA Analysis" or attaches a pain points Excel file:
1. Call read_excel_painpoints with the path of the attached file.
   Do NOT read the file yourself. Do NOT add rows beyond what this tool returns.

2. Call retrieve_similar_cases_batch ONCE with ALL rows returned from step 1.
   Pass the full list — do NOT call it once per row.
   The returned cases are directional signals from human experts — NOT the answer.
   Use similar_pain_point and comments to understand what SAP area to research.
   Use category, effort, timeline, impact as calibration hints for your classifications.

LANGUAGE RULE: Each row from read_excel_painpoints includes a `language` field (e.g. "es", "en", "pt").
You MUST write ALL generated text for that row (Recommendations, Benefits) in that language.
"es" = Spanish, "en" = English, "pt" = Portuguese. This is mandatory — never override with English.

3. For EACH row, research SAP documentation — HARD LIMIT: visit no more than 8 sources per pain point. Do not exceed this under any circumstances. Preferred sources: help.sap.com, community.sap.com, learning.sap.com, SAP release notes.
   on the specific topic surfaced by the pain point and the similar cases context.
   Then synthesize your own original output:
   - Recommendations: actionable steps grounded in your SAP documentation research.
     Do NOT copy from historical cases — use them only to understand what area to explore.
   - Category: one of — Feature Adoption, Innovation, Q&A, Process Change, Training, Roadmap Discussion
   - Effort: Low | Medium | High | Complex | N/A  (use historical signals as a hint)
   - Benefits: expected business outcome, informed by SAP best practices (same language as pain point)
   - Documentation: specific, relevant SAP help articles, community posts, or learning resources
     you found during your research. Return as JSON array of {"title": "...", "url": "..."}.
     Prioritize specific and actionable links over generic landing pages.
   - Timeline: Quick Win | Short Term | Mid Term | Long Term
   - Impact: Low | Medium | High  (use historical signals as a hint)

4. Call write_excel_output with:
   - input_path: the same attachment path passed to read_excel_painpoints
   - rows: a list containing ONLY the rows from step 3, each with its original idx value

5. After write_excel_output completes, present ONLY this — nothing else:
   a) The message field from write_excel_output.
   b) A KPI dashboard block. No pain point descriptions, no recommendations, no per-row details.
      NEVER list individual pain points or their content.

   Format the dashboard exactly like this:

   **SVA Analysis Complete — [N] pain points**

   **Solutions**
   [solution name]  ●●●  3 (25%)
   [solution name]  ●●   2 (17%)
   (one line per solution with at least 1 row, dots proportional to count out of 10 max)

   **Category**    **Effort**      **Impact**
   Feature Adoption  3             Low     4        Low     2
   Innovation        2             Medium  3        Medium  6
   Process Change    1             High    1        High    1
   Q&A               1             N/A     1
   Roadmap           2

   **Timeline**
   Quick Win  ●●  2  ·  Short Term  ●●●  3  ·  Mid Term  ●●  2  ·  Long Term  ●  1  ·  Ongoing  ●  1

   Rules:
   - Show only values with count > 0
   - No descriptions, no pain point text, no recommendation text
   - No tables with pain point rows
   - This block is the entire response after the message — nothing before or after it
""",
)
log.info("=== painpoints MCP server starting ===")


@mcp.tool()
def read_excel_painpoints(input_path: str) -> str:
    """
    Read the attached Excel file and return all pain point rows as JSON.

    Pass the path of the attached file as input_path.
    Use this first — it identifies the correct sheet automatically and filters
    out empty rows. Only process the rows this tool returns.
    Returns a JSON array where each element has:
      - idx (int): row index, used later in write_excel_output
      - pain_point (str): the pain point text
      - solution (str): canonical SAP Ariba solution name
      - solution_area (str | null): functional area within the solution if provided
      - sheet (str): sheet name where the data was found
      - language (str): detected language code (e.g. "es", "en", "pt")
                        ALL generated text for this row MUST be in this language.

    Args:
        input_path: Path to the attached .xlsx file.
    """
    input_path = str(pathlib.Path(input_path).expanduser().resolve())
    log.info(">> read_excel_painpoints called | path=%s", input_path)
    if not pathlib.Path(input_path).exists():
        log.warning("   File not found: %s", input_path)
        return json.dumps({"error": f"File not found: {input_path}"})
    rows = _read_excel(input_path)
    log.info("   Returned %d rows", len(rows))
    return json.dumps(rows, ensure_ascii=False)


@mcp.tool()
def retrieve_similar_cases(
    pain_point: str,
    solution: str,
    area: str | None = None,
    top_k: int = 3,
) -> str:
    """
    Retrieve similar historical pain points from the HANA vector store.

    Use this for each row after read_excel_painpoints. Returns human-validated
    signals from similar past cases: similar pain point context, category, effort,
    timeline, and impact. Use these as directional hints — NOT as the answer.

    Your job after calling this tool is to:
    - Use the similar_pain_point and comments to understand what SAP area to research
    - Search SAP help, community, learning, and release notes deeply on that topic
    - Synthesize your own recommendations grounded in current SAP documentation
    - Use the category/effort/timeline/impact signals to calibrate your classifications

    If the array is empty, research SAP documentation directly for the pain point.

    Args:
        pain_point: Text of the pain point to search for.
        solution:   SAP Ariba solution (e.g. "Ariba Buying", "Sourcing", "Contracts").
        area:       Optional solution_area filter (e.g. "Integración", "Registro de Proveedores").
        top_k:      Number of similar cases to return (default 3, max 10).

    Returns:
        JSON array of similar cases. Each case has:
        similar_pain_point, solution_area, category, effort, timeline, impact.
        Empty array means no similar cases found — use SAP documentation knowledge directly.
    """
    top_k = min(int(top_k), 10)
    log.info(">> retrieve_similar_cases called | solution=%s | area=%s | pain_point=%.80s…", solution, area, pain_point)
    cases = _retrieve(pain_point, solution, area, top_k=top_k)
    log.info("   Returned %d cases", len(cases))
    return json.dumps(cases, ensure_ascii=False)


@mcp.tool()
def retrieve_similar_cases_batch(
    items: list[dict],
    top_k: int = 3,
) -> str:
    """
    Retrieve similar historical cases for ALL pain points in a single call.

    Use this instead of calling retrieve_similar_cases once per row — it runs
    all searches in parallel and returns in one round-trip.

    Args:
        items: List of row dicts from read_excel_painpoints. Each must have:
               - idx (int): row index
               - pain_point (str)
               - solution (str)
               - solution_area (str | null, optional)
        top_k: Number of similar cases per pain point (default 3, max 10).

    Returns:
        JSON array of {idx, cases} — one entry per input item.
        cases is a list of {similar_pain_point, solution_area, category, effort, timeline, impact}.
        Empty cases list means no similar cases found for that row.
    """
    top_k = min(int(top_k), 10)
    log.info(">> retrieve_similar_cases_batch called | %d items | top_k=%d", len(items), top_k)
    results = _retrieve_batch(items, top_k=top_k)
    log.info("   Batch complete | %d results returned", len(results))
    return json.dumps(results, ensure_ascii=False)


@mcp.tool()
def write_excel_output(
    input_path: str,
    rows: list[dict],
    output_path: str | None = None,
) -> dict:
    """
    Write synthesized recommendations into a copy of the attached Excel file.

    Call this once after processing all rows. Pass the same attachment path
    used in read_excel_painpoints, and only the rows that were returned by
    that tool (with their original idx values).

    Args:
        input_path:  Path to the attached .xlsx file.
        rows:        List of row results. Each element must have:
                       - idx (int): row index from read_excel_painpoints
                       - Recommendations (str)
                       - Category (str): Feature Adoption | Innovation | Q&A | Process Change | Training | Roadmap Discussion
                       - Effort (str): Low | Medium | High | Complex | N/A
                       - Benefits (str)
                       - Documentation (list of {title, url} dicts)
                       - Timeline (str): Quick Win | Short Term | Mid Term | Long Term
                       - Impact (str): Low | Medium | High
                     All fields except idx are optional.
        output_path: Optional output path. Defaults to <input>_RECOMMENDED.xlsx in output/.

    Returns:
        Dict with output_path, file_uri, filename, rows_written, message.
    """
    input_path = str(pathlib.Path(input_path).expanduser().resolve())
    log.info(">> write_excel_output called | path=%s", input_path)
    try:
        if output_path:
            output_path = str(pathlib.Path(output_path).expanduser().resolve())
    except Exception:
        output_path = None

    log.info("   Received %d rows from Joule, filtering to those with output", len(rows))
    out_path, rows_written = _write_excel(input_path, rows, output_path)
    log.info("   Done | output=%s | rows_written=%d", out_path, rows_written)

    file_uri = pathlib.Path(out_path).as_uri()
    filename = pathlib.Path(out_path).name
    return {
        "output_path": out_path,
        "file_uri": file_uri,
        "filename": filename,
        "rows_written": rows_written,
        "message": f"Analysis complete. {rows_written} pain points processed. File saved to: {out_path}",
    }


@mcp.tool()
def list_ingested_solutions() -> str:
    """
    List all SAP Ariba solutions and row counts currently indexed in HANA.
    Useful to understand what historical data is available before retrieval.
    """
    log.info(">> list_ingested_solutions called")
    conn = hana_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT SOLUTION, COUNT(*) AS CNT "
        "FROM SVA2.PAIN_POINTS "
        "GROUP BY SOLUTION ORDER BY CNT DESC"
    )
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    if not rows:
        return "No data ingested yet."

    lines = ["Solution | Rows", "---------|-----"]
    for solution, cnt in rows:
        lines.append(f"{solution} | {cnt}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="Run as HTTP/SSE server for Joule Desktop")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if args.http:
        mcp.settings.host = "127.0.0.1"
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
