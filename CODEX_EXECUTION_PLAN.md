# CODEX_EXECUTION_PLAN.md

# Agentic Discovery Platform
## Internet-Wide MCP Tool & A2A Agent Discovery, Persistence, Inspection, and Sandboxed Testing

**Status:** Authoritative master execution plan for Codex / AI coding agents  
**Version:** 4.0  
**Backend:** Python + FastAPI only  
**Frontend:** Angular + TypeScript  
**Database:** ArcadeDB  
**Protocols:** MCP + A2A  
**Execution isolation:** Docker sandbox

---

# 1. PURPOSE

Build a central discovery platform where an AI application developer can search by natural language and discover reliable:

- MCP tools
- MCP servers
- A2A agents
- agent skills/capabilities

without already knowing their names, repositories, registries, or endpoints.

The platform must discover candidates from the public internet, validate them, deduplicate them, score reliability, persist approved results in ArcadeDB, and make them available for future searches.

The user must also be able to:

- inspect source/config/protocol metadata;
- test an MCP tool through MCP;
- test an A2A agent through A2A;
- observe live execution logs/traces;
- inspect agent → MCP dependency behavior;
- reuse previously discovered results through warm search.

---

# 2. THE CORE PRODUCT LOOP

```text
Natural-language query
        |
        v
Search Orchestrator
        |
        +---------------------+
        |                     |
        v                     v
ArcadeDB Catalog        Live Discovery
                              |
          +-------------------+-------------------+
          |                   |                   |
          v                   v                   v
       GitHub             Registries          Web Search
                                                  |
                                           Targeted Extraction
          |                   |                   |
          +-------------------+-------------------+
                              |
                              v
                       Candidate References
                              |
                              v
                     Protocol Resolution
                       /             \
                      /               \
                    MCP              A2A
                     |                |
             initialize/list       Agent Card
                     |                |
                     +-------+--------+
                             |
                             v
                  Security + Validation
                             |
                             v
                       Normalization
                             |
                             v
                        Deduplication
                             |
                             v
                     Reliability Engine
                             |
                     +-------+-------+
                     |               |
                  approved        rejected
                     |               |
                     v               v
                  ArcadeDB      rejection evidence
                     |
                     v
               Search / Ranking
                     |
                     v
                      UI
                     |
          +----------+----------+
          |                     |
          v                     v
     Test MCP Tool        Test A2A Agent
          |                     |
          v                     v
    MCP Sandbox             A2A Sandbox
          |                     |
          +----------+----------+
                     |
                     v
               TestRun + Logs
```

**Core principle:**

> Discovery is broad. Trust is evidence-based. Persistence is reusable. Execution is sandbox-only.

---

# 3. USER STORY

As an AI application developer, I want to search for agentic tools and skills through a central interface so that I can discover reliable implementations from the MCP/A2A ecosystem, inspect their source and protocol metadata, test them safely, and access them faster in future searches through ArcadeDB.

---

# 4. ACCEPTANCE CRITERIA

## 4.1 Cold Search

Given the user is on the Discovery page.

When the user searches for:

```text
Web scraping tool
```

the system must:

1. Search ArcadeDB.
2. Determine whether local results are sufficient/fresh.
3. If necessary, perform live discovery.
4. Search configured sources such as:
   - GitHub;
   - MCP registries;
   - A2A/agent registries;
   - web search;
   - targeted web extraction;
   - configured direct endpoints.
5. Produce candidates.
6. Resolve MCP candidates through MCP protocol.
7. Resolve A2A candidates through Agent Cards/A2A metadata.
8. Validate candidates.
9. Deduplicate candidates.
10. Calculate reliability.
11. Persist approved items in ArcadeDB.
12. Persist provenance/evidence.
13. Rank results.
14. Display interactive cards.

## 4.2 Warm Search

For a query already represented in the catalog:

1. Query ArcadeDB first.
2. Return useful cached results quickly.
3. Run live discovery in parallel/asynchronously when configured.
4. Merge cached and live results.
5. Deduplicate.
6. Revalidate changed candidates where necessary.
7. Update freshness/reliability.
8. Persist new approved discoveries.
9. Update the UI without unnecessarily waiting for slow sources.
10. Clearly distinguish cached/live/updated results.

## 4.3 View Code

When the user clicks `View Code`:

