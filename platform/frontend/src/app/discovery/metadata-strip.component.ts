import { Component, Input } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SearchMetadata, DiscoveryError } from './models';

@Component({
  selector: 'app-metadata-strip',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="metadata-strip" [class.metadata-strip--loading]="loading" aria-label="Search metadata">
      <!-- Mode chip -->
      <div class="mode-area">
        @if (loading) {
          <span class="mode-chip mode-chip--loading">Searching…</span>
        } @else if (metadata) {
          <span class="mode-chip" [class]="'mode-chip--' + metadata.mode">
            {{ modeLabel }}
          </span>
        }
      </div>

      <!-- Counts row -->
      @if (metadata && !loading) {
        <div class="counts-area">
          <span class="count-item">
            <strong>{{ metadata.cached_results }}</strong> cached
          </span>
          @if (metadata.live_candidates > 0) {
            <span class="count-sep">·</span>
            <span class="count-item">
              <strong>{{ metadata.live_candidates }}</strong> live candidates
            </span>
          }
          @if (metadata.approved_count > 0 || metadata.rejected_count > 0) {
            <span class="count-sep">·</span>
            <span class="count-item">
              <strong>{{ metadata.approved_count }}</strong> approved
            </span>
            <span class="count-sep">·</span>
            <span class="count-item">
              <strong>{{ metadata.rejected_count }}</strong> rejected
            </span>
          }
        </div>
      }

      <!-- Source grid -->
      @if (metadata && !loading) {
        <div class="sources-area">
          <span class="sources-label">Sources:</span>
          <div class="source-grid">
            @for (src of allSources; track src.name) {
              <span
                class="source-status"
                [class.source-status--success]="src.status === 'success'"
                [class.source-status--failed]="src.status === 'failed'"
                [class.source-status--pending]="src.status === 'pending'"
                [title]="src.name + ': ' + src.status"
              >
                @if (src.status === 'success') {
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" aria-hidden="true">
                    <polyline points="20 6 9 17 4 12"></polyline>
                  </svg>
                } @else if (src.status === 'failed') {
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" aria-hidden="true">
                    <line x1="18" y1="6" x2="6" y2="18"></line>
                    <line x1="6" y1="6" x2="18" y2="18"></line>
                  </svg>
                } @else {
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" aria-hidden="true">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="8" x2="12" y2="12"></line>
                    <line x1="12" y1="16" x2="12.01" y2="16"></line>
                  </svg>
                }
                <span>{{ src.displayName }}</span>
              </span>
            }
          </div>
        </div>
      }

      <!-- Fallback note -->
      @if (metadata?.plan?.used_fallback && !loading) {
        <p class="fallback-note">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <circle cx="12" cy="12" r="10"></circle>
            <line x1="12" y1="8" x2="12" y2="12"></line>
            <line x1="12" y1="16" x2="12.01" y2="16"></line>
          </svg>
          Used keyword fallback — Gemini unavailable or invalid.
          Expanded query: <em>{{ metadata?.plan?.expanded_query }}</em>
        </p>
      }
    </div>
  `,
  styles: [
    `
      .metadata-strip {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 0.75rem 1.5rem;
        padding: 0.875rem 1.125rem;
        border: 1px solid var(--color-border);
        border-radius: var(--radius-md);
        background: var(--color-surface);
        margin-bottom: 1.5rem;
        font-family: var(--font-mono);
        font-size: 0.75rem;
        line-height: 1;
      }
      .metadata-strip--loading {
        opacity: 0.65;
      }
      .mode-area {
        flex-shrink: 0;
      }
      .mode-chip {
        display: inline-block;
        padding: 0.25rem 0.625rem;
        border-radius: 999px;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        font-size: 0.6875rem;
      }
      .mode-chip--cached {
        background: #dbeafe;
        color: #1d4ed8;
      }
      .mode-chip--live {
        background: #dcfce7;
        color: #15803d;
      }
      .mode-chip--merged {
        background: #fef9c3;
        color: #a16207;
      }
      .mode-chip--loading {
        background: var(--color-border);
        color: var(--color-text-muted);
      }
      .counts-area {
        display: flex;
        align-items: center;
        gap: 0.375rem;
        flex-wrap: wrap;
        color: var(--color-text-muted);
      }
      .count-item strong {
        color: var(--color-text);
        font-weight: 700;
      }
      .count-sep {
        opacity: 0.4;
      }
      .sources-area {
        display: flex;
        align-items: center;
        gap: 0.625rem;
        margin-left: auto;
        flex-wrap: wrap;
      }
      .sources-label {
        color: var(--color-text-muted);
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        font-size: 0.6875rem;
      }
      .source-grid {
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
      }
      .source-status {
        display: inline-flex;
        align-items: center;
        gap: 0.3rem;
        padding: 0.2rem 0.5rem;
        border-radius: 999px;
        font-weight: 600;
        font-size: 0.6875rem;
      }
      .source-status--success {
        background: #dcfce7;
        color: #15803d;
      }
      .source-status--failed {
        background: #fee2e2;
        color: #b91c1c;
      }
      .source-status--pending {
        background: var(--color-border);
        color: var(--color-text-muted);
      }
      .fallback-note {
        width: 100%;
        margin: 0;
        padding: 0.5rem 0.875rem;
        border-top: 1px solid var(--color-border);
        background: #fef9c3;
        border-radius: 0 0 var(--radius-md) var(--radius-md);
        color: #92400e;
        font-size: 0.75rem;
        display: flex;
        align-items: flex-start;
        gap: 0.4rem;
        margin-top: -0.25rem;
      }
      .fallback-note em {
        font-style: normal;
        font-weight: 600;
      }
    `,
  ],
})
export class MetadataStripComponent {
  @Input() metadata: SearchMetadata | null | undefined = null;
  @Input() error: DiscoveryError | null = null;
  @Input() loading = false;

  get modeLabel(): string {
    if (!this.metadata) return '';
    const labels: Record<string, string> = {
      cached: 'Cached',
      live: 'Live',
      merged: 'Cached + Live',
    };
    return labels[this.metadata.mode] ?? this.metadata.mode;
  }

  get allSources(): Array<{ name: string; displayName: string; status: 'success' | 'failed' | 'pending' }> {
    if (!this.metadata) return [];

    const succeeded = new Set(this.metadata.sources_succeeded);
    const failed = new Set(this.metadata.sources_failed);

    return this.metadata.sources_attempted.map((name) => {
      const displayName = this.displayName(name);
      return {
        name,
        displayName,
        status: succeeded.has(name)
          ? 'success'
          : failed.has(name)
            ? 'failed'
            : 'pending',
      };
    });
  }

  private displayName(name: string): string {
    const map: Record<string, string> = {
      arcadedb: 'ArcadeDB',
      github: 'GitHub',
      mcp_registry: 'MCP Registry',
      a2a_registry: 'A2A Registry',
      web_search: 'Web Search',
      web_extraction: 'Web Extraction',
      well_known: 'Well-Known',
    };
    return map[name] ?? name;
  }
}
