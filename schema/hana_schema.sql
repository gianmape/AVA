-- Run once in your HANA Cloud instance
-- Schema
CREATE SCHEMA SVA2;

-- Main vector table
CREATE COLUMN TABLE SVA2.PAIN_POINTS (
    ID          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    SOURCE_FILE NVARCHAR(255),

    -- Embeddable fields (text used for vector generation)
    PAIN_POINT  NCLOB,
    COMMENTS    NCLOB,

    -- Metadata filters (used in WHERE clause before vector search)
    SOLUTION    NVARCHAR(100),
    AREA        NVARCHAR(100),
    CATEGORY    NVARCHAR(100),

    -- Payload fields (returned at retrieval time, not searched)
    RECOMMENDATIONS NCLOB,
    EFFORT          NVARCHAR(20),
    BENEFITS        NCLOB,
    DOCUMENTATION   NCLOB,
    TIMELINE        NVARCHAR(50),
    IMPACT          NVARCHAR(20),
    KEYS_TO_SUCCESS NVARCHAR(100),

    -- Timestamp
    CREATED_AT  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Vector column — 1536 dims for text-embedding-ada-002
    -- Adjust dimension if you use a different embedding model
    EMBEDDING   REAL_VECTOR(1536)
);

-- Index for fast metadata filtering before vector search
CREATE INDEX IDX_SOLUTION ON SVA2.PAIN_POINTS (SOLUTION);
CREATE INDEX IDX_AREA     ON SVA2.PAIN_POINTS (AREA);
CREATE INDEX IDX_CATEGORY ON SVA2.PAIN_POINTS (CATEGORY);
