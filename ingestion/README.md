# Pain Points — Ingestion

Indexes legacy SAP Ariba pain point Excel files into the HANA Cloud Vector Engine.
Once ingested, the data is used by the MCP server to retrieve similar historical cases
for new pain points submitted via Joule Desktop.

---

## Setup

```bash
# From the project root (painpoints/)
pip install -r ingestion/requirements.txt

# Configure credentials
cp .env.template .env
# Fill in AICORE_* and HANA_* values in .env
```

### Credentials needed

| Variable | Where to find it |
|---|---|
| `AICORE_AUTH_URL` | BTP subaccount → Service Keys → AI Core service key |
| `AICORE_CLIENT_ID` | Same service key |
| `AICORE_CLIENT_SECRET` | Same service key |
| `AICORE_BASE_URL` | Same service key |
| `AICORE_RESOURCE_GROUP` | `default` unless changed |
| `EMBEDDING_DEPLOYMENT_ID` | AI Launchpad → Deployments → text-embedding-ada-002 |
| `EMBEDDING_MODEL_NAME` | `text-embedding-ada-002` |
| `HANA_HOST` | BTP HANA Cloud Central → Connection details (no port) |
| `HANA_PASSWORD` | Password set when provisioning HANA Cloud |

---

## HANA Schema (run once)

Open the HANA Cloud SQL console and run `schema/hana_schema.sql`.

> Note: HANA does not support `IF NOT EXISTS` — if the schema already exists, skip the `CREATE SCHEMA` line.

---

## Running ingestion

```bash
# Single file
python ingestion/ingest.py data/legacy/my_file.xlsx

# All files in a folder
python ingestion/ingest.py data/legacy/

# Multiple files
python ingestion/ingest.py file1.xlsx file2.xlsx
```

The script will print progress every 10 rows and a summary at the end:
```
── Ingesting: data/legacy/client_painpoints.xlsx
  10 rows committed…
  20 rows committed…
  Done — inserted: 23, skipped: 4
```

---

## Excel file format

Legacy files are expected to have:

| Column | Required | Notes |
|---|---|---|
| Observation / Pain Point | Yes | The pain point text |
| Solution | Yes | Must match a valid SAP Ariba solution (see below) |
| Comments | No | Additional context, used in embedding |
| Area | No | Industry/segment |
| Category | No | Feature Adoption, Innovation, Q&A, Process Change, Roadmap Discussion |
| Recommendations | No | Historical recommendation (stored for context) |
| Effort | No | Low / Medium / High / N/A |
| Benefits | No | Historical benefits |
| Documentation | No | Links to SAP docs |
| Timeline | No | Quick Win / Short Term / Mid Term / Long Term / Ongoing |
| Impact | No | Low / Medium / High |
| 12 Keys To Success | No | Stored as-is |

**Title row handling:** If the first row contains column labels (e.g. the file has a title row above the headers), the script detects this automatically and skips it.

---

## Valid Solutions

```
Ariba Buying, Ariba Buying and Inv, Catalog, Commerce Auto, Contracts,
Discount Mgmnt, Discovery, Guided Buying, Integration, Invoice Mgmnt,
Overall, Reporting, Risk, Sourcing, Spend Analysis, SIPM, SLP,
Supply Chain Collaboration
```

Values are matched case-insensitively. Unrecognized values are stored as-is.

---

## Checking what's been ingested

Use Joule Desktop with the MCP server connected and ask:
```
list_ingested_solutions
```

Or query HANA directly:
```sql
SELECT SOLUTION, COUNT(*) FROM SVA2.PAIN_POINTS GROUP BY SOLUTION ORDER BY 2 DESC;
```