- open modal/side panel;
- preserve search state;
- display source code when legitimately available;
- display configuration files when available;
- display MCP schema for tools;
- display Agent Card for agents;
- display provenance/source URLs;
- syntax-highlight with Monaco;
- provide Copy to Clipboard;
- never execute the displayed code.

If source code is unavailable, clearly say so rather than fabricating it.

## 4.4 Test MCP Tool

When the user clicks `Test Tool`:

```text
validated MCP schema
      ↓
dynamic input form
      ↓
user input
      ↓
backend validation
      ↓
ephemeral Docker sandbox
      ↓
MCP client
      ↓
MCP initialize
      ↓
MCP tools/list as required
      ↓
MCP tools/call
      ↓
live logs
      ↓
output
      ↓
TestRun persistence
      ↓
sandbox cleanup
```

## 4.5 Test A2A Agent

When the user clicks `Test Agent`:

```text
natural-language task
      ↓
validated Agent Card
      ↓
ephemeral Docker sandbox
      ↓
A2A request/task
      ↓
agent response
      ↓
optional agent → MCP call
      ↓
MCP allowlist
      ↓
live trace/logs
      ↓
output
      ↓
dependency diff + TestRun
      ↓
sandbox cleanup
```

---

# 5. NON-NEGOTIABLE RULES

1. Execute phases in order.
2. Do not implement future phases prematurely.
3. Every phase requires tests and a Definition of Done.
4. Backend is Python + FastAPI. Do not introduce Node.js as the backend.
5. Angular/TypeScript is frontend.
6. MCP and A2A remain separate protocol paths.
7. A GitHub repository is a candidate, not automatically a trusted tool/agent.
8. A web search result is a candidate, not trusted protocol metadata.
9. Validate MCP candidates using MCP protocol semantics.
10. Validate A2A candidates using Agent Cards/A2A semantics.
11. Never fabricate schemas, Agent Cards, source code, registry responses, reliability scores, or execution results.
12. Never execute discovered code in FastAPI.
13. All untrusted execution occurs in Docker sandbox.
14. Never expose host filesystem or Docker socket to the sandbox.
15. Apply CPU, memory, timeout, process, filesystem, and network restrictions.
16. Protect all outbound URL fetching against SSRF.
17. Validate redirects.
18. Apply response-size and timeout limits.
19. Respect robots.txt and applicable provider terms for web extraction.
20. Respect external API/search rate limits.
21. One discovery-source failure must not fail the complete search.
22. Every trusted item must retain provenance.
23. Deduplicate by canonical identity, never by name alone.
24. Rejected candidates must retain a rejection reason/evidence trail.
25. Never persist secrets.
26. Never put secrets in logs or frontend state.
27. Never run destructive Docker volume/database commands automatically.
28. Do not use `docker compose down -v` as routine cleanup.
29. ArcadeDB must use the project's own image, network, and named volume.
30. Gemini is restricted to explicitly approved application use.
31. Gemini must not generate embeddings.
32. Sandbox code must not directly call Gemini.
33. Real integrations must be feature-flagged and independently testable.
34. Mocks may be used for deterministic tests but must never be presented as live discovery.
35. If a source cannot be validated safely, do not place it in the trusted executable catalog.

---

# 6. DISCOVERY STRATEGY

## MCP discovery sources

### Mandatory/core

1. Official/supported MCP registry adapters.
2. GitHub.
3. Web search.
4. Configured direct MCP endpoints.

### Additional

5. Targeted web extraction.
6. MCP-compatible sub-registries/catalogs.
7. Optional DNS discovery adapter.
8. Future enterprise/private registries.

## A2A discovery sources

### Mandatory/core

1. A2A/agent registries or catalogs.
2. GitHub.
3. Web search.
4. `.well-known/agent-card.json` where applicable.
5. Direct configured Agent Card URLs.

### Additional

6. Targeted web extraction.
7. Private/enterprise agent registries.
8. Future discovery adapters.

---

# 7. SOURCE ADAPTER ARCHITECTURE

Do not put provider-specific logic into the Search Orchestrator.

Use interfaces.

```text
DiscoveryOrchestrator
        |
        +--> MCPDiscoveryAdapter
        |       +--> GitHub
        |       +--> MCP Registry
        |       +--> Web Search
        |       +--> Web Extraction
        |       +--> Direct Endpoint
        |       +--> Optional DNS
        |
        +--> A2ADiscoveryAdapter
                +--> GitHub
                +--> Agent Registry
                +--> Web Search
                +--> Web Extraction
                +--> Well-Known
                +--> Direct Agent Card
```

