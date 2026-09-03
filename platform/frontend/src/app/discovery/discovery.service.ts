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