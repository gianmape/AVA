"""
ingestion/ingest_qa_pdf.py
---------------------------
Ingest Q&A PDF documents into SVA2.KNOWLEDGE_BASE as source_type='next_gen'.

Designed for the Product_Success_NG.pdf format:
  - Sections delimited by known headers (from TOC)
  - Q&A pairs: question starts with interrogative word, ends with ?
  - Answer runs until next question or section boundary

Usage:
    python ingestion/ingest_qa_pdf.py data/legacy/Product_Success_NG.pdf
    python ingestion/ingest_qa_pdf.py data/legacy/Product_Success_NG.pdf --dry-run
    python ingestion/ingest_qa_pdf.py data/legacy/Product_Success_NG.pdf --replace
"""
import sys
import os
import re
import argparse
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from pypdf import PdfReader
from shared.config import clean_str, embed_text, hana_connection

# ---------------------------------------------------------------------------
# Section -> Solution mapping
# ---------------------------------------------------------------------------
SECTION_SOLUTION_MAP = {
    # Solution-specific sections
    "sap ariba category management": "SAP Ariba Category Management",
    "sap ariba contracts": "Ariba Contracts",
    "sap ariba intake management": "SAP Ariba Intake Management",
    "sap ariba invoicing": "Ariba Invoice",
    "sap ariba launchpad": "SAP Ariba Launchpad",
    "sap ariba supplier management & risk": "Ariba SLP",
    "sap business network": "Business Network",
    "sap business technology platform": None,
    "spend intelligence, spend analysis & insights, sap business data cloud (bdc)": "Spend Analysis",
    # Generic sections -> SOLUTION = None (still found by cosine similarity)
    "introduction to the next-gen sap ariba q&a database": None,
    "commercial & licensing": None,
    "demo system & scripts for presales": None,
    "documentation": None,
    "next-gen architecture": None,
    "next-gen road map and release": None,
    "partner enablement": None,
    "suite integration": None,
    "transition": None,
}

# Section names exactly as they appear in the PDF (case-sensitive for detection)
SECTION_HEADERS = [
    "Introduction to the next-gen SAP Ariba Q&A Database",
    "Commercial & Licensing",
    "Demo System & Scripts for Presales",
    "Documentation",
    "Next-gen Architecture",
    "Next-gen Road Map and Release",
    "Partner Enablement",
    "SAP Ariba Category Management",
    "SAP Ariba Contracts",
    "SAP Ariba Intake Management",
    "SAP Ariba Invoicing",
    "SAP Ariba Launchpad",
    "SAP Ariba Supplier Management & Risk",
    "SAP Business Network",
    "SAP Business Technology Platform",
    "Spend Intelligence, Spend Analysis & Insights, SAP Business Data Cloud (BDC)",
    "Suite Integration",
    "Transition",
]

# Regex for question detection
_QUESTION_START_RE = re.compile(
    r"^(Will|Can|What|How|Is|Does|Are|Should|When|Where|Why|Do|If|Has|Have|Which|Could|Would|Was|Were)\s",
    re.IGNORECASE,
)

# SQL (same as ingest_knowledge.py)
INSERT_SQL = """
INSERT INTO SVA2.KNOWLEDGE_BASE
    (SOURCE_TYPE, SOLUTION, TITLE, CONTENT, RELEASE,
     AGENT_BASED, JOULE_BASED, SOURCE_FILE, EMBEDDING)
VALUES
    (?, ?, ?, ?, ?, ?, ?, ?, TO_REAL_VECTOR(?))
"""

# Footer pattern to strip
_FOOTER_RE = re.compile(
    r"©\s*20\d{2}\s+SAP\s+SE.*?(?:\d+\s*/\s*\d+)\s*\n\s*INTERNAL\s*[‒–—-]\s*SAP\s+Only\s*",
    re.DOTALL,
)


# ---------------------------------------------------------------------------
# PDF Extraction
# ---------------------------------------------------------------------------
def extract_text(pdf_path: str) -> str:
    """Extract full text from PDF, stripping headers/footers."""
    reader = PdfReader(pdf_path)
    pages_text = []
    for page in reader.pages:
        text = page.extract_text() or ""
        # Strip the copyright footer + page number + INTERNAL marker
        text = _FOOTER_RE.sub("", text)
        pages_text.append(text)
    return "\n".join(pages_text)


