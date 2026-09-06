// TypeScript interfaces mirroring the FastAPI /api/search contract.
// Keep in sync with:
//   platform/backend/app/api/schemas.py  SearchResponse / SearchResultItem
//   platform/backend/app/models/item.py  Item

export type ItemType = 'tool' | 'agent';
export type SourceType =
  | 'mcp_registry'
  | 'a2a_catalog'
  | 'well_known'
  | 'configured'
  | 'github'
  | 'web_search'
  | 'web_page';
export type SearchMode = 'cached' | 'live' | 'merged';
export type PreferredType = 'tool' | 'agent' | 'all';
export type SearchType = 'all' | 'tool' | 'agent';

// ---------------------------------------------------------------------------
// Sub-models
// ---------------------------------------------------------------------------

export interface DiscoverySource {
  type: SourceType;
  id: string;
  url?: string;
  provider?: string;
}

export interface DiscoveryEvidence {
  evidence_id: string;
  kind: string;
  statement: string;
  source: DiscoverySource;
  observed_at: string;
  details: Record<string, unknown>;
}

export interface Reliability {
  score: number;
  confidence: number;
  scoring_version: string;
  last_evaluated?: string;
  security_validation: number;
  signals: Record<string, number>;
  reasons: string[];
}

export interface DiscoveryMetadata {
  first_seen: string;
  last_seen: string;
  last_synced: string;
}

export interface ArtifactMetadata {
  source_available: boolean;
  source_url?: string;
  source_code?: string;
  config_files: unknown[];
  integration_cache_path?: string;
  source_tree_cache_path?: string;
  source_readme_cache_path?: string;
  source_downloaded_at?: string;
}

export interface ToolMetadata {
  server_id: string;
  tool_name: string;
  mcp_schema: Record<string, unknown>;
}

export interface AgentMetadata {
  endpoint: string;
  agent_card: Record<string, unknown>;
  skills: string[];
  capabilities: string[];
  declared_dependencies: string[];
}

export interface Item {
  item_id: string;
  canonical_id?: string;
  type: ItemType;
  name: string;
  description: string;
  source: DiscoverySource;
  provenance: DiscoverySource[];
  evidence: DiscoveryEvidence[];
  version?: string;
  status: string;
  reliability: Reliability;
  discovery: DiscoveryMetadata;
  tool?: ToolMetadata;
  agent?: AgentMetadata;
  artifacts: ArtifactMetadata;
  embedding?: number[];
}

export interface QueryPlan {
  keywords: string[];
  preferred_type: PreferredType;
  expanded_query: string;
  source_hints: string[];
  used_fallback: boolean;
}

export interface SearchMetadata {
  mode: SearchMode;
  sources_attempted: string[];
  sources_succeeded: string[];
  sources_failed: string[];
  cached_results: number;
  live_candidates: number;
  approved_count: number;
  rejected_count: number;
  plan?: QueryPlan;
}

export interface SearchResultItem {
  item: Item;
  final_score: number;
  relevance: number;
  reliability: number;
  freshness: number;
  evidence: number;
  classification?: ClassificationResult;
}

export interface SearchResponse {
  results: SearchResultItem[];
  tools: SearchResultItem[]; // alias for filtered view; we use results always
  metadata: SearchMetadata;
}

// ---------------------------------------------------------------------------
// Artifacts / View Code (Phase 14)
// ---------------------------------------------------------------------------

export interface IntegrationSnippet {
  available: boolean;
  snippet?: string;
  source?: string;
  note?: string;
}

export interface SourcePreview {
  available: boolean;
  language?: string;
  content?: string;
  path?: string;
  note?: string;
}

export interface ItemArtifactsResponse {
  item_id: string;
  artifacts: ArtifactMetadata;
  integration: IntegrationSnippet;
  source_preview: SourcePreview;
}

export interface SourceTreeNode {
  name: string;
  path: string;
  type: 'file' | 'dir';
  size?: number;
  children?: SourceTreeNode[];
}

export interface SourceTreeResponse {
  item_id: string;
  available: boolean;
  root?: string;
  note?: string;
  tree?: SourceTreeNode[];
}

export interface SourceFileResponse {
  item_id: string;
  path: string;
  available: boolean;
  language?: string;
  content?: string;
  size?: number;
  note?: string;
}

export interface ItemSchemaResponse {
  item_id: string;
  type: ItemType;
  schema?: Record<string, unknown>;
  agent_card?: Record<string, unknown>;
  note?: string;
}

export interface ItemProvenanceResponse {
  item_id: string;
  provenance: DiscoverySource[];
  evidence: DiscoveryEvidence[];
}

// ---------------------------------------------------------------------------
// Client-side error discriminated union
// ---------------------------------------------------------------------------

export type DiscoveryError =
  | { kind: 'network'; message: string }
  | { kind: 'backend'; status: number; message: string }
  | { kind: 'unknown'; message: string };

// ---------------------------------------------------------------------------
// Tool Test / Classification (Phase 15)
// Mirrors backend/app/sandbox/schemas.py ClassificationResult.
// ---------------------------------------------------------------------------

export type ClassificationMode =
  | 'remote'
  | 'remote_via_package'
  | 'local_stdio'
  | 'not_testable';

export interface RemoteCandidate {
  type: 'streamable-http' | 'sse';
  url: string;
}

export interface EnvironmentVariableHint {
  name: string;
  isSecret?: boolean;
  description?: string;
}

export interface LocalPackageHint {
  registryType?: string;
  identifier?: string;
  runtimeHint?: string;
  transportType?: string;
  installCommand?: string;
  environmentVariables: EnvironmentVariableHint[];
}

export interface ClassificationResult {
  testable: boolean;
  mode: ClassificationMode;
  detail?: RemoteCandidate[] | LocalPackageHint[];
  reason?: string;
}

export interface ItemClassificationResponse {
  item_id: string;
  classification: ClassificationResult;
}

// ---------------------------------------------------------------------------
// Tool Test / Connect (Phase 15)
// ---------------------------------------------------------------------------

export type AuthReason =
  | 'unauthorized'
  | 'payment_required'
  | 'not_found'
  | 'rate_limited'
  | 'server_error'
  | 'connection_error'
  | 'timeout'
  | 'unknown';

export interface ToolInfo {
  name: string;
  description?: string;
  inputSchema: Record<string, unknown>;
}

export interface ToolConnectResponse {
  connected: boolean;
  auth_required: boolean;
  transport?: string;
  tools: ToolInfo[];
  error?: string;
  requires_auth?: boolean;
  auth_reason?: AuthReason;
  user_message?: string;
  show_token_input?: boolean;
  show_oauth_button?: boolean;
}

export interface ToolInvokeRequest {
  tool_name: string;
  arguments: Record<string, unknown>;
}

export interface ToolInvokeResponse {
  status: 'success' | 'error';
  result?: unknown;
  error?: string;
  requires_auth?: boolean;
  duration_ms?: number;
  auth_reason?: AuthReason;
  user_message?: string;
  show_token_input?: boolean;
  show_oauth_button?: boolean;
}

export interface OAuthStartResponse {
  authorization_url: string;
  state: string;
}

export interface OAuthCallbackResponse {
  success: boolean;
  message: string;
}
