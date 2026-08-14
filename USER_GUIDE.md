# Autonomous Value Advisory (AVA) — User Guide

> Your AI-powered co-pilot for delivering evidence-backed SAP Ariba recommendations.

---

## What is AVA?

AVA is an intelligent advisory tool that lives inside **Joule Work Desktop**. It helps Solution Value Advisors prepare faster, deliver sharper recommendations, and anchor client conversations in real data.

**What it does:**
- Finds similar pain points from a curated library of real SAP Ariba engagements
- Enriches recommendations with Next-Gen product roadmap features
- Links advice to measurable Value KPIs (formulas, drivers, capabilities)
- Provides pre-validated SAP documentation links
- Outputs structured, ready-to-present recommendation cards

**Who it's for:**
- Solution Value Advisors (SVAs) preparing for or conducting client workshops
- Anyone in the SAP Ariba ecosystem who needs quick, evidence-backed advisory insights

---

## How It Works

```
You describe a pain point
        ↓
AVA searches its historical library (semantic similarity)
        ↓
Retrieves matching Next-Gen features + Value KPIs
        ↓
Finds relevant SAP documentation
        ↓
Joule synthesizes everything into a structured recommendation card
```

Behind the scenes, AVA uses vector embeddings and cosine similarity to find the most relevant historical cases — not keyword matching. This means you can describe a problem in your own words and still get accurate results.

---

## Getting Started

1. Open **Joule Work Desktop**
2. The AVA MCP server is already connected (no setup needed)
3. Type your pain point in the chat — that's it

AVA will either:
- Respond directly if the solution is clear from context
- Ask you to pick from a list of 13 Ariba solutions if the module is ambiguous

---

## Writing Effective Queries

The quality of your input directly affects the quality of AVA's output. Here's how to get the best results:

### Do This

| Instead of... | Try... |
|---------------|--------|
| "Integration doesn't work" | "CIG data sync fails when Supplier Life Cycle events update between systems" |
| "We need better approvals" | "Purchase requisitions over $10K require 3 approval levels but the workflow skips the second approver" |
| "Sourcing is slow" | "Sourcing events take 2+ weeks because contributors bypass sealed envelopes and weighted scoring isn't enforced" |
| "Reporting issues" | "Custom reports in Ariba Reporting don't reflect real-time PO status changes from the ERP" |

### Key Principles

1. **Be specific** — Include the functional area, the user action, and the business impact
2. **Describe the problem, not the solution** — Let AVA find the recommendation
3. **Include context signals** — Mention modules, areas, integrations, or user roles when relevant
4. **Use the client's language** — AVA supports English, Spanish, and Portuguese (output matches input language)
5. **One pain point at a time** — If you have multiple, enter them separately for dedicated cards

### Multiple Pain Points

If you provide several pain points in one message (as a numbered list or separate paragraphs), AVA will process each independently and return a separate card for each.

---

## Understanding the Output

AVA returns a structured recommendation card with these sections:

### Pain Point & Solution
Your original input, tagged with the identified Ariba module.

### Recommendation
Actionable, step-by-step advice synthesized from similar historical cases and Joule's SAP knowledge. This is not a copy-paste from the database — it's freshly generated based on the best matching cases.

### Expected Benefits
Business outcomes the client can expect if the recommendation is implemented.

### SVA Analysis

| Field | What It Means |
|-------|---------------|
| **Category** | Type of action: Feature Adoption, Innovation, Process Change, Training, Q&A, or Roadmap Discussion |
| **Effort** | Implementation complexity: Low (1–3 days), Medium (1–3 weeks), High (1–2 months), Complex (3+ months) |
| **Timeline** | Expected delivery: Quick Win, Short Term, Mid Term, or Long Term |
| **Impact** | Always "To be assessed" — this is for YOU to evaluate in the client's context |

### Documentation
Pre-validated links to SAP Help Portal articles and community threads. These are checked server-side to ensure they point to real, specific content (not landing pages).

### Next-Gen Coverage
Relevant features from SAP Ariba's next-generation platform (Q1 2026). Always includes a context note about transition requirements. See the [Next-Gen section below](#next-gen-features) for details.

### Value KPIs
Up to 3 matched KPIs showing:
- **Driver** — what business goal it serves (e.g., Cost Reduction)
- **Lever** — the mechanism (e.g., Spend Under Management)
- **Capability** — the Ariba feature that enables it
- **Formula** — how to measure it
- **Frequency** — how often to track

---

## Similarity Scores

Each historical case returned by AVA includes a similarity score (0 to 1). Use these to calibrate how much weight to give each case:

| Score | Meaning | How to Use |
|-------|---------|------------|
| **0.80+** | Strong match | Anchor your recommendation on this case — it's highly relevant |
| **0.65–0.79** | Moderate match | Good directional signal — apply your own judgment to adapt |
| **0.55–0.64** | Weak match | Background context only — don't rely on it directly |
| **Below 0.55** | Not shown | Filtered out automatically (too noisy to be useful) |

If you see mostly weak matches, try rephrasing your pain point with more specific details.

---

## Next-Gen Features

AVA surfaces relevant features from SAP Ariba's next-generation platform. Important context:

- These features are part of the **Q1 2026 release** (AI-native architecture)
- They are **not available today** on the current platform
- Clients need to plan a **transition** (greenfield or brownfield migration)
- No new contract is required, but readiness must be assessed

**When to use Next-Gen in client conversations:**
- Roadmap discussions and strategic planning
- Demonstrating SAP's investment direction
- Addressing pain points that will be fundamentally solved by the new architecture
- Building the case for early transition planning

