"""
ingestion/extract_roadmap_pdf.py
---------------------------------
Extract features from the Unified Roadmap PDF (SAP Ariba) into an intermediate
Excel compatible with merge_roadmap.py and ingest_knowledge.py.

The PDF is a PPTX exported to PDF. Each page corresponds to a slide.
Three types of pages are parsed:

  Type A – Bi-weekly update slides (e.g. pages 8-12):
    Two columns (Q3/2026 | Q4/2026) separated by a blank line or layout gap.
    Title with solution at the bottom: "SAP Ariba Next-gen Sourcing Roadmap – H2 2026"

  Type B – Product-line view slides (e.g. pages 33-38, 42-46, 54-68):
    Single quarter per column block. Solution inferred from page header/title.
    May contain multiple Q columns in a single page (multi-quarter layout).

  Type C – GA Scope slides (pages 14-18):
    Feature lists without a quarter column header.
    Release = "GA" with a specific date embedded in the text.

Output: data/Update/roadmap_2026_extracted.xlsx

Usage:
    python ingestion/extract_roadmap_pdf.py
    python ingestion/extract_roadmap_pdf.py --input path/to/roadmap.pdf
    python ingestion/extract_roadmap_pdf.py --debug     # verbose page output
"""
import sys
import re
import pathlib
import argparse
from dataclasses import dataclass, field

import pandas as pd
import fitz  # PyMuPDF

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_INPUT = "data/Update/Unified Roadmap SAP Ariba_MASTER_Copy_260821.pdf"
DEFAULT_OUTPUT = "data/Update/roadmap_2026_extracted.xlsx"

# Pages to skip entirely (0-indexed): cover, disclaimers, suite themes, etc.
SKIP_PAGES = {0, 1, 2, 3, 4, 5, 6, 18, 19, 20, 21, 22, 23, 24, 25, 61, 62, 68, 69, 70}

# Category headers that appear in PDF text (not actual features)
CATEGORY_HEADERS = {
    "core capabilities",
    "suite synergy and integrations",
    "suite synergy & integrations",
    "ai intelligence and innovations",
    "ai intelligence & innovations",
    "intelligence & innovation",
    "intelligence and innovation",
    "erp compatibility",
    "globalization",
    "erc compatibility",
}

# Quarter patterns: Q3/2026, Q4/2026, Q2/2026, H2 2026, Q1/2027 & Beyond, etc.
QUARTER_RE = re.compile(
    r"\b(Q[1-4]/20\d\d|Q[1-4] 20\d\d|H[12] 20\d\d|H[12]/20\d\d"
    r"|Q[1-4]/20\d\d\s*&\s*Beyond|Q[1-4] 20\d\d\s*&\s*Beyond"
    r"|20\d\d\+?|Roadmap)\b",
    re.IGNORECASE,
)

