"""
ingestion/analytics.py
----------------------
Quality and distribution analytics for SVA2.0 data.

Produces a report of:
  - PAIN_POINTS: solution distribution, quality score stats, use_count stats,
    similarity score distribution (requires running sample queries)
  - KNOWLEDGE_BASE: source_type distribution, solution coverage, NULL patterns

Run:
    python ingestion/analytics.py [--verbose]
"""
import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from shared.config import hana_connection, release_connection


def run_analytics(verbose: bool = False) -> None:
    conn = hana_connection()
    cursor = conn.cursor()
    try:
        print("=" * 60)
        print("SVA2.0 DATA ANALYTICS REPORT")
        print("=" * 60)

        # ---------------------------------------------------------------
        # PAIN_POINTS table
        # ---------------------------------------------------------------
        print("\n" + "─" * 60)
        print("📊 PAIN_POINTS — Solution distribution")
        print("─" * 60)
        cursor.execute(
            "SELECT SOLUTION, COUNT(*) AS CNT "
            "FROM SVA2.PAIN_POINTS GROUP BY SOLUTION ORDER BY CNT DESC"
        )
        total_pp = 0
        for sol, cnt in cursor.fetchall():
            print(f"  {sol:<30s} {cnt:>5d}")
            total_pp += cnt
        print(f"  {'TOTAL':<30s} {total_pp:>5d}")

        print("\n" + "─" * 60)
        print("⭐ PAIN_POINTS — Quality Score distribution")
        print("─" * 60)
        cursor.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN QUALITY_SCORE IS NULL THEN 1 ELSE 0 END) AS null_qs,
                SUM(CASE WHEN QUALITY_SCORE > 0 THEN 1 ELSE 0 END) AS rated,
                AVG(CASE WHEN QUALITY_SCORE > 0 THEN QUALITY_SCORE END) AS avg_rated,
                MIN(CASE WHEN QUALITY_SCORE > 0 THEN QUALITY_SCORE END) AS min_rated,
                MAX(CASE WHEN QUALITY_SCORE > 0 THEN QUALITY_SCORE END) AS max_rated
            FROM SVA2.PAIN_POINTS
        """)
        row = cursor.fetchone()
        total, null_qs, rated, avg_r, min_r, max_r = row
        print(f"  Total rows:           {total}")
        print(f"  NULL quality_score:   {null_qs} ({null_qs*100//total}%)")
        print(f"  Rated (score > 0):    {rated}")
        if rated and rated > 0:
            print(f"  Avg rated score:      {float(avg_r):.3f}")
            print(f"  Min rated score:      {float(min_r):.3f}")
            print(f"  Max rated score:      {float(max_r):.3f}")

        print("\n" + "─" * 60)
        print("🔄 PAIN_POINTS — USE_COUNT distribution")
        print("─" * 60)
        cursor.execute("""
            SELECT
                SUM(CASE WHEN USE_COUNT = 0 THEN 1 ELSE 0 END) AS never_used,
                SUM(CASE WHEN USE_COUNT BETWEEN 1 AND 5 THEN 1 ELSE 0 END) AS low_use,
                SUM(CASE WHEN USE_COUNT BETWEEN 6 AND 20 THEN 1 ELSE 0 END) AS medium_use,
                SUM(CASE WHEN USE_COUNT > 20 THEN 1 ELSE 0 END) AS high_use,
                AVG(USE_COUNT) AS avg_use,
                MAX(USE_COUNT) AS max_use
            FROM SVA2.PAIN_POINTS
        """)
        never, low, medium, high, avg_u, max_u = cursor.fetchone()
        print(f"  Never used (0):       {never}")
        print(f"  Low (1-5):            {low}")
        print(f"  Medium (6-20):        {medium}")
        print(f"  High (>20):           {high}")
        print(f"  Average USE_COUNT:    {float(avg_u):.1f}")
        print(f"  Max USE_COUNT:        {max_u}")

        print("\n" + "─" * 60)
        print("📁 PAIN_POINTS — Category distribution")
        print("─" * 60)
        cursor.execute(
            "SELECT COALESCE(CATEGORY, '(NULL)'), COUNT(*) "
            "FROM SVA2.PAIN_POINTS GROUP BY CATEGORY ORDER BY COUNT(*) DESC"
        )
        for cat, cnt in cursor.fetchall():
            print(f"  {cat:<30s} {cnt:>5d}")

        print("\n" + "─" * 60)
        print("⏱️ PAIN_POINTS — Effort distribution")
        print("─" * 60)
        cursor.execute(
            "SELECT COALESCE(EFFORT, '(NULL)'), COUNT(*) "
            "FROM SVA2.PAIN_POINTS GROUP BY EFFORT ORDER BY COUNT(*) DESC"
        )
        for eff, cnt in cursor.fetchall():
            print(f"  {eff:<30s} {cnt:>5d}")

        # ---------------------------------------------------------------
        # KNOWLEDGE_BASE table
        # ---------------------------------------------------------------
        print("\n" + "─" * 60)
        print("📚 KNOWLEDGE_BASE — Source type distribution")
        print("─" * 60)
        cursor.execute(
            "SELECT SOURCE_TYPE, COUNT(*) AS CNT "
            "FROM SVA2.KNOWLEDGE_BASE GROUP BY SOURCE_TYPE ORDER BY CNT DESC"
        )
        total_kb = 0
        for st, cnt in cursor.fetchall():
            print(f"  {st:<30s} {cnt:>5d}")
            total_kb += cnt
        print(f"  {'TOTAL':<30s} {total_kb:>5d}")

        print("\n" + "─" * 60)
        print("📚 KNOWLEDGE_BASE — Solution coverage (non-NULL)")
        print("─" * 60)
        cursor.execute("""
            SELECT SOURCE_TYPE, SOLUTION, COUNT(*) AS CNT
            FROM SVA2.KNOWLEDGE_BASE
            WHERE SOLUTION IS NOT NULL
            GROUP BY SOURCE_TYPE, SOLUTION
            ORDER BY SOURCE_TYPE, CNT DESC
        """)
        current_st = None
        for st, sol, cnt in cursor.fetchall():
            if st != current_st:
                current_st = st
                print(f"\n  [{st}]")
            print(f"    {sol:<30s} {cnt:>4d}")

        print("\n" + "─" * 60)
        print("📚 KNOWLEDGE_BASE — NULL solution count per source_type")
        print("─" * 60)
        cursor.execute("""
            SELECT SOURCE_TYPE,
                   SUM(CASE WHEN SOLUTION IS NULL THEN 1 ELSE 0 END) AS null_sol,
                   COUNT(*) AS total
            FROM SVA2.KNOWLEDGE_BASE
            GROUP BY SOURCE_TYPE
            ORDER BY SOURCE_TYPE
        """)
        for st, null_sol, total_st in cursor.fetchall():
            pct = null_sol * 100 // total_st if total_st > 0 else 0
            print(f"  {st:<20s} NULL: {null_sol:>4d} / {total_st:>4d} ({pct}%)")

        # ---------------------------------------------------------------
        # Retrieval effectiveness indicators
        # ---------------------------------------------------------------
        print("\n" + "─" * 60)
        print("🎯 RETRIEVAL — Top used cases (USE_COUNT > 10)")
        print("─" * 60)
        cursor.execute("""
            SELECT TOP 10 SOLUTION, LEFT(PAIN_POINT, 80) AS PP,
                   USE_COUNT, COALESCE(QUALITY_SCORE, 0) AS QS
            FROM SVA2.PAIN_POINTS
            WHERE USE_COUNT > 10
            ORDER BY USE_COUNT DESC
        """)
        rows = cursor.fetchall()
        if rows:
            for sol, pp, uc, qs in rows:
                print(f"  [{sol}] USE={uc} QS={float(qs):.2f}")
                print(f"    {pp}")
        else:
            print("  No cases with USE_COUNT > 10 yet.")

        print("\n" + "─" * 60)
        print("📈 RETRIEVAL — Threshold analysis (sample)")
        print("─" * 60)
        print(f"  Current SIMILARITY_THRESHOLD: {os.environ.get('SIMILARITY_THRESHOLD', '0.55')}")
        cursor.execute("""
            SELECT
                SUM(CASE WHEN QUALITY_SCORE >= 0.8 THEN 1 ELSE 0 END) AS excellent,
                SUM(CASE WHEN QUALITY_SCORE >= 0.5 AND QUALITY_SCORE < 0.8 THEN 1 ELSE 0 END) AS good,
                SUM(CASE WHEN QUALITY_SCORE > 0 AND QUALITY_SCORE < 0.5 THEN 1 ELSE 0 END) AS poor,
                COUNT(*) AS total
            FROM SVA2.PAIN_POINTS
            WHERE QUALITY_SCORE IS NOT NULL AND QUALITY_SCORE > 0
        """)
        row = cursor.fetchone()
        if row and row[3] > 0:
            exc, good, poor, total_r = row
            print(f"  Rated cases: {total_r}")
            print(f"    Excellent (≥0.8):  {exc}")
            print(f"    Good (0.5-0.8):    {good}")
            print(f"    Poor (<0.5):       {poor}")
        else:
            print("  No rated cases yet — run rate_recommendation to build this data.")

        print("\n" + "=" * 60)
        print("END OF REPORT")
        print("=" * 60)

    finally:
        cursor.close()
        release_connection(conn)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="SVA2.0 data quality analytics")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    run_analytics(verbose=args.verbose)