def detect_sections(full_text: str) -> list[tuple[str, str]]:
    """Split full text into (section_name, section_content) pairs.

    Handles section headers that may span multiple lines in the PDF
    (e.g., 'Spend Intelligence, Spend Analysis & Insights, SAP Business Data\\nCloud (BDC)').
    """
    sorted_headers = sorted(SECTION_HEADERS, key=len, reverse=True)

    # Build patterns that allow flexible whitespace (including newlines) between words
    header_patterns = []
    for h in sorted_headers:
        words = h.split()
        # Allow 1+ whitespace chars (including \n) between words
        pattern_str = r"\s+".join(re.escape(w) for w in words)
        header_patterns.append((h, re.compile(pattern_str, re.IGNORECASE)))

    # Find all section header positions
    found_positions = []  # (start_pos, end_pos, header_name, is_toc)
    for header_name, pat in header_patterns:
        for m in pat.finditer(full_text):
            # Strict validation: header must be on its own line(s)
            # Find the start of the line containing match start
            line_start = full_text.rfind("\n", 0, m.start())
            line_start = line_start + 1 if line_start >= 0 else 0
            # Find the end of the line containing match end
            line_end = full_text.find("\n", m.end())
            if line_end < 0:
                line_end = len(full_text)

            # Text before the match on the same line should be only whitespace
            before_on_line = full_text[line_start:m.start()].strip()
            # Text after the match on the same line
            after_on_line = full_text[m.end():line_end].strip()

            if before_on_line:
                continue  # Not at start of line

            # Detect TOC entries: they have trailing dots (....) and page numbers
            is_toc = bool(re.match(r"^[.\s\d]+$", after_on_line)) if after_on_line else False

            if not after_on_line or is_toc:
                found_positions.append((m.start(), m.end(), header_name, is_toc))

    if not found_positions:
        return [("Unknown", full_text)]

    # Sort by position
    found_positions.sort(key=lambda x: x[0])

    # Remove overlaps: keep longer matches and skip duplicates at close positions
    deduped = []
    last_end = -1
    for start, end, name, is_toc in found_positions:
        if start >= last_end:
            deduped.append((start, end, name, is_toc))
            last_end = end

    # Skip TOC entries: they have trailing dots (.........) on the same line.
    # Instead of using a position cutoff, detect TOC lines by their formatting.
    seen_headers = set()
    final_sections = []
    for start, end, name, is_toc in deduped:
        if is_toc:
            continue  # Skip TOC entries
        key = name.lower()
        if key in seen_headers:
            continue  # Skip duplicate matches
        seen_headers.add(key)
        final_sections.append((start, end, name))

    if not final_sections:
        return [("Unknown", full_text)]

    sections = []
    for i, (start, end, header_name) in enumerate(final_sections):
        next_start = final_sections[i + 1][0] if i + 1 < len(final_sections) else len(full_text)
        content = full_text[end:next_start].strip()
        sections.append((header_name, content))

    return sections


def parse_qa_pairs(section_text: str) -> list[tuple[str, str]]:
    """Extract Q&A pairs from a section's text content.

    Strategy:
    - Questions start with an interrogative word and end with '?'
    - Multi-line questions: collect up to 5 lines looking for '?'
    - If no '?' found within 5 lines, it wasn't a real question — treat as answer text
    - Compound questions (multiple '?' on same line) are kept together as one question
    """
    lines = section_text.split("\n")
    pairs = []
    current_question_lines = []
    current_answer_lines = []
    in_question = False
    question_line_count = 0

    _MAX_QUESTION_LINES = 5  # A real question rarely spans more than 5 lines

    def flush():
        if current_question_lines and current_answer_lines:
            q = " ".join(current_question_lines).strip()
            a = " ".join(current_answer_lines).strip()
            # Clean up: normalize multiple spaces, strip trailing empty lines
            q = re.sub(r"\s+", " ", q).strip()
            a = re.sub(r"\s{2,}", " ", a).strip()
            if q and a and len(a) > 10:
                pairs.append((q, a))
        current_question_lines.clear()
        current_answer_lines.clear()

    def abort_question():
        """Multi-line question exceeded max lines without '?' — not a real question."""
        # Push collected lines back into answer
        nonlocal in_question, question_line_count
        current_answer_lines.extend(current_question_lines)
        current_question_lines.clear()
        in_question = False
        question_line_count = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_question:
                # Blank line while collecting question — abort, it's not a question
                abort_question()
            continue

        if in_question:
            current_question_lines.append(stripped)
            question_line_count += 1
            if "?" in stripped:
                # Found the end of the question — extract the full question
                # Join all collected lines to find the last '?'
                full_q = " ".join(current_question_lines)
                last_q_idx = full_q.rfind("?")
                question_text = full_q[:last_q_idx + 1].strip()
                after_q = full_q[last_q_idx + 1:].strip()
                current_question_lines.clear()
                current_question_lines.append(question_text)
                in_question = False
                question_line_count = 0
                if after_q:
                    current_answer_lines.append(after_q)
            elif question_line_count >= _MAX_QUESTION_LINES:
                abort_question()
            continue

        # Not currently in a question — check if this line starts one
        if _QUESTION_START_RE.match(stripped):
            if "?" in stripped:
                # Line contains at least one '?'
                flush()
                last_q_idx = stripped.rfind("?")
                after_q = stripped[last_q_idx + 1:].strip()

                if after_q and _QUESTION_START_RE.match(after_q):
                    # Text after '?' starts another question — compound question continues
                    # Treat entire line as start of multi-line question
                    current_question_lines.append(stripped)
                    in_question = True
                    question_line_count = 1
                elif len(after_q) > 20:
                    # Substantial non-question text after '?' — answer on same line
                    first_q_idx = stripped.index("?")
                    question_text = stripped[:first_q_idx + 1].strip()
                    current_question_lines.append(question_text)
                    current_answer_lines.append(stripped[first_q_idx + 1:].strip())
                else:
                    # All text up to last '?' is the question
                    question_text = stripped[:last_q_idx + 1].strip()
                    current_question_lines.append(question_text)
                    if after_q:
                        current_answer_lines.append(after_q)
            else:
                # Potential multi-line question start
                flush()
                current_question_lines.append(stripped)
                in_question = True
                question_line_count = 1
        else:
            # Answer line
            current_answer_lines.append(stripped)

    # Flush last pair
    flush()

    return pairs


