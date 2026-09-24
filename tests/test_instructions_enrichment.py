"""
tests/test_instructions_enrichment.py
--------------------------------------
Unit tests for EKX + OneAI enrichment logic in SINGLE_QUERY_INSTRUCTIONS.

Tests verify:
- Step 2.6 is present and correctly positioned
- Trigger conditions A, B, C are defined
- EKX and OneAI connector checks are present
- NEXT-GEN COVERAGE content order (A→B→C→D) is enforced
- Guardrails: word-for-word, no Nexus, citations, no separate sections
- RECOMMENDATION EKX grounding rule
- Backwards compatibility: all pre-existing step structure intact

Run: python -m pytest tests/test_instructions_enrichment.py -v
"""
import sys
import pathlib
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from shared.instructions import SINGLE_QUERY_INSTRUCTIONS as I


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pos(token: str) -> int:
    """Return position of token in instructions, or -1 if absent."""
    return I.find(token)


def _present(token: str) -> bool:
    return token in I


# ---------------------------------------------------------------------------
# Step structure
# ---------------------------------------------------------------------------

class TestStepStructure:
    """All steps must be present and in correct order."""

    STEPS = [
        "0. MULTI-POINT",
        "1. Determine",
        "2. Once the user",
        "2.5 Immediately",
        "2.6 EXTERNAL",
        "2.7 DOCUMENTATION",
        "3. Synthesize",
    ]

    def test_all_steps_present(self):
        for step in self.STEPS:
            assert _present(step), f"Missing step: {step!r}"

    def test_step_order(self):
        positions = [_pos(s) for s in self.STEPS]
        for i in range(len(positions) - 1):
            assert positions[i] < positions[i + 1], (
                f"Order violation: {self.STEPS[i]!r} (pos {positions[i]}) "
                f"must come before {self.STEPS[i+1]!r} (pos {positions[i+1]})"
            )

    def test_step_26_between_25_and_27(self):
        assert _pos("2.5 Immediately") < _pos("2.6 EXTERNAL") < _pos("2.7 DOCUMENTATION")


# ---------------------------------------------------------------------------
# Trigger conditions
# ---------------------------------------------------------------------------

class TestTriggerConditions:
    """Step 2.6 must define three explicit trigger conditions."""

    def test_condition_a_weak_hana(self):
        assert _present("Condition A"), "Condition A (weak HANA signal) missing"
        # Must reference similarity or empty cases
        assert _present("similarity_score") or _present("0.65"), \
            "Condition A must reference similarity threshold"

    def test_condition_b_thin_next_gen(self):
        assert _present("Condition B"), "Condition B (thin next-gen data) missing"
        assert _present("fewer than 3"), \
            "Condition B must specify the < 3 entries threshold"

    def test_condition_c_qa_question(self):
        assert _present("Condition C"), "Condition C (Q&A question) missing"
        # Must include conceptual question signals
        assert _present("what is") or _present("explain") or _present("conceptual"), \
            "Condition C must list signals for conceptual/Q&A questions"

    def test_skip_when_no_condition_met(self):
        # Instructions must state that step is skipped when no condition applies
        assert _present("skip this step entirely"), \
            "Must instruct to skip step 2.6 when no condition is met"


# ---------------------------------------------------------------------------
# EKX connector rules
# ---------------------------------------------------------------------------

class TestEkxRules:
    """EKX (ask_ekx) usage rules."""

    def test_ask_ekx_tool_named(self):
        assert _present("ask_ekx"), "ask_ekx tool must be named"

    def test_ekx_silent_when_unavailable(self):
        # Must say to skip silently when ask_ekx is not available
        idx_ekx_block = I.find("ask_ekx is NOT available")
        assert idx_ekx_block != -1, "Must handle ask_ekx NOT available case"
        # 'silently' must appear near the NOT available instruction
        nearby = I[idx_ekx_block: idx_ekx_block + 150]
        assert "silently" in nearby, \
            "Must skip EKX silently when connector not available"

    def test_ekx_no_separate_section(self):
        assert _present("Do NOT create a separate"), \
            "Must forbid creating a separate EKX section in the card"

    def test_ekx_content_woven_into_card(self):
        assert _present("Weave") or _present("weave"), \
            "Must instruct to weave EKX content into existing card sections"

    def test_ekx_enriches_recommendation(self):
        assert _present("use EKX content as factual grounding"), \
            "RECOMMENDATION must reference EKX grounding when HANA is weak"

    def test_ekx_not_mentioned_in_recommendation(self):
        assert _present("Do not mention EKX explicitly"), \
            "Must forbid mentioning EKX explicitly in RECOMMENDATION text"

    def test_ekx_source_citation_in_next_gen(self):
        assert _present("Source: SAP Knowledge Graph (EKX)"), \
            "EKX citation must be defined for NEXT-GEN COVERAGE"

    def test_ekx_max_two_bullets(self):
        assert _present("at most 2 EKX bullets"), \
            "EKX contribution must be capped at 2 bullets"

    def test_ekx_no_repeat_rule(self):
        assert _present("Do NOT repeat information already stated"), \
            "Must forbid repeating content already covered by oneai_qa or HANA"


# ---------------------------------------------------------------------------
# OneAI connector rules
# ---------------------------------------------------------------------------

