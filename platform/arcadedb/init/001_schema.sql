-- ============================================================
-- Database Initialization
-- ============================================================

CREATE DATABASE platform IF NOT EXISTS;


-- ============================================================
-- Base Item (Vertex)
-- ============================================================

CREATE VERTEX TYPE Item;

CREATE PROPERTY Item.item_id STRING;
CREATE PROPERTY Item.type STRING;
CREATE PROPERTY Item.name STRING;
CREATE PROPERTY Item.description STRING;

CREATE PROPERTY Item.source_type STRING;
CREATE PROPERTY Item.source_id STRING;
CREATE PROPERTY Item.source_url STRING;

CREATE PROPERTY Item.version STRING;
CREATE PROPERTY Item.status STRING;

CREATE PROPERTY Item.reliability_score DOUBLE;
CREATE PROPERTY Item.reliability_confidence DOUBLE;
CREATE PROPERTY Item.scoring_version STRING;
CREATE PROPERTY Item.last_evaluated DATETIME;

CREATE PROPERTY Item.first_seen DATETIME;
CREATE PROPERTY Item.last_seen DATETIME;
CREATE PROPERTY Item.last_synced DATETIME;

-- Freeform metadata used by the unified Item model.
-- MAP (not EMBEDDED) because these are stored as plain JSON-like
-- dicts with no ArcadeDB "@type" entry — see repositories.py.
CREATE PROPERTY Item.tool MAP;
CREATE PROPERTY Item.agent MAP;
CREATE PROPERTY Item.artifacts MAP;

-- Populated later by the embedding pipeline.
CREATE PROPERTY Item.embedding ARRAY_OF_FLOATS;


-- ============================================================
-- Tool (Vertex)
-- ============================================================

CREATE VERTEX TYPE Tool;

CREATE PROPERTY Tool.server_id STRING;
CREATE PROPERTY Tool.tool_name STRING;
CREATE PROPERTY Tool.mcp_schema MAP;


-- ============================================================
-- Agent (Vertex)
-- ============================================================

CREATE VERTEX TYPE Agent;

CREATE PROPERTY Agent.endpoint STRING;
CREATE PROPERTY Agent.agent_card MAP;
CREATE PROPERTY Agent.skills LIST;
CREATE PROPERTY Agent.capabilities LIST;
CREATE PROPERTY Agent.declared_dependencies LIST;


-- ============================================================
-- Skill (Vertex)
-- ============================================================

CREATE VERTEX TYPE Skill;

CREATE PROPERTY Skill.name STRING;
CREATE PROPERTY Skill.description STRING;


-- ============================================================
-- Discovery Source (Vertex)
-- ============================================================

CREATE VERTEX TYPE DiscoverySource;

CREATE PROPERTY DiscoverySource.source_type STRING;
CREATE PROPERTY DiscoverySource.source_id STRING;
CREATE PROPERTY DiscoverySource.url STRING;


-- ============================================================
-- Artifact (Document)
-- ============================================================

CREATE DOCUMENT TYPE Artifact;

CREATE PROPERTY Artifact.artifact_type STRING;
CREATE PROPERTY Artifact.path STRING;
CREATE PROPERTY Artifact.content STRING;


-- ============================================================
-- Test Run (Vertex)
-- ============================================================

CREATE VERTEX TYPE TestRun;

CREATE PROPERTY TestRun.run_id STRING;
CREATE PROPERTY TestRun.item_id STRING;
CREATE PROPERTY TestRun.type STRING;

CREATE PROPERTY TestRun.started_at DATETIME;
CREATE PROPERTY TestRun.completed_at DATETIME;

CREATE PROPERTY TestRun.status STRING;

CREATE PROPERTY TestRun.input MAP;
CREATE PROPERTY TestRun.output MAP;

CREATE PROPERTY TestRun.duration_ms LONG;

CREATE PROPERTY TestRun.errors LIST;
CREATE PROPERTY TestRun.logs LIST;
CREATE PROPERTY TestRun.dependencies MAP;


-- ============================================================
-- Reliability Evaluation (Vertex)
-- ============================================================

CREATE VERTEX TYPE ReliabilityEvaluation;

CREATE PROPERTY ReliabilityEvaluation.item_id STRING;
CREATE PROPERTY ReliabilityEvaluation.score DOUBLE;
CREATE PROPERTY ReliabilityEvaluation.confidence DOUBLE;
CREATE PROPERTY ReliabilityEvaluation.scoring_version STRING;
CREATE PROPERTY ReliabilityEvaluation.evaluated_at DATETIME;
CREATE PROPERTY ReliabilityEvaluation.signals MAP;


-- ============================================================
-- Graph Edges
-- ============================================================

CREATE EDGE TYPE USES_TOOL;
CREATE EDGE TYPE HAS_SKILL;
CREATE EDGE TYPE DISCOVERED_FROM;
CREATE EDGE TYPE HAS_TEST_RUN;


-- ============================================================
-- Indexes
-- ============================================================

CREATE INDEX ON Item (item_id) UNIQUE;

CREATE INDEX ON Item (name) FULL_TEXT;
CREATE INDEX ON Item (description) FULL_TEXT;

CREATE INDEX ON Skill (name) FULL_TEXT;

CREATE INDEX ON DiscoverySource (source_id) NOTUNIQUE;

CREATE INDEX ON TestRun (run_id) UNIQUE;