# ---------------------------------------------------------------------------
# Main ingestion
# ---------------------------------------------------------------------------
def ingest_pdf(pdf_path: str, dry_run: bool = False, replace: bool = False):
    filename = os.path.basename(pdf_path)
    print(f"\n-- Extracting text from: {pdf_path}")
    full_text = extract_text(pdf_path)
    print(f"   Total extracted text: {len(full_text):,} characters")

    print("-- Detecting sections...")
    sections = detect_sections(full_text)
    print(f"   Found {len(sections)} sections")

    all_pairs = []
    for section_name, section_content in sections:
        solution = SECTION_SOLUTION_MAP.get(section_name.lower().strip())
        pairs = parse_qa_pairs(section_content)
        for q, a in pairs:
            all_pairs.append((section_name, solution, q, a))
        sol_label = solution or "(generic)"
        print(f"   [{sol_label:30s}] {section_name}: {len(pairs)} Q&A pairs")

    print(f"\n   TOTAL Q&A pairs: {len(all_pairs)}")

    if dry_run:
        print("\n-- DRY RUN — showing first 5 pairs:")
        for i, (section, solution, q, a) in enumerate(all_pairs[:5]):
            print(f"\n   [{i+1}] Section: {section}")
            print(f"       Solution: {solution or 'NULL'}")
            print(f"       Q: {q[:120]}{'...' if len(q) > 120 else ''}")
            print(f"       A: {a[:120]}{'...' if len(a) > 120 else ''}")
        print("\n-- No database changes made (dry run).")
        return

    # Database operations
    from dotenv import load_dotenv
    load_dotenv()

    conn = hana_connection()
    cursor = conn.cursor()

    if replace:
        cursor.execute(
            "SELECT COUNT(*) FROM SVA2.KNOWLEDGE_BASE WHERE SOURCE_FILE = ?",
            (filename,),
        )
        existing = cursor.fetchone()[0]
        if existing > 0:
            print(f"-- Deleting {existing} existing entries for {filename}...")
            cursor.execute(
                "DELETE FROM SVA2.KNOWLEDGE_BASE WHERE SOURCE_FILE = ?",
                (filename,),
            )
            conn.commit()
            print(f"   Deleted.")

    inserted = skipped = 0
    for section_name, solution, question, answer in all_pairs:
        title = question[:500]
        content = answer[:4000]

        if not title or not content:
            skipped += 1
            continue

        embed_input = f"{title} — {content}"
        try:
            vector = embed_text(embed_input)
        except Exception as e:
            print(f"  Warning: embedding failed, skipping '{title[:60]}': {e}")
            skipped += 1
            continue

        vector_str = "[" + ",".join(str(v) for v in vector) + "]"

        cursor.execute(INSERT_SQL, (
            "next_gen",     # SOURCE_TYPE
            solution,       # SOLUTION (None for generic sections)
            title,          # TITLE = question
            content,        # CONTENT = answer
            None,           # RELEASE
            None,           # AGENT_BASED
            None,           # JOULE_BASED
            filename,       # SOURCE_FILE
            vector_str,     # EMBEDDING
        ))

        inserted += 1
        if inserted % 10 == 0:
            conn.commit()
            print(f"  {inserted} rows committed...")

    conn.commit()
    cursor.close()
    conn.close()
    print(f"\n-- Done: inserted={inserted}, skipped={skipped}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest Q&A PDF into SVA2.KNOWLEDGE_BASE (source_type=next_gen)"
    )
    parser.add_argument("file", help="Path to PDF file")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and display stats without inserting into database",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete existing entries from this file before inserting",
    )
    args = parser.parse_args()

    if not pathlib.Path(args.file).exists():
        print(f"File not found: {args.file}")
        sys.exit(1)

    ingest_pdf(args.file, dry_run=args.dry_run, replace=args.replace)
