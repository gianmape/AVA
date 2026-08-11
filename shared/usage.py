"""
shared/usage.py
---------------
Usage tracking: log every MCP tool invocation and query adoption metrics.

log_usage()       — fire-and-forget INSERT into SVA2.USAGE_LOG
query_metrics()   — aggregated metrics for get_adoption_metrics tool
"""
import json
import logging
import time
import uuid
from datetime import datetime, timezone

from shared.config import hana_connection, release_connection

log = logging.getLogger("sva2.usage")

# ---------------------------------------------------------------------------
# Write: log a tool invocation
# ---------------------------------------------------------------------------

def log_usage(
    *,
    tool_name: str,
    client_ip: str | None = None,
    solution: str | None = None,
    pain_point_summary: str | None = None,
    results_count: int | None = None,
    top_similarity: float | None = None,
    response_time_ms: int | None = None,
    success: bool = True,
) -> None:
    """Insert a usage event into USAGE_LOG. Best-effort — never raises."""
    try:
        conn = hana_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO SVA2.USAGE_LOG "
                "(ID, TS, CLIENT_IP, TOOL_NAME, SOLUTION, PAIN_POINT_SUMMARY, "
                " RESULTS_COUNT, TOP_SIMILARITY, RESPONSE_TIME_MS, SUCCESS) "
                "VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    uuid.uuid4().hex,
                    client_ip,
                    tool_name,
                    solution,
                    (pain_point_summary or "")[:200] or None,
                    results_count,
                    top_similarity,
                    response_time_ms,
                    success,
                ],
            )
            conn.commit()
        finally:
            cursor.close()
            release_connection(conn)
    except Exception as e:
        log.warning("log_usage failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# Read: query aggregated metrics
# ---------------------------------------------------------------------------

_PERIOD_MAP = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "all": None,
}


def query_metrics(period: str = "7d", group_by: str = "summary") -> dict:
    """
    Return adoption metrics from USAGE_LOG.

    Args:
        period:   "7d", "30d", "90d", or "all"
        group_by: "summary" | "user" | "solution" | "tool" | "daily"

    Returns:
        dict ready to be serialized as JSON for the MCP tool response.
    """
    days = _PERIOD_MAP.get(period)
    time_filter = f"WHERE TS >= ADD_DAYS(CURRENT_TIMESTAMP, -{days})" if days else ""

    conn = hana_connection()
    cursor = conn.cursor()
    try:
        if group_by == "summary":
            return _summary(cursor, time_filter, period)
        elif group_by == "user":
            return _by_user(cursor, time_filter, period)
        elif group_by == "solution":
            return _by_solution(cursor, time_filter, period)
        elif group_by == "tool":
            return _by_tool(cursor, time_filter, period)
        elif group_by == "daily":
            return _by_daily(cursor, time_filter, period)
        else:
            return {"error": f"Invalid group_by '{group_by}'. Use: summary, user, solution, tool, daily."}
    finally:
        cursor.close()
        release_connection(conn)


def _summary(cursor, time_filter: str, period: str) -> dict:
    cursor.execute(f"""
        SELECT
            COUNT(*) AS total_queries,
            COUNT(DISTINCT CLIENT_IP) AS unique_clients,
            SUM(CASE WHEN SUCCESS = TRUE THEN 1 ELSE 0 END) AS successful,
            AVG(RESPONSE_TIME_MS) AS avg_latency_ms,
            MEDIAN(RESPONSE_TIME_MS) AS p50_latency_ms,
            AVG(TOP_SIMILARITY) AS avg_similarity
        FROM SVA2.USAGE_LOG
        {time_filter}
    """)
    row = cursor.fetchone()
    total, clients, successful, avg_lat, p50_lat, avg_sim = row

    # Top solution
    cursor.execute(f"""
        SELECT SOLUTION, COUNT(*) AS cnt
        FROM SVA2.USAGE_LOG
        {time_filter}
        {"AND" if time_filter else "WHERE"} SOLUTION IS NOT NULL
        GROUP BY SOLUTION ORDER BY cnt DESC
        LIMIT 3
    """)
    top_solutions = [{"solution": r[0], "queries": r[1]} for r in cursor.fetchall()]

    return {
        "period": period,
        "group_by": "summary",
        "total_queries": total or 0,
        "unique_clients": clients or 0,
        "success_rate": round((successful or 0) / max(total or 1, 1), 3),
        "avg_latency_ms": round(avg_lat or 0),
        "p50_latency_ms": round(p50_lat or 0),
        "avg_similarity": round(float(avg_sim or 0), 4),
        "top_solutions": top_solutions,
    }


