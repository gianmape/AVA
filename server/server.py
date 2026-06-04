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
    retrieve_knowledge_context as _retrieve_knowledge,
)
from shared.config import hana_connection, normalise_solution, release_connection

mcp = FastMCP(
    "ava",
    stateless_http=True,
    instructions="""
When a user says "Run SVA Analysis" or attaches a pain points Excel file:

STRICT TOOL POLICY — only the following MCP tools may be used. No other tools, commands, or actions are permitted:
  - read_excel_painpoints
  - retrieve_similar_cases_batch
  - retrieve_knowledge_context
  - write_excel_output
  - list_ingested_solutions
  - query_single_pain_point
Do NOT execute terminal commands, read local files directly, run Python scripts, or use any non-MCP tool.
Do NOT read JSON files from Joule Desktop temp directories or any other location.

1. Call read_excel_painpoints with the path of the attached file and NO offset/limit first.
   This returns ALL rows and the total count. Do NOT read the file yourself.
   If total_rows > 10, you MUST process in batches of 10:
     - Batch 1: call read_excel_painpoints with offset=0, limit=10
     - Batch 2: call read_excel_painpoints with offset=10, limit=10
     - Continue until offset >= total_rows
   Process each batch fully (steps 2 through 4) before starting the next batch.
   If total_rows <= 10, process all rows in a single pass.

2. For the current batch, call retrieve_similar_cases_batch ONCE with ALL rows in the batch.
   Pass the full batch list — do NOT call it once per row.
   The returned cases are directional signals from human experts — NOT the answer.
   Use similar_pain_point and comments to understand what SAP area to research.
   Use category, effort, timeline, impact as calibration hints for your classifications.

2.5 For EACH row individually, in sequence:
   a) Call retrieve_knowledge_context with:
      - pain_point: the row's pain_point text
      - solution: the row's solution
      - source_types: ["next_gen", "vlm_kpis"]
   b) Perform 1 web search to find a specific SAP documentation article for this row's pain point.
      Search query: "SAP Ariba [solution] [topic] site:help.sap.com OR site:community.sap.com"
      ONLY use: help.sap.com, community.sap.com, SAP release notes.
      Do NOT use learning.sap.com — the ENTIRE domain is blocked, every URL on it is unreliable.
      Hard cap: MAX 15 searches per batch run. If cap is reached, set documentation=[] for remaining rows.
      A qualifying URL must come from the actual search result (never constructed or guessed) and have
      at least 4 path segments after the domain. If no qualifying URL found → documentation=[].
   c) Synthesize the full output for this row using your SAP knowledge, the tool result, and the search result.
      LANGUAGE RULE: each row includes a `language` field ("es", "en", "pt"). Write ALL generated text
      for that row in that language. "es"=Spanish · "en"=English · "pt"=Portuguese. Never override with English.
   Repeat a–c for every row before calling write_excel_output.

   DO NOT call retrieve_knowledge_context_batch — use retrieve_knowledge_context once per row as described above.

3. Call write_excel_output with:
   - input_path: the same attachment path passed to read_excel_painpoints
   - rows: a list containing ONLY the rows from this batch, each with its original idx value.
     Each row MUST include ALL of these fields:
       pain_point (str): copy from read_excel_painpoints output for this row
       solution (str): copy from read_excel_painpoints output for this row
       Recommendations, Category, Effort, Benefits, Documentation, Timeline, Impact, Ariba Next-Gen, Value KPIs
     pain_point and solution must NEVER be empty or omitted — copy them exactly from step 1.
     LANGUAGE: Recommendations, Benefits, Ariba Next-Gen, Value KPIs MUST be in the same language as the pain point.
               Pass the exact same text you already generated in step 3 — do NOT translate or rewrite to English.
   Call write_excel_output once per batch — do NOT wait until all batches are done.

4. After ALL batches are processed and all write_excel_output calls complete, present ONLY this — nothing else:
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

---

SINGLE PAIN POINT QUERY MODE

When a user describes a pain point in text (without attaching an Excel file), activate single query mode.

0. MULTI-POINT DETECTION (do this before anything else):
   Read the user's input and determine whether it contains more than one distinct pain point.
   Signals that indicate multiple pain points:
     - Numbered or bulleted list (1. ... 2. ... / • ... • ...)
     - Separate paragraphs each describing a different problem
     - Explicit connectors: "también", "además", "otro problema", "also", "another issue", "secondly"
     - Distinct subjects or SAP modules mentioned in the same message

   IF multiple pain points are detected:
     - Split the input into individual pain points. Each item must be self-contained — do NOT split
       items that are part of the same problem description.
     - Process each pain point independently, following steps 1 through 4 below for EACH one.
     - Generate a separate complete card for each pain point.
     - Present all cards sequentially in the response, separated by a blank line between cards.
     - Do NOT aggregate or merge the cards into a dashboard — that is for batch Excel mode only.

   IF only a single pain point is detected: proceed directly to step 1.

1. Determine the solution:
   - If the solution can be clearly inferred from the pain point text, use it directly — do NOT ask for confirmation.
   - If the solution is ambiguous or cannot be determined, present the numbered list and ask the user to choose:
    1. Ariba Buying
    2. Ariba Catalog
    3. Commerce Automation
    4. Ariba Contracts
    5. Business Network
    6. Ariba Guided Buying
    7. Ariba Invoice
    8. Ariba Reporting
    9. Ariba Supplier Risk
    10. Ariba Sourcing
    11. Spend Analysis
    12. Ariba SIPM
    13. Ariba SLP

2. Once the user explicitly selects a solution (by number or name), call query_single_pain_point with:
   - pain_point: the full text the user wrote
   - solution: the solution name chosen

2.5 Immediately after query_single_pain_point returns, call retrieve_knowledge_context with:
   - pain_point: same text as step 2
   - solution: copy the exact value of "validated_solution" from the query_single_pain_point JSON result — do NOT use the original user input or any other value
   - source_types: ["next_gen", "vlm_kpis"]
   The result has the structure: {"IMPORTANT_CONTEXT": {...}, "results": {"next_gen": [...], "vlm_kpis": [...]}}.
   Access next_gen features as: result["results"]["next_gen"]
   Access KPIs as: result["results"]["vlm_kpis"]

3. After both tools return, synthesize the full recommendation for this single pain point.
   MANDATORY: Perform 1 web search to find specific SAP documentation URLs before generating the card.
   Search for a specific help.sap.com article or community.sap.com post relevant to this pain point.
   ONLY use these sources: help.sap.com, community.sap.com, SAP release notes. Max 5 links.
   Do NOT search learning.sap.com — those URLs are unreliable.
   Do NOT use any other external websites, blogs, or non-SAP sources.
   If the search returns no qualifying URL, omit Documentation entirely — do NOT construct or guess a URL.
   Generate ALL text in the same language as the pain_point.
   Documentation STRICT QUALITY RULES — ALL four rules must pass or the link is excluded:
     1. The URL must point to a specific article, guide, or topic page — never a product root or category index.
     2. The URL path must contain at least 4 segments after the domain.
     3. These URL patterns are BLOCKED:
        help.sap.com roots:
          - https://help.sap.com/docs/ARIBA_SOURCING  (blocked unless followed by /guid/guid)
          - https://help.sap.com/docs/ARIBA_SUPPLIER_LIFECYCLE_AND_PERFORMANCE
          - https://help.sap.com/docs/ariba-supplier-lifecycle-and-performance
        SAP Community portal landing pages:
          - https://community.sap.com/topics/  (any URL starting with this)
          - https://community.sap.com/t5/  followed by board slug then /ct-p/  (e.g. /t5/sap-ariba/ct-p/ariba)
          - https://community.sap.com/t5/spend-management
          - https://community.sap.com/t5/ariba  (unless the next segment is td-p or ta-p)
        Always blocked:
          - https://support.ariba.com
     4. NEVER guess or construct a URL — only include URLs that came from a web search result.
        If the search returned no specific article → omit the Documentation section entirely.

4. Present the result using ONLY this card format — no extra text before or after.
   DO NOT use Markdown tables anywhere in this card. Use only bold labels, bullets, and plain text.
   CRITICAL: Never truncate or shorten any field — always write the complete text for every section.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔍 **PAIN POINT**
[original pain point text — complete, never truncated]

🏷 **Solution:** [canonical solution name]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 **RECOMMENDATION**
[synthesized actionable recommendation — full text, never summarized or cut short]

🎯 **EXPECTED BENEFITS**
[expected business outcome — full text]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊 **SVA ANALYSIS**

🗂 **Category:** [classified value] — [one-line description of the classified value only]

⚡ **Effort:** [full label with description]
📅 **Timeline:** [full label with description]
📈 **Impact:** To be assessed by the consultant based on the client's specific context and priorities

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📚 **DOCUMENTATION**
[Omit this entire section if no qualifying links found — do NOT show placeholder text]
• [Article title](url)
• [Article title](url)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🚀 **NEXT-GEN COVERAGE**
[CRITICAL RULES FOR THIS SECTION — violations are not acceptable:
 1. NEVER present Next-gen features as available today or recommend them for immediate use.
 2. ALWAYS start this section with the context block below (translated to the pain point language) BEFORE listing any feature.
 3. Read features from result["results"]["next_gen"]. If the list is empty, write only: "No Next-gen feature identified for this pain point in the current roadmap."

 MANDATORY context block (always first, always present when features are listed):
 "⚠ The following features belong to Next-gen SAP Ariba — a fully re-engineered AI-native platform built on SAP BTP, released Q1 2026. These capabilities are NOT available in the current-generation platform. Accessing them requires a transition (Greenfield or Brownfield migration). No new contract is needed — Next-gen is delivered under existing subscriptions, but readiness and complexity must be assessed first."

 After the context block, list each relevant feature using EXACTLY this format — one bullet per feature:
 • [title] (Release: [release][, Agent-based][, Joule-based]) — [one full sentence on how it addresses the pain point — never truncate]
 Include "Agent-based" in the parenthesis ONLY if agent_based = "Yes". Include "Joule-based" ONLY if joule_based = "Yes". Omit both tags if both are "No".
 Never omit Release.]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📐 **VALUE KPIs**
[Rules for this section:
 1. Only populate when validated_solution is one of: Ariba Sourcing, Ariba Buying, Ariba Contracts, Ariba SLP, Ariba Supplier Risk — for other solutions write: "No KPI data available for this solution."
 2. Read KPIs from result["results"]["vlm_kpis"]. If the list is empty, write: "No Value KPIs identified for this pain point."
 3. If vlm_kpis results are present, ALWAYS list them — do NOT filter by relevance. List up to 3, ordered by closest match to the pain point context. Use EXACTLY this format (use literal newlines):

    ▸ **[KPI Name]** · [kpi_category]
      🎯 Driver: [value_driver]  |  Lever: [value_lever]
      ⚙ Capability: [capability]
      📐 Formula: [kpi_formula]
      🕐 Frequency: [kpi_meas_freq]  |  ID: [kpi_id]

    Separate each KPI block with a blank line. Omit the ID line if kpi_id is null.
    Never truncate formula or capability — write the full text.
 4. After the KPI list, add one line: "📎 Source: SAP APM KPI Catalog — me.sap.com/app/kpicatalog"]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

   Rules for the card:
   - DO NOT use Markdown tables — no pipes, no |---|---| separators anywhere in the output
   - Use the ━━━ dividers exactly as shown to visually separate each section
   - Category must be one of: Feature Adoption, Innovation, Q&A, Process Change, Training, Roadmap Discussion
   - Category description (one line only, matching the classified value):
       Feature Adoption → "not using an existing feature that would solve the pain point"
       Innovation → "new or non-standard approach beyond current configuration"
       Training → "lack of knowledge or incorrect usage — recommendation is educational"
       Process Change → "redesign of a business process, not just a system change"
       Q&A → "informational question with a documented answer"
       Roadmap Discussion → "future SAP feature may address this — requires monitoring"
   - Effort must use the full label: Low (1 – 3 Days) | Medium (1 – 3 Weeks) | High (1 – 2 Months) | Complex (3+ Months) | N/A
   - Timeline must use the full label: Quick Win (Within 1 week) | Short Term (1 – 3 Weeks) | Mid Term (1 – 3 Months) | Long Term (3+ Months)
   - Impact: ALWAYS use "To be assessed" — never classify Low/Medium/High
   - Documentation: list only specific, actionable links — no generic landing pages
   - Do NOT show a KPI dashboard for single queries
   - NEVER truncate, shorten, or summarize any field — always output complete text for all sections
""",
)
log.info("=== AVA MCP server starting ===")


