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
from shared.config import hana_connection, normalise_solution

mcp = FastMCP(
    "painpoints",
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

1. Call read_excel_painpoints with the path of the attached file.
   Do NOT read the file yourself. Do NOT add rows beyond what this tool returns.

2. Call retrieve_similar_cases_batch ONCE with ALL rows returned from step 1.
   Pass the full list — do NOT call it once per row.
   The returned cases are directional signals from human experts — NOT the answer.
   Use similar_pain_point and comments to understand what SAP area to research.
   Use category, effort, timeline, impact as calibration hints for your classifications.

2.5 Call retrieve_knowledge_context ONCE with ALL rows as separate calls — one per row:
   - pain_point: the pain point text of each row
   - solution: the solution of each row
   - source_types: ["next_gen", "vlm_kpis"] — always include both
   Store the results indexed by idx for use in step 3.

LANGUAGE RULE: Each row from read_excel_painpoints includes a `language` field (e.g. "es", "en", "pt").
You MUST write ALL generated text for that row (Recommendations, Benefits, Ariba Next-Gen) in that language.
"es" = Spanish, "en" = English, "pt" = Portuguese. This is mandatory — never override with English.

3. For EACH row, research SAP documentation — HARD LIMIT: no more than 3 sources per pain point.
   ONLY use these sources: help.sap.com, community.sap.com, learning.sap.com, SAP release notes.
   Do NOT use any other external websites, blogs, or non-SAP sources.
   Then synthesize your own original output:
   - Recommendations: actionable steps grounded in your SAP documentation research.
     Do NOT copy from historical cases — use them only to understand what area to explore.
   - Category: one of — Feature Adoption, Innovation, Q&A, Process Change, Training, Roadmap Discussion
   - Effort: use the full label including description — Low (1 – 3 Days) | Medium (1 – 3 Weeks) | High (1 – 2 Months) | Complex (3+ Months) | N/A
   - Benefits: expected business outcome, informed by SAP best practices (same language as pain point)
   - Documentation: specific, relevant SAP help articles, community posts, or learning resources
     you found during your research. Return as JSON array of {"title": "...", "url": "..."}.
     Prioritize specific and actionable links over generic landing pages.
   - Timeline: use the full label including description — Quick Win (Within 1 week) | Short Term (1 – 3 Weeks) | Mid Term (1 – 3 Months) | Long Term (3+ Months)
   - Impact: Low | Medium | High  (use historical signals as a hint)
   - Ariba Next-Gen: based on retrieve_knowledge_context results for this row's idx:
       * The features in next_gen belong to Next-gen SAP Ariba (AI-native platform on SAP BTP, Q1 2026).
         They are NOT available in the current platform — client must transition first (Greenfield or Brownfield).
         No new contract needed — delivered under existing subscriptions.
         NEVER present these as immediately available.
       * Write ONLY the feature names, Release, and Agent-based/Joule-based tags (if Yes) — no introductory context block, no warnings, no disclaimers.
       * If relevant features found: write a concise text explaining which Next-gen feature(s) address this pain point.
         For each feature include: name, Release, and — ONLY if the value is "Yes" — mention Agent-based and/or Joule-based explicitly.
       * If no relevant features: write exactly "No Next-gen coverage identified."
   - Value KPIs: based on the vlm_kpis results from retrieve_knowledge_context for this row's idx.
       * Available for: Ariba Sourcing, Ariba Buying, Ariba Contracts, Ariba SLP, Ariba Supplier Risk.
         For other solutions write "No KPI data available for this solution."
       * If relevant KPIs found: for each KPI write a single line:
         [KPI Name] (ID: [kpi_id]) — [value_driver] · [value_lever] | Target: [kpi_target]
         List up to 3 KPIs ordered by relevance. Omit any KPI where kpi_id or kpi_target is null.
       * If no relevant KPIs: write exactly "No Value KPIs identified."

4. Call write_excel_output with:
   - input_path: the same attachment path passed to read_excel_painpoints
   - rows: a list containing ONLY the rows from step 3, each with its original idx value.
     Each row must include: Recommendations, Category, Effort, Benefits, Documentation, Timeline, Impact, Ariba Next-Gen, Value KPIs

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

---

SINGLE PAIN POINT QUERY MODE

When a user describes a pain point in text (without attaching an Excel file), activate single query mode.

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

3. After both tools return, synthesize the full recommendation for this single pain point.
   Research SAP documentation — HARD LIMIT: no more than 3 sources.
   ONLY use these sources: help.sap.com, community.sap.com, learning.sap.com, SAP release notes.
   Do NOT use any other external websites, blogs, or non-SAP sources.
   Generate ALL text in the same language as the pain_point.

4. Present the result using ONLY this card format — no extra text before or after:

---
**Pain Point:** [original pain point text]
**Solution:** [canonical solution name]

**Recommendation:**
[synthesized actionable recommendation]

**Benefits:**
[expected business outcome]

**SVA Analysis**

| Field | Value | Short Description |
|---|---|---|
| Category | [classified value] | Feature Adoption: not using an existing feature · Innovation: new or non-standard approach · Training: lack of knowledge or incorrect usage · Process Change: redesign of a business process · Q&A: informational question with a documented answer · Roadmap Discussion: future SAP feature may address this |
| Effort | [full label with description] | |
| Timeline | [full label with description] | |
| Impact | To be assessed | To be assessed by the consultant based on the client's specific context and priorities |

**Documentation:**
- [Article title](url)
- [Article title](url)

**Next-gen Coverage:**
[CRITICAL RULES FOR THIS SECTION — violations are not acceptable:
 1. NEVER present Next-gen features as available today or recommend them for immediate use.
 2. ALWAYS start this section with the context block below (translated to the pain point language) BEFORE listing any feature.
 3. If retrieve_knowledge_context returns an empty list, write only: "No Next-gen feature identified for this pain point in the current roadmap."

 MANDATORY context block (always first, always present when features are listed):
 "⚠ The following features belong to Next-gen SAP Ariba — a fully re-engineered AI-native platform built on SAP BTP, released Q1 2026. These capabilities are NOT available in the current-generation platform. Accessing them requires a transition (Greenfield or Brownfield migration). No new contract is needed — Next-gen is delivered under existing subscriptions, but readiness and complexity must be assessed first."

 After the context block, list each relevant feature using EXACTLY this format — one bullet per feature:
 • [title] (Release: [release][, Agent-based][, Joule-based]) — [one sentence on how it addresses the pain point]
 Include "Agent-based" in the parenthesis ONLY if agent_based = "Yes". Include "Joule-based" ONLY if joule_based = "Yes". Omit both tags if both are "No".
 Never omit Release.

**Value KPIs:**
[Rules for this section:
 1. Only populate when validated_solution is one of: Ariba Sourcing, Ariba Buying, Ariba Contracts, Ariba SLP, Ariba Supplier Risk — for other solutions write: "No KPI data available for this solution."
 2. If vlm_kpis results are empty, write: "No Value KPIs identified for this pain point."
 3. If relevant KPIs found, list up to 3 using EXACTLY this format — one bullet per KPI:
    • [KPI Name] (ID: [kpi_id]) — [value_driver] · [value_lever]
      Measure: [content / formula]
      Target: [kpi_target]
 4. After the KPI list, add one line: "Source: SAP APM KPI Catalog — me.sap.com/app/kpicatalog"
---

   Rules for the card:
   - Category must be one of: Feature Adoption, Innovation, Q&A, Process Change, Training, Roadmap Discussion
   - Effort must use the full label: Low (1 – 3 Days) | Medium (1 – 3 Weeks) | High (1 – 2 Months) | Complex (3+ Months) | N/A
   - Timeline must use the full label: Quick Win (Within 1 week) | Short Term (1 – 3 Weeks) | Mid Term (1 – 3 Months) | Long Term (3+ Months)
   - Impact: ALWAYS use "To be assessed" as value — never classify Low/Medium/High
   - Short Description column: write ONLY the description that matches the classified value, not all options
   - Documentation: list only specific, actionable links — no generic landing pages
   - Do NOT show a KPI dashboard for single queries
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
                       - Ariba Next-Gen (str): Next-gen coverage text or "No Next-gen coverage identified."
                       - Value KPIs (str): KPI block text or "No Value KPIs identified." (Ariba Sourcing only)
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


@mcp.tool()
def retrieve_knowledge_context(
    pain_point: str,
    solution: str,
    source_types: list[str] = ["next_gen"],
) -> str:
    """
    Retrieve relevant internal knowledge base entries for a single pain point.

    Call this immediately after query_single_pain_point, before synthesizing the response.
    Use the results to generate the knowledge-based fields in the card.

    Args:
        pain_point:   Pain point text (same as passed to query_single_pain_point).
        solution:     Canonical solution name (validated_solution from query_single_pain_point result).
        source_types: Knowledge sources to query. Supported: ["next_gen", "vlm_kpis"]
                      "next_gen": Next-gen SAP Ariba roadmap features (all solutions)
                      "vlm_kpis": Value Lever & KPI index (Ariba Sourcing only)
                      Future sources: "ai_scenarios", "premium_services"

    Returns:
        JSON dict keyed by source_type. Each value is a list of matching entries.
        next_gen entries: {title, content, solution, release, agent_based, joule_based}
        vlm_kpis entries: {title, content, value_driver, value_lever, kpi_id, kpi_category, kpi_target}
        Empty list means no relevant entries found — use the "no coverage" message.
    """
    log.info(">> retrieve_knowledge_context | solution=%s | sources=%s | pain_point=%.80s…",
             solution, source_types, pain_point)
    results = _retrieve_knowledge(pain_point, solution, source_types, top_k=5)
    total = sum(len(v) for v in results.values())
    log.info("   Returned %d entries across %d source(s)", total, len(source_types))

    # Wrap results with mandatory context so Joule cannot omit it
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