def _by_user(cursor, time_filter: str, period: str) -> dict:
    cursor.execute(f"""
        SELECT
            CLIENT_IP,
            COUNT(*) AS queries,
            MAX(TS) AS last_active,
            COUNT(DISTINCT TO_DATE(TS)) AS active_days,
            AVG(RESPONSE_TIME_MS) AS avg_latency_ms
        FROM SVA2.USAGE_LOG
        {time_filter}
        GROUP BY CLIENT_IP
        ORDER BY queries DESC
        LIMIT 50
    """)
    clients = []
    for client_ip, queries, last_active, active_days, avg_lat in cursor.fetchall():
        clients.append({
            "client_ip": client_ip or "unknown",
            "queries": queries,
            "last_active": last_active.isoformat() if last_active else None,
            "active_days": active_days,
            "avg_latency_ms": round(avg_lat or 0),
        })

    return {"period": period, "group_by": "client", "clients": clients}


def _by_solution(cursor, time_filter: str, period: str) -> dict:
    cursor.execute(f"""
        SELECT
            SOLUTION,
            COUNT(*) AS queries,
            COUNT(DISTINCT CLIENT_IP) AS unique_clients,
            AVG(TOP_SIMILARITY) AS avg_similarity,
            SUM(CASE WHEN RESULTS_COUNT = 0 THEN 1 ELSE 0 END) AS empty_results
        FROM SVA2.USAGE_LOG
        {time_filter}
        {"AND" if time_filter else "WHERE"} SOLUTION IS NOT NULL
        GROUP BY SOLUTION
        ORDER BY queries DESC
    """)
    solutions = []
    for sol, queries, clients, avg_sim, empty in cursor.fetchall():
        solutions.append({
            "solution": sol,
            "queries": queries,
            "unique_clients": clients,
            "avg_similarity": round(float(avg_sim or 0), 4),
            "empty_rate": round((empty or 0) / max(queries, 1), 3),
        })

    return {"period": period, "group_by": "solution", "solutions": solutions}


def _by_tool(cursor, time_filter: str, period: str) -> dict:
    cursor.execute(f"""
        SELECT
            TOOL_NAME,
            COUNT(*) AS queries,
            COUNT(DISTINCT CLIENT_IP) AS unique_clients,
            AVG(RESPONSE_TIME_MS) AS avg_latency_ms,
            SUM(CASE WHEN SUCCESS = TRUE THEN 1 ELSE 0 END) AS successful
        FROM SVA2.USAGE_LOG
        {time_filter}
        GROUP BY TOOL_NAME
        ORDER BY queries DESC
    """)
    tools = []
    for tool, queries, clients, avg_lat, successful in cursor.fetchall():
        tools.append({
            "tool": tool,
            "queries": queries,
            "unique_clients": clients,
            "avg_latency_ms": round(avg_lat or 0),
            "success_rate": round((successful or 0) / max(queries, 1), 3),
        })

    return {"period": period, "group_by": "tool", "tools": tools}


def _by_daily(cursor, time_filter: str, period: str) -> dict:
    cursor.execute(f"""
        SELECT
            TO_DATE(TS) AS day,
            COUNT(*) AS queries,
            COUNT(DISTINCT CLIENT_IP) AS unique_clients,
            AVG(RESPONSE_TIME_MS) AS avg_latency_ms
        FROM SVA2.USAGE_LOG
        {time_filter}
        GROUP BY TO_DATE(TS)
        ORDER BY day DESC
        LIMIT 90
    """)
    days = []
    for day, queries, clients, avg_lat in cursor.fetchall():
        days.append({
            "date": day.isoformat() if hasattr(day, 'isoformat') else str(day),
            "queries": queries,
            "unique_clients": clients,
            "avg_latency_ms": round(avg_lat or 0),
        })

    return {"period": period, "group_by": "daily", "daily": days}