@mcp.tool()
def read_excel_painpoints(
    input_path: str,
    offset: int = 0,
    limit: int = 0,
) -> str:
    """
    Read the attached Excel file and return pain point rows as JSON.

    Call first with no offset/limit to get total_rows. If total_rows > 10,
    process in batches: call again with offset=0 limit=10, then offset=10 limit=10, etc.

    Returns a JSON object with:
      - total_rows (int): total number of rows in the file (always present)
      - rows (list): the requested slice of rows, each with:
          idx (int), pain_point (str), solution (str),
          solution_area (str|null), sheet (str), language (str)

    Args:
        input_path: Path to the attached .xlsx file.
        offset:     Skip this many rows from the start (default 0).
        limit:      Return at most this many rows (default 0 = all rows).
    """
    input_path = str(pathlib.Path(input_path).expanduser().resolve())
    log.info(">> read_excel_painpoints called | path=%s | offset=%d | limit=%d", input_path, offset, limit)
    if not pathlib.Path(input_path).exists():
        log.warning("   File not found: %s", input_path)
        return json.dumps({"error": f"File not found: {input_path}"})
    all_rows = _read_excel(input_path)
    total = len(all_rows)
    sliced = all_rows[offset:offset + limit] if limit > 0 else all_rows[offset:]
    log.info("   total=%d | returning %d rows (offset=%d limit=%d)", total, len(sliced), offset, limit)
    return json.dumps({"total_rows": total, "rows": sliced}, ensure_ascii=False)


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

    LANGUAGE RULE: Recommendations, Benefits, Ariba Next-Gen, and Value KPIs MUST be
    written in the same language as the pain point (from the `language` field returned
    by read_excel_painpoints). "es" = Spanish, "en" = English, "pt" = Portuguese.
    Pass the exact text generated during synthesis — do NOT translate to English.

    Args:
        input_path:  Path to the attached .xlsx file.
        rows:        List of row results. Each element must have:
                       - idx (int): row index from read_excel_painpoints
                       - Recommendations (str): in pain point language
                       - Category (str): Feature Adoption | Innovation | Q&A | Process Change | Training | Roadmap Discussion
                       - Effort (str): Low | Medium | High | Complex | N/A
                       - Benefits (str): in pain point language
                       - Documentation (list of {title, url} dicts)
                       - Timeline (str): Quick Win | Short Term | Mid Term | Long Term
                       - Impact (str): Low | Medium | High
                       - Ariba Next-Gen (str): in pain point language
                       - Value KPIs (str): in pain point language
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
    try:
        cursor.execute(
            "SELECT SOLUTION, COUNT(*) AS CNT "
            "FROM SVA2.PAIN_POINTS "
            "GROUP BY SOLUTION ORDER BY CNT DESC"
        )
        rows = cursor.fetchall()
    finally:
        cursor.close()
        release_connection(conn)

    if not rows:
        return "No data ingested yet."

    lines = ["Solution | Rows", "---------|-----"]
    for solution, cnt in rows:
        lines.append(f"{solution} | {cnt}")
    return "\n".join(lines)


