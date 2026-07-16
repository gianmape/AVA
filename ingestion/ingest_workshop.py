"""
ingestion/ingest_workshop.py
-----------------------------
Ingest Next-Gen SAP Ariba Workshop materials (PDFs + video transcripts)
into SVA2.KNOWLEDGE_BASE with source_type='workshop'.

Handles:
  - PDF presentation chunking (section detection, noise removal, roadmap parsing)
  - Video transcription via faster-whisper + chunking
  - Deduplication (intra-doc, cross-doc vs existing KB, within batch)

Usage:
    python ingestion/ingest_workshop.py "data/NG Workshop/"
    python ingestion/ingest_workshop.py "data/NG Workshop/" --mode pdf
    python ingestion/ingest_workshop.py "data/NG Workshop/" --mode video
    python ingestion/ingest_workshop.py "data/NG Workshop/" --dry-run
    python ingestion/ingest_workshop.py "data/NG Workshop/" --replace
    python ingestion/ingest_workshop.py "data/NG Workshop/" --whisper-model large-v3
"""
import sys
import os
import re
import argparse
import pathlib
import subprocess
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_CHUNK_CHARS = 3800  # Leave buffer below NVARCHAR(4000)
MIN_CHUNK_CHARS = 150   # Below this, merge with adjacent chunk
SOURCE_TYPE = "workshop"

# Noise patterns to remove (line-level)
_NOISE_LINE_PATTERNS = [
    re.compile(r"^\s*INTERNAL\s*[-–—]\s*SAP\s+(and|&)\s+Partners?\s+[Oo]nly\s*$", re.IGNORECASE),
    re.compile(r"^\s*INTERNAL\s*$", re.IGNORECASE),
    re.compile(r"^\s*PUBLI\s*C\s*$", re.IGNORECASE),
    re.compile(r"^\s*PUBLIC\s*$", re.IGNORECASE),
    re.compile(r"^\s*Inte\s*rnal\s.*SAP\s+and\s+(Partner|Exter)", re.IGNORECASE),
    re.compile(r"^\s*\d{1,3}\s*$"),  # Page numbers alone
    re.compile(r"^©\s*20\d{2}\s+SAP\s+SE", re.IGNORECASE),
    re.compile(r"^\s*Disclaimer.*subject to change", re.IGNORECASE),
]

