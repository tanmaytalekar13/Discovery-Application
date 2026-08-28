SECTION 0 — RULES OF EXECUTION (READ THIS FIRST, FOLLOW FOR THE ENTIRE PROJECT)

You are building a full production-quality platform, not a prototype. Follow these rules without exception:

Strict sequential phases. This plan has Phases 0 through 11. You must complete one phase fully — including code, tests, and the "Definition of Done" checklist for that phase — before starting the next phase. Never start Phase N+1 while Phase N is partially done. Never interleave work from two phases.
No silent skipping. If a task inside a phase cannot be completed (missing credential, ambiguous requirement, unavailable dependency), stop and ask a clarifying question instead of guessing, stubbing silently, or inventing a fake resolution. Only proceed once you have an answer or an explicit instruction to assume a default.
End-of-phase report is mandatory. After finishing a phase, produce a short report using the "Phase Completion Report" template in Section 10, including: what was built, how it was tested, what is stubbed/mocked vs real, and any open questions. Wait for confirmation ("continue" / "proceed to Phase X") before moving on, unless the user has explicitly told you to run all phases autonomously — if so, still write the report between phases so there is a clear audit trail in commit history.
Every phase must be runnable and testable in isolation. Each phase ends with something that actually runs (a passing test suite, a working endpoint you can curl, a UI screen you can load) — never "code written but unverified."
One commit (or commit group) per phase, with a commit message prefixed phase-N:. Do not mix code from two phases in one commit.
Never fabricate data sources, registries, or credentials. Where a real external registry/API is not confirmed, build against a clearly-labeled mock/stub adapter (see Section 2) with an interface that a real adapter can later implement without changing calling code.
Security is not optional in any phase. Sandbox isolation, input validation, and allowlisting must be implemented for real from Phase 9 onward — not deferred to "later hardening." Phase 11 hardens what already exists; it does not introduce basic security for the first time.
Keep the two protocols (MCP and A2A) architecturally separate. Never merge MCP tool discovery and A2A agent discovery into one code path "for simplicity." They have different discovery mechanisms, different schemas, and different execution models. Only the UI and the top-level Search Orchestrator unify them.
SECTION 1 — PROJECT OVERVIEW

Build a centralized web platform where an AI application developer can:

Search for MCP tools and A2A agents (across a persisted catalog and live discovery).
View a tool's/agent's source code, configuration, MCP schema, or Agent Card (read-only, no execution).
Test an MCP tool with real parameters inside an isolated sandbox.
Test an A2A agent with a natural-language task inside an isolated sandbox.
Persist reliable discoveries and test-execution history in ArcadeDB so future searches are faster and better-ranked.

Out of scope for V1 (do not build): publishing tools/agents back to public registries, billing, product-level rate limiting, building a public MCP/A2A registry, treating "skills" as independently executable entities, auto-modifying discovered tools/agents.

SECTION 2 — CONFIRMED TECHNICAL DECISIONS

These have been decided; do not re-litigate them or ask about them again:

Topic	Decision
Repository	Fresh repo, built from scratch. No existing codebase to integrate with.
Frontend	Angular (latest stable), TypeScript, Monaco Editor for code viewing
Backend	Python, FastAPI (async, OpenAPI docs, native SSE support)
Database	ArcadeDB (document + graph + vector search)
Discovery sources (MCP + A2A)	Pluggable adapter interfaces, backed by mock/stub data sources for V1. Real registries/catalogs are integrated later behind the same interface — do not hardcode assumptions about a specific real registry's API shape. Each adapter must be swappable via configuration (e.g. DISCOVERY_MODE=mock vs DISCOVERY_MODE=live).
Semantic search / embeddings	Local, open-source embedding model for vectors — no external API call for embeddings. Use a small local sentence-embedding model (e.g. sentence-transformers/all-MiniLM-L6-v2 run via the sentence-transformers Python library, CPU-friendly) to generate vectors stored in ArcadeDB.
Query understanding (generation)	Gemini API. Query understanding/planning (keyword extraction, preferred_type inference, query expansion/rewriting) is done via a call to the Gemini API, not rule-based NLP. This is a text-generation call, separate from the local embedding step above — embeddings stay local; only the understanding/planning step uses Gemini.
Mock A2A test agent "intelligence" (generation)	Gemini API. The local A2A test agent built in Phase 3 uses the Gemini API to actually generate its responses to a given task (instead of returning a canned/hardcoded string), so Phase 10's agent-testing flow exercises a real generation call end-to-end.
Sandbox	Docker, ephemeral containers, one container per test run
Protocols	MCP (Model Context Protocol) for tools, A2A (Agent-to-Agent) for agents
Environment variables (already provided — wire these in as config, do not hardcode)
ARCADEDB_ROOT_PASSWORD=playwithdata
ARCADEDB_HOST=localhost
ARCADEDB_PORT=2480
ARCADEDB_DATABASE=platform
ARCADEDB_USER=root
ARCADEDB_PASSWORD=playwithdata

