-- SVA2.KNOWLEDGE_BASE
-- Run once in HANA Cloud SQL console.
-- Stores internal knowledge chunks for semantic retrieval:
--   next_gen        — SAP Ariba Next-gen roadmap features
--   ai_scenarios    — AI agent / automation scenarios (future)
--   premium_services — SAP premium services catalogue (future)

CREATE COLUMN TABLE SVA2.KNOWLEDGE_BASE (
    ID           VARCHAR(36)    DEFAULT SYSUUID PRIMARY KEY,
    SOURCE_TYPE  NVARCHAR(50)   NOT NULL,   -- next_gen | ai_scenarios | premium_services
    SOLUTION     NVARCHAR(100),             -- canonical solution name (or raw if unknown)
    TITLE        NVARCHAR(500)  NOT NULL,   -- feature / functionality name
    CONTENT      NVARCHAR(4000) NOT NULL,   -- description text
    RELEASE      NVARCHAR(100),             -- release label: 2602, H1 2026, Roadmap, etc.
    AGENT_BASED  NVARCHAR(3),               -- Yes | No
    JOULE_BASED  NVARCHAR(3),               -- Yes | No
    SOURCE_FILE  NVARCHAR(200),
    CREATED_AT   TIMESTAMP      DEFAULT CURRENT_TIMESTAMP,
    EMBEDDING    REAL_VECTOR(3072)
);

CREATE INDEX IDX_KB_SOURCE_TYPE ON SVA2.KNOWLEDGE_BASE (SOURCE_TYPE);
CREATE INDEX IDX_KB_SOLUTION    ON SVA2.KNOWLEDGE_BASE (SOLUTION);