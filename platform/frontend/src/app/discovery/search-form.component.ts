import {
  Component,
  EventEmitter,
  Input,
  OnInit,
  Output,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { PreferredType } from './models';

export interface SearchFormSubmit {
  q: string;
  type: PreferredType;
}

@Component({
  selector: 'app-search-form',
  standalone: true,
  imports: [CommonModule],
  template: `
    <form class="search-form" (submit)="onSubmit($event)">
      <div class="input-row">
        <input
          class="search-input"
          type="text"
          name="q"
          placeholder="Search for MCP tools, A2A agents, capabilities…"
          [value]="q()"
          (input)="onInput($event)"
          [disabled]="disabled"
          autocomplete="off"
          autocorrect="off"
          spellcheck="false"
          aria-label="Discovery search query"
        />
        <button
          class="search-btn"
          type="submit"
          [disabled]="disabled || !q().trim()"
          aria-label="Search"
        >
          @if (disabled) {
            <span class="spinner" aria-hidden="true"></span>
          } @else {
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <circle cx="11" cy="11" r="8"></circle>
              <line x1="21" y1="21" x2="16.65" y2="16.65"></line>
            </svg>
          }
        </button>
      </div>

      <div class="filter-row">
        <label class="filter-label">Type:</label>
        <div class="type-toggle" role="group" aria-label="Filter by item type">
          @for (opt of typeOptions; track opt.value) {
            <button
              class="type-btn"
              type="button"
              [class.active]="type() === opt.value"
              [disabled]="disabled"
              (click)="onType(opt.value)"
            >
              {{ opt.label }}
            </button>
          }
        </div>
      </div>
    </form>
  `,
  styles: [
    `
      .search-form {
        margin-bottom: 2rem;
      }
      .input-row {
        display: flex;
        gap: 0.75rem;
        align-items: center;
      }
      .search-input {
        flex: 1;
        padding: 0.75rem 1rem;
        border: 2px solid var(--color-border);
        border-radius: var(--radius-md);
        background: var(--color-surface);
        color: var(--color-text);
        font: 400 1rem/1.5 var(--font-serif);
        outline: none;
        transition: border-color 0.15s ease;
      }
      .search-input::placeholder {
        color: var(--color-text-muted);
      }
      .search-input:focus {
        border-color: var(--color-accent);
      }
      .search-input:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }
      .search-btn {
        flex-shrink: 0;
        width: 44px;
        height: 44px;
        border: 2px solid var(--color-accent);
        border-radius: var(--radius-md);
        background: var(--color-accent);
        color: #fff;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: opacity 0.15s ease, transform 0.1s ease;
      }
      .search-btn:hover:not(:disabled) {
        opacity: 0.85;
      }
      .search-btn:active:not(:disabled) {
        transform: scale(0.95);
      }
      .search-btn:disabled {
        opacity: 0.4;
        cursor: not-allowed;
      }
      .spinner {
        display: inline-block;
        width: 16px;
        height: 16px;
        border: 2px solid rgba(255,255,255,0.4);
        border-top-color: #fff;
        border-radius: 50%;
        animation: spin 0.7s linear infinite;
      }
      @keyframes spin {
        to { transform: rotate(360deg); }
      }
      .filter-row {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        margin-top: 0.75rem;
      }
      .filter-label {
        font: 700 0.75rem/1 var(--font-mono);
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--color-text-muted);
      }
      .type-toggle {
        display: flex;
        gap: 0.375rem;
      }
      .type-btn {
        padding: 0.3rem 0.875rem;
        border: 1.5px solid var(--color-border);
        border-radius: 999px;
        background: transparent;
        color: var(--color-text-muted);
        font: 600 0.8125rem/1 var(--font-mono);
        cursor: pointer;
        transition: background 0.12s, color 0.12s, border-color 0.12s;
      }
      .type-btn:hover:not(:disabled) {
        border-color: var(--color-accent);
        color: var(--color-accent);
      }
      .type-btn.active {
        background: var(--color-accent);
        border-color: var(--color-accent);
        color: #fff;
      }
      .type-btn:disabled {
        opacity: 0.45;
        cursor: not-allowed;
      }
    `,
  ],
})
export class SearchFormComponent implements OnInit {
  @Input() disabled = false;
  @Input() initialValue = '';
  @Input() initialType: PreferredType = 'all';

  @Output() submitted = new EventEmitter<SearchFormSubmit>();

  readonly q = signal('');
  readonly type = signal<PreferredType>('all');

  readonly typeOptions: Array<{ value: PreferredType; label: string }> = [
    { value: 'all', label: 'All' },
    { value: 'tool', label: 'Tools' },
    { value: 'agent', label: 'Agents' },
  ];

  ngOnInit(): void {
    this.q.set(this.initialValue);
    this.type.set(this.initialType);
  }

  onInput(event: Event): void {
    this.q.set((event.target as HTMLInputElement).value);
  }

  onType(value: PreferredType): void {
    this.type.set(value);
  }

  onSubmit(event: Event): void {
    event.preventDefault();
    const trimmed = this.q().trim();
    if (!trimmed || this.disabled) return;
    this.submitted.emit({ q: trimmed, type: this.type() });
  }
}
