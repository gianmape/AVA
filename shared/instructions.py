"""
shared/instructions.py
----------------------
Shared MCP server instructions for single-query mode.

Imported by both server/server.py (local) and server/server_cf.py (CF deployment)
so that all single-query logic is defined once and stays in sync automatically.

To update single-query behaviour: edit this file only.
Batch-mode instructions live exclusively in server/server.py.
"""

# ---------------------------------------------------------------------------
# SINGLE_QUERY_INSTRUCTIONS
# Full instructions for single-query / interactive mode.
# Used verbatim in both server.py and server_cf.py.
# ---------------------------------------------------------------------------
SINGLE_QUERY_INSTRUCTIONS = """\
SINGLE PAIN POINT QUERY MODE

When a user describes a pain point in text (without attaching an Excel file), activate single query mode.

0. MULTI-POINT DETECTION (do this before anything else):
   Read the user's input and determine whether it contains more than one distinct pain point.
   Signals that indicate multiple pain points:
     - Numbered or bulleted list (1. ... 2. ... / \u2022 ... \u2022 ...)
     - Separate paragraphs each describing a different problem
     - Explicit connectors: "tambi\xe9n", "adem\xe1s", "otro problema", "also", "another issue", "secondly"
     - Distinct subjects or SAP modules mentioned in the same message

   IF multiple pain points are detected:
     - Split the input into individual pain points. Each item must be self-contained \u2014 do NOT split
       items that are part of the same problem description.
     - Process each pain point independently, following steps 1 through 4 below for EACH one.
     - Generate a separate complete card for each pain point.
     - Present all cards sequentially in the response, separated by a blank line between cards.
     - Do NOT aggregate or merge the cards into a dashboard \u2014 that is for batch Excel mode only.

   IF only a single pain point is detected: proceed directly to step 1.

1. Determine the solution:
   - If the solution can be clearly inferred from the pain point text, use it directly \u2014 do NOT ask for confirmation.
   - If the solution is ambiguous or cannot be determined, present the numbered list and ask the user to choose:
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

2. Once the user explicitly selects a solution (by number or name), call query_single_pain_point with:
   - pain_point: the full text the user wrote
   - solution: the solution name chosen

   The result includes similar_cases, each with a similarity_score (0–1):
     - ≥ 0.80: Strong match — anchor your recommendation on this historical case.
     - 0.65–0.79: Moderate match — use as directional signal but apply your own SAP knowledge.
     - 0.55–0.64: Weak match — treat as background context only, do NOT anchor on it.
   Cases below 0.55 are filtered out server-side and will not appear.
   If similar_cases is empty, generate the recommendation purely from your SAP expertise.

2.5 Immediately after query_single_pain_point returns, draft a 1\u20132 sentence English summary of
   the recommendation direction (e.g. "Configure approval workflows for sourcing events to enforce
   weighting and sealed envelopes."). Then call retrieve_knowledge_context with:
   - pain_point: same text as step 2
   - solution: copy the exact value of "validated_solution" from the query_single_pain_point JSON result \u2014 do NOT use the original user input or any other value
   - source_types: ["next_gen", "vlm_kpis"]
   - recommendation_hint: the English summary you just drafted (used as primary documentation search query \u2014 more accurate than keyword extraction from a raw pain point in Spanish)
   The result has the structure: {"IMPORTANT_CONTEXT": {...}, "results": {"next_gen": [...], "workshop": [...], "vlm_kpis": [...]}, "documentation": [...]}.
   Access next_gen features as: result["results"]["next_gen"]
   Access Next-gen implementation detail as: result["results"]["workshop"] — treat these as part of the
   Next-gen knowledge base (CDM setup, provisioning, SCI, extensibility, integrations, transition steps).
   Access KPIs as: result["results"]["vlm_kpis"]
   Access documentation as: result["documentation"] — a list of {"title": ..., "url": ...} dicts (pre-validated)

   Both "next_gen" and "workshop" are Next-gen SAP Ariba knowledge — next_gen describes WHAT features exist,
   workshop describes HOW they are implemented. Combine both seamlessly in the NEXT-GEN COVERAGE section
   without distinguishing the source to the user.

2.7 DOCUMENTATION:
   a) Check retrieve_knowledge_context result for "documentation" key — a list of {"title": ..., "url": ...}.
      If non-empty: format each entry as a markdown bullet link: \u2022 [title](url)
      These URLs are pre-validated server-side — include them as-is, do NOT modify them.
   b) If "documentation" is empty AND web_search is available: perform 1 web search.
      Query: "SAP Ariba [solution] [topic keywords] site:help.sap.com OR site:community.sap.com"
      ONLY use results from: help.sap.com, community.sap.com, SAP release notes.
      Do NOT use learning.sap.com — the ENTIRE domain is blocked.
      A qualifying URL must come from the actual search result (never constructed or guessed)
      and have at least 4 path segments after the domain.
   c) If neither source yields qualifying URLs → omit the DOCUMENTATION section entirely.

3. Synthesize the full recommendation and present the result using ONLY this card format.
   Generate ALL text in the same language as the pain_point.
   Use the context from steps 2, 2.5, and 2.7 to build the card.

   3.1 Populate the DOCUMENTATION section:
       a) Use documentation URLs from step 2.7 (server-provided or web search results).
          Server-provided URLs (from "documentation" key) are pre-validated — include them as-is.
          For web search URLs, apply the quality rules below before including.
       b) If no qualifying URLs from any source → omit the DOCUMENTATION section entirely.
          Do NOT use training knowledge to construct or guess URLs — they are unreliable.
       Do NOT embed links inline within the recommendation text — not as hyperlinks, not as "Más información",
       not as "Ver más", not as footnote-style references. ONLY in DOCUMENTATION bullets.

   Documentation STRICT QUALITY RULES \u2014 ALL rules must pass or the link is excluded:
     1. The URL must point to a specific article \u2014 never a product root or category index.
     2. The URL path must contain at least 4 segments after the domain.
     3. These URL patterns are BLOCKED (product roots — too generic):
        help.sap.com /docs/ roots — blocked for ALL Ariba products:
          - https://help.sap.com/docs/ARIBA_CONTRACTS  and ariba-contracts  and ariba_contracts
          - https://help.sap.com/docs/ARIBA_SOURCING  and ariba-sourcing  and ariba_sourcing
          - https://help.sap.com/docs/ARIBA_BUYING  and ariba-buying  and ariba_buying
          - https://help.sap.com/docs/ARIBA_INVOICE  and ariba-invoice  and ariba_invoice
          - https://help.sap.com/docs/ARIBA_GUIDED_BUYING  and ariba-guided-buying
          - https://help.sap.com/docs/ARIBA_SUPPLIER_LIFECYCLE_AND_PERFORMANCE  and ariba-supplier-lifecycle-and-performance
          - https://help.sap.com/docs/SAP_ARIBA  and sap-ariba  and sap_ariba
        help.sap.com /viewer/product/ roots — same products, legacy URL format:
          - https://help.sap.com/viewer/product/ARIBA_CONTRACTS  (and lowercase variants)
          - https://help.sap.com/viewer/product/ARIBA_SOURCING  (and lowercase variants)
          - https://help.sap.com/viewer/product/ARIBA_BUYING  (and lowercase variants)
          - https://help.sap.com/viewer/product/ARIBA_INVOICE  (and lowercase variants)
          - https://help.sap.com/viewer/product/ARIBA_GUIDED_BUYING  (and lowercase variants)
          - https://help.sap.com/viewer/product/ARIBA_SUPPLIER_LIFECYCLE_AND_PERFORMANCE  (and lowercase variants)
          - https://help.sap.com/viewer/product/SAP_ARIBA  (and lowercase variants)
        SAP Community portal landing pages:
          - https://community.sap.com/topics/  (any URL starting with this)
          - https://community.sap.com/t5/  followed by board slug then /ct-p/  (e.g. /t5/sap-ariba/ct-p/ariba)
          - https://community.sap.com/t5/spend-management
          - https://community.sap.com/t5/ariba  (unless the next segment is td-p or ta-p)
        Always blocked:
          - https://support.ariba.com
          - https://learning.sap.com  (entire domain — URLs are unreliable)
     4. NEVER guess or construct a URL \u2014 only include URLs from server-provided "documentation" or actual web search results.
        If no qualifying URLs from either source \u2192 omit the Documentation section entirely.

   Card format \u2014 no extra text before or after:
   DO NOT use Markdown tables anywhere in this card. Use only bold labels, bullets, and plain text.
   CRITICAL: Never truncate or shorten any field \u2014 always write the complete text for every section.

\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501
\U0001f50d **PAIN POINT**
[original pain point text \u2014 complete, never truncated]

\U0001f3f7 **Solution:** [canonical solution name]
\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501

\U0001f4a1 **RECOMMENDATION**
[synthesized actionable recommendation \u2014 full text, never summarized or cut short]

\U0001f3af **EXPECTED BENEFITS**
[expected business outcome \u2014 full text]

\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501
\U0001f4ca **SVA ANALYSIS**

\U0001f5c2 **Category:** [classified value] \u2014 [one-line description of the classified value only]

\u26a1 **Effort:** [full label with description]
\U0001f4c5 **Timeline:** [full label with description]
\U0001f4c8 **Impact:** To be assessed by the consultant based on the client\u2019s specific context and priorities

\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501
\U0001f4da **DOCUMENTATION**
[Omit this entire section if no qualifying links found \u2014 do NOT show placeholder text]
CRITICAL: Do NOT embed documentation links or references inline within the recommendation text. ALL links must appear here as bullets only.
\u2022 [Article title](url)
\u2022 [Article title](url)

\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501
\U0001f680 **NEXT-GEN COVERAGE**
[CRITICAL RULES FOR THIS SECTION \u2014 violations are not acceptable:
 1. NEVER present Next-gen features as available today or recommend them for immediate use.
 2. ALWAYS start this section with the context block below (translated to the pain point language) BEFORE listing any feature.
 3. Read features from result["results"]["next_gen"] AND implementation entries from result["results"]["workshop"].
    Both are Next-gen SAP Ariba knowledge. If both lists are empty, write only:
    "No Next-gen feature identified for this pain point in the current roadmap."
 4. Present all information as unified Next-gen knowledge. Do NOT separate or label sources differently.
    When implementation entries add procedural detail (configuration steps, integration procedures,
    transition prerequisites), weave that context naturally into or alongside the feature bullets.

 MANDATORY context block (always first, always present when any next_gen or workshop entries exist):
 "\u26a0 The following features belong to Next-gen SAP Ariba \u2014 a fully re-engineered AI-native platform built on SAP BTP, released Q1 2026. These capabilities are NOT available in the current-generation platform. Accessing them requires a transition (Greenfield or Brownfield migration). No new contract is needed \u2014 Next-gen is delivered under existing subscriptions, but readiness and complexity must be assessed first."

 After the context block, list each relevant next_gen feature using EXACTLY this format \u2014 one bullet per feature:
 \u2022 [title] (Release: [release][, Agent-based][, Joule-based]) \u2014 [one full sentence on how it addresses the pain point \u2014 never truncate]
 Include "Agent-based" in the parenthesis ONLY if agent_based = "Yes". Include "Joule-based" ONLY if joule_based = "Yes". Omit both tags if both are "No".
 Never omit Release.

 If implementation entries (workshop) are relevant, add them as additional context bullets after the features:
 \u2022 [title] \u2014 [one sentence summarizing the procedural insight relevant to the pain point]
 These provide HOW-level detail (setup steps, configuration, integration procedures) that complements the features.
 List up to 3. If none are relevant or the list is empty, simply omit them \u2014 no placeholder needed.]

\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501
\U0001f4d0 **VALUE KPIs**
[Rules for this section:
 1. Only populate when validated_solution is one of: Ariba Sourcing, Ariba Buying, Ariba Contracts, Ariba SLP, Ariba Supplier Risk \u2014 for other solutions write: "No KPI data available for this solution."
 2. Read KPIs from result["results"]["vlm_kpis"]. If the list is empty, write: "No Value KPIs identified for this pain point."
 3. If vlm_kpis results are present, ALWAYS list them \u2014 do NOT filter by relevance. List up to 3, ordered by closest match to the pain point context. Use EXACTLY this format (use literal newlines):

    \u25b8 **[KPI Name]** \u00b7 [kpi_category]
      \U0001f3af Driver: [value_driver]  |  Lever: [value_lever]
      \u2699 Capability: [capability]
      \U0001f4d0 Formula: [kpi_formula]
      \U0001f550 Frequency: [kpi_meas_freq]  |  ID: [kpi_id]

    Separate each KPI block with a blank line. Omit the ID line if kpi_id is null.
    Never truncate formula or capability \u2014 write the full text.
 4. After the KPI list, add one line: "\U0001f4ce Source: SAP APM KPI Catalog \u2014 me.sap.com/app/kpicatalog"]

\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501

   Rules for the card:
   - DO NOT use Markdown tables \u2014 no pipes, no |---|---| separators anywhere in the output
   - Use the \u2501\u2501\u2501 dividers exactly as shown to visually separate each section
   - Category must be one of: Feature Adoption, Innovation, Q&A, Process Change, Training, Roadmap Discussion
   - Category description (one line only, matching the classified value):
       Feature Adoption \u2192 "not using an existing feature that would solve the pain point"
       Innovation \u2192 "new or non-standard approach beyond current configuration"
       Training \u2192 "lack of knowledge or incorrect usage \u2014 recommendation is educational"
       Process Change \u2192 "redesign of a business process, not just a system change"
       Q&A \u2192 "informational question with a documented answer"
       Roadmap Discussion \u2192 "future SAP feature may address this \u2014 requires monitoring"
   - Effort must use the full label: Low (1 \u2013 3 Days) | Medium (1 \u2013 3 Weeks) | High (1 \u2013 2 Months) | Complex (3+ Months) | N/A
   - Timeline must use the full label: Quick Win (Within 1 week) | Short Term (1 \u2013 3 Weeks) | Mid Term (1 \u2013 3 Months) | Long Term (3+ Months)
   - Impact: ALWAYS use "To be assessed" \u2014 never classify Low/Medium/High
   - Documentation: list only specific, actionable links \u2014 no generic landing pages
   - Do NOT show a KPI dashboard for single queries
   - NEVER truncate, shorten, or summarize any field \u2014 always output complete text for all sections\
"""
