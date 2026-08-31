-- ============================================================
-- Database Initialization
-- ============================================================

CREATE DATABASE platform IF NOT EXISTS;


-- ============================================================
-- Base Item (Vertex)
-- ============================================================

CREATE VERTEX TYPE Item;

CREATE PROPERTY Item.item_id STRING;
CREATE PROPERTY Item.canonical_id STRING;
CREATE PROPERTY Item.type STRING;
CREATE PROPERTY Item.name STRING;
CREATE PROPERTY Item.description STRING;

CREATE PROPERTY Item.source_type STRING;
CREATE PROPERTY Item.source_id STRING;
CREATE PROPERTY Item.source_url STRING;
CREATE PROPERTY Item.source_provider STRING;
CREATE PROPERTY Item.provenance LIST;
CREATE PROPERTY Item.evidence_summary LIST;

CREATE PROPERTY Item.version STRING;
CREATE PROPERTY Item.status STRING;

CREATE PROPERTY Item.reliability_score DOUBLE;
CREATE PROPERTY Item.reliability_confidence DOUBLE;
CREATE PROPERTY Item.security_validation DOUBLE;
CREATE PROPERTY Item.reliability_signals MAP;
CREATE PROPERTY Item.reliability_reasons LIST;
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
-- Graph Edges
-- ============================================================

CREATE EDGE TYPE USES_TOOL;

CREATE EDGE TYPE HAS_TEST_RUN;


-- ============================================================
-- Indexes
-- ============================================================

CREATE INDEX ON Item (item_id) UNIQUE;

CREATE INDEX ON Item (name) FULL_TEXT;
CREATE INDEX ON Item (description) FULL_TEXT;

CREATE INDEX ON TestRun (run_id) UNIQUE;


-- ============================================================
-- Phase 10 discovery provenance/evidence
-- ============================================================

CREATE VERTEX TYPE DiscoverySource;
CREATE PROPERTY DiscoverySource.source_key STRING;
CREATE PROPERTY DiscoverySource.source_type STRING;
CREATE PROPERTY DiscoverySource.source_id STRING;
CREATE PROPERTY DiscoverySource.source_url STRING;
CREATE PROPERTY DiscoverySource.provider STRING;
CREATE PROPERTY DiscoverySource.first_seen DATETIME;
CREATE PROPERTY DiscoverySource.last_seen DATETIME;
CREATE INDEX ON DiscoverySource (source_key) UNIQUE;

CREATE VERTEX TYPE DiscoveryEvidence;
CREATE PROPERTY DiscoveryEvidence.evidence_id STRING;
CREATE PROPERTY DiscoveryEvidence.item_id STRING;
CREATE PROPERTY DiscoveryEvidence.kind STRING;
CREATE PROPERTY DiscoveryEvidence.statement STRING;
CREATE PROPERTY DiscoveryEvidence.source_type STRING;
CREATE PROPERTY DiscoveryEvidence.source_id STRING;
CREATE PROPERTY DiscoveryEvidence.source_url STRING;
CREATE PROPERTY DiscoveryEvidence.provider STRING;
CREATE PROPERTY DiscoveryEvidence.observed_at DATETIME;
CREATE PROPERTY DiscoveryEvidence.details MAP;
CREATE INDEX ON DiscoveryEvidence (evidence_id) UNIQUE;

CREATE VERTEX TYPE ReliabilityEvaluation;
CREATE PROPERTY ReliabilityEvaluation.evaluation_id STRING;
CREATE PROPERTY ReliabilityEvaluation.item_id STRING;
CREATE PROPERTY ReliabilityEvaluation.score DOUBLE;
CREATE PROPERTY ReliabilityEvaluation.confidence DOUBLE;
CREATE PROPERTY ReliabilityEvaluation.scoring_version STRING;
CREATE PROPERTY ReliabilityEvaluation.approved BOOLEAN;
CREATE PROPERTY ReliabilityEvaluation.signals MAP;
CREATE PROPERTY ReliabilityEvaluation.reasons LIST;
CREATE PROPERTY ReliabilityEvaluation.security_validation DOUBLE;
CREATE PROPERTY ReliabilityEvaluation.evaluated_at DATETIME;
CREATE INDEX ON ReliabilityEvaluation (evaluation_id) UNIQUE;

CREATE VERTEX TYPE DiscoveryRejection;
CREATE PROPERTY DiscoveryRejection.rejection_id STRING;
CREATE PROPERTY DiscoveryRejection.candidate_id STRING;
CREATE PROPERTY DiscoveryRejection.item_id STRING;
CREATE PROPERTY DiscoveryRejection.protocol STRING;
CREATE PROPERTY DiscoveryRejection.source_type STRING;
CREATE PROPERTY DiscoveryRejection.source_id STRING;
CREATE PROPERTY DiscoveryRejection.source_url STRING;
CREATE PROPERTY DiscoveryRejection.provider STRING;
CREATE PROPERTY DiscoveryRejection.reason STRING;
CREATE PROPERTY DiscoveryRejection.evidence LIST;
CREATE PROPERTY DiscoveryRejection.details MAP;
CREATE PROPERTY DiscoveryRejection.observed_at DATETIME;
CREATE INDEX ON DiscoveryRejection (rejection_id) UNIQUE;

CREATE EDGE TYPE HAS_DISCOVERY_SOURCE;
CREATE EDGE TYPE HAS_DISCOVERY_EVIDENCE;
CREATE EDGE TYPE HAS_RELIABILITY_EVALUATION;
