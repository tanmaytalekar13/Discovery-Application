CREATE DATABASE platform IF NOT EXISTS;


-- ============================================================
-- Base Item
-- ============================================================

CREATE DOCUMENT TYPE Item;

CREATE PROPERTY Item.item_id UUID;
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

-- Embedded metadata for the unified Item model.
CREATE PROPERTY Item.tool EMBEDDED;
CREATE PROPERTY Item.agent EMBEDDED;
CREATE PROPERTY Item.artifacts EMBEDDED;

-- Populated by the local embedding pipeline in Phase 5.
CREATE PROPERTY Item.embedding ARRAY_OF_FLOATS;


-- ============================================================
-- Tool
-- ============================================================

CREATE DOCUMENT TYPE Tool;

CREATE PROPERTY Tool.server_id STRING;
CREATE PROPERTY Tool.tool_name STRING;
CREATE PROPERTY Tool.mcp_schema EMBEDDED;


-- ============================================================
-- Agent
-- ============================================================

CREATE DOCUMENT TYPE Agent;

CREATE PROPERTY Agent.endpoint STRING;
CREATE PROPERTY Agent.agent_card EMBEDDED;
CREATE PROPERTY Agent.skills EMBEDDEDLIST;
CREATE PROPERTY Agent.capabilities EMBEDDEDLIST;
CREATE PROPERTY Agent.declared_dependencies EMBEDDEDLIST;


-- ============================================================
-- Skill
-- ============================================================

CREATE DOCUMENT TYPE Skill;

CREATE PROPERTY Skill.name STRING;
CREATE PROPERTY Skill.description STRING;


-- ============================================================
-- Discovery Source
-- ============================================================

CREATE DOCUMENT TYPE DiscoverySource;

CREATE PROPERTY DiscoverySource.source_type STRING;
CREATE PROPERTY DiscoverySource.source_id STRING;
CREATE PROPERTY DiscoverySource.source_key STRING;
CREATE PROPERTY DiscoverySource.url STRING;


-- ============================================================
-- Artifact
-- ============================================================

CREATE DOCUMENT TYPE Artifact;

CREATE PROPERTY Artifact.artifact_type STRING;
CREATE PROPERTY Artifact.path STRING;
CREATE PROPERTY Artifact.content STRING;


-- ============================================================
-- Test Run
-- ============================================================

CREATE DOCUMENT TYPE TestRun;

CREATE PROPERTY TestRun.run_id UUID;
CREATE PROPERTY TestRun.item_id UUID;
CREATE PROPERTY TestRun.type STRING;

CREATE PROPERTY TestRun.started_at DATETIME;
CREATE PROPERTY TestRun.completed_at DATETIME;

CREATE PROPERTY TestRun.status STRING;

CREATE PROPERTY TestRun.input EMBEDDED;
CREATE PROPERTY TestRun.output EMBEDDED;

CREATE PROPERTY TestRun.duration_ms LONG;

CREATE PROPERTY TestRun.errors EMBEDDEDLIST;
CREATE PROPERTY TestRun.logs EMBEDDEDLIST;
CREATE PROPERTY TestRun.dependencies EMBEDDED;


-- ============================================================
-- Reliability Evaluation
-- ============================================================

CREATE DOCUMENT TYPE ReliabilityEvaluation;

CREATE PROPERTY ReliabilityEvaluation.item_id UUID;
CREATE PROPERTY ReliabilityEvaluation.score DOUBLE;
CREATE PROPERTY ReliabilityEvaluation.confidence DOUBLE;
CREATE PROPERTY ReliabilityEvaluation.scoring_version STRING;
CREATE PROPERTY ReliabilityEvaluation.evaluated_at DATETIME;
CREATE PROPERTY ReliabilityEvaluation.signals EMBEDDED;


-- ============================================================
-- Graph Edge Types
-- ============================================================

CREATE EDGE TYPE USES_TOOL;

CREATE EDGE TYPE HAS_SKILL;

CREATE EDGE TYPE DISCOVERED_FROM;

CREATE EDGE TYPE HAS_TEST_RUN;


-- ============================================================
-- Indexes
-- ============================================================

CREATE INDEX Item.item_id UNIQUE;

CREATE INDEX Item.name FULL_TEXT;
CREATE INDEX Item.description FULL_TEXT;

CREATE INDEX Skill.name FULL_TEXT;

CREATE INDEX DiscoverySource.source_key NOTUNIQUE;

CREATE INDEX TestRun.run_id UNIQUE;