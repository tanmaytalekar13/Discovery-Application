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
-- Phase 10 provenance/evidence (removed)
--
-- The DiscoverySource / DiscoveryEvidence / ReliabilityEvaluation /
-- DiscoveryRejection vertex types were write-only: nothing in the
-- codebase ever read them back. All of that data is already denormalized
-- onto the Item vertex (provenance / evidence_summary / reliability_*).
-- See app/db/prune_unused_types.py to drop them from an existing
-- database.
-- ============================================================

-- ============================================================
-- MCP Server Verification registry (spec v2, Sections 2-12)
--
-- The Scored Registry is the single source of truth for the
-- read-only search API (Section 9's golden rule: search only ever
-- SELECTs). The verification background workers are the only
-- writers of status/quality_score.
-- ============================================================

CREATE VERTEX TYPE McpServer;
CREATE PROPERTY McpServer.server_id STRING;
CREATE PROPERTY McpServer.name STRING;
CREATE PROPERTY McpServer.source_url STRING;
CREATE PROPERTY McpServer.transport STRING;          -- local | remote
CREATE PROPERTY McpServer.install_cmd STRING;
CREATE PROPERTY McpServer.endpoint_url STRING;
CREATE PROPERTY McpServer.declared_tools LIST;       -- tool schema objects
CREATE PROPERTY McpServer.description STRING;
CREATE PROPERTY McpServer.registry_name STRING;      -- official registry identity
CREATE PROPERTY McpServer.repository_url STRING;
CREATE PROPERTY McpServer.provider_keys LIST;        -- provider names from provider_credentials

CREATE PROPERTY McpServer.auth_required BOOLEAN;
CREATE PROPERTY McpServer.auth_type STRING;          -- none | api_key | oauth2 | unknown
CREATE PROPERTY McpServer.oauth_flow STRING;         -- authorization_code | client_credentials | null
CREATE PROPERTY McpServer.oauth_provider STRING;     -- google | github | notion | slack | self | null

CREATE PROPERTY McpServer.status STRING;             -- spec v2 Section 8 status buckets
CREATE PROPERTY McpServer.quality_score INTEGER;     -- 0-100
CREATE PROPERTY McpServer.confidence STRING;         -- low | medium | high
CREATE PROPERTY McpServer.attempts_completed INTEGER;
CREATE PROPERTY McpServer.attempts_planned INTEGER;

CREATE PROPERTY McpServer.invocation_verified BOOLEAN;
CREATE PROPERTY McpServer.latency_p50_ms INTEGER;
CREATE PROPERTY McpServer.latency_category STRING;   -- fast | moderate | slow

CREATE PROPERTY McpServer.last_verified_at DATETIME;
CREATE PROPERTY McpServer.ttl_expires_at DATETIME;
CREATE PROPERTY McpServer.verification_details MAP;  -- per-stage results, errors, logs
CREATE PROPERTY McpServer.rejection_stage STRING;
CREATE PROPERTY McpServer.rejection_check STRING;
CREATE PROPERTY McpServer.first_seen_at DATETIME;
CREATE PROPERTY McpServer.last_attempt_at DATETIME;

CREATE INDEX ON McpServer (server_id) UNIQUE;
CREATE INDEX ON McpServer (source_url) UNIQUE;
CREATE INDEX ON McpServer (name) FULL_TEXT;
CREATE INDEX ON McpServer (description) FULL_TEXT;

-- Provider OAuth credentials live per-provider, never per-server
-- (Section 2.2 / rule 7). Servers reference providers by name so many
-- servers share one consent/token.
CREATE VERTEX TYPE ProviderCredential;
CREATE PROPERTY ProviderCredential.provider STRING;
CREATE PROPERTY ProviderCredential.client_id STRING;
CREATE PROPERTY ProviderCredential.redirect_uri STRING;
CREATE PROPERTY ProviderCredential.refresh_token STRING;   -- encrypted at rest by caller
CREATE PROPERTY ProviderCredential.access_token STRING;    -- encrypted at rest by caller
CREATE PROPERTY ProviderCredential.granted_scopes LIST;
CREATE PROPERTY ProviderCredential.requested_scopes LIST;
CREATE PROPERTY ProviderCredential.token_expires_at DATETIME;
CREATE PROPERTY ProviderCredential.status STRING;          -- active | revoked | reauth_required
CREATE PROPERTY ProviderCredential.updated_at DATETIME;

CREATE INDEX ON ProviderCredential (provider) UNIQUE;

-- Persisted cold-miss search terms (Section 9.1): one background
-- cascade per distinct term, de-duplicated against the scheduled cron.
CREATE VERTEX TYPE ColdMissQuery;
CREATE PROPERTY ColdMissQuery.query_key STRING;
CREATE PROPERTY ColdMissQuery.term STRING;
CREATE PROPERTY ColdMissQuery.status STRING;         -- queued | checking_registry | checking_github | done
CREATE PROPERTY ColdMissQuery.registry_done BOOLEAN;
CREATE PROPERTY ColdMissQuery.github_done BOOLEAN;
CREATE PROPERTY ColdMissQuery.servers_found INTEGER;
CREATE PROPERTY ColdMissQuery.created_at DATETIME;
CREATE PROPERTY ColdMissQuery.updated_at DATETIME;

CREATE INDEX ON ColdMissQuery (query_key) UNIQUE;

-- Observability (Section 12): one row per verification decision with
-- the stage/check that caused it, so a single miscalibrated check
-- dominating rejections is visible instead of silent.
CREATE VERTEX TYPE VerificationDecision;
CREATE PROPERTY VerificationDecision.decision_id STRING;
CREATE PROPERTY VerificationDecision.server_id STRING;
CREATE PROPERTY VerificationDecision.transport STRING;
CREATE PROPERTY VerificationDecision.stage STRING;
CREATE PROPERTY VerificationDecision.check STRING;
CREATE PROPERTY VerificationDecision.reason STRING;
CREATE PROPERTY VerificationDecision.quality_score INTEGER;
CREATE PROPERTY VerificationDecision.confidence STRING;
CREATE PROPERTY VerificationDecision.resulting_status STRING;
CREATE PROPERTY VerificationDecision.decided_at DATETIME;

CREATE INDEX ON VerificationDecision (decision_id) UNIQUE;

-- ============================================================
-- Verification graph edges
-- ============================================================

CREATE EDGE TYPE USES_PROVIDER;
CREATE EDGE TYPE HAS_VERIFICATION_DECISION;
