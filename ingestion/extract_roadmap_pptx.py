"""
ingestion/extract_roadmap_pptx.py
---------------------------------
Extract features from the Unified Roadmap PPTX into an intermediate Excel
compatible with ingest_knowledge.py.

Parses three types of slides:
  - Roadmap tables (1-row x N-col): feature names per quarter
  - Showcase slides: feature name + full description
  - Agent inventory (slide 11): agent names + GA dates

Output: data/Update/roadmap_2026_extracted.xlsx

Usage:
    python ingestion/extract_roadmap_pptx.py
    python ingestion/extract_roadmap_pptx.py --input path/to/deck.pptx
"""
import sys
import os
import re
import pathlib
import argparse
from dataclasses import dataclass, field

import pandas as pd
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_INPUT = "data/Update/Unified_Roadmap_SAP_Ariba_Fieldglass_7.2026_INTERNAL.pptx"
DEFAULT_OUTPUT = "data/Update/roadmap_2026_extracted.xlsx"

# Slides that contain roadmap tables (0-indexed)
TABLE_SLIDES = [14, 18, 22, 23, 27, 28, 30, 35, 36, 37, 38, 39]

# Slides that contain showcase features with descriptions (0-indexed)
SHOWCASE_SLIDES = [12, 13, 15, 16, 17, 19, 20, 21, 24, 25, 26, 29, 31, 32, 33, 34]

# Slide 11 (0-indexed = 10) has the agent inventory
AGENT_SLIDE_IDX = 10

# Category headers that appear in table cells (not actual features)
CATEGORY_HEADERS = {
    "core capabilities",
    "suite synergy and integrations",
    "suite synergy & integrations",
    "ai intelligence and innovations",
    "ai intelligence & innovations",
}

# Solution name extraction from slide titles
SOLUTION_TITLE_MAP = {
    "category management": "SAP Ariba Category Management",
    "strategic sourcing": "Ariba Sourcing",
    "sourcing": "Ariba Sourcing",
    "contracts": "Ariba Contracts",
    "supplier management": "Ariba SLP",
    "buying": "Ariba Buying",
    "invoicing": "Ariba Invoice",
    "intake management": "SAP Ariba Intake Management",
    "spend analysis": "Spend Analysis",
    "fieldglass external workforce": "SAP Fieldglass External Workforce",
    "fieldglass services procurement": "SAP Fieldglass Services Procurement",
}


# Agent name → solution inference (for orphan agents from slide 11)
AGENT_SOLUTION_HINTS = {
    "sourcing": "Ariba Sourcing",
    "bid": "Ariba Sourcing",
    "negotiation": "Ariba Sourcing",
    "auction": "Ariba Sourcing",
    "scenario award": "Ariba Sourcing",
    "scoring": "Ariba Sourcing",
    "grading": "Ariba Sourcing",
    "contract": "Ariba Contracts",
    "amendment": "Ariba Contracts",
    "sow": "Ariba Contracts",
    "clause": "Ariba Contracts",
    "invoice": "Ariba Invoice",
    "invoicing": "Ariba Invoice",
    "payment": "Ariba Invoice",
    "receivable": "Ariba Invoice",
    "consumption": "Ariba Invoice",
    "buying": "Ariba Buying",
    "requisition": "Ariba Buying",
    "purchase": "Ariba Buying",
    "demand": "Ariba Buying",
    "catalog": "Ariba Buying",
    "material": "Ariba Buying",
    "supplier": "Ariba SLP",
    "review and assessment": "Ariba SLP",
    "category": "SAP Ariba Category Management",
    "market": "SAP Ariba Category Management",
    "spend": "Spend Analysis",
    "goods": "Ariba Buying",
    "service entry": "Ariba Buying",
    "sbn": "Business Network",
    "procurement": "Ariba Buying",
    "time management": "SAP Fieldglass External Workforce",
    "mobilization": "SAP Fieldglass External Workforce",
    "rate": "SAP Fieldglass External Workforce",
    "worker": "SAP Fieldglass External Workforce",
    "scope": "SAP Fieldglass Services Procurement",
}