Each adapter returns a common `CandidateReference`.

---

# 8. CANDIDATE MODEL

```json
{
  "candidate_id": "UUID",
  "protocol": "mcp | a2a | unknown",
  "source_type": "github | mcp_registry | agent_registry | web_search | web_page | well_known | configured",
  "source_provider": "String",
  "source_id": "String",
  "url": "https://...",
  "repository_url": "https://...",
  "title": "String",
  "description": "String",
  "discovered_at": "Timestamp",
  "raw_metadata": {}
}
```

Candidate lifecycle:

```text
DISCOVERED
    ↓
CLASSIFIED
    ↓
RESOLVING
    ↓
PROTOCOL_VALIDATED
    ↓
NORMALIZED
    ↓
DEDUPLICATED
    ↓
RELIABILITY_EVALUATED
    ↓
APPROVED / REJECTED
```

---

# 9. MCP RESOLUTION

A candidate must be distinguished between:

```text
Repository
    ↓
MCP Server
    ↓
MCP Tool(s)
```

Resolution:

```text
candidate
   ↓
identify server
   ↓
resolve endpoint/startup configuration
   ↓
security validation
   ↓
MCP client connection
   ↓
protocol initialization
   ↓
capability/server metadata
   ↓
tools/list
   ↓
schema validation
   ↓
normalized Tool records
```

When supported by the selected MCP protocol version, use server discovery/capability metadata appropriately before normal initialization.

Do not create trusted tool schemas from README text alone.

---

# 10. A2A RESOLUTION

Resolution:

```text
candidate
   ↓
identify agent endpoint
   ↓
resolve Agent Card
   ├── direct Agent Card URL
   ├── /.well-known/agent-card.json
   └── registry-provided Agent Card
   ↓
validate Agent Card
   ↓
normalize identity/capabilities/skills
   ↓
normalized Agent
```

Agent Card is metadata, not executable code.

---

# 11. NORMALIZED CATALOG

## Tool

```text
item_id
type = tool
protocol = MCP
name
description
server_identity
tool_name
mcp_schema
version
status
provenance
reliability
freshness
artifacts
embedding
```

## Agent

```text
item_id
type = agent
protocol = A2A
name
description
canonical_identity
endpoint
agent_card
capabilities
skills
declared_dependencies
version
status
provenance
reliability
freshness
artifacts
embedding
```

---

# 12. DEDUPLICATION

Never deduplicate by display name.

## MCP

Prefer:

```text
canonical server identity
+
version where meaningful
+
tool name
```

Fallback:

```text
normalized endpoint
+
repository/provider identity
+
tool name
```

## A2A

Prefer:

```text
canonical agent identity
+
canonical endpoint
```

## Multi-source merge

```text
GitHub       -> Tool X
MCP Registry -> Tool X
Web Search   -> Tool X

              ↓

          ONE Tool X
          ├── GitHub provenance
          ├── Registry provenance
          ├── Web provenance
          └── combined evidence
```

---

# 13. PROVENANCE

Persist:

- source provider;
- source URL;
- provider ID;
- first discovered;
- last discovered;
- last synced;
- last validated;
- evidence type;
- validation status;
- protocol response evidence where applicable.

Every trusted result must be explainable.

---

# 14. RELIABILITY ENGINE

Pipeline:

```text
Candidate
   ↓
Basic validation
   ↓
Protocol validation
   ↓
Security validation
   ↓
Reliability evaluation
   ↓
Approval threshold
   ↓
ArcadeDB
```

Default threshold:

```text
0.75
```

Configurable.

## MCP signals

- valid schema;
- initialize success;
- tools/list success;
- endpoint availability;
- execution success;
- error rate;
- latency;
- maintenance/source evidence;
- freshness;
- security validation;
- provenance quality.

## A2A signals

- valid Agent Card;
- endpoint availability;
- successful task execution;
- response stability;
- dependency behavior;
- security validation;
- freshness;
- provenance quality.

Reliability must be explainable and versioned.

---

# 15. ARCADEDB PERSISTENCE

ArcadeDB is the persistent system of record for approved discoveries.

Persist:

```text
Item
Tool
Agent
Skill
DiscoverySource
DiscoveryEvidence
Artifact
ReliabilityEvaluation
TestRun
```

