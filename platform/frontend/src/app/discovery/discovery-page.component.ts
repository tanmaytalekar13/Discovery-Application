import { Component, signal } from '@angular/core';
import { CommonModule } from '@angular/common';
import { DiscoveryService, isError } from './discovery.service';
import {
  SearchResponse,
  DiscoveryError,
  PreferredType,
} from './models';
import { SearchFormComponent, SearchFormSubmit } from './search-form.component';
import { ResultListComponent } from './result-list.component';
import { MetadataStripComponent } from './metadata-strip.component';

@Component({
  selector: 'app-discovery-page',
  standalone: true,
  imports: [
    CommonModule,
    SearchFormComponent,
    ResultListComponent,
    MetadataStripComponent,
  ],
  template: `
    <div class="discovery-page">
      <header class="page-header">
        <p class="eyebrow">Agentic Discovery Platform</p>
        <h1>Find the right agentic building block.</h1>
        <p class="subtitle">
          Search MCP tools and A2A agents across the public internet.
        </p>
      </header>

      <app-search-form
        [disabled]="loading()"
        [initialValue]="query()"
        [initialType]="type()"
        (submitted)="onSearch($event)"
      ></app-search-form>

      @if (response() || error() || loading()) {
        <app-metadata-strip
          [metadata]="response()?.metadata"
          [error]="error()"
          [loading]="loading()"
        ></app-metadata-strip>
      }

      <app-result-list
        [results]="response()?.results || []"
        [loading]="loading()"
        [error]="error()"
        [hasSearched]="hasSearched()"
      ></app-result-list>
    </div>
  `,
  styles: [
    `
      .discovery-page {
        max-width: 960px;
        margin: 0 auto;
        padding: 8vh 2rem 4rem;
      }
      .page-header {
        margin-bottom: 3rem;
      }
      .eyebrow {
        color: var(--color-accent);
        font: 700 0.75rem/1.2 var(--font-mono);
        letter-spacing: 0.08em;
        text-transform: uppercase;
        margin: 0 0 1rem;
      }
      h1 {
        max-width: 14ch;
        color: var(--color-text);
        font: 700 clamp(2.5rem, 6vw, 4.5rem)/0.95 var(--font-serif);
        margin: 0 0 1rem;
      }
      .subtitle {
        color: var(--color-text-muted);
        font: 400 1.125rem/1.5 var(--font-serif);
        margin: 0;
      }
    `,
  ],
})
export class DiscoveryPageComponent {
  readonly query = signal('');
  readonly type = signal<PreferredType>('all');
  readonly loading = signal(false);
  readonly hasSearched = signal(false);
  readonly response = signal<SearchResponse | null>(null);
  readonly error = signal<DiscoveryError | null>(null);

  constructor(private readonly service: DiscoveryService) {}

  onSearch(submit: SearchFormSubmit): void {
    this.query.set(submit.q);
    this.type.set(submit.type);
    this.runSearch(submit.q, submit.type);
  }

  private runSearch(q: string, type: PreferredType): void {
    this.loading.set(true);
    this.error.set(null);
    this.hasSearched.set(true);

    this.service.search(q, type).subscribe({
      next: (result) => {
        if (isError(result)) {
          this.error.set(result);
          this.response.set(null);
        } else {
          this.response.set(result);
          this.error.set(null);
        }
        this.loading.set(false);
      },
    });
  }
}