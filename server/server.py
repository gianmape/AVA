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
from shared.instructions import SINGLE_QUERY_INSTRUCTIONS
from server.recommend import (
    read_excel_painpoints as _read_excel,
    retrieve_similar_cases as _retrieve,
    retrieve_similar_cases_batch as _retrieve_batch,
    write_excel_output as _write_excel,
    retrieve_knowledge_context as _retrieve_knowledge,
    search_documentation as _search_docs,
)
from shared.config import hana_connection, normalise_solution, release_connection

mcp = FastMCP(
    "ava",
    stateless_http=True,
    instructions=f"""
When a user says "Run SVA Analysis" or attaches a pain points Excel file:

STRICT TOOL POLICY — only the following tools may be used. No other tools, commands, or actions are permitted:
  MCP tools:
  - read_excel_painpoints
  - retrieve_similar_cases_batch
  - retrieve_knowledge_context
  - write_excel_output
  - list_ingested_solutions
  - query_single_pain_point
  - rate_recommendation
  Built-in tools (Joule Desktop):
  - web_search (for additional documentation — server also provides URLs via retrieve_knowledge_context)
Do NOT execute terminal commands, read local files directly, run Python scripts, or use any tool not listed above.
Do NOT read JSON files from Joule Desktop temp directories or any other location.

1. Call read_excel_painpoints with the path of the attached file and limit=1 first (offset=0, limit=1).
   This returns total_rows without loading all data. Do NOT read the file yourself.
   MANDATORY BATCHING — no exceptions:
     - ALWAYS use limit=10, regardless of total_rows.
     - Batch 1: call read_excel_painpoints with offset=0,  limit=10
     - Batch 2: call read_excel_painpoints with offset=10, limit=10
     - Batch 3: call read_excel_painpoints with offset=20, limit=10
     - Continue until offset >= total_rows.
   Process each batch fully (steps 2 through 4) before starting the next batch.
   NEVER load more than 10 rows at a time — even if total_rows <= 10, use offset=0, limit=10.

2. For the current batch, call retrieve_similar_cases_batch ONCE with ALL rows in the batch.
   Pass the full batch list — do NOT call it once per row.
   The returned cases are directional signals from human experts — NOT the answer.
   Use similar_pain_point and comments to understand what SAP area to research.
   Use category, effort, timeline, impact as calibration hints for your classifications.

   SIMILARITY SCORE GUIDANCE:
   Each case includes a similarity_score (0–1). Interpret it as follows:
     - ≥ 0.80: Strong match — the historical case is highly relevant, anchor your recommendation on it.
     - 0.65–0.79: Moderate match — use as directional signal but apply your own SAP knowledge.
     - 0.55–0.64: Weak match — treat as background context only, do NOT anchor on it.
   Cases below 0.55 are already filtered out by the server and will not appear.

2.5 For EACH row individually, in sequence — complete ALL four steps before moving to the next row:
   a) Draft a 1–2 sentence English summary of the recommendation you plan to give for this row.
      This becomes the recommendation_hint for step b.
   b) Call retrieve_knowledge_context with:
      - pain_point: the row's pain_point text
      - solution: the row's solution
      - source_types: ["next_gen", "vlm_kpis"]
      - recommendation_hint: the English summary from step a (REQUIRED — the server uses it as the
        primary query for documentation search, which is more accurate than keyword extraction
        from a raw Spanish pain point). Example: "Migrate classic sourcing templates to native
        guided sourcing project templates and reconfigure review tasks."
   c) MANDATORY — you MUST call web_search for this row before synthesizing it or moving to the next row.
      This step is NOT optional. Do NOT batch or defer web searches to after all retrieve_knowledge_context calls.
      Search query: "SAP Ariba [solution] [topic] site:help.sap.com OR site:community.sap.com"
      ONLY use: help.sap.com, community.sap.com, SAP release notes.
      Do NOT use learning.sap.com — the ENTIRE domain is blocked, every URL on it is unreliable.
      Hard cap: MAX 15 searches per batch run. If the cap is reached, set documentation=[] for that row
      and all remaining rows — do NOT reuse URLs from other rows as a substitute.
      A qualifying URL must come from the actual search result (never constructed or guessed) and have
      at least 4 path segments after the domain. If no qualifying URL found → documentation=[].
      UNIQUENESS RULE: each row's Documentation must be unique. If the search returns URLs already used
      in a previous row, run a second search with a more specific query before accepting the duplicate.
      If still no unique URL is found → documentation=[]. Never assign the same URL to two different rows.
   d) Synthesize the full output for this row using your SAP knowledge, the tool result, and the search result.
      Use the "documentation" field from the retrieve_knowledge_context response as a starting point;
      complement or replace with the web_search result if it is more specific.
      LANGUAGE RULE: each row includes a `language` field ("es", "en", "pt"). Write ALL generated text
      for that row in that language. "es"=Spanish · "en"=English · "pt"=Portuguese. Never override with English.

      RECOMMENDATIONS TEXT FORMATTING — MANDATORY rules for readable Excel output.
        The Recommendations field is the longest text in the Excel. Without formatting it becomes
        an unreadable wall of text. You MUST apply ALL of these formatting rules:

        STRUCTURE (in this exact order):
        a) INTRODUCTORY PARAGRAPH: 2–3 sentences framing the diagnosis and general direction.
           This paragraph stands alone — followed by an empty line.
        b) EMPTY LINE (literal newline character) separating intro from steps.
        c) NUMBERED STEPS: each step formatted as:
             "N) TÍTULO EN MAYÚSCULAS: texto explicativo del paso."
           Where N is the step number (1, 2, 3...).
        d) EMPTY LINE between every step — each step is its own visual block.

        MANDATORY FORMAT RULES:
        1. Step title (the text between "N)" and ":") MUST be ENTIRELY IN UPPERCASE.
           ✓ CORRECT: "1) CAPACITAR AL EQUIPO INTERNO:"
           ✗ WRONG:   "1. Capacitar al equipo interno sobre..."
        2. Use "N)" format (number + closing parenthesis), NOT "N." (number + period).
           ✓ CORRECT: "1) IDENTIFICAR..."  "2) VERIFICAR..."
           ✗ WRONG:   "1. Identificar..."  "2. Verificar..."
        3. Each step MUST be separated from the next by a blank line (two newlines).
           The text in the Excel cell must contain literal newline characters between steps.
           ✓ CORRECT: "1) TÍTULO: texto...\n\n2) TÍTULO: texto..."
           ✗ WRONG:   "1) TÍTULO: texto...\n2) TÍTULO: texto..." (no blank line)
           ✗ WRONG:   "1) Título: texto... 2) Título: texto..." (all in one line)
        4. The introductory paragraph must NOT start with a number. It is plain prose.
        5. Do NOT skip the introductory paragraph — jumping straight into "1)..." is not allowed.

        FULL CORRECT EXAMPLE (how it must look in the Excel cell):
        ───────────────────────────────────────────
        La discrepancia entre la fecha de vencimiento del certificado en el reporte y el estado
        real en el perfil del proveedor indica un problema de sincronización de datos. Pasos de
        diagnóstico y resolución:

        1) IDENTIFICAR LA FUENTE DEL REPORTE: Verificar si el reporte está extrayendo datos del
        proyecto de calificación (respuesta al cuestionario) o del perfil del proveedor.

        2) VERIFICAR VERSIONES ACTIVAS: Confirmar que el proveedor tiene un solo proyecto de
        calificación activo con el certificado actualizado.

        3) USAR EL REPORTE CORRECTO: El reporte de 'Respuestas a Cuestionario' estándar de SLP
        debe reflejar la última versión respondida.

        4) CERRAR PROYECTOS OBSOLETOS: Terminar o cancelar proyectos de calificación antiguos
        que puedan estar interfiriendo con la reportería.

        5) VALIDACIÓN: Seleccionar 5 proveedores con discrepancia y revisar manualmente para
        confirmar el patrón.
        ───────────────────────────────────────────

        INCORRECT EXAMPLE (this is what we do NOT want — no blank lines, no uppercase, no intro):
        ───────────────────────────────────────────
        1. Capacitar al equipo interno sobre la diferencia entre cuestionario interno y externo
        en SLP, con énfasis en cuándo NO se debe seleccionar el cuestionario externo. 2. Revisar
        la configuración del template de registro y la lógica de selección del cuestionario
        externo para prevenir selecciones incorrectas. 3. Establecer gobernanza: definir reglas
        claras sobre cuándo aplica el cuestionario externo vs. interno.
        ───────────────────────────────────────────

      BENEFITS FORMATTING — same readability approach:
        - If multiple benefits: use bullet points with blank lines between them:
            "• Reducción del tiempo de ciclo de aprobación en un 40%.\n\n• Eliminación de bloqueos por cuestionarios mal asignados.\n\n• Mayor visibilidad del estado de onboarding."
        - If a single narrative benefit: write as a clear paragraph (2–3 sentences).

   Complete steps a–d fully for row N before starting row N+1. Do NOT parallelize steps b and c across rows.

   DO NOT call retrieve_knowledge_context_batch — use retrieve_knowledge_context once per row as described above.

3. Call write_excel_output EXACTLY ONCE for the entire run — after ALL rows from ALL batches have been synthesized.
   CRITICAL: write_excel_output overwrites the output file completely each time it is called.
   If you call it more than once, only the last call's rows will appear in the final file.

   MULTI-BATCH EXAMPLE: If the Excel has 21 rows processed in 3 batches (10 + 10 + 1):
     - Batch 1: offset=0,  limit=10 → synthesize rows 0–9  → store in memory
     - Batch 2: offset=10, limit=10 → synthesize rows 10–19 → store in memory
     - Batch 3: offset=20, limit=10 → synthesize rows 20    → store in memory
     - THEN: call write_excel_output ONCE with all 21 rows combined

   Accumulate ALL synthesized rows (however many the Excel contained) into a single
   list and pass them all in one call.
   - input_path: the same attachment path passed to read_excel_painpoints
   - rows: the complete list of ALL rows synthesized, each with its original idx value.
     Each row MUST include ALL of these fields:
       pain_point (str): copy from read_excel_painpoints output for this row
       solution (str): copy from read_excel_painpoints output for this row
       Recommendations, Category, Effort, Benefits, Documentation, Timeline, Impact, Ariba Next-Gen, Value KPIs
     pain_point and solution must NEVER be empty or omitted — copy them exactly from step 1.
     LANGUAGE: Recommendations, Benefits, Ariba Next-Gen, Value KPIs MUST be in the same language as the pain point.
               Pass the exact same text you already generated — do NOT translate or rewrite to English.

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

{SINGLE_QUERY_INSTRUCTIONS}
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

    Call first with offset=0, limit=1 to get total_rows without loading all data.
    Then ALWAYS process in batches of 10: offset=0 limit=10, offset=10 limit=10, etc.

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
    "Ariba Catalog", "Ariba Guided Buying", "Ariba Invoice", "Ariba Reporting",
    "Ariba SIPM", "Business Network", "Commerce Automation", "Spend Analysis",
}


@mcp.tool()
def retrieve_knowledge_context(
    pain_point: str,
    solution: str,
    source_types: list[str] | None = None,
    recommendation_hint: str = "",
) -> str:
    """
    Retrieve relevant internal knowledge base entries for a single pain point.

    Call this once per row in batch mode (after retrieve_similar_cases_batch),
    then perform a web search, then synthesize the row's output — before moving
    to the next row.

    Args:
        pain_point:           Pain point text (same as passed to query_single_pain_point).
        solution:             Canonical solution name (validated_solution from query_single_pain_point result).
        source_types:         Knowledge sources to query. Always pass ["next_gen", "vlm_kpis"].
                              "next_gen": Next-gen SAP Ariba roadmap features (all solutions)
                              "vlm_kpis": Value Lever & KPI index (Ariba Sourcing, Ariba Buying, Ariba Contracts, Ariba SLP, Ariba Supplier Risk)
                              Future sources: "ai_scenarios", "premium_services"
        recommendation_hint:  Required in batch mode. Short English summary of the recommendation
                              you plan to synthesize for this row (1–2 sentences, max 80 chars used).
                              Used as the PRIMARY query for the documentation search — it is already
                              in English and more precise than keywords from the raw pain point.
                              If omitted, the server falls back to keyword extraction from pain_point.
                              Always pass this before or together with synthesizing the row.
                              the recommendation for this row.

    Returns:
        JSON object with keys:
        - "IMPORTANT_CONTEXT": warnings about next_gen availability — read before synthesizing.
        - "results": dict keyed by source_type, each value a list of matching entries.
          Access as: response["results"]["next_gen"] and response["results"]["vlm_kpis"]
          next_gen entries: {title, content, solution, release, agent_based, joule_based}
          vlm_kpis entries: {title, value_driver, value_lever, kpi_id, kpi_category, kpi_target, capability, kpi_formula, kpi_meas_freq}
          Empty list means no relevant entries found — use the "no coverage" message.
        - "documentation": list of {"title": ..., "url": ...} — pre-validated SAP Help Portal links.
    """
    if source_types is None:
        source_types = ["next_gen", "workshop"]

    # Always include vlm_kpis for solutions that have KPI data — do not rely on Joule passing it.
    if solution in _VLM_SOLUTIONS and "vlm_kpis" not in source_types:
        source_types = list(source_types) + ["vlm_kpis"]

    # Always include workshop for implementation context.
    if "workshop" not in source_types:
        source_types = list(source_types) + ["workshop"]

    log.info(">> retrieve_knowledge_context | solution=%s | sources=%s | hint=%.60s | pain_point=%.80s…",
             solution, source_types, recommendation_hint, pain_point)
    results = _retrieve_knowledge(pain_point, solution, source_types, top_k=5,
                                    recommendation_hint=recommendation_hint)
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
        "documentation": _search_docs(
            pain_point, solution, max_results=3,
            recommendation_hint=recommendation_hint,
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
def rate_recommendation(
    pain_point_id: str,
    rating: str,
    consultant_id: str = "",
) -> str:
    """
    Rate a historical case that was used in a recommendation — feedback loop for quality improvement.

    Call this when a consultant confirms a recommendation was useful or flags it as irrelevant.
    The rating updates QUALITY_SCORE in the database, improving future retrieval ranking.

    Args:
        pain_point_id: The ID of the historical pain point case (from similar_cases results).
                       If not available, pass the exact similar_pain_point text and the system
                       will look it up.
        rating:        "useful" — the case was relevant and helped the recommendation.
                       "not_useful" — the case was irrelevant or misleading.
        consultant_id: Optional identifier of the consultant providing feedback.

    Returns:
        Confirmation message with updated quality score.
    """
    if rating not in ("useful", "not_useful"):
        return json.dumps({"error": f"Invalid rating '{rating}'. Use 'useful' or 'not_useful'."})

    log.info(">> rate_recommendation | id=%s | rating=%s | consultant=%s", pain_point_id, rating, consultant_id)

    conn = hana_connection()
    cursor = conn.cursor()
    try:
        # Check if pain_point_id is a UUID (32 hex chars or 36 with dashes) or text lookup
        import re
        is_uuid = bool(re.fullmatch(r'[0-9a-fA-F]{32}', pain_point_id) or
                       re.fullmatch(r'[0-9a-fA-F-]{36}', pain_point_id))

        if is_uuid:
            cursor.execute(
                "SELECT ID, USE_COUNT, QUALITY_SCORE FROM SVA2.PAIN_POINTS WHERE ID = ?",
                [pain_point_id],
            )
        else:
            # Fallback: look up by exact pain_point text match
            cursor.execute(
                "SELECT TOP 1 ID, USE_COUNT, QUALITY_SCORE FROM SVA2.PAIN_POINTS WHERE PAIN_POINT = ?",
                [pain_point_id],
            )

        row = cursor.fetchone()
        if not row:
            return json.dumps({"error": f"Pain point not found: '{pain_point_id[:50]}...'"})

        case_id, use_count, current_score = row
        use_count = use_count or 0
        current_score = float(current_score or 0)

        # Update quality score using exponential moving average
        # New score = old_score * decay + new_signal * (1 - decay)
        # useful = 1.0, not_useful = 0.0
        _DECAY = 0.8
        signal = 1.0 if rating == "useful" else 0.0

        if current_score == 0 and use_count <= 1:
            # First rating — set directly
            new_score = signal
        else:
            new_score = current_score * _DECAY + signal * (1 - _DECAY)

        # Clamp to [0, 1]
        new_score = max(0.0, min(1.0, new_score))

        # Update the record
        update_fields = ["QUALITY_SCORE = ?"]
        update_params = [new_score]

        if consultant_id:
            update_fields.append("APPROVED_BY = ?")
            update_params.append(consultant_id)
            update_fields.append("APPROVED_AT = CURRENT_TIMESTAMP")

        update_params.append(case_id)
        cursor.execute(
            f"UPDATE SVA2.PAIN_POINTS SET {', '.join(update_fields)} WHERE ID = ?",
            update_params,
        )
        conn.commit()

        log.info("   Updated case %s: quality_score %.2f → %.2f (rating=%s)",
                 case_id, current_score, new_score, rating)

        return json.dumps({
            "success": True,
            "case_id": case_id,
            "previous_score": round(current_score, 3),
            "new_score": round(new_score, 3),
            "rating": rating,
            "message": f"Quality score updated: {current_score:.3f} → {new_score:.3f}",
        })
    except Exception as e:
        log.error("rate_recommendation failed: %s", e, exc_info=True)
        return json.dumps({"error": str(e)})
    finally:
        cursor.close()
        release_connection(conn)


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