Use graph relationships where useful:

```text
Agent --HAS_SKILL--> Skill
Agent --USES_TOOL--> Tool
Item --DISCOVERED_FROM--> DiscoverySource
Item --HAS_EVIDENCE--> DiscoveryEvidence
Item --HAS_TEST_RUN--> TestRun
```

Use:

- document fields;
- full-text indexes;
- vector embeddings;
- graph relationships.

---

# 16. ARTIFACT STORAGE

Artifact types:

```text
source
config
mcp_schema
agent_card
documentation
```

Store source code only when legitimately available/permitted.

Track:

```text
source_available
source_unavailable
source_restricted
source_not_retrieved
```

Never fabricate unavailable source code.

---

# 17. COLD SEARCH

```text
Query
 ↓
Query planner
 ↓
ArcadeDB lookup
 ↓
sufficient?
 ├── yes → return/rank
 └── no  → live discovery
                ↓
         protocol resolution
                ↓
            validation
                ↓
          normalization
                ↓
          deduplication
                ↓
           reliability
                ↓
             persist
                ↓
              rank
                ↓
               UI
```

---

# 18. WARM SEARCH

```text
Query
  |
  +--------------------+
  |                    |
  v                    v
ArcadeDB            Live Discovery
  |                    |
  +---------+----------+
            |
            v
          Merge
            |
            v
        Deduplicate
            |
            v
        Revalidate
            |
            v
      Reliability update
            |
            v
          ArcadeDB
            |
            v
           Rank
            |
            v
            UI
```

Cached results must be useful immediately and live results should update them without requiring a complete page restart.

---

# 19. FRESHNESS

Track:

```text
first_seen
last_seen
last_synced
last_validated
```

Implement configurable:

```text
fresh → aging → stale → refresh
```

Refreshing may update:

- metadata;
- MCP schema;
- Agent Card;
- endpoint;
- version;
- reliability;
- source status;
- embedding.

---

# 20. SEARCH / QUERY UNDERSTANDING

Gemini may be used for query understanding only.

Input:

```text
raw query
```

Output:

```json
{
  "keywords": [],
  "preferred_type": "tool | agent | all",
  "expanded_query": "",
  "source_hints": []
}
```

Strictly validate output.

Fallback:

```text
Gemini unavailable
      ↓
keyword extraction
      ↓
search
```

Never allow arbitrary model output to become executable instructions.

---

# 21. EMBEDDINGS

Use a local open-source embedding model.

Do not use Gemini embeddings.

Embed appropriate:

```text
name
description
tool metadata
agent metadata
skills
capabilities
selected source metadata
```

Use ArcadeDB vector search plus text/structured filters.

---

# 22. RANKING

Default configurable formula:

```text
final_score =
    0.45 * relevance
  + 0.35 * reliability
  + 0.10 * freshness
  + 0.10 * evidence
```

Do not allow multiple copies of the same item to inflate evidence merely because it appeared in multiple sources.

---

# 23. WEB DISCOVERY SECURITY

For every external URL:

```text
URL validation
 ↓
scheme validation
 ↓
hostname validation
 ↓
DNS/IP policy
 ↓
private/internal target blocking
 ↓
redirect validation
 ↓
timeout
 ↓
response-size limit
 ↓
content-type validation
 ↓
safe extraction
```

Protect against:

- localhost;
- loopback;
- private IP ranges;
- link-local;
- cloud metadata endpoints;
- internal hostnames;
- malicious redirects;
- unsupported protocols.

Never execute downloaded JavaScript or source code.

---

# 24. SANDBOX SECURITY

Every test run gets an ephemeral sandbox.

Required:

- non-root;
- CPU limit;
- memory limit;
- hard timeout;
- process termination;
- ephemeral filesystem;
- no host mounts;
- no Docker socket;
- restricted outbound network;
- destination allowlist;
- scoped credentials;
- secret redaction;
- output-size limit;
- log-size limit;
- audit events;
- guaranteed cleanup.

Lifecycle:

```text
create
 ↓
configure
 ↓
execute
 ↓
collect
 ↓
persist
 ↓
destroy
```

Cleanup must work on:

- success;
- failure;
- timeout;
- cancellation.

---

# 25. MCP TOOL TESTING

The user must be able to genuinely test an MCP tool.