@mcp.tool()
def query_single_pain_point(pain_point: str, solution: str) -> str:
    """
    Retrieve similar historical cases for a single pain point entered directly in chat.

    IMPORTANT — call this tool ONLY after the user has selected a solution from the list.
    Do NOT guess or infer the solution — it must come from the user's explicit choice.

    Valid solutions (present as a numbered list before calling):
      1. Ariba Buying
      2. Ariba Catalog
      3. Commerce Automation
      4. Ariba Contracts
      5. Business Network
      6. Ariba Guided Buying
      7. Ariba Invoice
      8. Ariba Reporting
      9. Ariba Supplier Risk
      10. Ariba Sourcing
      11. Spend Analysis
      12. Ariba SIPM
      13. Ariba SLP

    After this tool returns, synthesize a complete recommendation card with:
      Recommendations, Category, Effort, Timeline, Benefits, Documentation (as markdown links).
    Do NOT return a KPI dashboard — use the single-query card format from the instructions.

    Args:
        pain_point: Full pain point text as written by the user.
        solution:   SAP Ariba solution chosen by the user from the list above.

    Returns:
        JSON with validated_solution, pain_point, and similar_cases (historical signals).
        similar_cases contains: similar_pain_point, solution_area, category, effort,
        timeline, impact — use these as directional hints, not as the final answer.
    """
    validated = normalise_solution(solution)
    if not validated:
        log.warning("   Unknown solution: %s", solution)
        return json.dumps({
            "error": f"Unknown solution '{solution}'. Ask the user to choose from the valid list."
        })

    log.info(">> query_single_pain_point | solution=%s | pain_point=%.80s…", validated, pain_point)
    cases = _retrieve(pain_point, validated, area=None, top_k=3)
    log.info("   Returned %d similar cases", len(cases))

    return json.dumps({
        "pain_point": pain_point,
        "validated_solution": validated,
        "similar_cases": cases,
    }, ensure_ascii=False)


