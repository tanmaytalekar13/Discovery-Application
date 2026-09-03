import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SearchResultItem, DiscoveryError } from './models';
import { ResultCardComponent } from './result-card.component';

@Component({
  selector: 'app-result-list',
  standalone: true,
  imports: [CommonModule, ResultCardComponent],
  template: `
    @if (loading) {
      <div class="list-state list-state--loading" aria-live="polite" aria-busy="true">
        <span class="spinner" aria-hidden="true"></span>
        <p>Searching…</p>
      </div>
    }

    @if (error && !loading) {
      <div class="list-state list-state--error" role="alert">
        <svg class="error-icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <circle cx="12" cy="12" r="10"></circle>
          <line x1="12" y1="8" x2="12" y2="12"></line>
          <line x1="12" y1="16" x2="12.01" y2="16"></line>
        </svg>
        <p class="error-message">{{ errorMessage }}</p>
        <p class="error-hint">Check your network or try again in a moment.</p>
      </div>
    }

    @if (!loading && !error && hasSearched && results.length === 0) {
      <div class="list-state list-state--empty">
        <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
          <circle cx="11" cy="11" r="8"></circle>
          <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
        </svg>
        <p>No results found for this query.</p>
        <p class="empty-hint">Try different keywords, broaden the search, or enable more discovery sources in the backend configuration.</p>
      </div>
    }

    @if (!loading && !error && !hasSearched) {
      <div class="list-state list-state--idle">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2" aria-hidden="true">
          <circle cx="11" cy="11" r="8"></circle>
          <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
        </svg>
        <p class="idle-title">Ready to discover.</p>
        <p class="empty-hint">Type a query above to search for MCP tools and A2A agents across the public internet.</p>
      </div>
    }

    @if (!loading && !error && results.length > 0) {
      <ol class="result-list" aria-label="Discovery results">
        @for (result of results; track result.item.item_id) {
          <li>
            <app-result-card [result]="result"></app-result-card>
          </li>
        }
      </ol>
    }
  `,
  styles: [
    `
      .list-state {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 0.75rem;
        padding: 4rem 2rem;
        text-align: center;
        color: var(--color-text-muted);
        font: 400 1rem/1.5 var(--font-serif);
      }
      .list-state p {
        margin: 0;
      }
      .list-state--loading .spinner {
        display: inline-block;
        width: 32px;
        height: 32px;
        border: 3px solid var(--color-border);
        border-top-color: var(--color-accent);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }
      @keyframes spin {
        to { transform: rotate(360deg); }
      }
      .list-state--error {
        color: var(--color-red);
      }
      .error-icon {
        color: var(--color-red);
        opacity: 0.7;
      }
      .error-message {
        font-family: var(--font-mono);
        font-size: 0.9375rem;
        font-weight: 600;
      }
      .error-hint {
        font-size: 0.875rem;
        opacity: 0.75;
      }
      .list-state--empty svg {
        opacity: 0.3;
        color: var(--color-text-muted);
      }
      .list-state--idle {
        padding: 6rem 2rem;
      }
      .list-state--idle svg {
        opacity: 0.25;
        color: var(--color-text-muted);
      }
      .idle-title {
        font: 700 1.25rem/1.3 var(--font-serif);
        color: var(--color-text);
        margin: 0.5rem 0 0;
      }
      .empty-hint {
        font-size: 0.875rem;
        max-width: 40ch;
        opacity: 0.75;
      }
      .result-list {
        list-style: none;
        margin: 0;
        padding: 0;
        display: flex;
        flex-direction: column;
        gap: 1rem;
      }
    `,
  ],
})
export class ResultListComponent {
  @Input() results: SearchResultItem[] = [];
  @Input() loading = false;
  @Input() error: DiscoveryError | null = null;
  @Input() hasSearched = false;

  get errorMessage(): string {
    if (!this.error) return '';
    switch (this.error.kind) {
      case 'network':
        return 'Network error — backend unreachable.';
      case 'backend':
        return `Backend error (${this.error.status}).`;
      default:
        return this.error.message;
    }
  }
}
