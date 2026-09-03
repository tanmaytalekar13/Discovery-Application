import { Injectable } from '@angular/core';
import { HttpClient, HttpParams, HttpErrorResponse } from '@angular/common/http';
import { Observable, of } from 'rxjs';
import { catchError } from 'rxjs/operators';
import {
  SearchResponse,
  DiscoveryError,
  PreferredType,
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