# Explicit page-to-solution overrides for pages whose body text is ambiguous.
# Key: 0-indexed page number. Value: (solution, generation)
PAGE_SOLUTION_OVERRIDES: dict[int, tuple[str, str]] = {
    # Bi-weekly update slides (pages 8-12 → 0-indexed 7-11)
    7:  ("Ariba Sourcing",                     "next_gen"),
    8:  ("Ariba Contracts",                    "next_gen"),
    9:  ("Ariba SLP",                          "next_gen"),
    10: ("Ariba Buying",                       "next_gen"),
    11: ("Ariba Invoice",                      "next_gen"),
    # GA Scope slides (pages 14-18 → 0-indexed 13-17)
    13: ("Ariba Sourcing",                     "next_gen"),
    14: ("Ariba Contracts",                    "next_gen"),
    15: ("Ariba SLP",                          "next_gen"),
    16: ("Ariba Buying",                       "next_gen"),
    17: ("Ariba Invoice",                      "next_gen"),
    # Suite roadmap pages 20-21 (mix) → skip or assign to generic
    # Product-line view pages
    28: ("SAP Ariba Category Management",      "next_gen"),  # P29
    29: ("SAP Ariba Category Management",      "next_gen"),  # P30
    30: ("Ariba Sourcing",                     "next_gen"),  # P31
    31: ("Ariba Sourcing",                     "next_gen"),  # P32
    32: ("Ariba Sourcing",                     "next_gen"),  # P33
    33: ("Ariba Sourcing",                     "next_gen"),  # P34
    34: ("Ariba Contracts",                    "next_gen"),  # P35
    35: ("Ariba Contracts",                    "next_gen"),  # P36
    36: ("Ariba Contracts",                    "next_gen"),  # P37
    37: ("SAP Ariba Category Management",      "next_gen"),  # P38
    38: ("Ariba Contracts",                    "current_gen"), # P39
    39: ("Ariba SLP",                          "next_gen"),  # P40
    40: ("Ariba SLP",                          "next_gen"),  # P41
    41: ("Ariba SLP",                          "next_gen"),  # P42
    42: ("Ariba Contracts",                    "next_gen"),  # P43 (Buying agent page)
    43: ("Ariba Sourcing",                     "current_gen"), # P44
    44: ("Ariba Buying",                       "next_gen"),  # P45 (Buying agent)
    45: ("Ariba Buying",                       "next_gen"),  # P46
    46: ("Ariba Buying",                       "next_gen"),  # P47
    47: ("Ariba Invoice",                      "next_gen"),  # P48
    48: ("Ariba Invoice",                      "next_gen"),  # P49
    49: ("Ariba Invoice",                      "next_gen"),  # P50
    50: ("Ariba Invoice",                      "next_gen"),  # P51
    51: ("Ariba Invoice",                      "next_gen"),  # P52
    52: ("Ariba Invoice",                      "next_gen"),  # P53
    53: ("Ariba Invoice",                      "next_gen"),  # P54 — multi-quarter invoicing
    54: ("Ariba Sourcing",                     "next_gen"),  # P55 — multi-quarter sourcing current gen
    55: ("SAP Ariba Intake Management",        "next_gen"),  # P56
    56: ("Spend Analysis",                     "next_gen"),  # P57
    57: ("SAP Fieldglass External Workforce",  "next_gen"),  # P58
    58: ("SAP Fieldglass External Workforce",  "next_gen"),  # P59
    59: ("SAP Fieldglass Services Procurement","next_gen"),  # P60
    60: ("SAP Fieldglass Services Procurement","next_gen"),  # P61
    63: ("Ariba SLP",                          "current_gen"), # P64
    64: ("Ariba SLP",                          "current_gen"), # P65
    65: ("Ariba Sourcing",                     "current_gen"), # P66
    66: ("Ariba Contracts",                    "current_gen"), # P67
    67: ("Ariba Invoice",                      "current_gen"), # P68
}

# Solution title mapping — applied to lowercased page text (fallback when no override)
SOLUTION_TITLE_MAP = {
    "category management":              "SAP Ariba Category Management",
    "intake management":                "SAP Ariba Intake Management",
    "spend analysis":                   "Spend Analysis",
    "spend intelligence":               "Spend Analysis",
    "strategic sourcing":               "Ariba Sourcing",
    "sourcing":                         "Ariba Sourcing",
    "contracts":                        "Ariba Contracts",
    "supplier management":              "Ariba SLP",
    "slp":                              "Ariba SLP",
    "buying":                           "Ariba Buying",
    "invoic":                           "Ariba Invoice",
    "fieldglass services procurement":  "SAP Fieldglass Services Procurement",
    "services procurement":             "SAP Fieldglass Services Procurement",
    "fieldglass":                       "SAP Fieldglass External Workforce",
    "external workforce":               "SAP Fieldglass External Workforce",
}

# Agent / Joule keyword detection
AGENT_KEYWORDS = {"agent", "agentic", "eac"}
JOULE_KEYWORDS = {"joule", "copilot", "co-pilot"}

# Normalised category labels for the output
CATEGORY_MAP = {
    "core capabilities":                "Core Capabilities",
    "suite synergy and integrations":   "Suite Synergy & Integrations",
    "suite synergy & integrations":     "Suite Synergy & Integrations",
    "ai intelligence and innovations":  "AI Intelligence & Innovations",
    "ai intelligence & innovations":    "AI Intelligence & Innovations",
    "intelligence & innovation":        "AI Intelligence & Innovations",
    "intelligence and innovation":      "AI Intelligence & Innovations",
    "erp compatibility":                "ERP Compatibility",
    "globalization":                    "Globalization",
}