_VLM_SOLUTIONS = {
    "Ariba Sourcing", "Ariba Buying", "Ariba Contracts", "Ariba SLP", "Ariba Supplier Risk",
}


@mcp.tool()
def retrieve_knowledge_context(
    pain_point: str,
    solution: str,
    source_types: list[str] | None = None,
) -> str:
    """
    Retrieve relevant internal knowledge base entries for a single pain point.

    Call this once per row in batch mode (after retrieve_similar_cases_batch),
    then perform a web search, then synthesize the row's output — before moving
    to the next row.

    Args:
        pain_point:   Pain point text (same as passed to query_single_pain_point).
        solution:     Canonical solution name (validated_solution from query_single_pain_point result).
        source_types: Knowledge sources to query. Always pass ["next_gen", "vlm_kpis"].
                      "next_gen": Next-gen SAP Ariba roadmap features (all solutions)
                      "vlm_kpis": Value Lever & KPI index (Ariba Sourcing, Ariba Buying, Ariba Contracts, Ariba SLP, Ariba Supplier Risk)
                      Future sources: "ai_scenarios", "premium_services"

    Returns:
        JSON object with two keys:
        - "IMPORTANT_CONTEXT": warnings about next_gen availability — read before synthesizing.
        - "results": dict keyed by source_type, each value a list of matching entries.
          Access as: response["results"]["next_gen"] and response["results"]["vlm_kpis"]
          next_gen entries: {title, content, solution, release, agent_based, joule_based}
          vlm_kpis entries: {title, value_driver, value_lever, kpi_id, kpi_category, kpi_target, capability, kpi_formula, kpi_meas_freq}
          Empty list means no relevant entries found — use the "no coverage" message.
    """
    if source_types is None:
        source_types = ["next_gen"]

    # Always include vlm_kpis for solutions that have KPI data — do not rely on Joule passing it.
    if solution in _VLM_SOLUTIONS and "vlm_kpis" not in source_types:
        source_types = list(source_types) + ["vlm_kpis"]

    log.info(">> retrieve_knowledge_context | solution=%s | sources=%s | pain_point=%.80s…",
             solution, source_types, pain_point)
    results = _retrieve_knowledge(pain_point, solution, source_types, top_k=5)
    total = sum(len(v) for v in results.values())
    log.info("   Returned %d entries across %d source(s)", total, len(source_types))

    # Strip 'content' from vlm_kpis entries only — they have structured fields sufficient
    # for synthesis. next_gen entries keep 'content' (the feature description text).
    results = {
        st: (entries if st == "next_gen"
             else [{k: v for k, v in e.items() if k != "content"} for e in entries])
        for st, entries in results.items()
    }

    payload = {
        "IMPORTANT_CONTEXT": {
            "next_gen_warning": (
                "ALL entries in next_gen are exclusive to Next-gen SAP Ariba — "
                "a fully re-engineered AI-native platform on SAP BTP released Q1 2026. "
                "These features are NOT available in the current-generation platform. "
                "Clients must transition first (Greenfield or Brownfield). "
                "No new contract needed — delivered under existing subscriptions. "
                "NEVER recommend these as immediately available. "
                "ALWAYS state this context before listing any feature."
            )
        },
        "results": results,
    }
    return json.dumps(payload, ensure_ascii=False)


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