RELIABILITY_THRESHOLD=0.75
DISCOVERY_MODE=mock
GEMINI_API_KEY=<to be supplied by the user at runtime — do not hardcode, do not commit>
GEMINI_MODEL=gemini-2.5-flash   # default; confirm/adjust with the user if a different Gemini model is preferred

Gemini is used in exactly two places in this system (see Section 2 rows "Query understanding" and "Mock A2A test agent"): (1) query understanding/planning inside the Search Orchestrator, and (2) generating the local test agent's responses. It must never be used for embeddings (those stay local/open-source) and never be used inside the sandbox for arbitrary/untrusted execution — only for the two named generation call sites, each wrapped in its own thin, mockable client module so the API key is never referenced directly from business logic.

If any additional credential, port, or external URL is needed at any point that is not listed above, stop and ask rather than inventing one.

ArcadeDB Image Policy — Fresh, Project-Specific Image (Mandatory)

Do not reuse any pre-existing ArcadeDB container, image, or volume already present on the host or belonging to any other project. This project must build and run its own dedicated ArcadeDB image, isolated to this repo, from Phase 0 onward.

Rules:

Do not docker pull and run the stock arcadedata/arcadedb image directly as a service in docker-compose.yml. Instead, create platform/arcadedb/Dockerfile that uses the official ArcadeDB image as a base (FROM arcadedata/arcadedb:latest, or a pinned version — ask the user to confirm the version if not specified) and layers on project-specific setup:
Any project-specific init scripts (e.g. database/schema bootstrap invoked on first start, matching Phase 1's schema).
A clearly labeled LABEL project="agentic-discovery-platform" so docker ps / docker images unambiguously identifies it as belonging to this project.
Build this image locally as part of docker-compose.yml (build: ./arcadedb for the arcadedb service), not image: arcadedata/arcadedb pulled generically. This guarantees the running container is always this project's own image, never a shared/system one.
Credentials (ARCADEDB_ROOT_PASSWORD, ARCADEDB_USER, ARCADEDB_PASSWORD from the env vars above) must be injected at container runtime via docker-compose.yml's environment: block (sourced from a local .env file) — never baked into the Dockerfile itself, never committed. Use the same values already provided above (playwithdata / root / platform); do not silently generate different ones.
Container/network naming must follow the Docker Environment Rules below: service name arcadedb, dedicated Compose project/network, hostname arcadedb (never localhost) for backend connectivity.
Data volume: give this project's ArcadeDB container its own named, project-scoped volume (e.g. platform_arcadedb_data), distinct from any volume used by ArcadeDB containers from other projects on the same machine. Never mount or reuse another project's volume.
Before the first build, Codex must run the inspection commands (docker ps -a, docker network ls, docker volume ls, docker images) and explicitly confirm/report: (a) whether any unrelated ArcadeDB container/image/volume already exists on the host, and (b) that the new build will not collide with or reuse any of them. If a same-named container/volume from a previous run of this same project exists, follow the stale-container cleanup rule in the Docker Environment Rules section (safe stop/remove only for this project's own resources — never another project's).
SECTION 3 — PROPOSED REPOSITORY STRUCTURE
platform/
├── backend/
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── db/                  # ArcadeDB client + repositories
│   │   ├── models/               # Pydantic models (Item, TestRun, etc.)
│   │   ├── discovery/
│   │   │   ├── mcp/              # MCP discovery adapter (mock + interface for live)
│   │   │   └── a2a/              # A2A discovery adapter (mock + interface for live)
│   │   ├── reliability/          # normalization, dedup, scoring engine
│   │   ├── search/                # orchestrator, ranking, embeddings, indexing
│   │   ├── sandbox/               # docker sandbox manager, allowlist enforcement
│   │   ├── api/                   # FastAPI routers (search, items, artifacts, test, logs)
│   │   └── tests/
│   ├── requirements.txt
│   └── Dockerfile
├── arcadedb/
│   ├── Dockerfile                 # project-specific ArcadeDB image (see ArcadeDB Image Policy)
│   └── init/                      # project-specific bootstrap scripts, run on first container start
├── frontend/
│   ├── src/app/
│   │   ├── search/                # search page, cards, filters
│   │   ├── item-detail/
│   │   ├── code-viewer/           # Monaco integration
│   │   ├── tool-test/             # dynamic form + run panel
│   │   ├── agent-test/            # task input + run panel
│   │   └── shared/
│   └── package.json
├── docker-compose.yml             # ArcadeDB (built, not pulled) + backend + sandbox network
├── .env                            # local-only, gitignored — holds credentials from Section 2
└── README.md

Codex may adjust naming, but must keep the same separation of concerns (discovery/mcp vs discovery/a2a vs reliability vs search vs sandbox vs api), and must keep arcadedb/ as a first-class, project-owned build context — never a bare image: reference.

SECTION 4 — ARCHITECTURE DIAGRAMS
4.1 Overall architecture
┌─────────────────────────────────────────────┐
│                 Angular UI                   │
│ Search | Cards | Code Viewer | Test Panels   │
└──────────────────────┬───────────────────────┘
                        ▼
┌─────────────────────────────────────────────┐
│            FastAPI (API layer)               │
└──────────────────────┬───────────────────────┘
                        ▼
┌─────────────────────────────────────────────┐
│            Search Orchestrator                │
└──────────────┬────────────────┬───────────────┘
               ▼                ▼
      ┌──────────────┐   ┌──────────────┐
      │ MCP Discovery│   │ A2A Discovery│
      │ Adapter(mock)│   │ Adapter(mock)│
      └──────┬───────┘   └──────┬───────┘
             ▼                  ▼
        MCP Servers         Agent Cards
             ▼                  ▼
         tools/list           Skills
             ▼                  ▼
           Tools              Agents
             └────────┬─────────┘
                       ▼
                Normalization
                       ▼
                 Deduplication
                       ▼
              Reliability Engine
                       ▼
                    Ranking
                       ▼
                   ArcadeDB
                       ▼
                       UI
                       │
               ┌───────┴───────┐
               ▼               ▼
         Tool Testing     Agent Testing
               ▼               ▼
            Sandbox         Sandbox
                                │
                                ▼
                           A2A Agent
                                │
                           MCP Client
                                │
                            Allowlist
                                ▼
                           MCP Tools
4.2 MCP tool discovery (never assume a registry returns tools directly)
MCP Registry (mock in V1)
       ↓
MCP Servers
       ↓
Connect / Inspect (initialize)
       ↓
tools/list
       ↓
Tool Definitions
4.3 A2A agent discovery (discovers Agent Cards, not tools/list)
Discovery Source (registry / catalog / configured / .well-known)
       ↓
Agent Card
       ↓
Identity, Endpoint, Capabilities, Skills, Auth info
4.4 Cold search vs warm search
COLD SEARCH                          WARM SEARCH
User Query                           User Query
   ↓                                     ↓
ArcadeDB (empty/insufficient)      ┌─────┴─────┐
   ↓                               ▼           ▼
MCP + A2A live discovery      ArcadeDB    Live Discovery
   ↓                          (cached)    (MCP + A2A)
Normalize → Dedup →                 └─────┬─────┘
Reliability → Persist                     ▼
   ↓                                 Merge + Dedup
Rank                                       ↓
   ↓                                 Reliability
UI                                         ↓
                                          Rank
                                            ↓
                                        ArcadeDB
                                            ↓
                                            UI
4.5 "View Code" flow (inspection only — never executes anything)
User clicks View Code
        ↓
Angular opens side panel (no navigation, search context preserved)
        ↓
GET /api/items/{item_id}/artifacts
        ↓
Backend → ArcadeDB → available artifacts
        ↓
Angular renders Monaco tabs: Source / Config / MCP Schema (or Agent Card)
        ↓
User can Copy to Clipboard per tab

If no source is available, show: "Source code is not available for this item" plus the repository/source URL if known, and still show schema/config/metadata that IS available.

4.6 "Test Tool" flow (MCP tool execution)
User clicks Test Tool
        ↓
Open Test Panel
        ↓
Get MCP input schema (cached or GET /api/items/{item_id}/schema)
        ↓
Generate dynamic form from JSON Schema
        ↓
User fills parameters → clicks Run Tool
        ↓
POST /api/items/{item_id}/test  { "input": {...} }
        ↓
Validate input against schema
        ↓
Sandbox Manager creates ephemeral Docker container
        ↓
MCP Client inside sandbox connects to MCP server, calls the tool
        ↓
Logs streamed via SSE:  GET /api/items/{item_id}/test/{run_id}/logs
        ↓
Final result returned + status (success/failed/timeout/blocked/cancelled)
        ↓
TestRun persisted in ArcadeDB
        ↓
Sandbox destroyed
4.7 "Test Agent" flow (A2A execution)
User clicks Test Agent
        ↓
Task input (natural language textarea)
        ↓
POST /api/items/{agent_id}/test  { "input": { "task": "..." } }
        ↓
Agent Sandbox created
        ↓
A2A call to the agent's endpoint with the task
        ↓
If the agent needs MCP tools internally:
    MCP Client inside sandbox → tools must be on the declared allowlist
    Any undeclared/unknown tool call is BLOCKED and logged
        ↓
Final agent result + logs/trace streamed via SSE
        ↓
TestRun persisted (declared vs observed dependency diff recorded)
        ↓
Sandbox destroyed
SECTION 5 — DATA MODELS
5.1 Unified Item (Tool or Agent)
json
{
  "item_id": "UUID",
  "type": "tool | agent",
  "name": "String",
  "description": "String",
  "source": {
    "type": "mcp_registry | a2a_catalog | well_known | configured",
    "id": "String",
    "url": "String"
  },
  "version": "String",
  "status": "active | deprecated | unavailable",
  "reliability": {
    "score": 0.91,
    "confidence": 0.88,
    "scoring_version": "v1",
    "last_evaluated": "Timestamp"
  },
  "discovery": {
    "first_seen": "Timestamp",
    "last_seen": "Timestamp",
    "last_synced": "Timestamp"
  },
  "tool": {
    "server_id": "String",
    "tool_name": "String",
    "mcp_schema": {}
  },
  "agent": {
    "endpoint": "String",
    "agent_card": {},
    "skills": [],
    "capabilities": [],
    "declared_dependencies": []
  },
  "artifacts": {
    "source_available": false,
    "source_url": "String",
    "source_code": null,
    "config_files": []
  },
  "embedding": [0.0, 0.0]
}
5.2 TestRun
json
{
  "run_id": "UUID",
  "item_id": "UUID",
  "type": "tool | agent",
  "started_at": "Timestamp",
  "completed_at": "Timestamp",
  "status": "running | success | failed | timeout | blocked | cancelled",
  "input": {},
  "output": {},
  "duration_ms": 0,
  "errors": [],
  "logs": [],
  "dependencies": {
    "declared": [],
    "observed": [],
    "unexpected": []
  }
}
5.3 ArcadeDB logical graph model
Tool
Agent
Skill
DiscoverySource
Artifact
TestRun
ReliabilityEvaluation

Agent ──HAS_SKILL──> Skill
Agent ──USES_TOOL──> Tool
Tool  ──DISCOVERED_FROM──> DiscoverySource
Agent ──DISCOVERED_FROM──> DiscoverySource
Tool  ──HAS_TEST_RUN──> TestRun
Agent ──HAS_TEST_RUN──> TestRun

Deduplication key: never dedupe on name alone. Use source.type + source.id + (server_id/tool_name) for tools, and source.type + canonical agent endpoint/identity for agents. Track version separately.

SECTION 6 — API CONTRACTS
GET  /api/search?q={query}&type={all|tool|agent}
GET  /api/items/{item_id}
GET  /api/items/{item_id}/artifacts
GET  /api/items/{item_id}/schema
POST /api/items/{item_id}/test          { "input": {...} }  →  { "run_id", "status": "running" }
GET  /api/items/{item_id}/test/{run_id}/logs      (SSE stream)
GET  /api/items/{item_id}/test/{run_id}           (final status/result, polled fallback if SSE unavailable)

Final test response shape:

json
{ "run_id": "run-123", "status": "success", "duration_ms": 1240, "output": {} }
SECTION 7 — RELIABILITY & RANKING RULES

Pipeline: External Discovery → Basic Validation → Security Validation → Reliability Scoring → Approval Decision → ArcadeDB. Only items scoring ≥ RELIABILITY_THRESHOLD (config, default 0.75) are treated as part of the trusted, persisted catalog.

Tool signals: valid MCP schema, server availability, execution success rate, error rate, latency, security scan result, deprecated status, version/maintenance info.

Agent signals (measured at task level, not just "request returned"): valid Agent Card, endpoint availability, task completion rate, execution stability, security validation, dependency reliability, unexpected tool usage, deprecated status.

Ranking formula (weights configurable, not hardcoded):

score = 0.45 * relevance + 0.35 * reliability + 0.10 * freshness + 0.10 * popularity/evidence
SECTION 8 — SECURITY & SANDBOX REQUIREMENTS (mandatory from Phase 9 onward)
Container isolation, non-root execution inside the container.
CPU and memory limits per container.
Hard execution timeout with automatic kill.
Ephemeral, isolated filesystem — no host filesystem mounts.
Restricted/no outbound network by default; only the specific MCP server / A2A endpoint under test is reachable.
No credentials baked into images; injected per-run and scoped to that run.
Process isolation between concurrent test runs.
Audit log of every sandbox creation/destruction and every tool call made inside it.
MCP allowlist is mandatory for agent execution: an agent may only call MCP tools it declared; any other tool call attempt is blocked and recorded, and the diff between declared vs observed dependencies is stored on the TestRun.
No code from a discovered tool/agent is ever executed on the main backend process — sandbox only.
SECTION 9 — PHASE-BY-PHASE EXECUTION PLAN

Execute exactly one phase at a time, in order. Do not begin a phase until the previous phase's Definition of Done is fully satisfied and reported.

PHASE 0 — Repository Bootstrap & Tooling

Objective: A working skeleton that runs, with nothing faked yet.

Tasks:

Initialize monorepo per Section 3 structure.
Before writing any Docker config, run docker ps -a, docker network ls, docker volume ls, docker images and report findings — specifically flag any pre-existing ArcadeDB container/image/volume on the host that does not belong to this project, per the ArcadeDB Image Policy in Section 2.
Create platform/arcadedb/Dockerfile per the ArcadeDB Image Policy (Section 2): based on the official ArcadeDB image, adds a project label, and wires in a project-scoped init-script location — this is a fresh, project-owned image, not a bare pull of the public image.
docker-compose.yml bringing up ArcadeDB (built via build: ./arcadedb, using the provided env vars from a local .env file) + backend + a placeholder frontend dev server. The arcadedb service must use a project-scoped named volume (e.g. platform_arcadedb_data), never a shared/host-level one.
FastAPI app with a /health endpoint that actually checks ArcadeDB connectivity (via the arcadedb Compose hostname, never localhost).
Angular app scaffold with a placeholder home route that calls /health and displays status.
Config loading from environment variables (Section 2) with validation — fail fast with a clear error if a required var is missing.
Linting/formatting setup (ruff/black for Python, ESLint/Prettier for Angular). CI config optional but recommended.

Definition of Done: docker-compose up builds and starts this project's own ArcadeDB image (confirmed via docker images showing the project label, distinct from any other ArcadeDB image on the host) + backend; GET /health returns {"status":"ok","arcadedb":"connected"}; Angular dev server loads and shows that status. Phase 0 report must explicitly confirm the fresh-image build and the absence of collision with any pre-existing ArcadeDB resources. Commit phase-0: bootstrap.

PHASE 1 — ArcadeDB Foundation & Data Layer

Objective: Persistence layer for everything downstream.

Tasks:

Create ArcadeDB database/schema for: Tool, Agent, Skill, DiscoverySource, Artifact, TestRun, ReliabilityEvaluation, and the graph edges in Section 5.3. Schema/bootstrap logic should live in platform/arcadedb/init/ so it runs automatically the first time this project's ArcadeDB image starts (per the ArcadeDB Image Policy).
Implement the unified Item Pydantic model (Section 5.1) and TestRun model (Section 5.2).
Build a repository layer (CRUD) for items and test runs — no HTTP endpoints yet, just internal Python API + unit tests.
Set up indexes for full-text fields (name, description, tool/agent-specific fields) and a vector index field for embeddings (even if embeddings aren't generated yet — schema must support them).
Seed script that inserts a handful of hand-written sample tools/agents for testing later phases.

Definition of Done: Unit tests cover create/read/update/delete for Item and TestRun, including graph edge creation. Seed script runs and data is visible via ArcadeDB Studio/API, on this project's own ArcadeDB container. Commit phase-1: arcadedb-foundation.

PHASE 2 — MCP Discovery Adapter (Mock-Backed)

Objective: A pluggable interface for MCP discovery, backed by realistic mock data, ready to be swapped for a live registry later without changing callers.

Tasks:

Define an abstract MCPDiscoveryAdapter interface: list_servers(query) -> [MCPServerRef], inspect_server(server_ref) -> [ToolDefinition] (this should call real initialize + tools/list semantics against whatever server implementation is behind it).
Implement MockMCPDiscoveryAdapter: a small set of fake MCP servers (e.g. "Spotify MCP Server A/B/C") each exposing a couple of realistic tools with valid JSON Schema inputSchemas (mirror the search_tracks example in Section 1's flow).
Implement a genuine local MCP server (even a trivial one, e.g. a calculator or file-search tool) so that Phase 9's sandboxed execution has something real to call over the MCP protocol, not just fabricated JSON.
Config flag DISCOVERY_MODE=mock selects this adapter; leave a documented seam (DISCOVERY_MODE=live → not implemented yet, raises NotImplementedError with a clear message) for future real integration.
Normalize discovered tools into the Item model (type: "tool") — do not persist yet, this phase only discovers and returns in-memory objects.

Definition of Done: Given a query like "spotify", the adapter returns candidate mock servers, and inspecting one returns real tool definitions with valid schemas, using actual MCP client/server messages (not hardcoded JSON pretending to be a protocol response) for at least one real local MCP server. Unit + integration tests included. Commit phase-2: mcp-discovery-mock.

PHASE 3 — A2A Discovery Adapter (Mock-Backed)

Objective: Same pattern as Phase 2, for A2A agents.

Tasks:

Define an abstract A2ADiscoveryAdapter interface: list_sources(query) -> [AgentSourceRef], resolve_agent_card(source_ref) -> AgentCard.
Implement MockA2ADiscoveryAdapter with a few fake agents (e.g. a "Financial Research Agent") each with a realistic Agent Card (identity, endpoint, capabilities, skills, auth info, supported interfaces).
Implement one genuine trivial local A2A agent (an HTTP service that serves a real .well-known/agent-card.json and can accept a natural-language task) so Phase 10 has something real to test against. Instead of a canned/hardcoded response, this agent calls the Gemini API (via a small dedicated gemini_client module, using GEMINI_API_KEY/GEMINI_MODEL from config) to actually generate its reply to the given task — this is a real generation call, not a stub, so Phase 10's end-to-end test exercises genuine agent "thinking."
If GEMINI_API_KEY is not set when this phase starts, stop and ask rather than falling back to a canned response silently — a silent fallback would undermine Phase 10's test of real generation.
Normalize discovered agents into the Item model (type: "agent"), extracting skills as searchable metadata (not independently executable).
Same DISCOVERY_MODE seam as Phase 2 for future live integration.

Definition of Done: Query returns candidate agent sources; resolving one returns a parsed, valid Agent Card from a real .well-known/agent-card.json endpoint (served locally). Tests included. Commit phase-3: a2a-discovery-mock.

PHASE 4 — Normalization, Deduplication, Reliability Engine

Objective: Turn raw discovery output into trusted, persisted catalog entries.

Tasks:

Normalization layer: map MCP tool definitions and A2A Agent Cards into the unified Item shape consistently.
Deduplication using the composite keys defined in Section 5.3 (never name-only).
Reliability Engine implementing the pipeline in Section 7: basic validation (schema/Agent Card validity) → security validation (basic checks: no obviously malicious patterns, valid endpoint scheme, etc.) → scoring → threshold decision using RELIABILITY_THRESHOLD from config.
Persist only approved items to ArcadeDB (Phase 1's repository layer); rejected items are logged with the reason, not silently dropped.

Definition of Done: Feeding Phase 2/3 mock discovery output through this pipeline results in correctly deduplicated, scored items in ArcadeDB, with rejected items logged and explained. Unit tests cover dedup edge cases (same name, different source) and threshold behavior. Commit phase-4: reliability-engine.

PHASE 5 — Search Orchestrator, Local Embeddings & Ranking

Objective: Unified search across cache + live discovery, semantically aware, using local embeddings only.

Tasks:

Integrate a local embedding model (sentence-transformers/all-MiniLM-L6-v2 or equivalent small CPU model) running inside the backend process/container — confirm no network call is required at inference time (model downloaded once at build/setup time). Embeddings remain 100% local; do not route embedding generation through Gemini.
Generate and store embeddings for each persisted Item (name + description + tool/agent-specific text).
Implement the query planner as a Gemini API call: send the raw user query to Gemini (via the same shared gemini_client module used in Phase 3) and have it return structured JSON — extracted keywords, inferred preferred_type (tool/agent/all), and an optional expanded/rewritten query string for better recall. Enforce a strict response schema (e.g. via a JSON-mode prompt) and validate the returned JSON before using it; if Gemini is unreachable or returns invalid JSON, fall back to a simple keyword-split of the raw query and log the fallback (never let a Gemini outage break search entirely).
Implement the Search Orchestrator: cold search vs warm search behavior exactly as diagrammed in Section 4.4 — ArcadeDB results returned immediately where available, live discovery (Phase 2+3 adapters) triggered in parallel/async, merged, deduped, re-scored, re-persisted.
Implement ranking per Section 7's formula, with weights in config (not hardcoded).
Failure isolation: if one source (ArcadeDB, MCP discovery, or A2A discovery) fails/times out, still return whatever succeeded and flag which source failed in the response metadata.

Definition of Done: GET /api/search?q=spotify (still no HTTP layer required yet — test this at the orchestrator/service level first) returns ranked, deduped results combining cached + freshly discovered mock data, with embeddings actually used for at least one semantic-match test case (e.g. "audio track search" matching a "search_tracks" tool). At least one integration test hits the real Gemini API for query understanding (requires GEMINI_API_KEY to be set locally); the rest of the automated/CI test suite mocks the gemini_client module so tests don't depend on network access or burn API quota. The Gemini-outage fallback path (invalid JSON / unreachable API → keyword-split) is also covered by a test. Commit phase-5: search-orchestrator.

PHASE 6 — Backend API Layer (REST + SSE)

Objective: Expose everything built so far over HTTP, matching Section 6 exactly.

Tasks:

Implement all endpoints listed in Section 6 using FastAPI routers.
POST /api/items/{item_id}/test at this phase should only validate input against schema and return a run_id with status: "running" — actual sandboxed execution comes in Phase 9/10; for now, wire it to a stub executor that just marks the run as completed with a placeholder result so the contract can be tested end-to-end.
Implement SSE log streaming plumbing generically (works with the stub executor now, will carry real logs from Phase 9 onward without changing the endpoint contract).
OpenAPI docs auto-generated and reviewed for accuracy.
Integration tests hitting the real HTTP endpoints (via FastAPI's TestClient) covering search, item detail, artifacts, schema, test-run creation, and log streaming.

Definition of Done: Full REST/SSE surface is callable via curl/HTTPie against a running backend, backed by real Phase 1–5 logic (not further stubs) except execution, which is intentionally stubbed and clearly marked as such. Commit phase-6: api-layer.

PHASE 7 — Angular UI: Search, Cards, Filters

Objective: The primary discovery UI, wired to the real API from Phase 6.

Tasks:

Search page with query input, type filter (all/tool/agent), and result list.
Tool card and Agent card components matching Section 4's card layout (name, description, source/server, reliability score, View Code / Test Tool or Test Agent buttons).
Loading states: show cached results immediately, then update as live discovery results stream in (reflect the warm-search behavior visually, e.g. a subtle "refreshing…" indicator).
Error/empty states, including "one source failed" messaging from Phase 5/6.
Basic responsive layout; no execution logic yet — buttons exist but next two phases wire their targets.

Definition of Done: Searching for a seeded/mocked term (e.g. "spotify", "financial") returns visually correct cards with real reliability scores and correct tool/agent distinction. Component tests included. Commit phase-7: search-ui.

PHASE 8 — "View Code" Feature (Artifacts + Monaco)

Objective: Read-only inspection panel — implement exactly the flow in Section 4.5.

Tasks:

Side panel/modal opened without navigation, preserving search state.
Fetch /api/items/{item_id}/artifacts.
Monaco Editor integration in read-only mode, one tab per artifact (source file(s), config file(s), MCP schema or Agent Card as formatted JSON).
Language detection from filename extension (.py, .js, .ts, .json, .yaml/.yml, .env).
Per-tab "Copy to Clipboard" button.
Explicit "source code is not available" state showing the repository/source URL (if known) plus whatever metadata/schema IS available — this must be tested with at least one seeded item that has no source, and one that does.

Definition of Done: Both the "source available" and "source unavailable" paths render correctly and are covered by component tests; clicking View Code never triggers any execution or network call to a live tool/agent. Commit phase-8: view-code.

PHASE 9 — Sandbox Manager & Tool Testing (Real Execution Begins Here)

Objective: Replace the Phase 6 stub executor with real, sandboxed MCP tool execution. This is a security-critical phase — follow Section 8 exactly.

Tasks:

Sandbox Manager: creates an ephemeral Docker container per test run, applies CPU/memory limits, execution timeout, restricted network (only the target MCP server/endpoint reachable), no host filesystem mounts, non-root user inside the container.
"Test Tool" panel in Angular: dynamic form generated from the tool's inputSchema (JSON Schema → form fields, required/optional handling, basic type validation client-side).
Backend: on POST /api/items/{item_id}/test, validate input against the stored schema, spin up the sandbox, run an MCP client inside it that connects to the real local MCP server built in Phase 2 and calls the tool with the given input.
Stream real logs via SSE (connection established, calling tool, parameters, completion).
Persist the resulting TestRun (status, duration, output, errors, logs) and destroy the sandbox afterward — verify destruction actually happens (no orphaned containers) even on failure/timeout paths.
Distinguish and correctly surface all statuses: running, success, failed, timeout, blocked, cancelled.

Definition of Done: Running the tool test end-to-end (UI → API → sandbox → real local MCP server → response) against the real MCP tool built in Phase 2 works, is logged live, produces a persisted TestRun, and the container is confirmed destroyed after each run (test this explicitly, including a forced-timeout case). Commit phase-9: sandbox-tool-testing.

PHASE 10 — Agent Testing (A2A + Sandbox + Allowlist)

Objective: Same execution rigor as Phase 9, for A2A agents, plus the MCP-allowlist security requirement.

Tasks:

"Test Agent" panel in Angular: natural-language task textarea, "Run Agent" button (no schema-driven form — agents take free text tasks).
Backend: on test request, create an Agent Sandbox, call the real local A2A agent built in Phase 3 with the task.
If the agent uses MCP tools internally, enforce the allowlist from the agent's declared_dependencies: any tool call outside that list must be blocked and logged, not silently allowed.
Track and persist declared vs observed dependencies, and compute/store the "unexpected" diff on the TestRun (Section 5.2).
Stream logs/trace via SSE; persist TestRun; destroy sandbox afterward.
Explicitly test the security boundary: build a test case where the local agent attempts to call a tool NOT on its allowlist, and confirm it is blocked and recorded — this test is mandatory, not optional.

Definition of Done: End-to-end agent test works against the real local A2A agent, whose reply is genuinely generated via the Gemini API (Phase 3); the allowlist violation test case passes (blocked + logged, not executed); TestRun correctly stores declared/observed/unexpected dependency lists. At least one manual/integration run exercises the real Gemini call; CI/automated tests mock the gemini_client so the suite doesn't depend on network access or quota. Commit phase-10: agent-testing.

PHASE 11 — Warm Search, Caching, and Final Hardening

Objective: Polish and harden what already exists — do not introduce first-time security here (that was Phase 9/10).

Tasks:

Confirm/optimize warm-search behavior end-to-end in the UI: cached ArcadeDB results appear instantly, live discovery results merge in asynchronously without a jarring full-page reload.
Background/periodic re-sync job (even a simple scheduled task) that refreshes last_seen/last_synced and re-scores reliability for existing items.
Review and tighten sandbox resource limits and timeouts based on real numbers observed in Phase 9/10 testing.
Secrets/credential handling review: confirm nothing from Section 2's env vars or any per-run credential ever appears in logs, TestRun records, or the frontend.
Audit logging review: confirm sandbox create/destroy and every allowlist decision (Phase 10) is captured in a queryable audit trail.
ArcadeDB image/volume review: re-confirm the project is still running its own fresh ArcadeDB image and project-scoped volume (per the ArcadeDB Image Policy in Section 2), with no drift toward a shared/host-level container.
Full regression pass: re-run the full test suite from all prior phases together, plus a manual end-to-end walkthrough matching Section 4's complete user journey (search → view code → test tool, and search → test agent).
Write the top-level README.md: how to run everything (docker-compose up), how the project-specific ArcadeDB image is built and why, how to switch DISCOVERY_MODE from mock to live in the future, and a summary of what is genuinely live vs mocked in this V1.

Definition of Done: Full regression suite passes; README accurately describes current state; a fresh reviewer can docker-compose up and walk through the entire Section 4 user journey without hitting an unhandled error. Commit phase-11: hardening-and-docs.

SECTION 10 — PHASE COMPLETION REPORT TEMPLATE

At the end of every phase, produce a short report in this format before asking to proceed:

### Phase N complete: <phase name>

Built:
- ...

Tests run (and result):
- ...

Mocked/stubbed vs real:
- ...

Open questions / things I need confirmation on:
- ...

Ready to proceed to Phase N+1? (yes/no — waiting for confirmation)
SECTION 11 — FINAL REMINDERS TO CODEX
If at any point a requirement is ambiguous (e.g. exact ranking weight, a missing field, how a real registry's API will look later), ask a specific, answerable question rather than guessing silently. Prefer asking one focused question over pausing all work indefinitely.
Never merge MCP and A2A discovery logic into a shared code path beyond the top-level orchestrator and UI.
Never treat "source code available" as a precondition for a tool/agent being discoverable, searchable, or persisted — only reliability matters for persistence; source availability only affects the View Code panel.
Never execute untrusted code outside the sandbox, at any phase, for any reason, including "just to test something quickly."
Keep MCP tool execution and A2A agent execution as two distinct code paths in the sandbox layer, even though both use "the sandbox."
Never run ArcadeDB from anything other than this project's own built image and project-scoped volume — no shared/system ArcadeDB, ever.
One phase at a time. Full stop.
SECTION 12 — DOCKER ENVIRONMENT RULES

This project must have ONE authoritative Docker Compose stack.

Before creating or starting any Docker service, inspect the existing Docker environment:

docker ps -a
docker network ls
docker volume ls
docker images

Identify whether containers/images from this project already exist.

Use the project's docker-compose.yml as the SINGLE source of truth.
Do NOT create duplicate backend, frontend, or ArcadeDB containers outside Docker Compose.
All project services must be created and managed by the same docker-compose.yml.
Use explicit service names: arcadedb, backend, frontend.
Use a dedicated Compose project/network for this application.
Before starting the stack, detect existing containers with conflicting names, ports, networks, or images.
If old containers belong to this same project and are stale, stop/remove the containers safely and recreate them through Docker Compose.

ArcadeDB-specific rule (see Section 2's ArcadeDB Image Policy for full detail): the arcadedb service is always built from this project's own platform/arcadedb/Dockerfile, never run from a bare pulled image, and never pointed at a container/volume belonging to another project on the same host. If docker ps -a / docker volume ls shows an ArcadeDB resource not created by this project's Compose file, it must be left untouched and reported to the user, not adopted or deleted.

IMPORTANT:

Never delete Docker volumes automatically.
Never run docker compose down -v.
Never run docker volume rm.
Never delete or reset ArcadeDB data without explicit user approval.
Preserve the existing ArcadeDB data volume.

The database container must be managed by Docker Compose. The backend must connect to ArcadeDB using the Compose service hostname:

ARCADEDB_HOST=arcadedb
ARCADEDB_PORT=2480

Never use localhost for backend → ArcadeDB communication.

After creating the stack, verify:

docker compose ps

All expected services must appear under the same Compose project, and the arcadedb image must show this project's label (confirming it was built fresh, not pulled or reused).

Then verify:

docker compose exec backend ...
docker compose logs backend
docker compose logs arcadedb

Finally verify:

curl http://localhost:8000/health

Expected:

{"status":"ok","arcadedb":"connected"}

Do not proceed to the next phase until the complete Compose stack is verified.