```text
Test Tool
   ↓
Load validated schema
   ↓
Generate dynamic form
   ↓
User input
   ↓
Validate input
   ↓
Create TestRun
   ↓
Create Docker sandbox
   ↓
Apply security policy
   ↓
Inject approved configuration
   ↓
Start MCP client
   ↓
Connect to approved server
   ↓
MCP initialize
   ↓
Verify expected tool
   ↓
MCP tools/call
   ↓
Capture logs/output
   ↓
SSE to UI
   ↓
Persist TestRun
   ↓
Destroy sandbox
```

The backend must not directly import or execute arbitrary discovered source code.

---

# 26. A2A AGENT TESTING

The user must be able to genuinely test an A2A agent.

```text
Test Agent
   ↓
Natural-language task
   ↓
Validate Agent Card/endpoint
   ↓
Create TestRun
   ↓
Create Docker sandbox
   ↓
Apply security policy
   ↓
A2A request/task
   ↓
Agent execution
   ↓
Optional MCP dependency call
   ↓
Allowlist enforcement
   ↓
A2A response
   ↓
SSE trace
   ↓
Persist dependencies/TestRun
   ↓
Destroy sandbox
```

---

# 27. AGENT → MCP ALLOWLIST

For each agent:

```text
Declared MCP dependencies
          ↓
     Test execution
          ↓
     Observed calls
          ↓
        Compare
```

Example:

```text
declared  = [search_web, fetch_page]
observed  = [search_web]
unexpected = []
```

Violation:

```text
declared  = [search_web]
observed  = [search_web, execute_shell]
unexpected = [execute_shell]
```

The unexpected call must be blocked and logged unless explicitly allowed by the configured policy.

Persist:

```text
declared
observed
unexpected
```

---

# 28. REST API

Minimum API:

```text
GET  /api/search?q={query}&type={all|tool|agent}

GET  /api/items/{item_id}

GET  /api/items/{item_id}/artifacts

GET  /api/items/{item_id}/schema

GET  /api/items/{item_id}/provenance

POST /api/items/{item_id}/test

POST /api/items/{item_id}/agent-test

GET  /api/items/{item_id}/test/{run_id}

GET  /api/items/{item_id}/test/{run_id}/logs
```

Search response:

```json
{
  "results": [],
  "metadata": {
    "mode": "cached | live | merged",
    "sources_attempted": [],
    "sources_succeeded": [],
    "sources_failed": [],
    "cached_results": 0,
    "live_candidates": 0,
    "approved_count": 0,
    "rejected_count": 0
  }
}
```

---

# 29. SSE

Use Server-Sent Events for test logs/traces.

MCP example:

```text
Sandbox created
MCP client started
initialize ✓
tools/list ✓
tools/call: web_scrape
response received
test completed
sandbox destroyed
```

A2A example:

```text
Agent sandbox created
A2A task submitted
Agent requested MCP tool: search_web
allowlist: ALLOWED
agent response received
sandbox destroyed
```

---

# 30. FRONTEND

Use Angular + TypeScript.

## Search

```text
Search input
Tool / Agent / All filter
Result cards
Reliability
Freshness
Source badges
Cached/Live state
```

## Tool card

```text
Name
Description
MCP
Reliability
Sources
Freshness
[View Code]
[Test Tool]
```

## Agent card

```text
Name
Description
A2A
Skills
Capabilities
Reliability
Sources
[View Code]
[Test Agent]
```

## View Code

Tabs:

```text
Source
Config
MCP Schema
Agent Card
Metadata
Provenance
```

## Test Tool

Schema-generated form.

## Test Agent

Natural-language task input.

---

# 31. FAILURE ISOLATION

Example:

```text
GitHub        → success
MCP Registry  → timeout
A2A Registry  → success
Web Search    → success
```

Return successful results and expose source failure metadata.

If all live sources fail but ArcadeDB has valid results:

```text
return cached results
+
show live discovery unavailable
```

Never fabricate live results.

---

# 32. CONFIGURATION

Example:

```text
ARCADEDB_HOST=arcadedb
ARCADEDB_PORT=2480
ARCADEDB_DATABASE=platform
ARCADEDB_USER=root
ARCADEDB_PASSWORD=<runtime secret>

RELIABILITY_THRESHOLD=0.75

ENABLE_GITHUB_DISCOVERY=false
ENABLE_MCP_REGISTRY_DISCOVERY=false
ENABLE_A2A_REGISTRY_DISCOVERY=false
ENABLE_WEB_SEARCH_DISCOVERY=false
ENABLE_WEB_EXTRACTION=false
ENABLE_WELL_KNOWN_A2A=true

GITHUB_TOKEN=<runtime secret>

GEMINI_API_KEY=<runtime secret>
GEMINI_MODEL=gemini-2.5-flash
```

