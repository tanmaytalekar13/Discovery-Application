import { Component, Input, Output, EventEmitter, computed } from '@angular/core';
import { CommonModule } from '@angular/common';
import { SearchResultItem, ClassificationMode } from './models';

@Component({
  selector: 'app-result-card',
  standalone: true,
  imports: [CommonModule],
  template: `
    <article class="result-card" [attr.aria-label]="result.item.name">
      <div class="card-top">
        <div class="card-identity">
          <span class="protocol-chip" [class]="'protocol-chip--' + result.item.type">
            {{ result.item.type === 'tool' ? 'MCP' : 'A2A' }}
          </span>
          <h2 class="card-name">{{ result.item.name }}</h2>
        </div>
        <div class="card-score" [title]="scoreBreakdown">
          <span class="score-value">{{ (result.final_score * 100).toFixed(0) }}</span>
          <span class="score-unit">/100</span>
        </div>
      </div>

      <p class="card-description">{{ result.item.description }}</p>

      <div class="card-metrics">
        <!-- Reliability bar -->
        <div class="metric" title="Reliability: {{ (result.reliability * 100).toFixed(0) }}/100">
          <span class="metric-label">Reliability</span>
          <div class="reliability-bar-track">
            <div
              class="reliability-bar-fill"
              [class]="reliabilityClass"
              [style.width.%]="result.reliability * 100"
            ></div>
          </div>
          <span class="metric-value">{{ (result.reliability * 100).toFixed(0) }}</span>
        </div>

        <!-- Freshness -->
        <div class="metric" title="Freshness">
          <span class="metric-label">Freshness</span>
          <span class="metric-value freshness-value" [class]="freshnessClass">
            {{ freshnessLabel }}
          </span>
        </div>

        <!-- Evidence -->
        <div class="metric" title="Evidence: {{ (result.evidence * 100).toFixed(0) }}/100">
          <span class="metric-label">Evidence</span>
          <span class="metric-value">{{ (result.evidence * 100).toFixed(0) }}</span>
        </div>
      </div>

      <!-- Source badges -->
      @if (dedupedSources.length > 0) {
        <div class="source-badges" aria-label="Discovery sources">
          @for (src of dedupedSources; track src.id) {
            <span class="source-badge" [title]="src.provider || src.id">
              {{ sourceLabel(src.type) }}
            </span>
          }
        </div>
      }

      <!-- Server / endpoint info for tools -->
      @if (result.item.tool) {
        <p class="meta-detail">
          <span class="meta-key">Server</span>
          <code class="meta-value">{{ result.item.tool.server_id }}</code>
          <span class="meta-sep">·</span>
          <span class="meta-key">Tool</span>
          <code class="meta-value">{{ result.item.tool.tool_name }}</code>
        </p>
      }

      <!-- Endpoint / skills info for agents -->
      @if (result.item.agent) {
        <p class="meta-detail">
          <span class="meta-key">Endpoint</span>
          <code class="meta-value">{{ shortEndpoint }}</code>
        </p>
        @if (result.item.agent.skills.length > 0) {
          <div class="skills-list">
            @for (skill of result.item.agent.skills.slice(0, 5); track skill) {
              <span class="skill-chip">{{ skill }}</span>
            }
            @if (result.item.agent.skills.length > 5) {
              <span class="skill-chip skill-chip--more"
                >+{{ result.item.agent.skills.length - 5 }}</span
              >
            }
          </div>
        }
      }

      <!-- Action buttons -->
      <div class="card-actions">
        <button class="action-btn" (click)="onViewCode($event)">View Code</button>
        @if (result.item.type === 'tool') {
          @switch (testActionMode) {
            @case ('test') {
              <button class="action-btn action-btn--primary" (click)="onTestTool($event)">
                Test Tool
              </button>
            }
            @case ('run-locally') {
              <button
                class="action-btn action-btn--local"
                (click)="onTestTool($event)"
                title="This is a local stdio tool. Click for install instructions."
              >
                Run Locally
              </button>
            }
            @case ('test-source') {
              <button
                class="action-btn action-btn--local"
                (click)="onTestTool($event)"
                title="This tool's source is on GitHub — click to inspect the repo and run it in a sandbox."
              >
                Test Tool
              </button>
            }
            @case ('hidden') {
              <!-- no test button -->
            }
          }
        }
        @if (result.item.type === 'agent') {
          <button class="action-btn" disabled title="Available in Phase 16">Test Agent</button>
        }
      </div>
    </article>
  `,
  styles: [
    `
      .result-card {
        border: 1.5px solid var(--color-border);
        border-radius: var(--radius-lg);
        padding: 1.25rem 1.5rem;
        background: var(--color-surface);
        display: flex;
        flex-direction: column;
        gap: 0.875rem;
        transition:
          box-shadow 0.15s ease,
          border-color 0.15s ease;
      }
      .result-card:hover {
        border-color: var(--color-accent);
        box-shadow: var(--shadow-md);
      }
      .card-top {
        display: flex;
        justify-content: space-between;
        align-items: flex-start;
        gap: 1rem;
      }
      .card-identity {
        display: flex;
        align-items: center;
        gap: 0.625rem;
        flex-wrap: wrap;
      }
      .protocol-chip {
        flex-shrink: 0;
        display: inline-block;
        padding: 0.2rem 0.55rem;
        border-radius: 999px;
        font: 700 0.6875rem/1 var(--font-mono);
        letter-spacing: 0.05em;
        text-transform: uppercase;
      }
      .protocol-chip--tool {
        background: #dbeafe;
        color: #1d4ed8;
      }
      .protocol-chip--agent {
        background: #fce7f3;
        color: #9d174d;
      }
      .card-name {
        font: 700 1.125rem/1.2 var(--font-serif);
        color: var(--color-text);
        margin: 0;
      }
      .card-score {
        flex-shrink: 0;
        text-align: right;
        font-family: var(--font-mono);
        line-height: 1;
        cursor: default;
      }
      .score-value {
        font-size: 1.375rem;
        font-weight: 700;
        color: var(--color-accent);
      }
      .score-unit {
        font-size: 0.75rem;
        color: var(--color-text-muted);
      }
      .card-description {
        margin: 0;
        color: var(--color-text-muted);
        font: 400 0.9375rem/1.5 var(--font-serif);
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
      }
      .card-metrics {
        display: flex;
        gap: 1.5rem;
        flex-wrap: wrap;
        align-items: center;
      }
      .metric {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        font-family: var(--font-mono);
        font-size: 0.75rem;
      }
      .metric-label {
        color: var(--color-text-muted);
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .metric-value {
        font-weight: 700;
        color: var(--color-text);
      }
      .reliability-bar-track {
        width: 80px;
        height: 6px;
        background: var(--color-border);
        border-radius: 999px;
        overflow: hidden;
      }
      .reliability-bar-fill {
        height: 100%;
        border-radius: 999px;
        transition: width 0.3s ease;
      }
      .reliability-bar-fill--low {
        background: var(--color-red);
      }
      .reliability-bar-fill--mid {
        background: var(--color-amber);
      }
      .reliability-bar-fill--high {
        background: var(--color-green);
      }
      .freshness-value--fresh {
        color: var(--color-green);
      }
      .freshness-value--aging {
        color: var(--color-amber);
      }
      .freshness-value--stale {
        color: var(--color-red);
      }
      .source-badges {
        display: flex;
        flex-wrap: wrap;
        gap: 0.375rem;
      }
      .source-badge {
        display: inline-block;
        padding: 0.175rem 0.625rem;
        border: 1px solid var(--color-border);
        border-radius: 999px;
        background: var(--color-bg);
        font: 600 0.6875rem/1 var(--font-mono);
        color: var(--color-text-muted);
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .meta-detail {
        display: flex;
        align-items: center;
        gap: 0.375rem;
        margin: 0;
        font-size: 0.8125rem;
        flex-wrap: wrap;
      }
      .meta-key {
        font-family: var(--font-mono);
        font-weight: 700;
        font-size: 0.6875rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--color-text-muted);
      }
      .meta-value {
        font-family: var(--font-mono);
        font-size: 0.8125rem;
        color: var(--color-text);
        background: var(--color-bg);
        padding: 0.1rem 0.35rem;
        border-radius: 4px;
      }
      .meta-sep {
        color: var(--color-border);
      }
      .skills-list {
        display: flex;
        flex-wrap: wrap;
        gap: 0.375rem;
      }
      .skill-chip {
        display: inline-block;
        padding: 0.15rem 0.625rem;
        border-radius: 999px;
        background: #ede9fe;
        color: #5b21b6;
        font: 600 0.6875rem/1 var(--font-mono);
      }
      .skill-chip--more {
        background: var(--color-border);
        color: var(--color-text-muted);
      }
      .card-actions {
        display: flex;
        gap: 0.625rem;
        flex-wrap: wrap;
        border-top: 1px solid var(--color-border);
        padding-top: 0.875rem;
        margin-top: 0.25rem;
      }
      .action-btn {
        padding: 0.4rem 1rem;
        border: 1.5px solid var(--color-border);
        border-radius: var(--radius-md);
        background: transparent;
        color: var(--color-text-muted);
        font: 600 0.8125rem/1 var(--font-mono);
        cursor: pointer;
        opacity: 1;
        transition:
          border-color 0.15s,
          color 0.15s,
          background 0.15s;
      }
      .action-btn:hover:not(:disabled) {
        border-color: var(--color-accent);
        color: var(--color-accent);
        background: var(--color-bg);
      }
      .action-btn:disabled {
        cursor: not-allowed;
        opacity: 0.55;
      }
      .action-btn--primary {
        background: var(--color-accent);
        color: white;
        border-color: var(--color-accent);
      }
      .action-btn--primary:hover:not(:disabled) {
        background: var(--color-accent);
        color: white;
        border-color: var(--color-accent);
        filter: brightness(1.1);
      }
      .action-btn--local {
        background: #fef3c7;
        color: #92400e;
        border-color: #fde68a;
      }
      .action-btn--local:hover:not(:disabled) {
        background: #fef3c7;
        color: #92400e;
        border-color: #f59e0b;
      }
    `,
  ],
})
export class ResultCardComponent {
  @Input({ required: true }) result!: SearchResultItem;
  @Output() viewCode = new EventEmitter<SearchResultItem>();
  @Output() testTool = new EventEmitter<SearchResultItem>();

