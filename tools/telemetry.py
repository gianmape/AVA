#!/usr/bin/env python3
"""
tools/telemetry.py
------------------
Parse mcp_server.log for BATCH TELEMETRY and retrieve_similar_cases entries.
Outputs a summary report of similarity score health over time.

Usage:
    python tools/telemetry.py                    # Default: ./mcp_server.log
    python tools/telemetry.py path/to/log.log    # Custom log path
    python tools/telemetry.py --json             # Output as JSON
"""
import re
import sys
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


# Regex patterns for log lines
_BATCH_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*BATCH TELEMETRY: "
    r"(\d+) items \| avg_similarity=([\d.]+) \| min=([\d.]+) \| max=([\d.]+) \| empty=(\d+)/(\d+)"
)
_BATCH_EMPTY_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*BATCH TELEMETRY: "
    r"(\d+) items \| ALL returned empty"
)
_SINGLE_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*retrieve_similar_cases: "
    r"(\d+) cases above threshold [\d.]+ \(top score: ([\d.]+), weighted: ([\d.]+)\)"
)
_SINGLE_EMPTY_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*retrieve_similar_cases: "
    r"no cases above threshold"
)
_CIRCUIT_BREAKER_PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*CIRCUIT BREAKER: (\d+)/(\d+) failures"
)


def parse_log(log_path: str) -> dict:
    """Parse log file and extract telemetry data."""
    batches = []
    singles = []
    circuit_breaks = []
    empty_batches = 0
    empty_singles = 0

    with open(log_path, "r") as f:
        for line in f:
            m = _BATCH_PATTERN.search(line)
            if m:
                batches.append({
                    "timestamp": m.group(1),
                    "items": int(m.group(2)),
                    "avg": float(m.group(3)),
                    "min": float(m.group(4)),
                    "max": float(m.group(5)),
                    "empty": int(m.group(6)),
                    "total": int(m.group(7)),
                })
                continue

            m = _BATCH_EMPTY_PATTERN.search(line)
            if m:
                empty_batches += 1
                batches.append({
                    "timestamp": m.group(1),
                    "items": int(m.group(2)),
                    "avg": 0.0,
                    "min": 0.0,
                    "max": 0.0,
                    "empty": int(m.group(2)),
                    "total": int(m.group(2)),
                })
                continue

            m = _SINGLE_PATTERN.search(line)
            if m:
                singles.append({
                    "timestamp": m.group(1),
                    "cases": int(m.group(2)),
                    "top_score": float(m.group(3)),
                    "weighted": float(m.group(4)),
                })
                continue

            m = _SINGLE_EMPTY_PATTERN.search(line)
            if m:
                empty_singles += 1
                continue

            m = _CIRCUIT_BREAKER_PATTERN.search(line)
            if m:
                circuit_breaks.append({
                    "timestamp": m.group(1),
                    "failures": int(m.group(2)),
                    "total": int(m.group(3)),
                })

    return {
        "batches": batches,
        "singles": singles,
        "circuit_breaks": circuit_breaks,
        "empty_batches": empty_batches,
        "empty_singles": empty_singles,
    }


