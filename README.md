

<p align="center">
  <img src="data/images/ava.jpg" alt="AVA — Autonomous Value Advisory" width="800" />
</p>

Turn years of SAP Solution Value Advisory knowledge into instant, evidence-backed recommendations.


AVA (Autonomous Value Advisory) elevates value advisory inside Joule Work Desktop. It contextualizes client pain points using SVA’s historical library of real Ariba cases, enriches with Next‑Gen Ariba roadmap features and VLM KPIs, and returns prioritized recommendations with effort, timeline, and quantified benefits. AVA helps SVA reduce prep time, increase proposal quality, and anchor narratives in measurable outcomes. Multilingual and SAP‑native, it fits directly into your day-to-day advisory work.

## Why now
- Joule Work Desktop brings an always‑on conversational assistant to the desktop — no browser, instant context switching.
- The difference-maker is context. AVA operationalizes SVA’s expert knowledge and fuses it with Product’s roadmap and VLM metrics to deliver recommendations that are specific and defensible.

## The challenge for SVA
- Great value narratives require speed, depth, and evidence. Generic AI misses context; scattered assets slow teams down; proposals often lack quantified outcomes.

## What AVA is
- An MCP server on SAP BTP Cloud Foundry connecting Joule Desktop to two SAP HANA Cloud vector indexes:
    - A historical pain point library of real SAP Ariba cases (by solution and functional area).
    - An internal knowledge base with 83 Next‑Gen Ariba features and 86 VLM KPIs across 5 solutions (Buying, Sourcing, Contracts, SLP, Supplier Risk).

## How it works
- Given a client pain point (chat query or Excel batch), AVA runs semantic similarity search (cosine similarity in HANA Vector Engine), enriches with roadmap features and VLM KPIs, synthesizes prioritized recommendations with category, effort, timeline, and benefits, and outputs a structured analysis card in chat or a ready‑to‑deliver Excel.

## Why it matters
- Faster prep, higher consistency, and metric‑anchored proposals that increase credibility and win rates.
- Structured feedback loop from the field — which pains occur most, where roadmap features map to pains, and where gaps remain.

## Status and roadmap
- **Live on Cloud Foundry** — single-query mode via Joule Work Desktop.
- **Batch mode ready** — Excel intake → enriched corporate-formatted output; waiting for JWD to support it.
- Multilingual (English, Spanish, Portuguese, and any language Joule supports).
- Limited pilot with selected employees; presented at SAP’s flagship event.
- Next: expert‑in‑the‑loop learning and expansion to Fieldglass and additional solutions.

## Native SAP stack
- Gemini Embedding via SAP AI Core, HANA Cloud Vector Engine as the store, Joule Desktop as the reasoning layer, MCP streamable‑HTTP as the protocol.

---
## Architecture

```
Joule Work Desktop
     │  MCP (streamable-HTTP)
     ▼
┌─────────────────────────────────────────────────────────┐
│  server/server.py       ← Local: batch + single query   │
│  server/server_cf.py    ← Cloud Foundry: single query   │
│         │                                               │
│         ├── server/recommend.py  ← Core retrieval logic │
│         │       ├── read_excel_painpoints()             │
│         │       ├── retrieve_similar_cases()            │
│         │       ├── retrieve_similar_cases_batch()      │
│         │       ├── retrieve_knowledge_context()        │
│         │       ├── search_documentation()             │
│         │       ├── write_excel_output()               │
│         │       └── rate_recommendation()              │
│         │                                               │
│         ├── shared/config.py     ← SDK, pool, taxonomy  │
│         │       ├── embed_text()                       │
│         │       ├── hana_connection()                  │
│         │       └── normalise_*()                      │
│         │                                               │
│         └── shared/instructions.py ← Single-query card  │
└─────────────────────────────────────────────────────────┘
```

### Two server variants

| Server | Mode | Tools | Use case |
|--------|------|-------|----------|
| `server.py` | HTTP + stdio | 7 (all) | Local dev, batch Excel, Claude Desktop |
| `server_cf.py` | HTTP only | 5 (single-query subset) | Cloud Foundry production (no local filesystem) |

### Pipeline — Single query (live on CF)