@dataclass
class Feature:
    solution: str
    title: str
    description: str = ""
    release: str = ""
    agent_based: str = "No"
    joule_based: str = "No"
    generation: str = "next_gen"
    category: str = ""
    source_page: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def infer_solution(text: str) -> str:
    """Return canonical solution name from a block of text."""
    lower = text.lower()
    for key, canonical in SOLUTION_TITLE_MAP.items():
        if key in lower:
            return canonical
    return ""


def infer_generation(text: str) -> str:
    lower = text.lower()
    if "current gen" in lower and "next gen" not in lower:
        return "current_gen"
    return "next_gen"


def normalise_quarter(raw: str) -> str:
    """Normalise quarter strings to canonical form used in HANA."""
    raw = raw.strip()
    # "Q3/2026" → "Q3 2026", "H2/2026" → "H2 2026"
    raw = re.sub(r"([QH]\d)/", r"\1 ", raw)
    # Remove "& Beyond" suffix
    raw = re.sub(r"\s*&\s*Beyond.*", "+", raw, flags=re.IGNORECASE)
    # "2026+" stays as-is
    return raw


def is_category_header(line: str) -> bool:
    return line.strip().lower() in CATEGORY_HEADERS


def is_bullet(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("•") or stripped.startswith("–")


def is_lone_bullet(line: str) -> bool:
    """True if the line is just a bullet character with nothing after it."""
    return line.strip() in ("•", "–")


def clean_bullet(line: str) -> str:
    """Remove bullet character and leading whitespace."""
    stripped = line.strip()
    for ch in ("•", "–"):
        if stripped.startswith(ch):
            stripped = stripped[1:].strip()
            break
    return stripped


def is_noise(line: str) -> bool:
    """Lines that are not features: disclaimers, page numbers, dates, roadmap titles."""
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.isdigit():
        return True
    lower = stripped.lower()
    if lower.startswith("disclaimer"):
        return True
    if lower.startswith("for sap internal"):
        return True
    if lower.startswith("confidential"):
        return True
    if lower.startswith("for bi-weekly"):
        return True
    if re.match(r"^\d{1,2}(st|nd|rd|th)?\s+\w+\s+20\d\d", stripped):  # "31st July 2026"
        return True
    if lower.startswith("date of update"):
        return True
    if lower.startswith("delivered feature"):
        return True
    if lower.startswith("* ="):
        return True
    if lower.startswith("content published"):
        return True
    if lower.startswith("supports on-prem"):
        return True
    if lower.startswith("updated "):
        return True
    if "roadmap" in lower and ("sap ariba" in lower or "fieldglass" in lower):
        return True  # Roadmap title lines like "SAP Ariba Next-gen Sourcing Roadmap – H2 2026"
    if lower.startswith("sales presentations under"):
        return True
    return False


def detect_flags(title: str) -> tuple[str, str]:
    """Return (agent_based, joule_based) flags from feature title."""
    lower = title.lower()
    agent = "Yes" if any(kw in lower for kw in AGENT_KEYWORDS) else "No"
    joule = "Yes" if any(kw in lower for kw in JOULE_KEYWORDS) else "No"
    return agent, joule


def extract_quarters_from_page(text: str) -> list[str]:
    """Find all distinct quarter tokens in a page text, in order of appearance."""
    seen = []
    for m in QUARTER_RE.finditer(text):
        q = normalise_quarter(m.group(0))
        if q not in seen:
            seen.append(q)
    return seen


# ---------------------------------------------------------------------------
# Page type detection
# ---------------------------------------------------------------------------
def is_ga_scope_page(text: str) -> bool:
    lower = text.lower()
    return "ga scope" in lower or "next-gen sap ariba ga scope" in lower


def is_biweekly_page(text: str) -> bool:
    lower = text.lower()
    return "bi-weekly nexus update" in lower or (
        "q3/2026" in lower and "q4/2026" in lower and "for bi-weekly" in lower
    )


# ---------------------------------------------------------------------------
# Parse Bi-weekly pages (Type A): two quarter columns on a single page
# The PDF renders left column first, then right column.
# We split by the SECOND occurrence of a known category header OR by the
# second quarter token to separate the two column groups.
# ---------------------------------------------------------------------------
def parse_biweekly_page(text: str, solution: str, generation: str,
                        page_num: int, debug: bool) -> list[Feature]:
    lines = text.splitlines()
    quarters = extract_quarters_from_page(text)

    if debug:
        print(f"  [biweekly] quarters={quarters}")

    if len(quarters) < 2:
        # Fallback: treat the whole page as a single unknown quarter
        quarters = quarters or ["H2 2026"]
        return parse_single_quarter_block(lines, solution, generation,
                                          quarters[0], page_num)

    # Split lines into two groups by the position of the second quarter occurrence
    q1, q2 = quarters[0], quarters[1]

    # Find line indices where each quarter header appears
    q1_idx = q2_idx = -1
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if q1_idx == -1 and re.match(rf"^{re.escape(q1)}$", stripped, re.IGNORECASE):
            q1_idx = i
        elif q2_idx == -1 and re.match(rf"^{re.escape(q2)}$", stripped, re.IGNORECASE):
            q2_idx = i

    # Also try slash-variant if normalised form not found directly
    if q1_idx == -1:
        q1_raw = q1.replace(" ", "/", 1)
        for i, ln in enumerate(lines):
            if ln.strip() == q1_raw:
                q1_idx = i
                break
    if q2_idx == -1:
        q2_raw = q2.replace(" ", "/", 1)
        for i, ln in enumerate(lines):
            if ln.strip() == q2_raw:
                q2_idx = i
                break

    if debug:
        print(f"  [biweekly] q1_idx={q1_idx} q2_idx={q2_idx}")

    if q2_idx > q1_idx >= 0:
        block1 = lines[q1_idx + 1: q2_idx]
        block2 = lines[q2_idx + 1:]
    else:
        # Cannot split — assign all to first quarter
        block1 = lines
        block2 = []

    features = []
    features += parse_single_quarter_block(block1, solution, generation, q1, page_num)
    if block2:
        features += parse_single_quarter_block(block2, solution, generation, q2, page_num)
    return features


# ---------------------------------------------------------------------------
# Parse a single-quarter block of lines
# ---------------------------------------------------------------------------
def parse_single_quarter_block(lines: list[str], solution: str, generation: str,
                                release: str, page_num: int) -> list[Feature]:
    features = []
    current_category = ""
    pending_bullet = False
    current_title_parts: list[str] = []

    def flush_title():
        nonlocal current_title_parts
        if current_title_parts:
            title = " ".join(current_title_parts).strip()
            current_title_parts = []
            return title
        return None

    def commit(title: str):
        if not title or len(title) < 5:
            return
        if title.endswith(":"):
            return
        # Skip generic sub-section labels that are not real features
        if title.lower() in {
                "sal cards", "messaging", "insights", "events", "projects",
                "library", "item 360", "open apis", "agentic", "decision support",
                "decision support:", "usability", "uc", "bom", "bom quotes"}:
            return
        agent, joule = detect_flags(title)
        features.append(Feature(
            solution=solution,
            title=title,
            release=release,
            agent_based=agent,
            joule_based=joule,
            generation=generation,
            category=current_category,
            source_page=page_num,
        ))

    for line in lines:
        if is_noise(line):
            if current_title_parts:
                commit(flush_title())
                pending_bullet = False
            continue

        stripped = line.strip()
        lower = stripped.lower()

        if is_category_header(stripped):
            if current_title_parts:
                commit(flush_title())
            pending_bullet = False
            current_category = CATEGORY_MAP.get(lower, stripped)
            continue

        if is_lone_bullet(stripped):
            if current_title_parts:
                commit(flush_title())
            pending_bullet = True
            continue

        if is_bullet(stripped):
            if current_title_parts:
                commit(flush_title())
            pending_bullet = False
            title = clean_bullet(stripped)
            if title:
                current_title_parts = [title]
            continue

        if pending_bullet or current_title_parts:
            # Continuation line: part of the current feature title
            if stripped:
                current_title_parts.append(stripped)
            pending_bullet = False
            continue

    # Flush any remaining
    if current_title_parts:
        commit(flush_title())

    return features


# ---------------------------------------------------------------------------
# Parse GA Scope pages (Type C)
# ---------------------------------------------------------------------------
def parse_ga_scope_page(text: str, solution: str, page_num: int,
                        debug: bool) -> list[Feature]:
    lines = text.splitlines()
    # Reuse single-quarter block parser with release="GA"
    features = parse_single_quarter_block(lines, solution, "next_gen", "GA", page_num)
    return features


# ---------------------------------------------------------------------------
# Parse multi-quarter product-line view pages (Type B) using block coordinates
#
# These pages have 2-4 columns. Each column is a separate fitz block with
# a distinct x-coordinate. The quarter headers appear in a single block at
# the top of the page listing the quarter names in reading order.
# We sort blocks by x0, assign each content block to the nearest quarter
# column header, then extract bullets per block.
# ---------------------------------------------------------------------------
def parse_multicolumn_page(text: str, solution: str, generation: str,
                           quarters: list[str], page_num: int,
                           debug: bool, page_obj=None) -> list[Feature]:
    if page_obj is None:
        # Fallback to text-based parsing if no page object available
        lines = text.splitlines()
        return parse_single_quarter_block(lines, solution, generation,
                                          quarters[0] if quarters else "Roadmap", page_num)

    blocks = page_obj.get_text("blocks")

    # Find the block containing the quarter headers (has multiple quarter tokens)
    header_block = None
    for b in blocks:
        btext = b[4]
        q_found = [q for q in quarters if q in btext or q.replace(" ", "/", 1) in btext]
        if len(q_found) >= 2:
            header_block = b
            break

    if header_block is None:
        if debug:
            print(f"  [multicolumn] no header block found, falling back")
        lines = text.splitlines()
        return parse_single_quarter_block(lines, solution, generation,
                                          quarters[0] if quarters else "Roadmap", page_num)

    # Parse quarter x-positions from the header block
    # The header block spans the full width; we need to distribute quarters evenly
    hx0, hx1 = header_block[0], header_block[2]
    col_width = (hx1 - hx0) / max(len(quarters), 1)

    # Build column-to-quarter mapping: (x_start, x_end, quarter)
    col_map: list[tuple[float, float, str]] = []
    for i, q in enumerate(quarters):
        col_map.append((hx0 + i * col_width, hx0 + (i + 1) * col_width, q))

    if debug:
        print(f"  [multicolumn] col_map: {[(f'{c[0]:.0f}-{c[1]:.0f}', c[2]) for c in col_map]}")

    def assign_quarter(bx0: float) -> str:
        for cstart, cend, q in col_map:
            if cstart <= bx0 < cend:
                return q
        # If outside range, assign to nearest column
        mid = bx0
        best_q = quarters[0]
        best_dist = abs(mid - (col_map[0][0] + col_map[0][1]) / 2)
        for cstart, cend, q in col_map:
            dist = abs(mid - (cstart + cend) / 2)
            if dist < best_dist:
                best_dist = dist
                best_q = q
        return best_q

    features = []
    for b in blocks:
        bx0, by0, bx1, by1, btext, bnum, btype = b
        if btype != 0:  # text blocks only
            continue
        if b is header_block:
            continue
        # Skip noise blocks
        stripped = btext.strip()
        if not stripped or len(stripped) < 4:
            continue
        if stripped.isdigit():
            continue
        if any(stripped.lower().startswith(n) for n in
               ("disclaimer", "for sap", "confidential", "date of update",
                "content published", "updated ", "* =")):
            continue

        q = assign_quarter(bx0)
        block_lines = btext.splitlines()
        features += parse_single_quarter_block(block_lines, solution, generation, q, page_num)

    if debug and features:
        print(f"  → {len(features)} features extracted (multicolumn blocks)")

    return features


# ---------------------------------------------------------------------------
# Main extraction loop
# ---------------------------------------------------------------------------
def extract_pdf(pdf_path: str, debug: bool = False) -> list[Feature]:
    doc = fitz.open(pdf_path)
    all_features: list[Feature] = []

    for page_num in range(len(doc)):
        if page_num in SKIP_PAGES:
            continue

        page = doc[page_num]
        text = page.get_text("text")

        if not text.strip():
            continue

        # Use explicit override first, then fallback to text inference
        if page_num in PAGE_SOLUTION_OVERRIDES:
            solution, generation = PAGE_SOLUTION_OVERRIDES[page_num]
        else:
            solution = infer_solution(text)
            generation = infer_generation(text)
            if not solution:
                if debug:
                    print(f"  P{page_num+1}: no solution detected, skipping")
                continue

        quarters = extract_quarters_from_page(text)

        if debug:
            print(f"\nP{page_num+1}: solution={solution!r} gen={generation} quarters={quarters}")

        features: list[Feature] = []

        if is_ga_scope_page(text):
            features = parse_ga_scope_page(text, solution, page_num + 1, debug)
        elif is_biweekly_page(text):
            features = parse_biweekly_page(text, solution, generation, page_num + 1, debug)
        elif len(quarters) >= 2:
            features = parse_multicolumn_page(text, solution, generation,
                                              quarters, page_num + 1, debug, page_obj=page)
        elif len(quarters) == 1:
            lines = text.splitlines()
            features = parse_single_quarter_block(lines, solution, generation,
                                                  quarters[0], page_num + 1)
        else:
            lines = text.splitlines()
            features = parse_single_quarter_block(lines, solution, generation,
                                                  "Roadmap", page_num + 1)

        if debug and features:
            print(f"  → {len(features)} features extracted")

        all_features.extend(features)

    doc.close()
    return all_features


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def deduplicate(features: list[Feature]) -> list[Feature]:
    """
    Deduplicate by (solution, normalised_title).
    Keep the entry with the earlier/more specific release.
    Prefer non-GA releases over GA (GA is a fallback from scope pages).
    """
    seen: dict[tuple[str, str], Feature] = {}

    def release_priority(rel: str) -> int:
        """Lower = more specific / earlier."""
        if not rel:
            return 99
        lower = rel.lower()
        if lower == "ga":
            return 50  # GA is valid but lower priority than dated quarter
        if "q1" in lower or "2601" in lower:
            return 1
        if "q2" in lower or "h1" in lower or "2602" in lower:
            return 2
        if "q3" in lower:
            return 3
        if "q4" in lower or "h2" in lower:
            return 4
        if "2026+" in lower or "q1/2027" in lower or "q1 2027" in lower:
            return 5
        if "roadmap" in lower:
            return 6
        return 7

    for f in features:
        key = (f.solution, f.title.strip().lower())
        if key not in seen:
            seen[key] = f
        else:
            existing = seen[key]
            if release_priority(f.release) < release_priority(existing.release):
                # Preserve description from whichever has one
                if not f.description and existing.description:
                    f.description = existing.description
                seen[key] = f
            elif f.description and not existing.description:
                existing.description = f.description

    return list(seen.values())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Extract roadmap features from PDF")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="Path to roadmap PDF")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output Excel path")
    parser.add_argument("--debug", action="store_true", help="Verbose page-by-page output")
    args = parser.parse_args()

    if not pathlib.Path(args.input).exists():
        print(f"Error: PDF not found: {args.input}")
        sys.exit(1)

    print(f"Extracting roadmap features from: {args.input}")
    features = extract_pdf(args.input, debug=args.debug)

    print(f"  {len(features)} features extracted (before dedup)")
    features = deduplicate(features)
    print(f"  {len(features)} features after deduplication")

    if not features:
        print("No features extracted. Exiting.")
        sys.exit(1)

    rows = []
    for f in features:
        rows.append({
            "SAP Ariba Solution / Product":       f.solution,
            "Feature or Functionality Announced": f.title,
            "Description":                        f.description,
            "Release":                            f.release,
            "Agent-based":                        f.agent_based,
            "Joule-based":                        f.joule_based,
            "Generation":                         f.generation,
            "Category":                           f.category,
            "Source Page":                        f.source_page,
        })

    df = pd.DataFrame(rows)

    # Summary by solution
    print("\nFeature count by solution:")
    for sol, count in df["SAP Ariba Solution / Product"].value_counts().items():
        print(f"  {sol}: {count}")

    print("\nFeature count by release:")
    for rel, count in df["Release"].value_counts().items():
        print(f"  {rel!r}: {count}")

    pathlib.Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    df.to_excel(args.output, index=False, engine="openpyxl")
    print(f"\nOutput written: {args.output}")


if __name__ == "__main__":
    main()
