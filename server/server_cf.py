"""
server/server_cf.py
-------------------
MCP server for Cloud Foundry deployment — single query mode only.

Exposes only:
  - query_single_pain_point
  - retrieve_knowledge_context
  - list_ingested_solutions

Batch Excel tools (read_excel_painpoints, write_excel_output,
retrieve_similar_cases_batch) are NOT available — no local file access in CF.

Run (CF via HTTP):
    python server/server_cf.py --http --port 8080
"""
import json
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MCP-CF] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("sva2.cf")

from dotenv import load_dotenv
load_dotenv()

# CF runs single query mode only — sequential HANA access, never parallel.
# Pool of 2 is sufficient and avoids opening 10 idle connections on startup.
# Uses setdefault so an explicit HANA_POOL_SIZE in .env is always respected.
import os as _os
_os.environ.setdefault("HANA_POOL_SIZE", "2")

from mcp.server.fastmcp import FastMCP
from shared.instructions import SINGLE_QUERY_INSTRUCTIONS
from server.recommend import (
    retrieve_similar_cases as _retrieve,
    retrieve_knowledge_context as _retrieve_knowledge,
    search_documentation as _search_docs,
)
from shared.config import hana_connection, normalise_solution, release_connection

mcp = FastMCP(
    "ava",
    stateless_http=True,
    instructions=f"""
TOOL POLICY — STRICT. Violation = wrong behavior.
  MCP tools (provided by this server):
    - query_single_pain_point
    - retrieve_knowledge_context
    - list_ingested_solutions
    - rate_recommendation
  Built-in tools (Joule Desktop):
    - web_search (optional fallback — documentation is provided server-side via retrieve_knowledge_context)
  FORBIDDEN: terminal commands, Python scripts, local file reads, any code execution, any tool not listed above.

{SINGLE_QUERY_INSTRUCTIONS}
""",
)
log.info("=== AVA MCP server (CF / single query) starting ===")

_VLM_SOLUTIONS = {
    "Ariba Sourcing", "Ariba Buying", "Ariba Contracts", "Ariba SLP", "Ariba Supplier Risk",
}


@mcp.tool()
def read_excel_painpoints(input_path: str, offset: int = 0, limit: int = 0) -> str:
    """
    Read pain point rows from an Excel file for batch SVA analysis.
    This feature is not yet available in the cloud deployment.
    """
    log.info(">> read_excel_painpoints called (CF stub) — batch mode not available")
    return json.dumps({
        "error": "batch_not_available",
        "message": (
            "⚠️ **Batch Excel analysis is currently unavailable in this deployment.**\n\n"
            "This is due to a current limitation of Joule Desktop (JWD), which does not yet support "
            "file transfer to external MCP servers. Once JWD adds this capability, batch processing "
            "will be enabled automatically.\n\n"
            "In the meantime, you can analyze pain points one by one directly in chat — "
            "just describe the pain point and I'll generate a full SVA recommendation card."
        ),
    }, ensure_ascii=False)


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
    source_types: list[str] | None = None,
    recommendation_hint: str = "",
) -> str:
    """
    Retrieve relevant internal knowledge base entries for a single pain point.

    Call this immediately after query_single_pain_point, before synthesizing the response.
    Use the results to generate the knowledge-based fields in the card.

    Args:
        pain_point:          Pain point text (same as passed to query_single_pain_point).
        solution:            Canonical solution name (validated_solution from query_single_pain_point result).
        source_types:        Knowledge sources to query. Always pass ["next_gen", "vlm_kpis"].
                             "next_gen": Next-gen SAP Ariba roadmap features (all solutions)
                             "vlm_kpis": Value Lever & KPI index (Ariba Sourcing, Ariba Buying, Ariba Contracts, Ariba SLP, Ariba Supplier Risk)
        recommendation_hint: Optional. Short English summary (1–2 sentences, max 80 chars used) of
                             the recommendation you plan to synthesize. Used as the primary query
                             for the documentation search — more precise than keyword extraction
                             from a raw pain point, especially when the pain point is in Spanish.
                             Pass this if you have already drafted a direction for the recommendation.

    Returns:
        JSON dict with keys: "IMPORTANT_CONTEXT", "results", "documentation".
        results: dict keyed by source_type, each value a list of matching entries.
        next_gen entries: {title, content, solution, release, agent_based, joule_based}
        vlm_kpis entries: {title, value_driver, value_lever, kpi_id, kpi_category, kpi_target, capability, kpi_formula, kpi_meas_freq}
        documentation: list of {"title": ..., "url": ...} — pre-validated SAP Help Portal links.
        Empty list means no relevant entries found — use the "no coverage" message.
    """
    if source_types is None:
        source_types = ["next_gen", "workshop"]

    if solution in _VLM_SOLUTIONS and "vlm_kpis" not in source_types:
        source_types = list(source_types) + ["vlm_kpis"]

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
    log.info("   Documentation URLs returned: %d", len(payload["documentation"]))
    return json.dumps(payload, ensure_ascii=False)


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
        import re
        is_uuid = bool(re.fullmatch(r'[0-9a-fA-F]{32}', pain_point_id) or
                       re.fullmatch(r'[0-9a-fA-F-]{36}', pain_point_id))

        if is_uuid:
            cursor.execute(
                "SELECT ID, USE_COUNT, QUALITY_SCORE FROM SVA2.PAIN_POINTS WHERE ID = ?",
                [pain_point_id],
            )
        else:
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

        _DECAY = 0.8
        signal = 1.0 if rating == "useful" else 0.0

        if current_score == 0 and use_count <= 1:
            new_score = signal
        else:
            new_score = current_score * _DECAY + signal * (1 - _DECAY)

        new_score = max(0.0, min(1.0, new_score))

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


def _build_http_app():
    import os
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse

    expected_header = os.environ.get("JWD_CALLER_HEADER", "").strip().lower()
    expected_value  = os.environ.get("JWD_CALLER_VALUE",  "").strip()
    guard_active    = bool(expected_header and expected_value)

    if guard_active:
        log.info("=== Caller guard ACTIVE — header=%r expected=%r ===", expected_header, expected_value)
    else:
        log.info("=== Caller guard INACTIVE — logging headers only ===")

    _SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key", "x-auth-token"}

    class CallerMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            header_dump = " | ".join(
                f"{k}=<redacted>" if k.lower() in _SENSITIVE_HEADERS else f"{k}={v}"
                for k, v in request.headers.items()
            )
            log.info("HEADERS [%s %s] %s", request.method, request.url.path, header_dump)

            if guard_active:
                actual = request.headers.get(expected_header, "")
                if actual != expected_value:
                    log.warning(
                        "REJECTED — header %r=%r (expected %r) from %s",
                        expected_header, actual, expected_value,
                        request.client.host if request.client else "unknown",
                    )
                    return JSONResponse(
                        {"error": "Unauthorized caller — only Joule Desktop is allowed."},
                        status_code=403,
                    )

            return await call_next(request)

    base_app = mcp.streamable_http_app()
    return Starlette(
        middleware=[Middleware(CallerMiddleware)],
        routes=base_app.routes,
        lifespan=base_app.router.lifespan_context,
    )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true", help="Run as HTTP server (CF)")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if args.http:
        import uvicorn
        host = "0.0.0.0"  # CF requires binding to all interfaces
        app = _build_http_app()
        uvicorn.run(app, host=host, port=args.port)
    else:
        mcp.run(transport="stdio")