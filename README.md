# SVA2.0

> Turn years of SAP Ariba consultant knowledge into instant recommendations.

Pain Points RAG indexes legacy SAP Ariba client pain point trackers (Excel) into a
vector database and surfaces semantically similar historical cases to Joule — SAP's AI
assistant — which synthesizes actionable recommendations, effort estimates, timelines,
and documentation links for every new pain point, written back into Excel with all
formatting preserved.

## How it works

1. **Ingest** — Legacy Excel files are parsed, embedded (`text-embedding-ada-002` via SAP AI Core),
   and stored in SAP HANA Cloud Vector Engine with full metadata.
2. **Retrieve** — For each new pain point, the MCP server performs a cosine similarity
   search against the vector store (filtered by SAP Ariba solution module).
3. **Synthesize** — Joule Desktop receives the raw historical cases and generates
   recommendations, categories, effort/impact/timeline classifications, and curated
   SAP documentation links — in the same language as the input.
4. **Output** — Results are written back into a copy of the original Excel file via
   `openpyxl`, preserving all formatting, styles, and merged cells.

## Stack

| Layer | Technology |
|---|---|
| LLM + SAP docs | Joule Work Desktop |
| Embeddings | Gemini Embedding · SAP AI Core |
| Vector store | SAP HANA Cloud Vector Engine |
| Orchestration | MCP server (FastMCP · HTTP · SAP BTP / Cloud Foundry) |
| Excel I/O | openpyxl |
| BTP SDK | gen-ai-hub-sdk |

## Project structure

```
painpoints/
├── mcp_server.py           ← MCP server, stdio transport (Joule Desktop)
├── config.py               ← shared SDK init, HANA connection, taxonomy, helpers
├── requirements.txt
├── schema/
│   └── hana_schema.sql     ← run once in HANA Cloud SQL console
├── ingestion/
│   └── ingest.py           ← indexes legacy Excel files into HANA
├── server/
│   ├── server.py           ← HTTP server mode
│   └── recommend.py        ← HANA retrieval + batch vector search
└── data/
    ├── legacy/             ← legacy Excel files for ingestion
    └── new/                ← new input files for recommendation
```

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.template .env
```

Fill in the following variables:

| Variable | Description |
|---|---|
| `AICORE_AUTH_URL` | OAuth token endpoint for your BTP subaccount |
| `AICORE_CLIENT_ID` | Service key client ID |
| `AICORE_CLIENT_SECRET` | Service key client secret |
| `AICORE_BASE_URL` | AI Core API base URL |
| `AICORE_RESOURCE_GROUP` | Resource group (default: `default`) |
| `EMBEDDING_DEPLOYMENT_ID` | text-embedding-ada-002 deployment ID from AI Launchpad |
| `EMBEDDING_MODEL_NAME` | `text-embedding-ada-002` |
| `HANA_HOST` | HANA Cloud host (no port) |
| `HANA_PORT` | `443` |
| `HANA_USER` | `DBADMIN` |
| `HANA_PASSWORD` | Password set during HANA provisioning |
| `HANA_SCHEMA` | `SVA2` |

### 3. Create HANA schema

Run the contents of `schema/hana_schema.sql` once in the HANA Cloud SQL console.

> Note: HANA does not support `IF NOT EXISTS` — run `CREATE SCHEMA SVA2;` separately first.

### 4. Ingest legacy files

```bash
python ingestion/ingest.py data/legacy/
```

### 5. Connect Joule Desktop

Add the MCP server to your Joule Desktop config 

## MCP tools

| Tool | Description |
|---|---|
| `read_excel_painpoints` | Parse all pain point rows from an Excel file |
| `retrieve_similar_cases` | Single-row HANA cosine vector search |
| `retrieve_similar_cases_batch` | Batch parallel HANA vector search (primary) |
| `write_excel_output` | Write Joule results back into a copy of the Excel file |
| `list_ingested_solutions` | Show HANA row counts by solution module |

## BTP services required

| Service | Plan | Purpose |
|---|---|---|
| SAP AI Core | standard | Embedding deployment |
| SAP HANA Cloud | hana | Vector store |
| SAP HANA Cloud | tools | HANA Cloud Central UI + SQL console |
| Joule Desktop | — | LLM reasoning + SAP documentation access |