```
1. Consultant describes a pain point in Joule chat

2. query_single_pain_point(pain_point, solution)
        └─ embed → cosine search on SVA2.PAIN_POINTS
        └─ filtered by SOLUTION, threshold ≥ 0.55
        └─ quality-weighted scoring

3. retrieve_knowledge_context(pain_point, solution, ["next_gen", "vlm_kpis"])
        └─ parallel cosine search on SVA2.KNOWLEDGE_BASE
        └─ next_gen: solution-filtered, fallback to global
        └─ vlm_kpis: always solution-filtered
        └─ includes server-side documentation search (SAP Help Portal)

4. Joule synthesizes structured card:
        🔍 Pain Point → 💡 Recommendation → 🎯 Benefits
        📊 SVA Analysis → 📚 Documentation → 🚀 Next-Gen → 📋 KPIs
```

### Pipeline — Excel batch (ready, pending JWD support)

```
1. read_excel_painpoints(input_path, offset, limit=10)
        └─ detect sheet, normalise columns, detect language per row

2. retrieve_similar_cases_batch(items)
        └─ ThreadPoolExecutor — parallel HANA vector search
        └─ circuit breaker: aborts if >50% tasks fail

3. retrieve_knowledge_context(pain_point, solution, sources, hint)
        └─ called per row with Joule's drafted hint

4. Joule synthesizes per row:
        Recommendations · Category · Effort · Timeline · Benefits
        Documentation · Ariba Next-Gen · Value KPIs

5. write_excel_output(input_path, rows)
        └─ expands short labels, strips blocked URLs
        └─ SAP corporate format (Quick Reference Card spec)
        └─ saves to ~/Downloads/<stem>_RECOMMENDED.xlsx
```

---

## HANA Tables

### `SVA2.PAIN_POINTS`

Historical pain point library — indexed from legacy client Excel trackers.

| Column | Type | Description |
|---|---|---|
| `ID` | VARCHAR(36) | UUID primary key |
| `SOLUTION` | NVARCHAR(100) | Canonical SAP Ariba solution |
| `SOLUTION_AREA` | NVARCHAR(200) | Functional area within the solution |
| `PAIN_POINT` | NVARCHAR(4000) | Pain point text |
| `RECOMMENDATION` | NVARCHAR(4000) | Consultant recommendation |
| `CATEGORY` | NVARCHAR(100) | Feature Adoption / Innovation / Q&A / Process Change / Training / Roadmap Discussion |
| `EFFORT` | NVARCHAR(50) | Low / Medium / High / Complex / N/A |
| `BENEFITS` | NVARCHAR(2000) | Expected business outcome |
| `TIMELINE` | NVARCHAR(100) | Quick Win / Short Term / Mid Term / Long Term |
| `IMPACT` | NVARCHAR(20) | Low / Medium / High |
| `USE_COUNT` | INTEGER | Times retrieved — auto-incremented |
| `EMBEDDING` | REAL_VECTOR(3072) | Gemini embedding of pain_point text |

### `SVA2.KNOWLEDGE_BASE`

Internal knowledge — two source types currently ingested.

| Column | Type | Description |
|---|---|---|
| `ID` | VARCHAR(36) | UUID primary key |
| `SOURCE_TYPE` | NVARCHAR(50) | `next_gen` \| `vlm_kpis` \| `ai_scenarios` \| `premium_services` |
| `SOLUTION` | NVARCHAR(100) | Canonical solution name |
| `TITLE` | NVARCHAR(500) | Feature name (next_gen) or KPI name (vlm_kpis) |
| `CONTENT` | NVARCHAR(4000) | Description (next_gen) or KPI definition (vlm_kpis) |
| `RELEASE` | NVARCHAR(100) | next_gen: release label (2602, H1 2026, Roadmap…) |
| `AGENT_BASED` | NVARCHAR(3) | next_gen: Yes \| No |
| `JOULE_BASED` | NVARCHAR(3) | next_gen: Yes \| No |
| `VALUE_DRIVER` | NVARCHAR(200) | vlm_kpis: e.g. Cost Reduction, Process Efficiency |
| `VALUE_LEVER` | NVARCHAR(500) | vlm_kpis: specific lever activated |
| `KPI_ID` | NVARCHAR(50) | vlm_kpis: SAP APM KPI Catalog ID |
| `KPI_CATEGORY` | NVARCHAR(100) | vlm_kpis: Throughput \| Backlog \| Process Progress \| Changes \| Master Data |
| `KPI_TARGET` | NVARCHAR(1000) | vlm_kpis: KPI Catalog link |
| `CAPABILITY` | NVARCHAR(2000) | vlm_kpis: Ariba capability enabling this KPI |
| `KPI_FORMULA` | NVARCHAR(2000) | vlm_kpis: calculation formula |
| `KPI_MEAS_FREQ` | NVARCHAR(100) | vlm_kpis: Daily \| Weekly \| Monthly |
| `SOURCE_FILE` | NVARCHAR(200) | Source filename |
| `EMBEDDING` | REAL_VECTOR(3072) | Gemini embedding |

