-- SVA2.KNOWLEDGE_BASE
-- Run once in HANA Cloud SQL console.
-- Stores internal knowledge chunks for semantic retrieval:
--   next_gen         — SAP Ariba Next-gen roadmap features
--   ai_scenarios     — AI agent / automation scenarios (future)
--   premium_services — SAP premium services catalogue (future)
--   vlm_kpis         — Value Lever & KPI index (pain point → value driver → KPI)

CREATE COLUMN TABLE SVA2.KNOWLEDGE_BASE (
    ID           VARCHAR(36)    DEFAULT SYSUUID PRIMARY KEY,
    SOURCE_TYPE  NVARCHAR(50)   NOT NULL,   -- next_gen | ai_scenarios | premium_services | vlm_kpis
    SOLUTION     NVARCHAR(100),             -- canonical solution name (or raw if unknown)
    TITLE        NVARCHAR(500)  NOT NULL,   -- feature name (next_gen) | KPI name (vlm_kpis)
    CONTENT      NVARCHAR(4000) NOT NULL,   -- description (next_gen) | formula/measure (vlm_kpis)
    RELEASE      NVARCHAR(100),             -- next_gen: release label (2602, H1 2026, Roadmap…)
    AGENT_BASED  NVARCHAR(3),               -- next_gen: Yes | No
    JOULE_BASED  NVARCHAR(3),               -- next_gen: Yes | No
    -- VLM KPI fields (populated only for source_type = 'vlm_kpis')
    VALUE_DRIVER  NVARCHAR(200),             -- e.g. Cost Reduction, Process Efficiency
    VALUE_LEVER   NVARCHAR(500),             -- specific lever activated
    KPI_ID        NVARCHAR(50),              -- SAP APM KPI Catalog ID (e.g. KBSRM00701)
    KPI_CATEGORY  NVARCHAR(100),             -- Throughput | Backlog | Process Progress | Changes | Master Data
    KPI_TARGET    NVARCHAR(1000),            -- KPI Catalog link / typical benchmark
    CAPABILITY    NVARCHAR(2000),            -- Ariba capability / recommendation enabling this KPI
    KPI_FORMULA   NVARCHAR(2000),            -- formula or calculation for the KPI
    KPI_MEAS_FREQ NVARCHAR(100),             -- measurement frequency (Daily, Weekly, Monthly…)
    SOURCE_FILE  NVARCHAR(200),
    CREATED_AT   TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
    EMBEDDING    REAL_VECTOR(3072)
);

CREATE INDEX IDX_KB_SOURCE_TYPE ON SVA2.KNOWLEDGE_BASE (SOURCE_TYPE);
CREATE INDEX IDX_KB_SOLUTION    ON SVA2.KNOWLEDGE_BASE (SOLUTION);

-- ALTER statements to add VLM columns to an existing table (run if table already exists):
-- ALTER TABLE SVA2.KNOWLEDGE_BASE ADD (VALUE_DRIVER NVARCHAR(200));
-- ALTER TABLE SVA2.KNOWLEDGE_BASE ADD (VALUE_LEVER  NVARCHAR(500));
-- ALTER TABLE SVA2.KNOWLEDGE_BASE ADD (KPI_ID       NVARCHAR(50));
-- ALTER TABLE SVA2.KNOWLEDGE_BASE ADD (KPI_CATEGORY NVARCHAR(100));
-- ALTER TABLE SVA2.KNOWLEDGE_BASE ADD (KPI_TARGET   NVARCHAR(1000));
-- ALTER TABLE SVA2.KNOWLEDGE_BASE ADD (KPI_FORMULA  NVARCHAR(2000));
-- ALTER TABLE SVA2.KNOWLEDGE_BASE ADD (CAPABILITY   NVARCHAR(2000));
-- ALTER TABLE SVA2.KNOWLEDGE_BASE ADD (KPI_MEAS_FREQ NVARCHAR(100));