def infer_agent_solution(agent_name: str) -> str:
    """Infer solution from agent name keywords."""
    lower = agent_name.lower()
    for keyword, solution in AGENT_SOLUTION_HINTS.items():
        if keyword in lower:
            return solution
    return ""
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
    status: str = ""
    source_slide: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def extract_solution_from_title(title_text: str) -> tuple[str, str]:
    """Extract canonical solution name and generation from a slide title."""
    if not title_text:
        return ("", "next_gen")

    # Determine generation
    title_lower = title_text.lower()
    if "current gen" in title_lower and "next gen" not in title_lower:
        gen = "current_gen"
    elif "current gen" in title_lower and "next gen" in title_lower:
        gen = "next_gen"  # mixed slides treated as next_gen
    else:
        gen = "next_gen"

    # Extract solution
    for key, canonical in SOLUTION_TITLE_MAP.items():
        if key in title_lower:
            return (canonical, gen)

    return (title_text.strip(), gen)


def extract_quarters_from_slide(slide) -> list[str]:
    """Extract quarter labels from GroupShapes, sorted by left position."""
    quarters = []
    for shape in slide.shapes:
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for child in shape.shapes:
                if child.has_text_frame:
                    t = child.text_frame.text.strip()
                    if ("Q" in t and "/" in t) or ("H" in t and "20" in t):
                        quarters.append((shape.left, t))
    quarters.sort(key=lambda x: x[0])
    return [q[1] for q in quarters]


def is_category_header(line: str) -> bool:
    """Check if a line is a category header rather than a feature name."""
    return line.strip().lower() in CATEGORY_HEADERS


def get_current_category(line: str) -> str | None:
    """If line is a category header, return its normalized name."""
    lower = line.strip().lower()
    if "core capabilities" in lower:
        return "Core Capabilities"
    elif "suite synergy" in lower:
        return "Suite Synergy & Integrations"
    elif "ai intelligence" in lower:
        return "AI Intelligence & Innovations"
    return None


def is_agent(title: str) -> bool:
    """Heuristic: is this feature an AI agent?"""
    lower = title.lower()
    return "agent" in lower


def is_joule(title: str) -> bool:
    """Heuristic: does this feature involve Joule?"""
    lower = title.lower()
    return "joule" in lower or "assistant" in lower


def clean_feature_name(name: str) -> str:
    """Clean up a feature name from table cell."""
    name = name.strip()
    # Remove control characters (illegal in Excel)
    name = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", name)
    # Remove zero-width spaces and other Unicode control chars
    name = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", name)
    # Remove leading bullets or dashes
    name = re.sub(r"^[\-•·▪►▸]\s*", "", name)
    # Remove trailing asterisks
    name = name.rstrip("*").strip()
    return name


def clean_text(text: str) -> str:
    """Remove illegal Excel characters from any text."""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    return text.strip()