# Full page noise detection
_NOISE_PAGE_PATTERNS = [
    re.compile(r"confidential and proprietary to SAP", re.IGNORECASE),
    re.compile(r"^Presenters?\s*$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"^Thank you!\s*$", re.MULTILINE | re.IGNORECASE),
]

# Disclaimer block (multi-line) embedded in content pages
_DISCLAIMER_BLOCK_RE = re.compile(
    r"Disclaimer:?\s*Th?is\s+document.*?without\s+notice\.?",
    re.DOTALL | re.IGNORECASE,
)

# Roadmap header detection
_ROADMAP_HEADER_RE = re.compile(
    r"SAP\s+Ariba\s+(.+?)\s+Road\s*[Mm]ap\s*[–—-]\s*2026",
    re.IGNORECASE,
)

# Knowledge Check detection
_KNOWLEDGE_CHECK_RE = re.compile(r"KNOWLEDGE\s+CHECK", re.IGNORECASE)

# Quarter detection in roadmaps
_QUARTER_RE = re.compile(r"(Q[1-4]/2026|Q[1-4]\s*2026)", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Solution Mapping
# ---------------------------------------------------------------------------
ROADMAP_SOLUTION_MAP = {
    "strategic sourcing": "Ariba Sourcing",
    "sourcing": "Ariba Sourcing",
    "contracts": "Ariba Contracts",
    "supplier management": "Ariba SLP",
    "supplier": "Ariba SLP",
    "buying": "Ariba Buying",
    "invoicing": "Ariba Invoice",
    "category management": "SAP Ariba Category Management",
    "spend analysis": "Spend Analysis",
}

SECTION_SOLUTION_MAP = {
    "supplier information management": "Ariba SLP",
    "supplier concepts": "Ariba SLP",
    "supplier management": "Ariba SLP",
    "sim concept": "Ariba SLP",
    "sourcing": "Ariba Sourcing",
    "contracts": "Ariba Contracts",
    "buying": "Ariba Buying",
    "invoicing": "Ariba Invoice",
    "category management": "SAP Ariba Category Management",
}

VIDEO_SOLUTION_MAP = {
    "supplier-information-management": "Ariba SLP",
}

# SQL
INSERT_SQL = """
INSERT INTO SVA2.KNOWLEDGE_BASE
    (SOURCE_TYPE, SOLUTION, TITLE, CONTENT, SOURCE_FILE, EMBEDDING)
VALUES
    (?, ?, ?, ?, ?, TO_REAL_VECTOR(?))
"""


# ---------------------------------------------------------------------------
# PDF Processing
# ---------------------------------------------------------------------------
def extract_pdf_text(pdf_path: str) -> str:
    """Extract full text from PDF using pdftotext with layout preservation."""
    result = subprocess.run(
        ["pdftotext", "-layout", pdf_path, "-"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {result.stderr}")
    return result.stdout


def split_pages(text: str) -> list[str]:
    """Split pdftotext output into pages by form-feed character."""
    return text.split("\f")


def is_noise_page(page_text: str) -> bool:
    """Detect pages that should be entirely skipped."""
    stripped = page_text.strip()
    if not stripped or len(stripped) < 30:
        return True
    # Disclaimer pages (long legal text with specific markers)
    if "confidential and proprietary to SAP" in stripped.lower():
        return True
    if "not a commitment, promise or legal obligation" in stripped.lower():
        return True
    # Pure presenter/thank you/transition slides
    clean = re.sub(r"\s+", " ", stripped).strip()
    if len(clean) < 120:
        if re.search(r"(Presenters?|Thank you!|Coffee Break|Break for lunch|Demo\s*$|Day \d|Agenda)", clean, re.IGNORECASE):
            return True
    return False


def clean_page(page_text: str) -> str:
    """Remove noise lines from a page."""
    lines = page_text.split("\n")
    cleaned = []
    for line in lines:
        skip = False
        for pattern in _NOISE_LINE_PATTERNS:
            if pattern.search(line):
                skip = True
                break
        if not skip:
            cleaned.append(line)
    text = "\n".join(cleaned)
    # Remove embedded disclaimer blocks
    text = _DISCLAIMER_BLOCK_RE.sub("", text)
    # Remove excessive blank lines
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


_INVALID_TITLES = {
    "disclaimer", "agenda", "presenters", "thank you!", "demo",
    "coffee break", "break for lunch", "hands-on", "video",
    "knowledge check", "day 1", "day 2", "day 3",
}


def detect_section_title(page_text: str) -> str | None:
    """Detect if a page starts with a section title (short, standalone line)."""
    lines = [l.strip() for l in page_text.strip().split("\n") if l.strip()]
    if not lines:
        return None
    first = lines[0]
    # Section titles are typically short (< 100 chars) and not a bullet point
    if len(first) < 100 and not first.startswith(("•", "-", "▪", "1.", "2.", "3.")):
        # Check it's not just a number or a very short phrase with special chars
        if len(first) > 5 and not re.match(r"^\d+$", first):
            # Filter out known non-title headers
            if first.lower().strip(":").strip() in _INVALID_TITLES:
                return None
            return first
    return None


def map_solution_from_title(title: str) -> str | None:
    """Map a section/chunk title to a canonical solution name."""
    title_lower = title.lower()
    for key, solution in SECTION_SOLUTION_MAP.items():
        if key in title_lower:
            return solution
    return None


def parse_roadmap_chunks(page_text: str, source_file: str) -> list[dict]:
    """Parse a roadmap page into per-solution chunks."""
    chunks = []
    # Detect solution from roadmap header
    m = _ROADMAP_HEADER_RE.search(page_text)
    if not m:
        return []

    raw_solution = m.group(1).strip()
    solution = None
    for key, sol in ROADMAP_SOLUTION_MAP.items():
        if key in raw_solution.lower():
            solution = sol
            break
    if not solution:
        solution = raw_solution

    # Extract content after the header
    content_start = m.end()
    content = page_text[content_start:].strip()

    if not content or len(content) < 50:
        return []

    # Try to split by quarters
    quarter_positions = []
    for qm in _QUARTER_RE.finditer(content):
        quarter_positions.append((qm.start(), qm.group(1).upper().replace(" ", "/")))

    if len(quarter_positions) >= 2:
        # Split content by quarter markers
        for i, (pos, quarter) in enumerate(quarter_positions):
            end_pos = quarter_positions[i + 1][0] if i + 1 < len(quarter_positions) else len(content)
            quarter_content = content[pos:end_pos].strip()
            # Remove the quarter header itself from content
            quarter_content = _QUARTER_RE.sub("", quarter_content, count=1).strip()
            if quarter_content and len(quarter_content) > 50:
                title = f"{solution} Roadmap {quarter}"
                chunks.append({
                    "title": title[:500],
                    "content": quarter_content[:MAX_CHUNK_CHARS],
                    "solution": solution,
                    "source_file": source_file,
                })
    else:
        # Can't split by quarter — treat entire roadmap as one chunk
        title = f"{solution} Roadmap 2026"
        if len(content) > MAX_CHUNK_CHARS:
            # Split at midpoints
            for i in range(0, len(content), MAX_CHUNK_CHARS):
                chunk_text = content[i:i + MAX_CHUNK_CHARS]
                part = f" (Part {i // MAX_CHUNK_CHARS + 1})" if i > 0 else ""
                chunks.append({
                    "title": f"{title}{part}"[:500],
                    "content": chunk_text,
                    "solution": solution,
                    "source_file": source_file,
                })
        else:
            chunks.append({
                "title": title[:500],
                "content": content[:MAX_CHUNK_CHARS],
                "solution": solution,
                "source_file": source_file,
            })

    return chunks


def parse_knowledge_checks(pages: list[str], source_file: str) -> list[dict]:
    """Extract Knowledge Check Q&A from pages."""
    chunks = []
    i = 0
    while i < len(pages):
        page = pages[i]
        if not _KNOWLEDGE_CHECK_RE.search(page):
            i += 1
            continue

        # This page has a question. The NEXT page typically has the same question + answer highlighted
        lines = [l.strip() for l in page.split("\n") if l.strip()]
        # Find the question number and text
        question_text = None
        options = []
        for line in lines:
            if _KNOWLEDGE_CHECK_RE.match(line):
                continue
            if re.match(r"^\d+$", line):
                continue
            # Question line (usually after the number)
            if not question_text and len(line) > 20 and "?" in line:
                question_text = line
            elif question_text and re.match(r"^[A-D]\s", line):
                options.append(line)
            elif not question_text and len(line) > 20:
                question_text = line

        # Look for the answer page (next page usually repeats with answer marked)
        # In these PDFs, the answer page is identical but the correct answer appears
        # The pattern is: question page, then answer page (same content)
        # We take the options from the first page and look for the correct answer
        # The correct answer is typically the one that appears in both pages
        if question_text and i + 1 < len(pages):
            next_page = pages[i + 1]
            if _KNOWLEDGE_CHECK_RE.search(next_page):
                # Answer page — extract the correct answer
                next_lines = [l.strip() for l in next_page.split("\n") if l.strip()]
                answer_options = [l for l in next_lines if re.match(r"^[A-D]\s", l)]
                if answer_options:
                    # The correct answer is typically listed — take the answer from options
                    # In these PDFs, the answer slide shows the correct answer
                    correct = answer_options[0] if answer_options else (options[0] if options else "")
                    if correct and question_text:
                        # Format: question + all options + correct answer
                        content_parts = [f"Options: {'; '.join(options)}"] if options else []
                        content_parts.append(f"Correct answer: {correct}")
                        chunks.append({
                            "title": question_text[:500],
                            "content": " | ".join(content_parts)[:MAX_CHUNK_CHARS],
                            "solution": map_solution_from_title(question_text),
                            "source_file": source_file,
                        })
                i += 2  # Skip both question and answer pages
                continue

        i += 1

    return chunks


def chunk_pdf(pdf_path: str) -> list[dict]:
    """Process a single PDF into chunks."""
    filename = os.path.basename(pdf_path)
    print(f"  Processing: {filename}")

    text = extract_pdf_text(pdf_path)
    pages = split_pages(text)
    print(f"    Pages extracted: {len(pages)}")

    # Filter noise pages
    clean_pages = []
    for page in pages:
        if not is_noise_page(page):
            cleaned = clean_page(page)
            if cleaned and len(cleaned) > MIN_CHUNK_CHARS:
                clean_pages.append(cleaned)

    print(f"    Useful pages after noise removal: {len(clean_pages)}")

    chunks = []

    # 1. Extract Knowledge Check Q&A (from raw pages for pattern matching)
    kc_chunks = parse_knowledge_checks(pages, filename)
    chunks.extend(kc_chunks)
    print(f"    Knowledge Check Q&A extracted: {len(kc_chunks)}")

    # 2. Extract Roadmap chunks
    roadmap_chunks = []
    non_roadmap_pages = []
    for page in clean_pages:
        if _ROADMAP_HEADER_RE.search(page):
            rc = parse_roadmap_chunks(page, filename)
            roadmap_chunks.extend(rc)
        else:
            non_roadmap_pages.append(page)

    chunks.extend(roadmap_chunks)
    print(f"    Roadmap chunks extracted: {len(roadmap_chunks)}")

    # 3. Process remaining pages into section-based chunks
    current_section_title = None
    current_section_content = []

    def flush_section():
        nonlocal current_section_title, current_section_content
        if not current_section_content:
            return
        content = "\n\n".join(current_section_content).strip()
        if len(content) < MIN_CHUNK_CHARS:
            current_section_content.clear()
            return

        title = current_section_title or "Next-gen SAP Ariba Workshop"
        solution = map_solution_from_title(title)

        # Split if too long
        if len(content) <= MAX_CHUNK_CHARS:
            chunks.append({
                "title": title[:500],
                "content": content[:MAX_CHUNK_CHARS],
                "solution": solution,
                "source_file": filename,
            })
        else:
            # Split at paragraph boundaries
            paragraphs = content.split("\n\n")
            current_chunk = ""
            part_num = 1
            for para in paragraphs:
                if len(current_chunk) + len(para) + 2 > MAX_CHUNK_CHARS:
                    if current_chunk.strip():
                        suffix = f" (Part {part_num})" if part_num > 1 else ""
                        chunks.append({
                            "title": f"{title}{suffix}"[:500],
                            "content": current_chunk.strip()[:MAX_CHUNK_CHARS],
                            "solution": solution,
                            "source_file": filename,
                        })
                        part_num += 1
                    current_chunk = para
                else:
                    current_chunk += "\n\n" + para if current_chunk else para
            if current_chunk.strip() and len(current_chunk.strip()) >= MIN_CHUNK_CHARS:
                suffix = f" (Part {part_num})" if part_num > 1 else ""
                chunks.append({
                    "title": f"{title}{suffix}"[:500],
                    "content": current_chunk.strip()[:MAX_CHUNK_CHARS],
                    "solution": solution,
                    "source_file": filename,
                })

        current_section_content.clear()

    for page in non_roadmap_pages:
        # Skip pages that are primarily Knowledge Check content
        if _KNOWLEDGE_CHECK_RE.search(page):
            continue

        section_title = detect_section_title(page)
        if section_title and section_title != current_section_title:
            flush_section()
            current_section_title = section_title
            # Don't add the title line itself to content — it's metadata
            lines = page.strip().split("\n")
            content_lines = lines[1:] if len(lines) > 1 else lines
            page_content = "\n".join(content_lines).strip()
            if page_content:
                current_section_content.append(page_content)
        else:
            current_section_content.append(page.strip())

    flush_section()

    print(f"    Section chunks: {len(chunks) - len(kc_chunks) - len(roadmap_chunks)}")
    print(f"    TOTAL chunks from {filename}: {len(chunks)}")
    return chunks


# ---------------------------------------------------------------------------
# Video Transcription
# ---------------------------------------------------------------------------
def transcribe_video(video_path: str, model_size: str = "medium") -> list[dict]:
    """Transcribe a video file using faster-whisper and return chunks."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("  ERROR: faster-whisper not installed. Run: pip install faster-whisper")
        return []

    filename = os.path.basename(video_path)
    print(f"  Transcribing: {filename} (model={model_size})...")

    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        video_path,
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=2000),
    )

    # Collect all segments
    all_segments = []
    for segment in segments:
        all_segments.append({
            "start": segment.start,
            "end": segment.end,
            "text": segment.text.strip(),
        })

    if not all_segments:
        print(f"    No speech detected in {filename}")
        return []

    print(f"    Segments transcribed: {len(all_segments)}")
    print(f"    Duration: {info.duration:.1f}s")

    # Group segments into paragraphs (merge by pauses > 2s)
    paragraphs = []
    current_para = []
    last_end = 0

    for seg in all_segments:
        if current_para and (seg["start"] - last_end) > 2.0:
            paragraphs.append(" ".join(s["text"] for s in current_para))
            current_para = []
        current_para.append(seg)
        last_end = seg["end"]

    if current_para:
        paragraphs.append(" ".join(s["text"] for s in current_para))

    # Chunk paragraphs into max-size chunks
    chunks = []
    current_chunk = ""
    topic = _video_topic_from_filename(filename)
    solution = _video_solution(filename)

    for para in paragraphs:
        if len(current_chunk) + len(para) + 2 > MAX_CHUNK_CHARS:
            if current_chunk.strip() and len(current_chunk.strip()) >= MIN_CHUNK_CHARS:
                part = len(chunks) + 1
                title = f"[Video] {topic} (Part {part})"
                chunks.append({
                    "title": title[:500],
                    "content": current_chunk.strip()[:MAX_CHUNK_CHARS],
                    "solution": solution,
                    "source_file": filename,
                })
            current_chunk = para
        else:
            current_chunk += " " + para if current_chunk else para

    if current_chunk.strip() and len(current_chunk.strip()) >= MIN_CHUNK_CHARS:
        part = len(chunks) + 1
        title = f"[Video] {topic}" if part == 1 else f"[Video] {topic} (Part {part})"
        chunks.append({
            "title": title[:500],
            "content": current_chunk.strip()[:MAX_CHUNK_CHARS],
            "solution": solution,
            "source_file": filename,
        })

    print(f"    Chunks from video: {len(chunks)}")
    return chunks


def _video_topic_from_filename(filename: str) -> str:
    """Extract a human-readable topic from the video filename."""
    name = filename.replace(".mp4", "").replace("next-gen-sap-ariba-", "")
    return name.replace("-", " ").replace("_", " ").title()


def _video_solution(filename: str) -> str | None:
    """Map video filename to canonical solution."""
    for key, sol in VIDEO_SOLUTION_MAP.items():
        if key in filename:
            return sol
    return None


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------
def _normalize_text(text: str) -> str:
    """Normalize text for comparison."""
    return re.sub(r"\s+", " ", text.lower().strip())


def _jaccard_similarity(text_a: str, text_b: str) -> float:
    """Compute Jaccard similarity between two texts (word-level)."""
    words_a = set(text_a.lower().split())
    words_b = set(text_b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


def deduplicate_chunks(
    chunks: list[dict],
    existing_titles: set[str] | None = None,
    existing_contents: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """Remove duplicate chunks. Returns (unique_chunks, stats)."""
    stats = {"intra_doc": 0, "vs_existing": 0, "total_removed": 0}
    seen_keys = set()  # (normalized_title, first_200_content)
    unique = []

    for chunk in chunks:
        title_norm = _normalize_text(chunk["title"])
        content_norm = _normalize_text(chunk["content"])
        content_key = content_norm[:200]

        # Intra-batch dedup
        dedup_key = (title_norm, content_key)
        if dedup_key in seen_keys:
            stats["intra_doc"] += 1
            stats["total_removed"] += 1
            continue
        seen_keys.add(dedup_key)

        # Cross-document dedup against existing KB
        if existing_titles and title_norm in existing_titles:
            stats["vs_existing"] += 1
            stats["total_removed"] += 1
            continue

        if existing_contents:
            is_dup = False
            for existing in existing_contents:
                if _jaccard_similarity(content_norm, existing) > 0.90:
                    is_dup = True
                    break
            if is_dup:
                stats["vs_existing"] += 1
                stats["total_removed"] += 1
                continue

        unique.append(chunk)

    return unique, stats


def fetch_existing_kb_entries(conn) -> tuple[set[str], list[str]]:
    """Fetch existing titles and content snippets from KB for dedup."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT TITLE, CONTENT FROM SVA2.KNOWLEDGE_BASE "
        "WHERE SOURCE_TYPE IN ('next_gen', 'workshop')"
    )
    titles = set()
    contents = []
    for row in cursor.fetchall():
        titles.add(_normalize_text(row[0] or ""))
        contents.append(_normalize_text((row[1] or "")[:200]))
    cursor.close()
    return titles, contents


# ---------------------------------------------------------------------------
# Database Operations
# ---------------------------------------------------------------------------
def insert_chunks(chunks: list[dict], conn, dry_run: bool = False) -> tuple[int, int]:
    """Batch-insert chunks with embeddings. Returns (inserted, skipped)."""
    from shared.config import embed_text

    if dry_run:
        return 0, 0

    cursor = conn.cursor()
    inserted = skipped = 0

    for chunk in chunks:
        title = chunk["title"][:500]
        content = chunk["content"][:4000]

        if not title or not content:
            skipped += 1
            continue

        embed_input = f"{title} — {content}"
        try:
            vector = embed_text(embed_input)
        except Exception as e:
            print(f"    Warning: embedding failed for '{title[:60]}': {e}")
            skipped += 1
            continue

        vector_str = "[" + ",".join(str(v) for v in vector) + "]"

        cursor.execute(INSERT_SQL, (
            SOURCE_TYPE,
            chunk.get("solution"),
            title,
            content,
            chunk.get("source_file", ""),
            vector_str,
        ))

        inserted += 1
        if inserted % 10 == 0:
            conn.commit()
            print(f"    {inserted} rows committed...")

    conn.commit()
    cursor.close()
    return inserted, skipped


# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------
def ingest_workshop(
    data_dir: str,
    mode: str = "all",
    dry_run: bool = False,
    replace: bool = False,
    whisper_model: str = "medium",
):
    """Main ingestion pipeline."""
    data_path = pathlib.Path(data_dir)
    if not data_path.exists():
        print(f"ERROR: Directory not found: {data_dir}")
        sys.exit(1)

    all_chunks = []

    # --- PDF Processing ---
    if mode in ("all", "pdf"):
        pdf_files = sorted(data_path.glob("*.pdf"))
        print(f"\n{'='*60}")
        print(f"PDF PROCESSING ({len(pdf_files)} files)")
        print(f"{'='*60}")

        for pdf_file in pdf_files:
            chunks = chunk_pdf(str(pdf_file))
            all_chunks.extend(chunks)

        print(f"\nTotal PDF chunks: {len(all_chunks)}")

    # --- Video Transcription ---
    if mode in ("all", "video"):
        video_files = sorted(data_path.glob("*.mp4"))
        print(f"\n{'='*60}")
        print(f"VIDEO TRANSCRIPTION ({len(video_files)} files, model={whisper_model})")
        print(f"{'='*60}")

        # Check ffmpeg
        if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
            print("  ERROR: ffmpeg not installed. Run: brew install ffmpeg")
            if mode == "video":
                sys.exit(1)
        else:
            video_chunk_count = 0
            for video_file in video_files:
                chunks = transcribe_video(str(video_file), model_size=whisper_model)
                all_chunks.extend(chunks)
                video_chunk_count += len(chunks)
            print(f"\nTotal video chunks: {video_chunk_count}")

    print(f"\n{'='*60}")
    print(f"TOTAL CHUNKS BEFORE DEDUP: {len(all_chunks)}")
    print(f"{'='*60}")

    # --- Deduplication ---
    print("\n-- Deduplication pass...")
    existing_titles = None
    existing_contents = None

    if not dry_run:
        from dotenv import load_dotenv
        load_dotenv()
        from shared.config import hana_connection
        conn = hana_connection()
        existing_titles, existing_contents = fetch_existing_kb_entries(conn)
        print(f"   Existing KB entries loaded: {len(existing_titles)} titles")
    else:
        existing_titles = set()
        existing_contents = []

    unique_chunks, dedup_stats = deduplicate_chunks(
        all_chunks, existing_titles, existing_contents
    )

    print(f"   Removed {dedup_stats['total_removed']} duplicates:")
    print(f"     - Intra-document: {dedup_stats['intra_doc']}")
    print(f"     - Vs existing KB: {dedup_stats['vs_existing']}")
    print(f"   UNIQUE CHUNKS TO INSERT: {len(unique_chunks)}")

    # --- Dry Run Report ---
    if dry_run:
        print(f"\n{'='*60}")
        print("DRY RUN — Sample chunks:")
        print(f"{'='*60}")
        for i, chunk in enumerate(unique_chunks[:8]):
            print(f"\n  [{i+1}] Title: {chunk['title'][:80]}")
            print(f"       Solution: {chunk.get('solution') or 'NULL'}")
            print(f"       Source: {chunk.get('source_file', '?')}")
            print(f"       Content ({len(chunk['content'])} chars): {chunk['content'][:120]}...")

        # Stats by source file
        print(f"\n\n-- Distribution by source file:")
        by_file = defaultdict(int)
        for chunk in unique_chunks:
            by_file[chunk.get("source_file", "unknown")] += 1
        for f, count in sorted(by_file.items()):
            print(f"     {f}: {count} chunks")

        # Stats by solution
        print(f"\n-- Distribution by solution:")
        by_sol = defaultdict(int)
        for chunk in unique_chunks:
            by_sol[chunk.get("solution") or "NULL (cross-cutting)"] += 1
        for sol, count in sorted(by_sol.items()):
            print(f"     {sol}: {count} chunks")

        # Content length stats
        lengths = [len(c["content"]) for c in unique_chunks]
        if lengths:
            print(f"\n-- Content length stats:")
            print(f"     Min: {min(lengths)} chars")
            print(f"     Max: {max(lengths)} chars")
            print(f"     Avg: {sum(lengths) // len(lengths)} chars")
            over_limit = sum(1 for l in lengths if l > 4000)
            print(f"     Over 4000 chars: {over_limit}")

        print("\n-- No database changes made (dry run).")
        return

    # --- Database Insert ---
    if replace:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM SVA2.KNOWLEDGE_BASE WHERE SOURCE_TYPE = ?",
            (SOURCE_TYPE,),
        )
        existing_count = cursor.fetchone()[0]
        if existing_count > 0:
            print(f"\n-- Deleting {existing_count} existing workshop entries...")
            cursor.execute(
                "DELETE FROM SVA2.KNOWLEDGE_BASE WHERE SOURCE_TYPE = ?",
                (SOURCE_TYPE,),
            )
            conn.commit()
            print("   Deleted.")
        cursor.close()

    print(f"\n-- Inserting {len(unique_chunks)} chunks into HANA...")
    inserted, skipped = insert_chunks(unique_chunks, conn, dry_run=dry_run)
    conn.close()

    print(f"\n{'='*60}")
    print(f"DONE: inserted={inserted}, skipped={skipped}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Ingest NG Workshop materials (PDFs + videos) into SVA2.KNOWLEDGE_BASE"
    )
    parser.add_argument("dir", help="Path to NG Workshop directory")
    parser.add_argument(
        "--mode", choices=["all", "pdf", "video"], default="all",
        help="What to process: all (default), pdf only, or video only",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and show stats without database writes",
    )
    parser.add_argument(
        "--replace", action="store_true",
        help="Delete existing workshop entries before inserting",
    )
    parser.add_argument(
        "--whisper-model", default="medium",
        help="Whisper model size for video transcription (default: medium)",
    )
    args = parser.parse_args()

    ingest_workshop(
        data_dir=args.dir,
        mode=args.mode,
        dry_run=args.dry_run,
        replace=args.replace,
        whisper_model=args.whisper_model,
    )
