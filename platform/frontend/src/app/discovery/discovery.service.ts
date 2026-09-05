import { Injectable } from '@angular/core';
import { HttpClient, HttpParams, HttpErrorResponse } from '@angular/common/http';
import { Observable, of } from 'rxjs';
import { catchError } from 'rxjs/operators';
import {
  SearchResponse,
  DiscoveryError,
  PreferredType,
  ItemArtifactsResponse,
  SourceTreeResponse,
  SourceFileResponse,
  ItemSchemaResponse,
  ItemProvenanceResponse,
  ToolConnectResponse,
  ToolInvokeRequest,
  ToolInvokeResponse,
  OAuthStartResponse,
  OAuthCallbackResponse,
} from './models';

export type DiscoveryResult = SearchResponse | DiscoveryError;

export function isError(
  result: DiscoveryResult | null,
): result is DiscoveryError {
  return result !== null && 'kind' in result;
}

@Injectable({
  providedIn: 'root',
})
export class DiscoveryService {
  private readonly apiBase = '/api';

  constructor(private readonly http: HttpClient) {}

  /** GET /api/search?q=...&type=...&limit=... */
  search(
    query: string,
    type: PreferredType = 'all',
    limit: number = 25,
  ): Observable<DiscoveryResult> {
    const params = new HttpParams()
      .set('q', query)
      .set('type', type)
      .set('limit', String(limit));

    return this.http
      .get<SearchResponse>(`${this.apiBase}/search`, { params })
      .pipe(catchError(this.handleError()));
  }

  /** GET /api/items/{item_id}/artifacts */
  getArtifacts(itemId: string): Observable<ItemArtifactsResponse> {
    return this.http.get<ItemArtifactsResponse>(
      `${this.apiBase}/items/${itemId}/artifacts`,
    );
  }

  /** GET /api/items/{item_id}/source/tree */
  getSourceTree(itemId: string): Observable<SourceTreeResponse> {
    return this.http.get<SourceTreeResponse>(
      `${this.apiBase}/items/${itemId}/source/tree`,
    );
  }

  /** GET /api/items/{item_id}/source/files/{path} */
  getSourceFile(itemId: string, path: string): Observable<SourceFileResponse> {
    return this.http.get<SourceFileResponse>(
      `${this.apiBase}/items/${itemId}/source/files/${path}`,
    );
  }

  /** GET /api/items/{item_id}/schema */
  getSchema(itemId: string): Observable<ItemSchemaResponse> {
    return this.http.get<ItemSchemaResponse>(
      `${this.apiBase}/items/${itemId}/schema`,
    );
  }

  /** GET /api/items/{item_id}/provenance */
  getProvenance(itemId: string): Observable<ItemProvenanceResponse> {
    return this.http.get<ItemProvenanceResponse>(
      `${this.apiBase}/items/${itemId}/provenance`,
    );
  }

  // -------------------------------------------------------------------------
  // Tool Test endpoints (Phase 15)
  // -------------------------------------------------------------------------

  /**
   * POST /api/items/{item_id}/test/connect
   * Open an MCP session, run initialize + tools/list.
   * Returns the list of available tools (or auth_required=true).
   */
  testConnect(
    itemId: string,
    sessionId?: string,
  ): Observable<ToolConnectResponse> {
    let params = new HttpParams();
    if (sessionId) params = params.set('session_id', sessionId);
    return this.http.post<ToolConnectResponse>(
      `${this.apiBase}/items/${itemId}/test/connect`,
      null,
      { params },
    );
  }

  /**
   * POST /api/items/{item_id}/test/invoke
   * Call a tool with the given arguments.
   */
  testInvoke(
    itemId: string,
    body: ToolInvokeRequest,
    sessionId?: string,
  ): Observable<ToolInvokeResponse> {
    let params = new HttpParams();
    if (sessionId) params = params.set('session_id', sessionId);
    return this.http.post<ToolInvokeResponse>(
      `${this.apiBase}/items/${itemId}/test/invoke`,
      body,
      { params },
    );
  }

  /**
   * POST /api/items/{item_id}/test/authorize/start
   * Begin an OAuth flow. Backend returns a 501 for now (spec gating).
   */
  testAuthorizeStart(
    itemId: string,
    sessionId?: string,
  ): Observable<OAuthStartResponse> {
    let params = new HttpParams();
    if (sessionId) params = params.set('session_id', sessionId);
    return this.http.post<OAuthStartResponse>(
      `${this.apiBase}/items/${itemId}/test/authorize/start`,
      null,
      { params },
    );
  }

  /**
   * GET /api/items/{item_id}/test/authorize/callback
   * OAuth provider callback (server-to-server).
   */
  testAuthorizeCallback(
    itemId: string,
    code: string,
    state: string,
    sessionId: string,
  ): Observable<OAuthCallbackResponse> {
    const params = new HttpParams()
      .set('code', code)
      .set('state', state)
      .set('session_id', sessionId);
    return this.http.get<OAuthCallbackResponse>(
      `${this.apiBase}/items/${itemId}/test/authorize/callback`,
      { params },
    );
  }

  /**
   * POST /api/items/{item_id}/test/disconnect
   * Immediately discard the stored token for a test session.
   */
  testDisconnect(
    itemId: string,
    sessionId: string,
  ): Observable<{ status: string; message: string }> {
    const params = new HttpParams().set('session_id', sessionId);
    return this.http.post<{ status: string; message: string }>(
      `${this.apiBase}/items/${itemId}/test/disconnect`,
      null,
      { params },
    );
  }

  /**
   * POST /api/items/{item_id}/test/manual-token
   * MVP fallback: user provides a bearer token / API key directly.
   * Returns a session_id that subsequent connect/invoke calls should send.
   */
  testSubmitManualToken(
    itemId: string,
    token: string,
    sessionId?: string,
  ): Observable<{ status: string; session_id: string; message: string }> {
    let params = new HttpParams().set('token', token);
    if (sessionId) params = params.set('session_id', sessionId);
    return this.http.post<{ status: string; session_id: string; message: string }>(
      `${this.apiBase}/items/${itemId}/test/manual-token`,
      null,
      { params },
    );
  }

  private handleError() {
    return (error: HttpErrorResponse): Observable<DiscoveryError> => {
      if (error.error instanceof ErrorEvent) {
        // Client-side / network error
        return of<DiscoveryError>({
          kind: 'network',
          message: `Network error: ${error.error.message}`,
        });
      }
      // Backend error
      return of<DiscoveryError>({
        kind: 'backend',
        status: error.status,
        message: `Backend returned ${error.status}: ${error.message}`,
      });
    };
  }
}