**Embedding strategy per source type:**
- `next_gen`: `"{title} — {content}"`
- `vlm_kpis`: `"{pain_point} | {value_lever} | {kpi_name} | {capability}"`

**Current knowledge base contents:**

| source_type | File | Rows |
|---|---|---|
| `next_gen` | SAP_Ariba_NextGen_Features.xlsx | 83 |
| `vlm_kpis` | SAP_Ariba_VLM_Index_Complete.xlsx | 86 (Buying 29, Sourcing 24, Contracts 17, SLP 16) |

---

## MCP Tools

| Tool | Server | Description |
|---|---|---|
| `query_single_pain_point` | both | Single pain point query — retrieves similar cases |
| `retrieve_knowledge_context` | both | Parallel knowledge base search per source_type + documentation |
| `list_ingested_solutions` | both | Show row counts per solution in PAIN_POINTS |
| `rate_recommendation` | both | Quality feedback loop — updates QUALITY_SCORE |
| `read_excel_painpoints` | local only | Parse input Excel — auto-detects sheet, header row, language |
| `retrieve_similar_cases_batch` | local only | Parallel HANA vector search for all rows (circuit breaker) |
| `write_excel_output` | local only | Generate output xlsx with corporate format |

---

## Output Excel Format (Quick Reference Card spec)

| Element | Spec |
|---|---|
| Header fill | `#00144A` |
| Header font | Arial Bold 11pt White, centered, height 30px |
| Even rows | `#D1EFFF` fill, Arial 10pt `#444444` |
| Odd rows | `#FFFFFF` fill, Arial 10pt `#444444` |
| Outer border | `#002A86` medium |
| Inner border | `#EAECEE` thin |
| Alignment | top-left, wrap text |

Output columns: `Pain Point · Solution · [Solution Area] · Recommendations · Category · Effort · Timeline · Benefits · Documentation · Impact · Ariba Next-Gen · Value KPIs`

Solution Area column is injected only when at least one input row contains it.

---

## Ingestion

### Pain points

```bash
python ingestion/ingest.py data/legacy/<file>.xlsx
```

Input Excel must have columns: `Observation / Pain Point`, `Solution`, optionally `Solution Area`, `Category`, `Effort`, `Benefits`, `Timeline`, `Impact`.

### Knowledge base

```bash
# Next-gen roadmap features
python ingestion/ingest_knowledge.py data/legacy/SAP_Ariba_NextGen_Features.xlsx --source-type next_gen

# VLM KPI index
python ingestion/ingest_knowledge.py data/legacy/SAP_Ariba_VLM_Index_Complete.xlsx --source-type vlm_kpis
```

Supported `--source-type` values: `next_gen`, `vlm_kpis`, `ai_scenarios`, `premium_services`

To re-ingest VLM after a new file arrives:
```sql
DELETE FROM SVA2.KNOWLEDGE_BASE WHERE SOURCE_TYPE = 'vlm_kpis';
```
Then run the ingestion command above.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| Similarity threshold **0.55** | Filters noise; only meaningful matches reach Joule |
| Quality weighting | Exponential moving average (decay=0.8) from consultant feedback |
| Circuit breaker (batch) | Aborts remaining if >50% tasks fail — prevents cascading timeouts |
| Solution-filtered retrieval | next_gen: tries filtered first, fallback global; vlm_kpis: always filtered |
| Server-side documentation search | SAP Help Portal API; replaces unreliable LLM web search |
| Blocked URL domains | learning.sap.com, support.ariba.com — known to return invalid links |
| Language enforcement | All output in same language as the pain point (langdetect with deterministic seed) |
| No Impact classification | Always "To be assessed" — consultant-only field |

---

## Project Structure

```
SVA2.0/
├── server/
│   ├── server.py           ← FastMCP server (local: batch + single query)
│   ├── server_cf.py        ← FastMCP server (CF: single query only)
│   └── recommend.py        ← HANA retrieval, batch search, Excel reader/writer
├── shared/
│   ├── config.py           ← AI Core SDK init, HANA pool, taxonomy, embed_text()
│   └── instructions.py     ← Unified single-query mode instructions
├── ingestion/
│   ├── ingest.py           ← Ingest pain points into SVA2.PAIN_POINTS
│   ├── ingest_knowledge.py ← Ingest knowledge files into SVA2.KNOWLEDGE_BASE
│   └── ingest_qa_pdf.py    ← Extract Q&A pairs from PDF → KNOWLEDGE_BASE
├── schema/
│   ├── hana_schema.sql     ← SVA2.PAIN_POINTS DDL
│   └── knowledge_base.sql  ← SVA2.KNOWLEDGE_BASE DDL
├── tests/
│   ├── test_circuit_breaker.py  ← Circuit breaker resilience tests
│   └── test_output_format.py    ← Excel output formatting validation
├── tools/
│   └── telemetry.py        ← Log parser for similarity score analysis
├── data/
│   └── legacy/             ← Source Excel/PDF files for ingestion
├── manifest.yml            ← Cloud Foundry deployment config
├── requirements.txt
└── .env                    ← Credentials (not committed)
```

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