**When NOT to present Next-Gen as a solution:**
- Client needs an immediate fix (use current-platform recommendations instead)
- Client hasn't started transition planning

---

## Value KPIs

Value KPIs connect your recommendations to measurable business outcomes. They come from SAP's Value Lever Model and are available for these solutions:

| Solution | KPI Count |
|----------|-----------|
| Ariba Buying | 29 |
| Ariba Sourcing | 24 |
| Ariba Contracts | 17 |
| Ariba SLP | 16 |
| Ariba Supplier Risk | — |

**How to use KPIs in client conversations:**
- Quantify the "Expected Benefits" section with specific formulas
- Show clients HOW to measure success (not just promise outcomes)
- Use the frequency field to set realistic tracking cadence
- Reference the capability field to tie metrics back to Ariba features

---

## Supported Solutions

AVA covers **13 SAP Ariba solutions**:

1. Ariba Buying
2. Ariba Catalog
3. Ariba Contracts
4. Ariba Guided Buying
5. Ariba Invoice
6. Ariba Reporting
7. Ariba SIPM
8. Ariba SLP
9. Ariba Sourcing
10. Ariba Supplier Risk
11. Business Network
12. Commerce Automation
13. Spend Analysis

When entering a pain point, you don't need to use the exact name. AVA recognizes common aliases:
- "Sourcing" → Ariba Sourcing
- "SLP" → Ariba SLP
- "Risk" → Ariba Supplier Risk
- "Buying and Invoicing" → recognized as combined

---

## Tips & Tricks

### Before a Client Call
- Enter the client's known pain points one by one to pre-populate your talking points
- Review the Next-Gen features for strategic conversation starters
- Note the KPI formulas to propose measurement frameworks

### During a Live Call
- When a client raises an unexpected issue, query AVA in real-time
- Use "Let me check our latest insights" as a natural transition
- The card format gives you a structured narrative to follow

### After the Call
- Rate recommendations that were particularly useful or off-target (this improves future results for everyone)
- Use the batch mode (when available) to process all workshop findings at once

### General Best Practices
- **Layer AVA over your expertise** — it finds patterns, you contextualize for the client
- **Override when needed** — if a historical case doesn't fit, trust your judgment
- **Use KPIs to close the loop** — "Here's what we recommend, here's how we'll measure success"
- **Don't present Impact as final** — it's always "To be assessed" because only you know the client's context
- **Combine with your own research** — AVA is a starting point, not the final deliverable

---

## Rating Recommendations

After receiving a recommendation, you can rate it. This feeds back into the system and improves future results:

- **Useful** — the recommendation was relevant and actionable
- **Not useful** — it missed the mark

Ratings use an exponential moving average, so one bad rating won't destroy a good case — but consistent feedback trains the system over time.

---

## Limitations

| Constraint | Details |
|------------|---------|
| **Ariba-only** | Currently covers 13 SAP Ariba solutions. No S/4HANA, Fieldglass, or other SAP products yet |
| **Historical depth** | Results depend on what's been ingested. Novel or niche pain points may not have strong matches |
| **Impact is never auto-filled** | By design — this requires consultant judgment in client context |
| **Documentation is English-only** | SAP Help Portal search runs in English regardless of input language |
| **KPIs for 5 solutions** | Only Buying, Sourcing, Contracts, SLP, and Supplier Risk have KPI data currently |
| **Stateless** | AVA doesn't remember previous queries in a session — each query is independent |
| **Next-Gen = future** | Features shown are Q1 2026; they cannot be implemented today |

---

## FAQ

**Q: I got "No similar cases found" — what do I do?**
A: This means the pain point is novel or too different from the historical library. AVA will still provide Next-Gen features and KPIs if relevant. Try rephrasing with more specific details, or rely on Joule's general SAP knowledge.

**Q: Why is Impact always "To be assessed"?**
A: By design. The same recommendation can be high-impact for one client and low-impact for another. This field is for you to evaluate based on the client's specific situation, budget, and priorities.

**Q: Can I use AVA in Spanish or Portuguese?**
A: Yes. Write your pain point in any supported language and AVA will respond in the same language.

**Q: How current is the data?**
A: Pain points come from historical engagements (periodically updated). Next-Gen features reflect the Q1 2026 release. KPIs are from the current SAP Value Lever Model.

**Q: Can I add new pain points to the library?**
A: Not directly. Contact the AVA team to ingest new case libraries. They require a specific Excel format and admin access.

**Q: What if AVA suggests something I disagree with?**
A: Override it. AVA is a signal, not gospel. Your expertise and knowledge of the client context always takes precedence. Rate it "Not useful" to help improve future results.

**Q: Does rating a recommendation change it?**
A: Not immediately. Ratings affect the weighting of historical cases in future searches. Over time, highly-rated cases surface more prominently.

**Q: Is there a batch mode for multiple pain points?**
A: The batch mode (Excel input/output) is built and ready, but currently awaiting Joule Work Desktop support for file attachments. When available, you'll be able to upload an Excel with multiple pain points and get a fully formatted output file.

---

## Quick Reference

| Action | How |
|--------|-----|
| Get a recommendation | Type a pain point in Joule chat |
| Specify the solution | Mention it in your query, or pick from the list when prompted |
| Get multiple recommendations | Enter them as a numbered list |
| Rate a recommendation | Tell Joule "rate this as useful" or "rate this as not useful" |
| See available solutions | Ask "What solutions does AVA cover?" |
| Check adoption metrics | Ask "Show AVA usage metrics" |

---

*Last updated: August 2026*
