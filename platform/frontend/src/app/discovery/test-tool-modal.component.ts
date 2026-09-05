import {
  Component,
  Input,
  Output,
  EventEmitter,
  signal,
  computed,
  OnInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { DiscoveryService } from './discovery.service';
import {
  SearchResultItem,
  ClassificationResult,
  RemoteCandidate,
  LocalPackageHint,
  ToolConnectResponse,
  ToolInfo,
  ToolInvokeResponse,
} from './models';

// ---------------------------------------------------------------------------
// Modal states
// ---------------------------------------------------------------------------
type ModalState =
  | 'idle'          // Initial loading
  | 'connecting'    // POST /connect in-flight
  | 'auth-required' // 401/403 received — offer OAuth or manual token
  | 'tools-ready'   // Connected, showing tool list
  | 'tool-form'    // Tool selected — show dynamic input form
  | 'invoking'     // POST /invoke in-flight
  | 'result'       // Invoke result shown
  | 'error'        // Connection error
  | 'local-stdio'; // local_stdio mode — show install instructions

@Component({
  selector: 'app-test-tool-modal',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="modal-backdrop" (click)="onBackdropClick($event)">
      <div class="modal" role="dialog" aria-modal="true" [attr.aria-label]="'Test tool: ' + result.item.name">

        <!-- Header -->
        <header class="modal-header">
          <div class="modal-title-row">
            <span class="protocol-chip">MCP</span>
            <h2 class="modal-title">{{ result.item.name }}</h2>
          </div>
          <button class="close-btn" (click)="close.emit()" aria-label="Close">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </header>

        <!-- Transport badge -->
        @if (remoteCandidate()) {
          <div class="transport-badge">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M5 12.55a11 11 0 0 1 14.08 0"></path>
              <path d="M1.42 9a16 16 0 0 1 21.16 0"></path>
              <path d="M8.53 16.11a6 6 0 0 1 6.95 0"></path>
              <line x1="12" y1="20" x2="12.01" y2="20"></line>
            </svg>
            <span>{{ remoteCandidate()?.type === 'streamable-http' ? 'streamable-http' : 'SSE' }}</span>
            <code class="transport-url">{{ shortUrl(remoteCandidate()?.url ?? '') }}</code>
          </div>
        }

        <!-- Body -->
        <div class="modal-body">

          <!-- Disclaimer banner -->
          <div class="disclaimer-banner">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
              <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
            </svg>
            <span>Credentials are used only for this test session and are never stored permanently.</span>
          </div>

          <!-- idle: starting -->
          @if (state() === 'idle') {
            <div class="state-container">
              <span class="spinner"></span>
              <p>Connecting to MCP server…</p>
            </div>
          }

          <!-- connecting -->
          @if (state() === 'connecting') {
            <div class="state-container">
              <span class="spinner"></span>
              <p>Connecting to MCP server…</p>
            </div>
          }

          <!-- error -->
          @if (state() === 'error') {
            <div class="state-container state-container--error">
              <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="8" x2="12" y2="12"></line>
                <line x1="12" y1="16" x2="12.01" y2="16"></line>
              </svg>
              <p class="error-title">Connection failed</p>
              <p class="error-message">{{ errorMessage() }}</p>
              <button class="btn btn--secondary" (click)="retryConnect()">Try Again</button>
            </div>
          }

          <!-- auth-required -->
          @if (state() === 'auth-required') {
            <div class="state-container">
              <div class="auth-icon">
                <svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                  <rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>
                  <path d="M7 11V7a5 5 0 0 1 10 0v4"></path>
                </svg>
              </div>
              <p class="auth-title">Authentication required</p>
              <p class="auth-desc">
                This MCP server requires credentials.
                Provide your API key or bearer token to continue.
              </p>

              <!-- Token input -->
              <div class="token-form">
                <div class="form-field">
                  <label class="form-label" for="token-input">Bearer Token / API Key</label>
                  <div class="token-input-row">
                    <input
                      id="token-input"
                      class="form-input"
                      [type]="showToken() ? 'text' : 'password'"
                      [(ngModel)]="tokenInput"
                      placeholder="sk-... or Bearer ..."
                      autocomplete="off"
                      spellcheck="false"
                    />
                    <button class="toggle-visibility-btn" (click)="showToken.set(!showToken())" type="button" [attr.aria-label]="showToken() ? 'Hide token' : 'Show token'">
                      @if (showToken()) {
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                          <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"></path>
                          <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"></path>
                          <line x1="1" y1="1" x2="23" y2="23"></line>
                        </svg>
                      } @else {
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                          <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
                          <circle cx="12" cy="12" r="3"></circle>
                        </svg>
                      }
                    </button>
                  </div>
                </div>

                @if (tokenSubmitError()) {
                  <p class="form-error">{{ tokenSubmitError() }}</p>
                }

                <button
                  class="btn btn--primary"
                  (click)="submitToken()"
                  [disabled]="!tokenInput || tokenSubmitting()"
                >
                  @if (tokenSubmitting()) {
                    <span class="btn-spinner"></span> Connecting…
                  } @else {
                    Connect with Token
                  }
                </button>
              </div>

              <button class="btn btn--ghost" (click)="retryConnect()">Use without token</button>
            </div>
          }

          <!-- tools-ready -->
          @if (state() === 'tools-ready') {
            <div class="tools-panel">
              <p class="tools-count">{{ tools().length }} tool{{ tools().length !== 1 ? 's' : '' }} available</p>
              <div class="tools-list">
                @for (tool of tools(); track tool.name) {
                  <button class="tool-card" (click)="selectTool(tool)">
                    <div class="tool-card-header">
                      <span class="tool-name">{{ tool.name }}</span>
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <polyline points="9 18 15 12 9 6"></polyline>
                      </svg>
                    </div>
                    @if (tool.description) {
                      <p class="tool-desc">{{ tool.description }}</p>
                    }
                  </button>
                }
              </div>
            </div>
          }

          <!-- tool-form -->
          @if (state() === 'tool-form') {
            <div class="tool-form-panel">
              <button class="back-btn" (click)="backToTools()">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="15 18 9 12 15 6"></polyline>
                </svg>
                Back to tools
              </button>

              <div class="tool-form-header">
                <h3 class="tool-form-name">{{ selectedTool()?.name }}</h3>
                @if (selectedTool()?.description) {
                  <p class="tool-form-desc">{{ selectedTool()?.description }}</p>
                }
              </div>

              <!-- Dynamic form -->
              @if (formFields().length > 0) {
                <form class="dynamic-form" (ngSubmit)="invokeTool()">
                  @for (field of formFields(); track field.name) {
                    <div class="form-field">
                      <label class="form-label" [for]="'field-' + field.name">
                        {{ field.label }}
                        @if (!field.required) {
                          <span class="form-label-optional">(optional)</span>
                        }
                      </label>

                      @if (field.type === 'boolean') {
                        <label class="toggle-label">
                          <input
                            type="checkbox"
                            [id]="'field-' + field.name"
                            [(ngModel)]="formValues[field.name]"
                            [name]="field.name"
                          />
                          <span class="toggle-switch"></span>
                        </label>
                      }

                      @if (field.type === 'string' || field.type === 'number' || field.type === 'integer') {
                        <input
                          class="form-input"
                          [id]="'field-' + field.name"
                          [type]="field.type === 'number' || field.type === 'integer' ? 'number' : 'text'"
                          [(ngModel)]="formValues[field.name]"
                          [name]="field.name"
                          [placeholder]="field.placeholder || ''"
                        />
                      }

                      @if (field.type === 'array' && field.itemsEnum) {
                        <select class="form-input" [id]="'field-' + field.name" [(ngModel)]="formValues[field.name]" [name]="field.name">
                          <option value="">Select…</option>
                          @for (opt of field.itemsEnum; track opt) {
                            <option [value]="opt">{{ opt }}</option>
                          }
                        </select>
                      }

                      @if (field.type === 'array' && !field.itemsEnum) {
                        <input
                          class="form-input"
                          [id]="'field-' + field.name"
                          type="text"
                          [(ngModel)]="formValues[field.name]"
                          [name]="field.name"
                          placeholder="Comma-separated values"
                        />
                      }

                      @if (field.description) {
                        <p class="form-hint">{{ field.description }}</p>
                      }
                    </div>
                  }

                  @if (invokeError()) {
                    <p class="form-error">{{ invokeError() }}</p>
                  }

                  <div class="form-actions">
                    <button type="submit" class="btn btn--primary" [disabled]="invoking()">
                      @if (invoking()) {
                        <span class="btn-spinner"></span> Invoking…
                      } @else {
                        Invoke {{ selectedTool()?.name }}
                      }
                    </button>
                  </div>
                </form>
              }

              @if (formFields().length === 0) {
                <form class="dynamic-form" (ngSubmit)="invokeTool()">
                  <p class="no-params-note">This tool takes no parameters.</p>
                  <div class="form-actions">
                    <button type="submit" class="btn btn--primary" [disabled]="invoking()">
                      @if (invoking()) {
                        <span class="btn-spinner"></span> Invoking…
                      } @else {
                        Invoke {{ selectedTool()?.name }}
                      }
                    </button>
                  </div>
                </form>
              }
            </div>
          }

          <!-- invoking -->
          @if (state() === 'invoking') {
            <div class="state-container">
              <span class="spinner"></span>
              <p>Invoking <strong>{{ selectedTool()?.name }}</strong>…</p>
            </div>
          }

          <!-- result -->
          @if (state() === 'result') {
            <div class="result-panel">
              <div class="result-header">
                <div class="result-status" [class]="resultStatusClass()">
                  @if (invokeResult()?.status === 'success') {
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                      <polyline points="20 6 9 17 4 12"></polyline>
                    </svg>
                    <span>Success</span>
                  } @else {
                    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                      <circle cx="12" cy="12" r="10"></circle>
                      <line x1="12" y1="8" x2="12" y2="12"></line>
                      <line x1="12" y1="16" x2="12.01" y2="16"></line>
                    </svg>
                    <span>Error</span>
                  }
                </div>
                @if (invokeResult()?.duration_ms) {
                  <span class="result-duration">{{ invokeResult()?.duration_ms }}ms</span>
                }
              </div>

              @if (invokeResult()?.result) {
                <div class="result-body">
                  <pre class="result-json">{{ formatResult(invokeResult()?.result) }}</pre>
                </div>
              }

              @if (invokeResult()?.error) {
                <div class="result-error-body">
                  <p class="result-error-text">{{ invokeResult()?.error }}</p>
                </div>
              }

              @if (invokeResult()?.requires_auth) {
                <div class="auth-retry-banner">
                  <p>This tool requires authentication. Provide your token and try again.</p>
                  <button class="btn btn--secondary" (click)="goToAuth()">Provide Token</button>
                </div>
              }

              <div class="result-actions">
                <button class="btn btn--secondary" (click)="backToTools()">Try Another Tool</button>
                <button class="btn btn--ghost" (click)="close.emit()">Close</button>
              </div>
            </div>
          }

          <!-- local-stdio: install instructions -->
          @if (state() === 'local-stdio') {
            <div class="stdio-panel">
              <div class="stdio-icon">
                <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2">
                  <polyline points="4 17 10 11 4 5"></polyline>
                  <line x1="12" y1="19" x2="20" y2="19"></line>
                </svg>
              </div>
              <p class="stdio-title">Run this tool locally</p>
              <p class="stdio-desc">
                This is a local <code>stdio</code> tool. Live testing requires
                running the MCP server on your machine.
              </p>

              @if (localHint()?.installCommand) {
                <div class="install-block">
                  <p class="install-label">Install command:</p>
                  <pre class="install-cmd">{{ localHint()?.installCommand }}</pre>
                </div>
              }

              @if (localHint()?.environmentVariables?.length) {
                <div class="env-block">
                  <p class="env-label">Environment variables needed:</p>
                  <ul class="env-list">
                    @for (env of localHint()?.environmentVariables; track env.name) {
                      <li>
                        <code>{{ env.name }}</code>
                        @if (env.isSecret) {
                          <span class="env-secret-chip">secret</span>
                        }
                        @if (env.description) {
                          <span class="env-desc"> — {{ env.description }}</span>
                        }
                      </li>
                    }
                  </ul>
                </div>
              }

              <p class="stdio-note">
                Sandbox execution for local tools is planned for a future update.
              </p>
            </div>
          }

        </div>

        <!-- Footer: disconnect when session is active -->
        @if (sessionId() && state() !== 'idle' && state() !== 'connecting') {
          <footer class="modal-footer">
            <span class="session-info">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <circle cx="12" cy="12" r="10"></circle>
                <polyline points="12 6 12 12 16 14"></polyline>
              </svg>
              Test session active
            </span>
            <button class="disconnect-btn" (click)="disconnect()">Disconnect</button>
          </footer>
        }
      </div>
    </div>
  `,
  styles: [
    `
      .modal-backdrop {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.65);
        backdrop-filter: blur(4px);
        z-index: 1000;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 1.5rem;
      }
      .modal {
        background: var(--color-surface);
        border-radius: var(--radius-xl);
        box-shadow: var(--shadow-xl);
        width: 100%;
        max-width: 580px;
        max-height: 88vh;
        display: flex;
        flex-direction: column;
        overflow: hidden;
      }
      .modal-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 1rem 1.25rem;
        border-bottom: 1px solid var(--color-border);
        flex-shrink: 0;
        gap: 0.75rem;
      }
      .modal-title-row {
        display: flex;
        align-items: center;
        gap: 0.625rem;
        min-width: 0;
      }
      .protocol-chip {
        flex-shrink: 0;
        background: #3b82f6;
        color: white;
        font: 700 0.6rem/1.2 var(--font-mono);
        letter-spacing: 0.06em;
        padding: 0.2rem 0.45rem;
        border-radius: 4px;
      }
      .modal-title {
        font: 700 1rem/1.2 var(--font-serif);
        color: var(--color-text);
        margin: 0;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .close-btn {
        flex-shrink: 0;
        background: none;
        border: none;
        padding: 0.375rem;
        cursor: pointer;
        color: var(--color-text-muted);
        border-radius: 6px;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: background 0.15s, color 0.15s;
      }
      .close-btn:hover {
        background: var(--color-border);
        color: var(--color-text);
      }
      .transport-badge {
        display: flex;
        align-items: center;
        gap: 0.375rem;
        padding: 0.375rem 1.25rem;
        background: var(--color-ground);
        border-bottom: 1px solid var(--color-border);
        font: 500 0.6875rem/1.2 var(--font-mono);
        color: var(--color-text-muted);
        flex-shrink: 0;
      }
      .transport-badge span {
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--color-accent);
      }
      .transport-url {
        color: var(--color-text);
        font-size: 0.6875rem;
        max-width: 280px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .modal-body {
        flex: 1;
        overflow-y: auto;
        padding: 1.25rem;
        display: flex;
        flex-direction: column;
        gap: 1rem;
      }
      .disclaimer-banner {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.5rem 0.75rem;
        background: #fffbeb;
        border: 1px solid #fde68a;
        border-radius: 6px;
        font: 500 0.75rem/1.4 var(--font-mono);
        color: #92400e;
      }
      @media (prefers-color-scheme: dark) {
        .disclaimer-banner {
          background: #292524;
          border-color: #44403c;
          color: #fde68a;
        }
      }
      .state-container {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 0.75rem;
        padding: 2.5rem 1rem;
        color: var(--color-text-muted);
        font: 400 0.9375rem/1.5 var(--font-serif);
        text-align: center;
      }
      .state-container p { margin: 0; }
      .state-container--error { color: var(--color-red); }
      .error-title { font: 700 1rem/1.2 var(--font-serif); }
      .error-message { font-size: 0.875rem; opacity: 0.8; }
      .spinner {
        display: inline-block;
        width: 28px;
        height: 28px;
        border: 3px solid var(--color-border);
        border-top-color: var(--color-accent);
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }
      @keyframes spin { to { transform: rotate(360deg); } }
      .btn {
        display: inline-flex;
        align-items: center;
        gap: 0.375rem;
        padding: 0.5rem 1.125rem;
        border-radius: 8px;
        font: 600 0.875rem/1.2 var(--font-mono);
        cursor: pointer;
        border: 1.5px solid transparent;
        transition: border-color 0.15s, background 0.15s, color 0.15s, opacity 0.15s;
      }
      .btn:disabled { opacity: 0.55; cursor: not-allowed; }
      .btn--primary {
        background: var(--color-accent);
        color: white;
        border-color: var(--color-accent);
      }
      .btn--primary:hover:not(:disabled) { filter: brightness(1.1); }
      .btn--secondary {
        background: transparent;
        color: var(--color-text);
        border-color: var(--color-border);
      }
      .btn--secondary:hover:not(:disabled) {
        border-color: var(--color-accent);
        color: var(--color-accent);
      }
      .btn--ghost {
        background: transparent;
        color: var(--color-text-muted);
        border-color: transparent;
      }
      .btn--ghost:hover:not(:disabled) { color: var(--color-text); }
      .btn-spinner {
        display: inline-block;
        width: 14px;
        height: 14px;
        border: 2px solid currentColor;
        border-top-color: transparent;
        border-radius: 50%;
        animation: spin 0.8s linear infinite;
      }
      /* Auth state */
      .auth-icon { color: var(--color-accent); }
      .auth-title { font: 700 1.0625rem/1.2 var(--font-serif); margin: 0; color: var(--color-text); }
      .auth-desc { font-size: 0.875rem; margin: 0; max-width: 40ch; }
      .token-form { width: 100%; display: flex; flex-direction: column; gap: 0.875rem; }
      .form-field { display: flex; flex-direction: column; gap: 0.375rem; }
      .form-label {
        font: 600 0.75rem/1.2 var(--font-mono);
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: var(--color-text-muted);
      }
      .form-label-optional {
        font-weight: 400;
        text-transform: none;
        letter-spacing: 0;
        margin-left: 0.25rem;
      }
      .token-input-row { position: relative; display: flex; }
      .form-input {
        flex: 1;
        padding: 0.5rem 0.75rem;
        border: 1.5px solid var(--color-border);
        border-radius: 8px;
        background: var(--color-ground);
        color: var(--color-text);
        font: 500 0.875rem/1.4 var(--font-mono);
        outline: none;
        transition: border-color 0.15s;
      }
      .form-input:focus { border-color: var(--color-accent); }
      .form-input::placeholder { color: var(--color-text-muted); opacity: 0.6; }
      .toggle-visibility-btn {
        position: absolute;
        right: 0.5rem;
        top: 50%;
        transform: translateY(-50%);
        background: none;
        border: none;
        cursor: pointer;
        color: var(--color-text-muted);
        display: flex;
        align-items: center;
        padding: 0.25rem;
      }
      .toggle-visibility-btn:hover { color: var(--color-text); }
      .token-input-row .form-input { padding-right: 2.5rem; }
      .form-hint { font-size: 0.75rem; color: var(--color-text-muted); margin: 0; }
      .form-error { font: 600 0.75rem/1.3 var(--font-mono); color: var(--color-red); margin: 0; }
      .form-actions { display: flex; gap: 0.5rem; }
      /* Toggle switch */
      .toggle-label { display: flex; align-items: center; cursor: pointer; }
      .toggle-label input { position: absolute; opacity: 0; width: 0; height: 0; }
      .toggle-switch {
        position: relative;
        display: inline-block;
        width: 40px;
        height: 22px;
        background: var(--color-border);
        border-radius: 11px;
        transition: background 0.2s;
      }
      .toggle-switch::after {
        content: '';
        position: absolute;
        top: 3px;
        left: 3px;
        width: 16px;
        height: 16px;
        background: white;
        border-radius: 50%;
        transition: transform 0.2s;
      }
      .toggle-label input:checked + .toggle-switch { background: var(--color-accent); }
      .toggle-label input:checked + .toggle-switch::after { transform: translateX(18px); }
      /* Tools list */
      .tools-panel { display: flex; flex-direction: column; gap: 0.75rem; }
      .tools-count { font: 600 0.75rem/1.2 var(--font-mono); color: var(--color-text-muted); text-transform: uppercase; letter-spacing: 0.04em; margin: 0; }
      .tools-list { display: flex; flex-direction: column; gap: 0.5rem; }
      .tool-card {
        width: 100%;
        text-align: left;
        background: var(--color-ground);
        border: 1.5px solid var(--color-border);
        border-radius: 8px;
        padding: 0.75rem 1rem;
        cursor: pointer;
        transition: border-color 0.15s, background 0.15s;
        display: flex;
        flex-direction: column;
        gap: 0.25rem;
      }
      .tool-card:hover { border-color: var(--color-accent); background: var(--color-surface); }
      .tool-card-header { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
      .tool-name { font: 700 0.875rem/1.2 var(--font-mono); color: var(--color-text); }
      .tool-card-header svg { color: var(--color-text-muted); flex-shrink: 0; }
      .tool-desc { font-size: 0.8125rem; color: var(--color-text-muted); margin: 0; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
      /* Tool form */
      .tool-form-panel { display: flex; flex-direction: column; gap: 1rem; }
      .back-btn {
        display: inline-flex;
        align-items: center;
        gap: 0.375rem;
        background: none;
        border: none;
        cursor: pointer;
        font: 500 0.8125rem/1.2 var(--font-mono);
        color: var(--color-text-muted);
        padding: 0;
        transition: color 0.15s;
      }
      .back-btn:hover { color: var(--color-accent); }
      .tool-form-header { display: flex; flex-direction: column; gap: 0.25rem; }
      .tool-form-name { font: 700 1rem/1.2 var(--font-mono); color: var(--color-text); margin: 0; }
      .tool-form-desc { font-size: 0.875rem; color: var(--color-text-muted); margin: 0; }
      .dynamic-form { display: flex; flex-direction: column; gap: 1rem; }
      .no-params-note { font-size: 0.875rem; color: var(--color-text-muted); font-style: italic; margin: 0; }
      /* Result panel */
      .result-panel { display: flex; flex-direction: column; gap: 0.875rem; }
      .result-header { display: flex; align-items: center; justify-content: space-between; }
      .result-status {
        display: inline-flex;
        align-items: center;
        gap: 0.375rem;
        font: 700 0.875rem/1.2 var(--font-mono);
        padding: 0.25rem 0.75rem;
        border-radius: 999px;
      }
      .result-status--success { background: #dcfce7; color: #15803d; }
      .result-status--error { background: #fee2e2; color: #b91c1c; }
      @media (prefers-color-scheme: dark) {
        .result-status--success { background: #14532d; color: #86efac; }
        .result-status--error { background: #450a0a; color: #fca5a5; }
      }
      .result-duration { font: 500 0.75rem/1.2 var(--font-mono); color: var(--color-text-muted); }
      .result-body { overflow-x: auto; }
      .result-json {
        background: var(--color-ground);
        padding: 1rem;
        border-radius: 8px;
        font: 500 0.8125rem/1.5 var(--font-mono);
        color: var(--color-text);
        margin: 0;
        white-space: pre-wrap;
        word-break: break-all;
        max-height: 300px;
        overflow-y: auto;
      }
      .result-error-body {
        background: #fff1f2;
        border: 1px solid #fecdd3;
        border-radius: 8px;
        padding: 0.875rem 1rem;
      }
      @media (prefers-color-scheme: dark) {
        .result-error-body { background: #2d0009; border-color: #4c0519; }
      }
      .result-error-text { font: 500 0.875rem/1.5 var(--font-mono); color: #b91c1c; margin: 0; }
      @media (prefers-color-scheme: dark) { .result-error-text { color: #fca5a5; } }
      .auth-retry-banner {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.75rem;
        padding: 0.625rem 0.875rem;
        background: var(--color-ground);
        border: 1px solid var(--color-border);
        border-radius: 8px;
        flex-wrap: wrap;
      }
      .auth-retry-banner p { font-size: 0.8125rem; color: var(--color-text-muted); margin: 0; }
      .result-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
      /* local_stdio panel */
      .stdio-panel {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 0.875rem;
        padding: 1.5rem 0.5rem;
        text-align: center;
        color: var(--color-text-muted);
      }
      .stdio-icon { color: var(--color-accent); opacity: 0.7; }
      .stdio-title { font: 700 1.0625rem/1.2 var(--font-serif); color: var(--color-text); margin: 0; }
      .stdio-desc { font-size: 0.875rem; margin: 0; max-width: 42ch; }
      .stdio-desc code { background: var(--color-ground); padding: 0.1rem 0.3rem; border-radius: 4px; font-size: 0.8125rem; }
      .install-block, .env-block { width: 100%; text-align: left; }
      .install-label, .env-label { font: 600 0.6875rem/1.2 var(--font-mono); text-transform: uppercase; letter-spacing: 0.04em; color: var(--color-text-muted); margin: 0 0 0.5rem; }
      .install-cmd {
        background: var(--color-ground);
        border: 1px solid var(--color-border);
        border-radius: 8px;
        padding: 0.75rem 1rem;
        font: 600 0.8125rem/1.5 var(--font-mono);
        color: var(--color-text);
        margin: 0;
        white-space: pre-wrap;
        word-break: break-all;
        text-align: left;
      }
      .env-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
      .env-list li { display: flex; align-items: baseline; gap: 0.375rem; flex-wrap: wrap; }
      .env-list code { background: var(--color-ground); padding: 0.1rem 0.35rem; border-radius: 4px; font-size: 0.8125rem; color: var(--color-text); }
      .env-secret-chip { background: #fee2e2; color: #b91c1c; font: 600 0.6rem/1 var(--font-mono); padding: 0.1rem 0.35rem; border-radius: 999px; text-transform: uppercase; letter-spacing: 0.04em; }
      @media (prefers-color-scheme: dark) {
        .env-secret-chip { background: #450a0a; color: #fca5a5; }
      }
      .env-desc { font-size: 0.8125rem; }
      .stdio-note { font-size: 0.8125rem; opacity: 0.7; margin: 0; font-style: italic; }
      /* Footer */
      .modal-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 0.625rem 1.25rem;
        border-top: 1px solid var(--color-border);
        background: var(--color-ground);
        flex-shrink: 0;
      }
      .session-info {
        display: flex;
        align-items: center;
        gap: 0.375rem;
        font: 500 0.6875rem/1.2 var(--font-mono);
        color: var(--color-text-muted);
      }
      .disconnect-btn {
        background: none;
        border: none;
        cursor: pointer;
        font: 600 0.6875rem/1.2 var(--font-mono);
        color: var(--color-text-muted);
        text-transform: uppercase;
        letter-spacing: 0.04em;
        padding: 0.25rem 0.5rem;
        border-radius: 4px;
        transition: color 0.15s, background 0.15s;
      }
      .disconnect-btn:hover { color: var(--color-red); background: #fee2e2; }
      @media (prefers-color-scheme: dark) { .disconnect-btn:hover { background: #450a0a; } }
    `,
  ],
})
export class TestToolModalComponent implements OnInit {
  @Input({ required: true }) result!: SearchResultItem;
  @Output() close = new EventEmitter<void>();

  // ---------------------------------------------------------------------------
  // State
  // ---------------------------------------------------------------------------
  state = signal<ModalState>('idle');
  sessionId = signal<string | null>(null);
  errorMessage = signal<string>('');
  tools = signal<ToolInfo[]>([]);
  selectedTool = signal<ToolInfo | null>(null);
  invokeResult = signal<ToolInvokeResponse | null>(null);
  invoking = signal(false);
  invokeError = signal<string>('');
  tokenInput = '';
  showToken = signal(false);
  tokenSubmitting = signal(false);
  tokenSubmitError = signal<string>('');

  // ---------------------------------------------------------------------------
  // Form fields (derived from selected tool's inputSchema)
  // ---------------------------------------------------------------------------
  formFields = signal<FormField[]>([]);
  formValues: Record<string, unknown> = {};

  // ---------------------------------------------------------------------------
  // Derived
  // ---------------------------------------------------------------------------
  classification = computed(() => this.result?.classification ?? null);

  remoteCandidate = computed((): RemoteCandidate | null => {
    const cls = this.classification();
    if (!cls || cls.mode === 'local_stdio' || cls.mode === 'not_testable') return null;
    const detail = cls.detail;
    if (!Array.isArray(detail) || detail.length === 0) return null;
    const first = detail[0];
    if ('type' in first && 'url' in first) return first as RemoteCandidate;
    return null;
  });

  localHint = computed((): LocalPackageHint | null => {
    const cls = this.classification();
    if (!cls) return null;
    const detail = cls.detail;
    if (!Array.isArray(detail) || detail.length === 0) return null;
    const first = detail[0];
    if ('installCommand' in first || 'runtimeHint' in first) return first as LocalPackageHint;
    return null;
  });

  resultStatusClass = computed(() => {
    const r = this.invokeResult();
    if (!r) return '';
    return r.status === 'success' ? 'result-status--success' : 'result-status--error';
  });

  constructor(private readonly service: DiscoveryService) {}

  ngOnInit(): void {
    this.startConnection();
  }

  // ---------------------------------------------------------------------------
  // Connection flow
  // ---------------------------------------------------------------------------
  private startConnection(): void {
    const cls = this.classification();

    if (!cls || !cls.testable) {
      this.state.set('local-stdio');
      return;
    }

    if (cls.mode === 'local_stdio') {
      this.state.set('local-stdio');
      return;
    }

    // Remote or remote_via_package — attempt connect
    this.state.set('connecting');
    this.doConnect();
  }

  retryConnect(): void {
    this.state.set('connecting');
    this.tokenInput = '';
    this.tokenSubmitting.set(false);
    this.tokenSubmitError.set('');
    this.doConnect();
  }

  private doConnect(token?: string): void {
    const itemId = this.result.item.item_id;
    const sid = token ? this.sessionId() ?? undefined : this.sessionId() ?? undefined;

    this.service.testConnect(itemId, sid ?? undefined).subscribe({
      next: (res) => this.handleConnectResponse(res, token),
      error: (err) => {
        this.state.set('error');
        this.errorMessage.set(this.extractErrorMessage(err));
      },
    });
  }

  private handleConnectResponse(res: ToolConnectResponse, token?: string): void {
    if (res.connected) {
      this.tools.set(res.tools ?? []);
      this.state.set('tools-ready');
      return;
    }

    if (res.auth_required || res.requires_auth) {
      // If token was already provided but still auth-required, show error
      if (token) {
        this.state.set('auth-required');
        this.tokenSubmitError.set('The provided token is invalid or expired.');
        return;
      }
      this.state.set('auth-required');
      return;
    }

    if (res.error) {
      this.state.set('error');
      this.errorMessage.set(res.error);
      return;
    }

    this.state.set('error');
    this.errorMessage.set('Connection failed for an unknown reason.');
  }

  // ---------------------------------------------------------------------------
  // Token submission
  // ---------------------------------------------------------------------------
  submitToken(): void {
    if (!this.tokenInput.trim()) return;
    this.tokenSubmitting.set(true);
    this.tokenSubmitError.set('');
    this.invokeError.set('');

    const itemId = this.result.item.item_id;
    const existingSid = this.sessionId();

    this.service.testSubmitManualToken(itemId, this.tokenInput.trim(), existingSid ?? undefined).subscribe({
      next: (res) => {
        this.tokenSubmitting.set(false);
        this.sessionId.set(res.session_id);
        // Re-connect with the new session
        this.doConnect(this.tokenInput.trim());
        this.tokenInput = '';
      },
      error: (err) => {
        this.tokenSubmitting.set(false);
        this.tokenSubmitError.set(this.extractErrorMessage(err) || 'Failed to store token.');
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Tool selection
  // ---------------------------------------------------------------------------
  selectTool(tool: ToolInfo): void {
    this.selectedTool.set(tool);
    this.invokeResult.set(null);
    this.invokeError.set('');
    this.buildFormFields(tool);
    this.state.set('tool-form');
  }

  backToTools(): void {
    this.selectedTool.set(null);
    this.invokeResult.set(null);
    this.invokeError.set('');
    this.state.set('tools-ready');
  }

  private buildFormFields(tool: ToolInfo): void {
    const schema = tool.inputSchema ?? {};
    const properties = (schema['properties'] ?? {}) as Record<string, unknown>;
    const required: string[] = (schema['required'] as string[]) ?? [];

    const fields: FormField[] = [];
    for (const [name, prop] of Object.entries(properties)) {
      const p = prop as Record<string, unknown>;
      const type = this.inferFieldType(p);
      fields.push({
        name,
        label: this.humanizeName(name),
        type,
        required: required.includes(name),
        description: (p['description'] as string) ?? null,
        placeholder: null,
        itemsEnum: type === 'array' ? this.getArrayEnum(p) : null,
      });
    }

    this.formFields.set(fields);
    // Initialize default values
    this.formValues = {};
    for (const f of fields) {
      if (f.type === 'boolean') {
        this.formValues[f.name] = false;
      } else {
        this.formValues[f.name] = '';
      }
    }
  }

  private inferFieldType(prop: Record<string, unknown>): string {
    const type = prop['type'] as string | undefined;
    if (type === 'string') return 'string';
    if (type === 'number') return 'number';
    if (type === 'integer') return 'integer';
    if (type === 'boolean') return 'boolean';
    if (type === 'array') return 'array';
    if (type === 'object') return 'string'; // Fallback to text
    return 'string';
  }

  private getArrayEnum(prop: Record<string, unknown>): string[] | null {
    const items = prop['items'] as Record<string, unknown> | undefined;
    if (!items) return null;
    const enumVals = items['enum'] as string[] | undefined;
    return enumVals ?? null;
  }

  private humanizeName(name: string): string {
    return name
      .replace(/[_-]/g, ' ')
      .replace(/([a-z])([A-Z])/g, '$1 $2')
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }

  // ---------------------------------------------------------------------------
  // Tool invocation
  // ---------------------------------------------------------------------------
  invokeTool(): void {
    const tool = this.selectedTool();
    if (!tool) return;

    this.invoking.set(true);
    this.invokeError.set('');

    // Build arguments object from form values
    const args: Record<string, unknown> = {};
    for (const [key, val] of Object.entries(this.formValues)) {
      const field = this.formFields().find((f) => f.name === key);
      if (field?.type === 'array' && typeof val === 'string' && val) {
        // Parse comma-separated string into array
        args[key] = val.split(',').map((v) => v.trim()).filter(Boolean);
      } else {
        args[key] = val;
      }
    }

    const itemId = this.result.item.item_id;
    this.service.testInvoke(
      itemId,
      { tool_name: tool.name, arguments: args },
      this.sessionId() ?? undefined,
    ).subscribe({
      next: (res) => {
        this.invoking.set(false);
        this.invokeResult.set(res);
        this.state.set('result');
      },
      error: (err) => {
        this.invoking.set(false);
        if (this.isAuthError(err)) {
          this.invokeResult.set({
            status: 'error',
            error: this.extractErrorMessage(err),
            requires_auth: true,
          });
          this.state.set('result');
        } else {
          this.invokeError.set(this.extractErrorMessage(err) || 'Invocation failed.');
        }
      },
    });
  }

  goToAuth(): void {
    this.state.set('auth-required');
  }

  // ---------------------------------------------------------------------------
  // Disconnect
  // ---------------------------------------------------------------------------
  disconnect(): void {
    const sid = this.sessionId();
    if (!sid) return;
    const itemId = this.result.item.item_id;
    this.service.testDisconnect(itemId, sid).subscribe({
      next: () => {
        this.sessionId.set(null);
        this.close.emit();
      },
      error: () => {
        // Even on error, close the modal
        this.sessionId.set(null);
        this.close.emit();
      },
    });
  }

  // ---------------------------------------------------------------------------
  // Utilities
  // ---------------------------------------------------------------------------
  shortUrl(url: string): string {
    try {
      const u = new URL(url);
      return u.hostname + (u.port ? `:${u.port}` : '') + u.pathname;
    } catch {
      return url;
    }
  }

  formatResult(result: unknown): string {
    try {
      return JSON.stringify(result, null, 2);
    } catch {
      return String(result);
    }
  }

  private extractErrorMessage(err: unknown): string {
    if (!err) return 'Unknown error';
    if (typeof err === 'string') return err;
    if (err instanceof Error) return err.message;
    // Try to parse HTTP error response
    const anyErr = err as Record<string, unknown>;
    if (anyErr['message']) return String(anyErr['message']);
    if (anyErr['detail']) return String(anyErr['detail']);
    return 'An unexpected error occurred.';
  }

  private isAuthError(err: unknown): boolean {
    const anyErr = err as Record<string, unknown>;
    if (!anyErr) return false;
    const status = anyErr['status'];
    return status === 401 || status === 403;
  }

  onBackdropClick(event: MouseEvent): void {
    if ((event.target as HTMLElement).classList.contains('modal-backdrop')) {
      this.close.emit();
    }
  }
}

// ---------------------------------------------------------------------------
// Helper types
// ---------------------------------------------------------------------------
interface FormField {
  name: string;
  label: string;
  type: string;
  required: boolean;
  description: string | null;
  placeholder: string | null;
  itemsEnum: string[] | null;
}