  onViewCode(event: MouseEvent): void {
    event.stopPropagation();
    this.viewCode.emit(this.result);
  }

  onTestTool(event: MouseEvent): void {
    event.stopPropagation();
    this.testTool.emit(this.result);
  }

  /** Determines which test button variant to show based on classification.mode. */
  // Getter update — teesra case add karo:
  get testActionMode(): 'test' | 'run-locally' | 'test-source' | 'hidden' {
    const cls = this.result.classification;
    if (!cls) return 'hidden';
    if (cls.mode === 'remote' || cls.mode === 'remote_via_package') return 'test';
    if (cls.mode === 'local_stdio') return 'run-locally';
    if (cls.mode === 'local_source') return 'test-source'; // NEW
    return 'hidden'; // not_testable
  }

  get reliabilityClass(): string {
    const s = this.result.reliability;
    if (s >= 0.75) return 'reliability-bar-fill--high';
    if (s >= 0.5) return 'reliability-bar-fill--mid';
    return 'reliability-bar-fill--low';
  }

  get freshnessLabel(): string {
    const score = this.result.freshness;
    if (score >= 0.8) return 'Fresh';
    if (score >= 0.4) return 'Aging';
    return 'Stale';
  }

  get freshnessClass(): string {
    const score = this.result.freshness;
    if (score >= 0.8) return 'freshness-value--fresh';
    if (score >= 0.4) return 'freshness-value--aging';
    return 'freshness-value--stale';
  }

  get scoreBreakdown(): string {
    return (
      `Final: ${(this.result.final_score * 100).toFixed(1)} | ` +
      `Relevance: ${(this.result.relevance * 100).toFixed(1)} | ` +
      `Reliability: ${(this.result.reliability * 100).toFixed(1)} | ` +
      `Freshness: ${(this.result.freshness * 100).toFixed(1)} | ` +
      `Evidence: ${(this.result.evidence * 100).toFixed(1)}`
    );
  }

  get dedupedSources() {
    const seen = new Set<string>();
    return this.result.item.provenance.filter((s) => {
      const key = `${s.type}:${s.id}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  get shortEndpoint(): string {
    const ep = this.result.item.agent?.endpoint ?? '';
    try {
      const u = new URL(ep);
      return u.hostname + (u.port ? `:${u.port}` : '') + u.pathname;
    } catch {
      return ep;
    }
  }

  sourceLabel(type: string): string {
    const labels: Record<string, string> = {
      mcp_registry: 'MCP Registry',
      a2a_catalog: 'A2A Catalog',
      well_known: 'Well-Known',
      configured: 'Configured',
      github: 'GitHub',
      web_search: 'Web Search',
      web_page: 'Web Page',
    };
    return labels[type] ?? type;
  }
}