Provider URLs, credentials, limits and concurrency must be configuration-driven.

---

# 33. DISCOVERY MODES

## Mock

Deterministic local testing.

```text
DISCOVERY_MODE=mock
```

## Live

Configured real providers.

```text
DISCOVERY_MODE=live
```

## Mixed

Recommended architecture:

```text
ArcadeDB
+
enabled live sources
+
optional mock source
```

Each source must be independently enabled/disabled.

---

# 34. CACHING

Cache:

1. trusted ArcadeDB catalog;
2. appropriate discovery-source responses;
3. validated protocol metadata;
4. artifacts;
5. embeddings.

Never cache secrets.

Cache keys should include relevant provider/source/protocol/query/identity information.

---

# 35. RATE LIMITS AND RETRIES

Do not confuse provider protection with user-facing product rate limiting.

For external sources implement:

- bounded retries;
- exponential backoff where appropriate;
- Retry-After handling;
- per-provider concurrency;
- timeouts;
- source health tracking;
- temporary suppression after repeated failures.

---

# 36. OBSERVABILITY

Track:

## Discovery

- query;
- source;
- latency;
- candidates;
- validated;
- approved;
- rejected;
- source errors;
- rate limits.

## Persistence

- inserted;
- updated;
- deduplicated;
- rejected.

## Execution

- run ID;
- item;
- duration;
- status;
- sandbox lifecycle;
- tool-call decisions;
- dependency diff.

Never log secret values.

---

# 37. REPOSITORY TARGET STRUCTURE

```text
backend/
├── app/
│   ├── api/
│   ├── db/
│   ├── models/
│   ├── discovery/
│   │   ├── common/
│   │   ├── mcp/
│   │   └── a2a/
│   ├── normalization/
│   ├── deduplication/
│   ├── search/
│   ├── reliability/
│   ├── artifacts/
│   ├── sandbox/
│   ├── security/
│   ├── gemini/
│   └── tests/
├── Dockerfile
└── requirements.txt

frontend/
└── src/app/
    ├── search/
    ├── item-detail/
    ├── code-viewer/
    ├── tool-test/
    ├── agent-test/
    └── shared/

arcadedb/
docker-compose.yml
.env.example
README.md
```

If the existing repository has a different structure, preserve its valid conventions rather than blindly replacing it.

---

# 38. PHASE ORDER

Detailed phase specifications should live in separate files when this master plan is split.

The authoritative implementation order is:

```text
PHASE 00  Docker/repository baseline
PHASE 01  ArcadeDB schema/persistence
PHASE 02  MCP protocol core
PHASE 03  A2A protocol core
PHASE 04  GitHub discovery
PHASE 05  MCP registry discovery
PHASE 06  A2A registry + Well-Known discovery
PHASE 07  Web search discovery
PHASE 08  Safe web extraction
PHASE 09  Multi-source orchestrator
PHASE 10  Normalization/dedup/reliability/persistence
PHASE 11  Query planning/embeddings/ranking
PHASE 12  REST + SSE
PHASE 13  Angular discovery UI
PHASE 14  View Code/artifact inspection
PHASE 15  Secure MCP testing
PHASE 16  Secure A2A testing
PHASE 17  Warm search/refresh/final hardening
```

---

# 39. PHASE DEFINITIONS OF DONE

## Phase 00

- project Docker stack starts;
- project-owned ArcadeDB image works;
- backend connects to ArcadeDB;
- frontend reaches backend;
- no unrelated Docker resource is destroyed.

## Phase 01

- catalog models exist;
- provenance exists;
- evidence exists;
- TestRun exists;
- graph/text/vector foundation works.

## Phase 02

- real local MCP server works;
- MCP client works;
- initialize works;
- tools/list works;
- schemas are normalized.

## Phase 03

- real local A2A agent works;
- Agent Card works;
- `.well-known` works;
- A2A task works.

## Phase 04

- GitHub adapter works;
- MCP/A2A candidate classification works;
- provenance works;
- rate-limit/error handling works.