def generate_synthetic_description(feature: Feature) -> str:
    """Generate a synthetic description for features without one."""
    parts = [f"{feature.title} capability for {feature.solution}."]
    if feature.category:
        parts.append(f"Category: {feature.category}.")
    if feature.release:
        parts.append(f"Release: {feature.release}.")
    if feature.agent_based == "Yes":
        parts.append("This is an AI agent-based feature.")
    if feature.joule_based == "Yes":
        parts.append("This feature is Joule-enabled.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Extraction: Agent inventory (slide 11)
# ---------------------------------------------------------------------------
def extract_agents(slide) -> dict[str, str]:
    """
    Extract agent names and their GA dates from slide 11.
    Returns dict: {agent_name: ga_release}
    """
    agents = []
    ga_dates = []

    for shape in slide.shapes:
        if shape.has_text_frame and not shape.has_table:
            name = shape.name
            t = shape.text_frame.text.strip()
            if not t:
                continue

            # Agent shapes use "Rectangle: Rounded Corners 598" pattern
            if "Rounded Corners 598" in name and t and "GA" not in t:
                agents.append((shape.left, shape.top, clean_feature_name(t)))
            # GA date shapes use "Rounded Rectangle" pattern
            elif "Rounded Rectangle" in name and "GA" in t:
                ga_dates.append((shape.left, shape.top, t.strip()))
            # Also check named shapes for assistants
            elif any(kw in t for kw in ["Assistant", "Agent"]) and "Rounded" not in name:
                if "Autonomous" not in t and "SAP" not in t and len(t) < 80:
                    agents.append((shape.left, shape.top, clean_feature_name(t)))

        # Check inside groups too
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for child in shape.shapes:
                if child.has_text_frame:
                    ct = child.text_frame.text.strip()
                    if ct and "Agent" in ct and len(ct) < 80:
                        agents.append((shape.left + child.left, shape.top + child.top, clean_feature_name(ct)))

    # Match agents to GA dates by proximity (closest GA date below/right of agent)
    agent_ga_map = {}
    for ax, ay, aname in agents:
        if not aname or aname in agent_ga_map:
            continue
        # Find closest GA date
        best_ga = ""
        best_dist = float("inf")
        for gx, gy, gtext in ga_dates:
            # GA label should be near the agent (within reasonable distance)
            dist = abs(gx - ax) + abs(gy - ay)
            if dist < best_dist:
                best_dist = dist
                best_ga = gtext
        if best_ga:
            # Parse GA text: "2607 GA" -> "2607"
            match = re.search(r"(\d{4,5})\s*GA", best_ga)
            if match:
                agent_ga_map[aname] = match.group(1)

    return agent_ga_map


# ---------------------------------------------------------------------------
# Extraction: Roadmap tables
# ---------------------------------------------------------------------------
def extract_table_features(slide, slide_num: int) -> list[Feature]:
    """Extract features from a roadmap table slide."""
    features = []

    # Get solution and generation from title
    title_text = ""
    for shape in slide.shapes:
        if shape.has_text_frame and not shape.has_table:
            t = shape.text_frame.text.strip()
            if t and ("Roadmap" in t or "SAP" in t or "Fieldglass" in t):
                if "Disclaimer" not in t and "INTERNAL" not in t:
                    title_text = t
                    break

    solution, generation = extract_solution_from_title(title_text)
    if not solution:
        return features

    # Get quarter labels
    quarters = extract_quarters_from_slide(slide)

    # Get table data
    for shape in slide.shapes:
        if not shape.has_table:
            continue

        table = shape.table
        num_cols = len(table.columns)

        for ci in range(num_cols):
            # Determine quarter for this column
            quarter = quarters[ci] if ci < len(quarters) else f"Col{ci}"
            # Clean quarter label: "Q3/2026 & Beyond" -> "Q3/2026+"
            release = quarter.replace(" & Beyond", "+")

            # Parse cell content
            cell_text = table.rows[0].cells[ci].text.strip()
            lines = cell_text.split("\n")

            current_category = ""
            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # Check if this is a category header
                cat = get_current_category(line)
                if cat:
                    current_category = cat
                    continue

                # Skip if it's a known non-feature line
                if is_category_header(line):
                    continue

                feature_name = clean_feature_name(line)
                if not feature_name or len(feature_name) < 3:
                    continue

                feat = Feature(
                    solution=solution,
                    title=feature_name,
                    release=release,
                    agent_based="Yes" if is_agent(feature_name) else "No",
                    joule_based="Yes" if is_joule(feature_name) else "No",
                    generation=generation,
                    category=current_category,
                    source_slide=slide_num,
                )
                features.append(feat)

    return features


# ---------------------------------------------------------------------------
# Extraction: Showcase slides
# ---------------------------------------------------------------------------
def extract_showcase_features(slide, slide_num: int) -> list[Feature]:
    """Extract features from a showcase slide (has full description)."""
    features = []

    solution = ""
    feature_name = ""
    description = ""
    status = ""

    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        name = shape.name
        t = shape.text_frame.text.strip()
        if not t:
            continue
        if "INTERNAL" in t or "ROADMAP" in t or "Disclaimer" in t:
            continue

        # Title shape -> solution
        if name.startswith("Title") or name.startswith("Titel"):
            solution = t
        # Trapezium -> status label
        elif name.startswith("Trapez"):
            status = t
        # Text content -> feature name (first line) + description (rest)
        elif name.startswith("Textfeld") or name.startswith("TextBox") or "Placeholder" in name:
            if "Content" in name or "Text" in name or "Textfeld" in name:
                lines = t.split("\n", 1)
                candidate_name = lines[0].strip()
                candidate_desc = lines[1].strip() if len(lines) > 1 else ""
                # Use the one with actual content
                if candidate_name and (not feature_name or len(candidate_desc) > len(description)):
                    feature_name = candidate_name
                    description = candidate_desc

    if not feature_name or not solution:
        return features

    canonical_solution, generation = extract_solution_from_title(solution)

    feat = Feature(
        solution=canonical_solution,
        title=feature_name,
        description=description,
        release="",  # Showcase slides don't specify quarter
        agent_based="Yes" if is_agent(feature_name) else "No",
        joule_based="Yes" if is_joule(feature_name) else "No",
        generation=generation,
        category="AI Intelligence & Innovations" if is_agent(feature_name) else "",
        status=status,
        source_slide=slide_num,
    )
    features.append(feat)
    return features


# ---------------------------------------------------------------------------
# Deduplication and merging
# ---------------------------------------------------------------------------
def merge_features(table_features: list[Feature],
                   showcase_features: list[Feature],
                   agent_ga_map: dict[str, str]) -> list[Feature]:
    """
    Merge features from all sources:
    1. Showcase features have descriptions — they take priority
    2. Agent GA dates from slide 11 enrich matching features
    3. Table features provide the bulk (name-only)
    """
    # Index showcase features by normalized title
    showcase_by_title: dict[str, Feature] = {}
    for f in showcase_features:
        key = f.title.lower().strip()
        showcase_by_title[key] = f

    # Index agent GA dates by normalized name
    agent_ga_by_name: dict[str, str] = {}
    for name, ga in agent_ga_map.items():
        agent_ga_by_name[name.lower().strip()] = ga

    # Process table features, merging with showcase and agent data
    merged: dict[str, Feature] = {}

    # First, add all showcase features (they have descriptions)
    for f in showcase_features:
        key = f"{f.solution}|{f.title.lower().strip()}"
        # Enrich with agent GA if available
        agent_key = f.title.lower().strip()
        if agent_key in agent_ga_by_name:
            f.release = agent_ga_by_name[agent_key]
            f.agent_based = "Yes"
        merged[key] = f

    # Then add table features (skip if showcase already has it)
    for f in table_features:
        key = f"{f.solution}|{f.title.lower().strip()}"
        if key in merged:
            # Showcase already has this — just update release if table has one
            existing = merged[key]
            if not existing.release and f.release:
                existing.release = f.release
            continue

        # Enrich with agent GA if available
        agent_key = f.title.lower().strip()
        if agent_key in agent_ga_by_name:
            f.release = agent_ga_by_name[agent_key]
            f.agent_based = "Yes"

        merged[key] = f

    # Add agents that weren't in any table or showcase
    for agent_name, ga_release in agent_ga_map.items():
        # Check if already present
        found = False
        for key in merged:
            if agent_name.lower().strip() in key.lower():
                found = True
                break
        if not found:
            solution = infer_agent_solution(agent_name)
            feat = Feature(
                solution=solution,
                title=agent_name,
                release=ga_release,
                agent_based="Yes",
                joule_based="Yes" if "assistant" in agent_name.lower() else "No",
                generation="next_gen",
                category="AI Intelligence & Innovations",
            )
            merged[f"{solution}|{agent_name.lower().strip()}"] = feat

    return list(merged.values())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Extract roadmap features from PPTX")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="Path to PPTX file")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output Excel path")
    args = parser.parse_args()

    if not pathlib.Path(args.input).exists():
        print(f"Error: File not found: {args.input}")
        sys.exit(1)

    print(f"Loading: {args.input}")
    prs = Presentation(args.input)
    total_slides = len(prs.slides)
    print(f"  Total slides: {total_slides}")

    # --- Extract agents from slide 11 ---
    print("\n── Extracting agent inventory (slide 11)...")
    agent_ga_map = extract_agents(prs.slides[AGENT_SLIDE_IDX])
    print(f"  Found {len(agent_ga_map)} agents with GA dates")

    # --- Extract from roadmap tables ---
    print("\n── Extracting roadmap table features...")
    table_features = []
    for si in TABLE_SLIDES:
        if si < total_slides:
            feats = extract_table_features(prs.slides[si], si + 1)
            print(f"  Slide {si+1:2d}: {len(feats)} features")
            table_features.extend(feats)
    print(f"  Total table features: {len(table_features)}")

    # --- Extract from showcase slides ---
    print("\n── Extracting showcase features...")
    showcase_features = []
    for si in SHOWCASE_SLIDES:
        if si < total_slides:
            feats = extract_showcase_features(prs.slides[si], si + 1)
            for f in feats:
                print(f"  Slide {si+1:2d}: {f.title[:60]}")
            showcase_features.extend(feats)
    print(f"  Total showcase features: {len(showcase_features)}")

    # --- Merge and deduplicate ---
    print("\n── Merging and deduplicating...")
    all_features = merge_features(table_features, showcase_features, agent_ga_map)
    print(f"  Final feature count: {len(all_features)}")

    # --- Generate synthetic descriptions for name-only features ---
    print("\n── Generating synthetic descriptions...")
    synthetic_count = 0
    for f in all_features:
        if not f.description:
            f.description = generate_synthetic_description(f)
            synthetic_count += 1
    print(f"  Features with real descriptions: {len(all_features) - synthetic_count}")
    print(f"  Features with synthetic descriptions: {synthetic_count}")

    # --- Build DataFrame and save ---
    print(f"\n── Writing output: {args.output}")
    rows = []
    for f in all_features:
        rows.append({
            "SAP Ariba Solution / Product": f.solution,
            "Feature or Functionality Announced": clean_text(f.title),
            "Description": clean_text(f.description),
            "Release": f.release,
            "Agent-based": f.agent_based,
            "Joule-based": f.joule_based,
            "Generation": f.generation,
            "Category": f.category,
            "Status": f.status,
            "Source Slide": f.source_slide,
        })

    df = pd.DataFrame(rows)

    # Sort by solution, then release, then title
    df = df.sort_values(["SAP Ariba Solution / Product", "Release", "Feature or Functionality Announced"])
    df = df.reset_index(drop=True)

    # Ensure output directory exists
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    df.to_excel(args.output, index=False, engine="openpyxl")

    # --- Summary ---
    print(f"\n{'='*60}")
    print("EXTRACTION SUMMARY")
    print(f"{'='*60}")
    print(f"  Total features extracted: {len(df)}")
    print(f"  With real descriptions: {len(all_features) - synthetic_count}")
    print(f"  With synthetic descriptions: {synthetic_count}")
    print(f"  Agent-based features: {(df['Agent-based'] == 'Yes').sum()}")
    print(f"  Joule-based features: {(df['Joule-based'] == 'Yes').sum()}")
    print(f"  Next-gen features: {(df['Generation'] == 'next_gen').sum()}")
    print(f"  Current-gen features: {(df['Generation'] == 'current_gen').sum()}")
    print(f"\n  By solution:")
    for sol, count in df["SAP Ariba Solution / Product"].value_counts().items():
        print(f"    {sol}: {count}")
    print(f"\n  Output: {args.output}")


if __name__ == "__main__":
    main()