def generate_report(data: dict) -> str:
    """Generate human-readable telemetry report."""
    lines = []
    lines.append("=" * 60)
    lines.append("  SVA2.0 — SIMILARITY SCORE TELEMETRY REPORT")
    lines.append("=" * 60)
    lines.append("")

    # Batch summary
    batches = data["batches"]
    if batches:
        total_items = sum(b["items"] for b in batches)
        total_empty = sum(b["empty"] for b in batches)
        avg_scores = [b["avg"] for b in batches if b["avg"] > 0]
        overall_avg = sum(avg_scores) / len(avg_scores) if avg_scores else 0
        min_score = min((b["min"] for b in batches if b["min"] > 0), default=0)
        max_score = max((b["max"] for b in batches), default=0)

        lines.append(f"BATCH MODE ({len(batches)} batch runs)")
        lines.append("-" * 40)
        lines.append(f"  Total items processed:  {total_items}")
        lines.append(f"  Items with no matches:  {total_empty} ({total_empty/total_items*100:.1f}%)" if total_items else "")
        lines.append(f"  Avg similarity score:   {overall_avg:.3f}")
        lines.append(f"  Min similarity score:   {min_score:.3f}")
        lines.append(f"  Max similarity score:   {max_score:.3f}")
        lines.append(f"  All-empty batches:      {data['empty_batches']}")
        lines.append("")

        # Health assessment
        if overall_avg >= 0.75:
            lines.append("  Health: ✓ EXCELLENT — corpus is well-matched to queries")
        elif overall_avg >= 0.65:
            lines.append("  Health: ✓ GOOD — corpus covers most queries adequately")
        elif overall_avg >= 0.55:
            lines.append("  Health: ⚠ FAIR — consider ingesting more historical data")
        else:
            lines.append("  Health: ✗ POOR — corpus needs significant expansion")
        lines.append("")

        # Trend (first vs last batch)
        if len(batches) >= 3:
            first_avg = sum(b["avg"] for b in batches[:3]) / 3
            last_avg = sum(b["avg"] for b in batches[-3:]) / 3
            delta = last_avg - first_avg
            if delta > 0.02:
                lines.append(f"  Trend: ↑ improving (+{delta:.3f} from first to last 3 batches)")
            elif delta < -0.02:
                lines.append(f"  Trend: ↓ degrading ({delta:.3f} from first to last 3 batches)")
            else:
                lines.append(f"  Trend: → stable (delta {delta:+.3f})")
            lines.append("")

    else:
        lines.append("BATCH MODE: No batch telemetry found in log.")
        lines.append("")

    # Single query summary
    singles = data["singles"]
    if singles:
        top_scores = [s["top_score"] for s in singles]
        avg_top = sum(top_scores) / len(top_scores)
        lines.append(f"SINGLE QUERY MODE ({len(singles)} queries)")
        lines.append("-" * 40)
        lines.append(f"  Avg top score:          {avg_top:.3f}")
        lines.append(f"  Min top score:          {min(top_scores):.3f}")
        lines.append(f"  Max top score:          {max(top_scores):.3f}")
        lines.append(f"  Queries with no match:  {data['empty_singles']}")
        lines.append("")

        # Score distribution
        high = sum(1 for s in top_scores if s >= 0.80)
        med = sum(1 for s in top_scores if 0.65 <= s < 0.80)
        low = sum(1 for s in top_scores if s < 0.65)
        lines.append(f"  Distribution:")
        lines.append(f"    Strong (≥0.80):   {high} ({high/len(top_scores)*100:.0f}%)")
        lines.append(f"    Moderate (0.65+): {med} ({med/len(top_scores)*100:.0f}%)")
        lines.append(f"    Weak (<0.65):     {low} ({low/len(top_scores)*100:.0f}%)")
        lines.append("")
    else:
        lines.append("SINGLE QUERY MODE: No single query telemetry found in log.")
        lines.append("")

    # Circuit breaker events
    if data["circuit_breaks"]:
        lines.append(f"CIRCUIT BREAKER EVENTS ({len(data['circuit_breaks'])})")
        lines.append("-" * 40)
        for cb in data["circuit_breaks"]:
            lines.append(f"  {cb['timestamp']} — {cb['failures']}/{cb['total']} failures")
        lines.append("")
        lines.append("  ⚠ Circuit breaker events indicate HANA connectivity issues.")
        lines.append("    Investigate if these are frequent.")
    else:
        lines.append("CIRCUIT BREAKER: No events (healthy)")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


def main():
    # Parse arguments
    log_path = "mcp_server.log"
    output_json = False

    for arg in sys.argv[1:]:
        if arg == "--json":
            output_json = True
        elif not arg.startswith("-"):
            log_path = arg

    if not Path(log_path).exists():
        print(f"Log file not found: {log_path}")
        print("Usage: python tools/telemetry.py [path/to/log] [--json]")
        sys.exit(1)

    data = parse_log(log_path)

    if output_json:
        # Add computed summary
        batches = data["batches"]
        avg_scores = [b["avg"] for b in batches if b["avg"] > 0]
        summary = {
            "batch_count": len(batches),
            "single_count": len(data["singles"]),
            "overall_avg_similarity": sum(avg_scores) / len(avg_scores) if avg_scores else 0,
            "circuit_breaker_events": len(data["circuit_breaks"]),
            "empty_rate": sum(b["empty"] for b in batches) / sum(b["items"] for b in batches) if batches else 0,
        }
        print(json.dumps({"summary": summary, "data": data}, indent=2, default=str))
    else:
        print(generate_report(data))


if __name__ == "__main__":
    main()