## Phase 05

- supported MCP registry adapter works;
- registry candidates enter MCP validation.

## Phase 06

- A2A registry works;
- Well-Known Agent Card resolution works;
- Agent Cards validate.

## Phase 07

- web search returns candidates;
- candidates enter protocol resolution;
- web failures are isolated.

## Phase 08

- targeted extraction works;
- SSRF protections work;
- robots/terms-aware behavior exists;
- unsafe URLs are blocked.

## Phase 09

- sources execute concurrently;
- one source can fail without breaking search;
- candidates aggregate correctly.

## Phase 10

- tools/agents normalize;
- duplicates merge;
- reliability works;
- approved records persist.

## Phase 11

- Gemini planner works;
- fallback works;
- local embeddings work;
- semantic + structured ranking works.

## Phase 12

- API contracts work;
- SSE works;
- integration tests pass.

## Phase 13

- search UI works;
- result cards show source/reliability/freshness;
- partial source failures are understandable.

## Phase 14

- View Code works;
- Monaco works;
- source/schema/Agent Card/config display works;
- no execution occurs.

## Phase 15

- user can test an MCP tool;
- actual MCP protocol is used;
- Docker sandbox is used;
- logs/output stream;
- TestRun persists;
- cleanup works.

## Phase 16

- user can test an A2A agent;
- actual A2A protocol is used;
- Docker sandbox is used;
- Agent Card is validated;
- MCP allowlist works;
- dependency diff persists;
- cleanup works.

## Phase 17

- warm search works;
- stale refresh works;
- cached/live merge works;
- full regression passes;
- documentation is accurate.

---

# 40. REQUIRED TEST MATRIX

## Discovery

- GitHub success/failure/rate-limit.
- MCP registry success/failure.
- A2A registry success/failure.
- Web search success/failure.
- Web extraction timeout.
- Web extraction oversized response.
- Well-Known missing/invalid/valid.
- Direct Agent Card valid/invalid.
- MCP initialize failure.
- MCP tools/list failure.
- Invalid MCP schema.
- Duplicate candidates.
- False-positive repository.

## Search

- cold search;
- warm search;
- cached-only;
- live-only;
- cached + live;
- semantic query;
- Gemini failure;
- keyword fallback;
- one source failure;
- all live sources failure;
- stale refresh.

## Persistence

- CRUD;
- provenance;
- evidence;
- artifact;
- reliability;
- TestRun;
- graph edges;
- vector search;
- deduplication;
- freshness update.

## MCP execution

- valid input;
- invalid input;
- initialize failure;
- tools/list failure;
- tools/call success;
- tools/call error;
- timeout;
- blocked network;
- secret redaction;
- cleanup.

## A2A execution

- valid Agent Card;
- invalid Agent Card;
- task success;
- task failure;
- timeout;
- declared MCP call;
- undeclared MCP call;
- blocked call;
- dependency diff;
- cleanup.

## Security

- localhost SSRF;
- private IP SSRF;
- metadata endpoint SSRF;
- malicious redirect;
- invalid URL scheme;
- oversized response;
- secret leakage;
- host filesystem access;
- Docker socket access;
- unrestricted network;
- orphan container;
- timeout cleanup.

---

# 41. COMPLETE MCP END-TO-END ACCEPTANCE TEST

```text
"web scraping tool"
        ↓
ArcadeDB lookup
        ↓
GitHub + MCP Registry + Web
        ↓
candidate
        ↓
MCP resolution
        ↓
initialize
        ↓
tools/list
        ↓
schema
        ↓
validation/reliability
        ↓
ArcadeDB persistence
        ↓
UI card
        ↓
View Code
        ↓
Test Tool
        ↓
dynamic form
        ↓
Docker sandbox
        ↓
MCP tools/call
        ↓
SSE logs
        ↓
output
        ↓
TestRun
        ↓
cleanup
        ↓
second search
        ↓
cached + live refresh
```

---

# 42. COMPLETE A2A END-TO-END ACCEPTANCE TEST

```text
"research agent"
        ↓
ArcadeDB lookup
        ↓
GitHub + Agent Registry + Web
        ↓
candidate
        ↓
Agent Card
        ↓
validation/reliability
        ↓
ArcadeDB persistence
        ↓
UI card
        ↓
View Code / Agent Card
        ↓
Test Agent
        ↓
natural-language task
        ↓
Docker sandbox
        ↓
A2A task
        ↓
agent response
        ↓
optional MCP call
        ↓
allowlist
        ↓
SSE trace
        ↓
dependency diff
        ↓
TestRun
        ↓
cleanup
        ↓
future warm search
```