Create `.env` with:

```
# SAP AI Core (consumed by gen-ai-hub-sdk automatically)
AICORE_AUTH_URL=https://<subaccount>.authentication.<region>.hana.ondemand.com/oauth/token
AICORE_CLIENT_ID=...
AICORE_CLIENT_SECRET=...
AICORE_BASE_URL=https://api.ai.<region>.ml.hana.ondemand.com
AICORE_RESOURCE_GROUP=default

# Gemini Embedding deployment (from AI Launchpad → ML Operations → Deployments)
EMBEDDING_DEPLOYMENT_ID=...
EMBEDDING_MODEL_NAME=gemini-embedding

# SAP HANA Cloud
HANA_HOST=<uuid>.hana.<region>.hanacloud.ondemand.com
HANA_PORT=443
HANA_USER=DBADMIN
HANA_PASSWORD=...
HANA_SCHEMA=SVA2
```

### 3. Create HANA schema

Run in HANA Cloud SQL console — in order:
```sql
CREATE SCHEMA SVA2;
```
Then execute `schema/hana_schema.sql` and `schema/knowledge_base.sql`.

### 4. Ingest data

```bash
python ingestion/ingest.py data/legacy/<pain_points_file>.xlsx
python ingestion/ingest_knowledge.py data/legacy/SAP_Ariba_NextGen_Features.xlsx --source-type next_gen
python ingestion/ingest_knowledge.py data/legacy/SAP_Ariba_VLM_Index_Complete.xlsx --source-type vlm_kpis
```

### 5. Run MCP server locally

```bash
# HTTP mode — for Joule Desktop (streamable-HTTP, port 8000 by default)
python server/server.py --http

# Custom port
python server/server.py --http --port 9000

# stdio mode — for Claude Desktop
python server/server.py
```

The HTTP server binds to `127.0.0.1:8000` and exposes the MCP endpoint at:
```
http://127.0.0.1:8000/mcp
```

Logs are written to both stderr and `mcp_server.log` in the project root.

### 6. Deploy to Cloud Foundry

```bash
cf push
```

Production endpoint (single-query mode only):
```
https://<your-app>.cfapps.<region>.hana.ondemand.com/mcp
```

### 7. Connect Joule Desktop

Add the MCP server in Joule Desktop settings pointing to:
- **Local:** `http://127.0.0.1:8000/mcp`
- **CF:** `https://<your-app>.cfapps.<region>.hana.ondemand.com/mcp`

---

## Testing

```bash
# Run all tests
python -m pytest tests/ -v

# Individual suites
python -m pytest tests/test_circuit_breaker.py -v   # Circuit breaker resilience
python -m pytest tests/test_output_format.py -v     # Excel output formatting
```

---

## Telemetry

Parse `mcp_server.log` for similarity scores, batch metrics, and circuit breaker events:

```bash
python tools/telemetry.py                  # Default log file
python tools/telemetry.py --json           # JSON output
python tools/telemetry.py path/to/log.log  # Custom path
```

---

## BTP Services Required

| Service | Plan | Purpose |
|---|---|---|
| SAP AI Core | standard | Gemini Embedding deployment |
| SAP HANA Cloud | hana | Vector store (PAIN_POINTS + KNOWLEDGE_BASE) |
| SAP HANA Cloud | tools | HANA Cloud Central UI + SQL console |
| Joule Work Desktop | — | LLM reasoning + user-facing chat interface |

---

## Cloud Foundry Deployment

```yaml
# manifest.yml
applications:
  - name: ava
    command: python server/server_cf.py --http --port $PORT
    buildpacks: [python_buildpack]
    memory: 512M
    disk_quota: 1G
    instances: 1
    routes:
      - route: <your-app>.cfapps.<region>.hana.ondemand.com
```

Secrets are set via `cf set-env ava <KEY> <VALUE>` (never committed).