class TestOneAiRules:
    """OneAI (chat/search) usage rules."""

    def test_chat_tool_named(self):
        assert _present("chat tool"), "chat tool must be named"

    def test_space_id_present(self):
        assert _present("52406d42-3826-450d-8133-87f6e9636f8f"), \
            "OneAI spaceId must be hardcoded in instructions"

    def test_context_empty_array(self):
        assert _present("context: []"), \
            "OneAI chat call must pass context: []"

    def test_search_fallback_when_sources_empty(self):
        assert _present("call the search tool"), \
            "Must fall back to search when chat returns content but sources is empty"

    def test_oneai_silent_when_unavailable(self):
        idx = I.find("chat is NOT available")
        assert idx != -1, "Must handle chat NOT available case"
        nearby = I[idx: idx + 150]
        assert "silently" in nearby, \
            "Must skip OneAI silently when connector not available"

    def test_word_for_word_rule(self):
        assert _present("word-for-word"), \
            "OneAI answers must be presented word-for-word"
        assert _present("Never paraphrase"), \
            "Must explicitly forbid paraphrasing Q&A answers"

    def test_direct_match_defined(self):
        assert _present("DIRECT MATCH"), "DIRECT MATCH case must be defined"

    def test_close_match_defined(self):
        assert _present("CLOSE MATCH"), "CLOSE MATCH case must be defined"

    def test_close_match_preamble(self):
        assert _present("Note: the following answer addresses the closely related question"), \
            "Close match must include the specified preamble text"

    def test_qa_citation_required(self):
        assert _present("Source: Product Success - Next-gen SAP Ariba Q&A Database (INTERNAL)"), \
            "Q&A citation must be appended after every OneAI answer"

    def test_no_nexus_guardrail(self):
        assert _present('NEVER use the internal name "Nexus"') or \
               _present("NEVER use the internal name"), \
            "Must forbid using 'Nexus' in responses"

    def test_oneai_url_present(self):
        assert _present("oneai-api.cfapps"), \
            "OneAI connector URL must be referenced for identification"

    def test_four_search_rules_present(self):
        for n in ("Rule 1", "Rule 2", "Rule 3", "Rule 4"):
            assert _present(n), f"{n} for search fallback evaluation must be defined"


# ---------------------------------------------------------------------------
# NEXT-GEN COVERAGE content order
# ---------------------------------------------------------------------------

class TestNextGenCoverageOrder:
    """Content blocks A→B→C→D must appear in correct order."""

    BLOCKS = [
        ("A) OneAI Q&A answer",         "block_A"),
        ("B) HANA next_gen feature",     "block_B"),
        ("C) EKX next-gen context",      "block_C"),
        ("D) Workshop implementation",   "block_D"),
    ]

    def test_all_blocks_present(self):
        for token, _ in self.BLOCKS:
            assert _present(token), f"Missing block: {token!r}"

    def test_block_order(self):
        positions = [_pos(token) for token, _ in self.BLOCKS]
        labels = [label for _, label in self.BLOCKS]
        for i in range(len(positions) - 1):
            assert positions[i] < positions[i + 1], (
                f"Order violation: {labels[i]} (pos {positions[i]}) "
                f"must come before {labels[i+1]} (pos {positions[i+1]})"
            )

    def test_oneai_presented_first(self):
        """OneAI Q&A must appear before HANA next_gen bullets."""
        assert _pos("A) OneAI Q&A answer") < _pos("B) HANA next_gen feature")

    def test_ekx_after_hana_before_workshop(self):
        """EKX supplementary must be sandwiched between HANA and Workshop."""
        assert _pos("B) HANA next_gen feature") < _pos("C) EKX next-gen context") \
               < _pos("D) Workshop implementation")

    def test_mandatory_context_block_present(self):
        assert _present("MANDATORY context block"), \
            "Mandatory context block instruction must be present"

    def test_all_sources_empty_fallback(self):
        assert _present("ALL of these are empty"), \
            "Must define fallback when all sources (next_gen, workshop, oneai_qa, EKX) are empty"

    def test_content_order_label(self):
        assert _present("CONTENT ORDER"), \
            "CONTENT ORDER label must be present to guide the LLM"


# ---------------------------------------------------------------------------
# Backwards compatibility
# ---------------------------------------------------------------------------

class TestBackwardsCompatibility:
    """Pre-existing behaviour must not be broken."""

    def test_similarity_score_thresholds_intact(self):
        for threshold in ("0.80", "0.65", "0.55"):
            assert _present(threshold), f"Similarity threshold {threshold} missing"

    def test_solution_list_intact(self):
        for sol in ("Ariba Buying", "Ariba Sourcing", "Ariba Contracts",
                    "Business Network", "Spend Analysis"):
            assert _present(sol), f"Solution {sol!r} missing from list"

    def test_card_sections_intact(self):
        for section in ("PAIN POINT", "RECOMMENDATION", "EXPECTED BENEFITS",
                        "SVA ANALYSIS", "DOCUMENTATION", "CURRENT PLATFORM",
                        "NEXT-GEN COVERAGE", "VALUE KPIs"):
            assert _present(section), f"Card section {section!r} missing"

    def test_effort_labels_intact(self):
        for label in ("Low (1", "Medium (1", "High (1", "Complex (3+"):
            assert _present(label), f"Effort label {label!r} missing"

    def test_timeline_labels_intact(self):
        for label in ("Quick Win", "Short Term", "Mid Term", "Long Term"):
            assert _present(label), f"Timeline label {label!r} missing"

    def test_category_values_intact(self):
        for cat in ("Feature Adoption", "Innovation", "Q&A", "Process Change",
                    "Training", "Roadmap Discussion"):
            assert _present(cat), f"Category {cat!r} missing"

    def test_no_markdown_tables_rule_intact(self):
        assert _present("DO NOT use Markdown tables"), \
            "No markdown tables rule must remain"

    def test_never_truncate_rule_intact(self):
        assert _present("NEVER truncate") or _present("Never truncate"), \
            "Never truncate rule must remain"

    def test_multi_point_detection_intact(self):
        assert _present("MULTI-POINT DETECTION"), \
            "Multi-point detection logic must remain"