---

# 43. OUT OF SCOPE

- Publishing modified tools back to public registries.
- Building a public registry.
- Billing.
- User-facing sandbox billing/rate limiting.
- Unrestricted whole-web crawling.
- Automatic modification of discovered source code.
- Automatic deployment of arbitrary discovered code.
- Treating skills as independently executable entities.
- Executing arbitrary discovered code directly in FastAPI.

---

# 44. CODEX EXECUTION PROTOCOL

Before starting:

1. Read this master plan.
2. Inspect the existing repository.
3. Inspect current Docker resources without destroying anything.
4. Identify existing implementations that satisfy requirements.
5. Do not duplicate existing functionality unnecessarily.
6. Determine the current phase from repository state.
7. Read the detailed phase specification if present.
8. Implement only that phase.
9. Run tests.
10. Verify Definition of Done.
11. Produce a phase completion report.
12. Only then continue.

For every implementation decision:

```text
Requirement
   ↓
Existing code
   ↓
Smallest correct change
   ↓
Tests
   ↓
Verification
```

Do not create placeholder classes/endpoints merely to make a phase appear complete.

---

# 45. FINAL ACCEPTANCE

The project is complete only when:

- natural-language discovery works;
- user does not need prior knowledge of item names;
- GitHub discovery works;
- configured MCP registry discovery works;
- configured A2A registry discovery works;
- web search discovery works;
- safe targeted extraction works;
- A2A Well-Known discovery works;
- MCP candidates are actually protocol-validated;
- A2A candidates are actually Agent-Card-validated;
- sources are merged and deduplicated;
- provenance is retained;
- reliability is explainable;
- approved tools persist in ArcadeDB;
- approved agents persist in ArcadeDB;
- source artifacts are handled honestly;
- semantic search works;
- warm search works;
- stale refresh works;
- View Code is read-only;
- MCP Tool testing uses MCP;
- A2A Agent testing uses A2A;
- both execute only in sandbox;
- A2A MCP dependencies are allowlisted;
- unexpected MCP calls are blocked;
- live logs/traces work;
- TestRuns persist;
- cleanup works on success/failure/timeout;
- secrets remain protected;
- SSRF protections are tested;
- external-source failures are isolated;
- full regression passes.

---

# 46. FINAL ARCHITECTURAL CONTRACT

```text
                  ┌────────────────────┐
                  │    Angular UI      │
                  └─────────┬──────────┘
                            │
                            ▼
                  ┌────────────────────┐
                  │ Python + FastAPI   │
                  └─────────┬──────────┘
                            │
                            ▼
                  ┌────────────────────┐
                  │ Search Orchestrator│
                  └─────────┬──────────┘
                            │
            ┌───────────────┼────────────────┐
            │               │                │
            ▼               ▼                ▼
        ArcadeDB        MCP Discovery     A2A Discovery
                            │                │
                      ┌─────┼─────┐    ┌────┼─────┐
                      │     │     │    │    │     │
                   GitHub Registry Web GitHub Registry Web
                                             +
                                         Well-Known
                            │                │
                            ▼                ▼
                       MCP Resolver      A2A Resolver
                            │                │
                      initialize/list     Agent Card
                            │                │
                            └───────┬────────┘
                                    ▼
                            Security Validation
                                    ▼
                               Normalization
                                    ▼
                                Deduplication
                                    ▼
                              Reliability
                                    ▼
                                 ArcadeDB
                                    ▼
                            Vector/Text Search
                                    ▼
                                  Ranking
                                    ▼
                                    UI
                             ┌──────┴──────┐
                             │             │
                             ▼             ▼
                        Test Tool      Test Agent
                             │             │
                             ▼             ▼
                        MCP Sandbox    A2A Sandbox
                             │             │
                             ▼             ▼
                        tools/call      A2A task
                                           │
                                      optional MCP
                                           │
                                       allowlist
                                           │
                             └──────┬──────┘
                                    ▼
                              TestRun + SSE
```

**Implementation target:** a real, testable discovery platform—not a mock catalog, not a single-registry search, and not a backend that executes untrusted code.

