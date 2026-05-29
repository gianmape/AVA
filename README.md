# AVA — Autonomous Value Advisory

Turn years of SAP Ariba consultant knowledge into instant, evidence-backed recommendations.

AVA (Autonomous Value Advisory) is an MCP server that connects Joule Desktop to two SAP HANA Cloud vector indexes: a historical pain point library and an internal knowledge base (Next-gen roadmap features + VLM KPIs). Given a client's pain points Excel, Joule retrieves semantically similar cases, enriches them with roadmap and KPI context, synthesizes actionable recommendations, and writes a formatted output Excel — all orchestrated through MCP tool calls.

---

## Architecture

```
Joule Desktop
     │  MCP (streamable-http)
     ▼
server/server.py          ← FastMCP server — tool definitions + Joule instructions
     │
     ├── server/recommend.py
     │       ├── read_excel_painpoints()      — parse input Excel, detect sheet/header
     │       ├── retrieve_similar_cases()     — single cosine search, SVA2.PAIN_POINTS
     │       ├── retrieve_similar_cases_batch() — parallel cosine search, all rows
     │       ├── retrieve_knowledge_context() — parallel cosine search, SVA2.KNOWLEDGE_BASE
     │       └── write_excel_output()         — generate output xlsx (corporate format)
     │
     └── shared/config.py
             ├── embed_text()       — Gemini Embedding via SAP AI Core gen-ai-hub-sdk
             ├── hana_connection()  — hdbcli connection from .env
             └── normalise_*()      — solution / solution_area taxonomy helpers
```

### Pipeline (Excel batch mode)

```
1. read_excel_painpoints(input_path)
        └─ detect sheet by name hint or column scan
        └─ normalise columns, detect language per row
        └─ return [{idx, pain_point, solution, solution_area, language}]

2. retrieve_similar_cases_batch(items)
        └─ ThreadPoolExecutor — one HANA query per row in parallel
        └─ cosine_similarity(embedding, TO_REAL_VECTOR(?)) on SVA2.PAIN_POINTS
        └─ filtered by SOLUTION (and optionally SOLUTION_AREA)
        └─ increments USE_COUNT on every hit

3. retrieve_knowledge_context(pain_point, solution, ["next_gen", "vlm_kpis"])
        └─ ThreadPoolExecutor — one query per source_type in parallel
        └─ cosine_similarity on SVA2.KNOWLEDGE_BASE, no solution filter
        └─ top_k=5 per source_type

4. Joule synthesizes per row:
        Recommendations · Category · Effort · Timeline · Benefits
        Documentation (quality-filtered SAP links)
        Ariba Next-Gen (roadmap features)
        Value KPIs (VLM KPI blocks with formula + frequency)

5. write_excel_output(input_path, rows)
        └─ backfills pain_point/solution from source Excel if Joule omits them
        └─ expands short labels ("High" → "High (1 – 2 Months)")
        └─ strips blocked/generic documentation URLs
        └─ generates fresh .xlsx with SAP corporate format (Quick Reference Card spec)
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

| Tool | Called by | Description |
|---|---|---|
| `read_excel_painpoints` | Joule (step 1) | Parse input Excel — auto-detects sheet, header row, language |
| `retrieve_similar_cases_batch` | Joule (step 2) | Parallel HANA vector search for all rows |
| `retrieve_knowledge_context` | Joule (step 3) | Parallel knowledge base search per source_type |
| `write_excel_output` | Joule (step 5) | Generate output xlsx with corporate format |
| `query_single_pain_point` | Joule (single mode) | Single pain point query — retrieves similar cases |
| `list_ingested_solutions` | Joule / debug | Show row counts per solution in PAIN_POINTS |

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

## Project Structure

```
SVA2.0/
├── server/
│   ├── server.py           ← FastMCP server, Joule instructions, tool definitions
│   └── recommend.py        ← HANA retrieval, batch search, Excel reader/writer
├── shared/
│   └── config.py           ← AI Core SDK init, HANA connection, taxonomy, embed_text()
├── ingestion/
│   ├── ingest.py           ← ingest pain points into SVA2.PAIN_POINTS
│   └── ingest_knowledge.py ← ingest knowledge files into SVA2.KNOWLEDGE_BASE
├── schema/
│   ├── hana_schema.sql     ← SVA2.PAIN_POINTS DDL
│   └── knowledge_base.sql  ← SVA2.KNOWLEDGE_BASE DDL + ALTER history
├── data/
│   └── legacy/             ← source Excel files for ingestion
├── requirements.txt
└── .env                    ← credentials (not committed)
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

### 5. Run MCP server

```bash
# HTTP mode for Joule Desktop
python server/server.py --http --port 8000

# stdio mode for Claude Desktop
python server/server.py
```

### 6. Connect Joule Desktop

Add MCP server in Joule Desktop settings pointing to `http://127.0.0.1:8000`.

---

## BTP Services Required

| Service | Plan | Purpose |
|---|---|---|
| SAP AI Core | standard | Gemini Embedding deployment |
| SAP HANA Cloud | hana | Vector store (PAIN_POINTS + KNOWLEDGE_BASE) |
| SAP HANA Cloud | tools | HANA Cloud Central UI + SQL console |
| Joule Work Desktop | — | LLM reasoning + SAP documentation access |
