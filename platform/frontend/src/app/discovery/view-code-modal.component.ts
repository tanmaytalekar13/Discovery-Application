import {
  Component,
  Input,
  Output,
  EventEmitter,
  OnInit,
  OnDestroy,
  signal,
  computed,
  ElementRef,
  ViewChild,
  AfterViewInit,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import loader from '@monaco-editor/loader';
import type * as Monaco from 'monaco-editor';
import { DiscoveryService } from './discovery.service';
import {
  SearchResultItem,
  SourceTreeNode,
  ItemArtifactsResponse,
  ItemSchemaResponse,
  ItemProvenanceResponse,
} from './models';

export type ViewCodeTab = 'source' | 'config' | 'schema' | 'agent' | 'provenance';

@Component({
  selector: 'app-view-code-modal',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="modal-backdrop" (click)="onBackdropClick($event)">
      <div class="modal" role="dialog" aria-modal="true" [attr.aria-label]="'View code: ' + item?.item?.name">
        <!-- Header -->
        <header class="modal-header">
          <div class="modal-title-row">
            <span class="protocol-chip" [class.tool]="item?.item?.type === 'tool'" [class.agent]="item?.item?.type === 'agent'">
              {{ item?.item?.type?.toUpperCase() }}
            </span>
            <h2 class="modal-title">{{ item?.item?.name }}</h2>
          </div>
          <button class="close-btn" (click)="close.emit()" aria-label="Close">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"></line>
              <line x1="6" y1="6" x2="18" y2="18"></line>
            </svg>
          </button>
        </header>

        <!-- Tabs -->
        <nav class="tab-bar" role="tablist">
          @for (tab of tabs(); track tab.id) {
            <button role="tab" [attr.aria-selected]="activeTab() === tab.id"
              [class.active]="activeTab() === tab.id" (click)="setTab(tab.id)"
              [disabled]="tab.disabled">
              {{ tab.label }}
            </button>
          }
        </nav>

        <!-- Body -->
        <div class="modal-body">
          <!-- Source Tab -->
          <ng-container *ngIf="activeTab() === 'source'">
            <div class="source-layout">
              <!-- File Tree -->
              <aside class="file-tree">
                <div class="file-tree-header">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
                  </svg>
                  Files
                </div>
                <div class="tree-content">
                  <ng-container *ngIf="treeLoading()">Loading tree…</ng-container>
                  <ng-container *ngIf="!treeLoading() && sourceTree().length === 0">
                    <span class="tree-empty">No source tree available</span>
                  </ng-container>
                  <ng-container *ngIf="!treeLoading() && sourceTree().length > 0">
                    <ng-container *ngFor="let node of sourceTree()">
                      <ng-container *ngTemplateOutlet="treeNodeTpl; context: { node: node, depth: 0 }"></ng-container>
                    </ng-container>
                  </ng-container>
                </div>
              </aside>

              <!-- Editor Pane -->
              <div class="editor-pane">
                <div class="editor-header">
                  <span class="file-path">{{ currentFilePath() || 'Select a file' }}</span>
                  <span *ngIf="fileLoading()" class="file-loading">Loading…</span>
                </div>
                <div #editorContainer class="editor-container"></div>
              </div>
            </div>
          </ng-container>

          <!-- Config Tab -->
          <ng-container *ngIf="activeTab() === 'config'">
            <div class="config-pane">
              <ng-container *ngIf="artifactsLoading()">
                <div class="tab-loading">Loading artifacts…</div>
              </ng-container>
              <ng-container *ngIf="!artifactsLoading() && configFiles().length === 0">
                <div class="tab-empty">
                  <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                    <polyline points="14 2 14 8 20 8"></polyline>
                  </svg>
                  <p>No configuration files available</p>
                </div>
              </ng-container>
              <ng-container *ngIf="!artifactsLoading() && configFiles().length > 0">
                <div *ngFor="let file of configFiles()" class="config-file">
                  <div class="config-file-name">{{ file }}</div>
                  <pre class="config-file-content">{{ getConfigContent(file) }}</pre>
                </div>
              </ng-container>
            </div>
          </ng-container>

          <!-- Schema Tab -->
          <ng-container *ngIf="activeTab() === 'schema'">
            <div class="schema-pane">
              <ng-container *ngIf="schemaLoading()">
                <div class="tab-loading">Loading schema…</div>
              </ng-container>
              <ng-container *ngIf="!schemaLoading() && !schemaData()">
                <div class="tab-empty">
                  <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                    <polyline points="16 18 22 12 16 6"></polyline>
                    <polyline points="8 6 2 12 8 18"></polyline>
                  </svg>
                  <p>{{ item?.item?.type === 'tool' ? 'No MCP schema available' : 'No Agent Card schema available' }}</p>
                </div>
              </ng-container>
              <pre *ngIf="!schemaLoading() && schemaData()" class="schema-content">{{ schemaData() | json }}</pre>
            </div>
          </ng-container>

          <!-- Agent Card Tab -->
          <ng-container *ngIf="activeTab() === 'agent'">
            <div class="agent-pane">
              <ng-container *ngIf="schemaLoading()">
                <div class="tab-loading">Loading agent card…</div>
              </ng-container>
              <ng-container *ngIf="!schemaLoading() && !agentCard()">
                <div class="tab-empty">
                  <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                    <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
                    <circle cx="12" cy="7" r="4"></circle>
                  </svg>
                  <p>{{ item?.item?.type === 'agent' ? 'No agent card available' : 'This is a tool, not an agent' }}</p>
                </div>
              </ng-container>
              <pre *ngIf="!schemaLoading() && agentCard()" class="agent-card-content">{{ agentCard() | json }}</pre>
            </div>
          </ng-container>

          <!-- Provenance Tab -->
          <ng-container *ngIf="activeTab() === 'provenance'">
            <div class="provenance-pane">
              <ng-container *ngIf="provenanceLoading()">
                <div class="tab-loading">Loading provenance…</div>
              </ng-container>
              <ng-container *ngIf="!provenanceLoading() && (!provenanceData() || provenanceData()?.provenance?.length === 0)">
                <div class="tab-empty">
                  <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="8" x2="12" y2="12"></line>
                    <line x1="12" y1="16" x2="12.01" y2="16"></line>
                  </svg>
                  <p>No provenance data available</p>
                </div>
              </ng-container>
              <ng-container *ngIf="!provenanceLoading() && provenanceData() && provenanceData()?.provenance?.length! > 0">
                <div class="provenance-list">
                  <div *ngFor="let src of provenanceData()?.provenance" class="provenance-item">
                    <div class="provenance-type">{{ src.type }}</div>
                    <div class="provenance-id">{{ src.id }}</div>
                    <div *ngIf="src.url" class="provenance-url">{{ src.url }}</div>
                    <div *ngIf="src.provider" class="provenance-provider">via {{ src.provider }}</div>
                  </div>
                </div>
              </ng-container>
            </div>
          </ng-container>
        </div>
      </div>
    </div>

    <!-- Tree Node Template -->
    <ng-template #treeNodeTpl let-node="node" let-depth="depth">
      <div class="tree-item" [style.padding-left.px]="depth * 16 + 8" (click)="onTreeNodeClick(node)">
        <span class="tree-chevron" *ngIf="node.type === 'dir'" [class.expanded]="isExpanded(node.path)">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
            <polyline points="9 18 15 12 9 6"></polyline>
          </svg>
        </span>
        <span class="tree-chevron-placeholder" *ngIf="node.type === 'file'"></span>
        <span class="tree-icon" [class.dir]="node.type === 'dir'">
          <svg *ngIf="node.type === 'dir'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>
          </svg>
          <svg *ngIf="node.type === 'file'" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
            <polyline points="14 2 14 8 20 8"></polyline>
          </svg>
        </span>
        <span class="tree-name" [class.active]="currentFilePath() === node.path">{{ node.name }}</span>
      </div>
      <ng-container *ngIf="node.type === 'dir' && node.children && isExpanded(node.path)">
        <ng-container *ngFor="let child of node.children">
          <ng-container *ngTemplateOutlet="treeNodeTpl; context: { node: child, depth: depth + 1 }"></ng-container>
        </ng-container>
      </ng-container>
    </ng-template>
  `,
  styles: [
    `
      .modal-backdrop {
        position: fixed;
        inset: 0;
        background: rgba(0, 0, 0, 0.6);
        backdrop-filter: blur(4px);
        z-index: 1000;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 2rem;
      }
      .modal {
        background: var(--color-surface);
        border-radius: var(--radius-lg);
        box-shadow: var(--shadow-xl);
        width: 100%;
        max-width: 1200px;
        height: 80vh;
        display: flex;
        flex-direction: column;
        overflow: hidden;
      }
      .modal-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 1rem 1.5rem;
        border-bottom: 1px solid var(--color-border);
        flex-shrink: 0;
      }
      .modal-title-row {
        display: flex;
        align-items: center;
        gap: 0.75rem;
      }
      .protocol-chip {
        font: 700 0.6875rem/1.2 var(--font-mono);
        letter-spacing: 0.06em;
        padding: 0.25rem 0.5rem;
        border-radius: var(--radius-sm);
        background: var(--color-accent);
        color: white;
      }
      .protocol-chip.tool { background: #3b82f6; }
      .protocol-chip.agent { background: #8b5cf6; }
      .modal-title {
        font: 600 1.125rem/1.3 var(--font-serif);
        color: var(--color-text);
        margin: 0;
      }
      .close-btn {
        background: none;
        border: none;
        padding: 0.5rem;
        cursor: pointer;
        color: var(--color-text-muted);
        border-radius: var(--radius-sm);
        display: flex;
        align-items: center;
        justify-content: center;
        transition: background 0.15s, color 0.15s;
      }
      .close-btn:hover {
        background: var(--color-border);
        color: var(--color-text);
      }
      .tab-bar {
        display: flex;
        gap: 0;
        border-bottom: 1px solid var(--color-border);
        padding: 0 1.5rem;
        flex-shrink: 0;
        overflow-x: auto;
      }
      .tab-bar button {
        background: none;
        border: none;
        padding: 0.75rem 1rem;
        font: 500 0.875rem/1.2 var(--font-mono);
        color: var(--color-text-muted);
        cursor: pointer;
        border-bottom: 2px solid transparent;
        margin-bottom: -1px;
        transition: color 0.15s, border-color 0.15s;
        white-space: nowrap;
      }
      .tab-bar button:hover:not(:disabled) {
        color: var(--color-text);
      }
      .tab-bar button.active {
        color: var(--color-accent);
        border-bottom-color: var(--color-accent);
      }
      .tab-bar button:disabled {
        opacity: 0.4;
        cursor: not-allowed;
      }
      .modal-body {
        flex: 1;
        overflow: hidden;
        display: flex;
        flex-direction: column;
      }
      .source-layout {
        display: flex;
        height: 100%;
      }
      .file-tree {
        width: 240px;
        flex-shrink: 0;
        border-right: 1px solid var(--color-border);
        display: flex;
        flex-direction: column;
        background: var(--color-ground);
      }
      .file-tree-header {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.75rem 1rem;
        font: 600 0.75rem/1.2 var(--font-mono);
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: var(--color-text-muted);
        border-bottom: 1px solid var(--color-border);
        flex-shrink: 0;
      }
      .tree-content {
        flex: 1;
        overflow-y: auto;
        padding: 0.5rem 0;
      }
      .tree-empty, .tree-loading {
        display: block;
        padding: 1rem;
        font-size: 0.8125rem;
        color: var(--color-text-muted);
      }
      .tree-item {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        padding: 0.25rem 0;
        cursor: pointer;
        transition: background 0.1s;
        font-size: 0.8125rem;
        color: var(--color-text);
      }
      .tree-item:hover {
        background: var(--color-border);
      }
      .tree-name.active {
        color: var(--color-accent);
        font-weight: 600;
      }
      .tree-icon {
        display: flex;
        align-items: center;
        flex-shrink: 0;
      }
      .tree-icon.dir { color: #f59e0b; }
      .tree-icon:not(.dir) { color: var(--color-text-muted); }
      .tree-chevron {
        display: flex;
        align-items: center;
        justify-content: center;
        width: 14px;
        height: 14px;
        flex-shrink: 0;
        color: var(--color-text-muted);
        transition: transform 0.15s ease;
      }
      .tree-chevron.expanded {
        transform: rotate(90deg);
      }
      .tree-chevron-placeholder {
        display: inline-block;
        width: 14px;
        height: 14px;
        flex-shrink: 0;
      }
      .editor-pane {
        flex: 1;
        display: flex;
        flex-direction: column;
        overflow: hidden;
      }
      .editor-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 0.5rem 1rem;
        background: var(--color-ground);
        border-bottom: 1px solid var(--color-border);
        flex-shrink: 0;
      }
      .file-path {
        font: 500 0.8125rem/1.2 var(--font-mono);
        color: var(--color-text-muted);
      }
      .file-loading {
        font-size: 0.75rem;
        color: var(--color-accent);
      }
      .editor-container {
        flex: 1;
        overflow: hidden;
      }
      .tab-loading, .tab-empty {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        gap: 0.75rem;
        height: 100%;
        color: var(--color-text-muted);
        font-size: 0.9375rem;
      }
      .tab-empty svg { opacity: 0.3; }
      .tab-empty p { margin: 0; }
      .config-pane, .schema-pane, .agent-pane, .provenance-pane {
        flex: 1;
        overflow-y: auto;
        padding: 1rem;
      }
      .config-file {
        margin-bottom: 1.5rem;
      }
      .config-file-name {
        font: 600 0.8125rem/1.2 var(--font-mono);
        color: var(--color-text);
        padding: 0.5rem 0;
        border-bottom: 1px solid var(--color-border);
        margin-bottom: 0.5rem;
      }
      .config-file-content {
        background: var(--color-ground);
        padding: 1rem;
        border-radius: var(--radius-md);
        font: 500 0.8125rem/1.5 var(--font-mono);
        color: var(--color-text);
        overflow-x: auto;
        margin: 0;
        white-space: pre-wrap;
        word-break: break-all;
      }
      .schema-content, .agent-card-content {
        background: var(--color-ground);
        padding: 1rem;
        border-radius: var(--radius-md);
        font: 500 0.8125rem/1.5 var(--font-mono);
        color: var(--color-text);
        overflow-x: auto;
        margin: 0;
        white-space: pre-wrap;
        word-break: break-all;
      }
      .provenance-list {
        display: flex;
        flex-direction: column;
        gap: 0.75rem;
      }
      .provenance-item {
        background: var(--color-ground);
        padding: 1rem;
        border-radius: var(--radius-md);
        border-left: 3px solid var(--color-accent);
      }
      .provenance-type {
        font: 700 0.6875rem/1.2 var(--font-mono);
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: var(--color-accent);
        margin-bottom: 0.25rem;
      }
      .provenance-id {
        font: 500 0.875rem/1.4 var(--font-mono);
        color: var(--color-text);
      }
      .provenance-url {
        font-size: 0.8125rem;
        color: var(--color-text-muted);
        word-break: break-all;
        margin-top: 0.25rem;
      }
      .provenance-provider {
        font-size: 0.8125rem;
        color: var(--color-text-muted);
        margin-top: 0.25rem;
      }
    `,
  ],
})
export class ViewCodeModalComponent implements OnInit, AfterViewInit, OnDestroy {
  @Input() item: SearchResultItem | null = null;
  @Output() close = new EventEmitter<void>();
  @ViewChild('editorContainer', { static: false }) editorContainer!: ElementRef<HTMLDivElement>;

  // Tabs (computed based on item type)
  readonly tabs = computed(() => {
    const isAgent = this.item?.item?.type === 'agent';
    return [
      { id: 'source' as ViewCodeTab, label: 'Source', disabled: false },
      { id: 'config' as ViewCodeTab, label: 'Config', disabled: false },
      { id: 'schema' as ViewCodeTab, label: isAgent ? 'Schema' : 'MCP Schema', disabled: false },
      { id: 'agent' as ViewCodeTab, label: 'Agent Card', disabled: !isAgent },
      { id: 'provenance' as ViewCodeTab, label: 'Provenance', disabled: false },
    ];
  });
  activeTab = signal<ViewCodeTab>('source');

  // Loading states
  artifactsLoading = signal(true);
  treeLoading = signal(true);
  schemaLoading = signal(true);
  provenanceLoading = signal(true);
  fileLoading = signal(false);

  // Data
  artifactsData = signal<ItemArtifactsResponse | null>(null);
  sourceTree = signal<SourceTreeNode[]>([]);
  schemaData = signal<Record<string, unknown> | null>(null);
  agentCard = signal<Record<string, unknown> | null>(null);
  provenanceData = signal<ItemProvenanceResponse | null>(null);
  configFiles = signal<string[]>([]);
  configContents = signal<Record<string, string>>({});

  // Editor state
  currentFilePath = signal('');
  expandedDirs = signal<Set<string>>(new Set());
  private monacoEditor: Monaco.editor.IStandaloneCodeEditor | null = null;
  private monaco: typeof Monaco | null = null;

  constructor(private readonly service: DiscoveryService) {}

  ngOnInit(): void {
    if (this.item) {
      this.loadArtifacts();
      this.loadSourceTree();
      this.loadSchema();
      this.loadProvenance();
    }
  }

  ngAfterViewInit(): void {
    this.initMonaco();
  }

  ngOnDestroy(): void {
    this.monacoEditor?.dispose();
  }

  private async initMonaco(): Promise<void> {
    try {
      this.monaco = await loader.init();
      if (this.editorContainer?.nativeElement && this.monaco) {
        this.monacoEditor = this.monaco.editor.create(this.editorContainer.nativeElement, {
          value: '// Select a file from the tree to view its contents…',
          language: 'plaintext',
          theme: 'vs-dark',
          readOnly: true,
          minimap: { enabled: true },
          fontSize: 13,
          fontFamily: "'Fira Code', 'Cascadia Code', Consolas, monospace",
          lineNumbers: 'on',
          wordWrap: 'on',
          automaticLayout: true,
          scrollBeyondLastLine: false,
        });
      }
    } catch (err) {
      console.error('Monaco init failed:', err);
    }
  }

  private loadArtifacts(): void {
    if (!this.item) return;
    this.artifactsLoading.set(true);
    this.service.getArtifacts(this.item.item.item_id).subscribe({
      next: (data) => {
        this.artifactsData.set(data);
        // Parse config files from artifacts
        const files: string[] = [];
        const contents: Record<string, string> = {};
        if (data.artifacts.config_files && Array.isArray(data.artifacts.config_files)) {
          for (const entry of data.artifacts.config_files) {
            if (entry && typeof entry === 'object') {
              const obj = entry as Record<string, unknown>;
              if (obj['kind']) {
                const filename = `${String(obj['kind'])}.json`;
                files.push(filename);
                contents[filename] = JSON.stringify(entry, null, 2);
              } else if (obj['name']) {
                const filename = `${String(obj['name'])}.json`;
                files.push(filename);
                contents[filename] = JSON.stringify(entry, null, 2);
              } else {
                files.push('config.json');
                contents['config.json'] = JSON.stringify(entry, null, 2);
              }
            } else if (typeof entry === 'string') {
              files.push(entry);
              contents[entry] = '';
            }
          }
        }
        this.configFiles.set(files);
        this.configContents.set(contents);
        this.artifactsLoading.set(false);

        // Auto-open README or integration snippet
        if (data.source_preview.available && data.source_preview.content) {
          this.openFile(data.source_preview.path || 'README.md');
        } else if (data.integration.available && data.integration.snippet) {
          const fname = this.getSourceFileName(data.integration.source);
          this.openFile(fname);
        } else {
          this.openFile('metadata.json');
        }
      },
      error: () => {
        this.artifactsLoading.set(false);
        // Fallback: open metadata.json
        this.openFile('metadata.json');
      },
    });
  }

  private loadSourceTree(): void {
    if (!this.item) return;
    this.treeLoading.set(true);
    this.service.getSourceTree(this.item.item.item_id).subscribe({
      next: (data) => {
        // Use real tree if available, otherwise build synthetic tree
        const flatTree = data.tree || [];
        if (flatTree.length > 0) {
          // Convert flat list to nested tree structure
          const nestedTree = this.buildNestedTree(flatTree);
          this.sourceTree.set(nestedTree);
          // Auto-expand root directories
          const rootDirs = nestedTree.filter(n => n.type === 'dir').map(n => n.path);
          this.expandedDirs.set(new Set(rootDirs));
        } else {
          // Build synthetic tree from available data
          const synthetic = this.buildSyntheticTree();
          this.sourceTree.set(synthetic);
          // Auto-expand synthetic directories
          const syntheticDirs = synthetic.filter(n => n.type === 'dir').map(n => n.path);
          this.expandedDirs.set(new Set(syntheticDirs));
        }
        this.treeLoading.set(false);
      },
      error: () => {
        // On error, still try synthetic tree
        const synthetic = this.buildSyntheticTree();
        this.sourceTree.set(synthetic);
        this.treeLoading.set(false);
      },
    });
  }

  private buildNestedTree(flatNodes: { path: string; type: string; size?: number | null }[]): SourceTreeNode[] {
    const root: SourceTreeNode[] = [];
    const nodeMap = new Map<string, SourceTreeNode>();

    // Sort to ensure parents come before children
    const sorted = [...flatNodes].sort((a, b) => a.path.localeCompare(b.path));

    for (const node of sorted) {
      const parts = node.path.split('/');
      const name = parts[parts.length - 1];
      const isDir = node.type === 'tree';
      const depth = parts.length - 1;

      const treeNode: SourceTreeNode = {
        name,
        path: node.path,
        type: isDir ? 'dir' : 'file',
        size: node.size ?? undefined,
        children: isDir ? [] : undefined,
      };

      if (depth === 0) {
        // Root level
        root.push(treeNode);
        nodeMap.set(node.path, treeNode);
      } else {
        // Find parent
        const parentPath = parts.slice(0, -1).join('/');
        const parent = nodeMap.get(parentPath);
        if (parent && parent.children) {
          parent.children.push(treeNode);
        } else {
          // Parent not found, add to root
          root.push(treeNode);
        }
        nodeMap.set(node.path, treeNode);
      }
    }

    // Sort: dirs first, then files, alphabetically
    return this.sortTree(root);
  }

  private sortTree(nodes: SourceTreeNode[]): SourceTreeNode[] {
    return nodes
      .sort((a, b) => {
        if (a.type !== b.type) {
          return a.type === 'dir' ? -1 : 1;
        }
        return a.name.localeCompare(b.name);
      })
      .map(node => {
        if (node.children) {
          node.children = this.sortTree(node.children);
        }
        return node;
      });
  }

  private buildSyntheticTree(): SourceTreeNode[] {
    if (!this.item) return [];
    const tree: SourceTreeNode[] = [];
    const item = this.item.item;

    // README file (from source_preview or integration)
    const artifacts = this.artifactsData();
    if (artifacts) {
      if (artifacts.source_preview.available && artifacts.source_preview.content) {
        tree.push({
          name: artifacts.source_preview.path || 'README.md',
          path: artifacts.source_preview.path || 'README.md',
          type: 'file',
          size: artifacts.source_preview.content.length,
        });
      }
      if (artifacts.integration.available && artifacts.integration.snippet) {
        const sourceName = this.getSourceFileName(artifacts.integration.source);
        tree.push({
          name: sourceName,
          path: sourceName,
          type: 'file',
          size: artifacts.integration.snippet.length,
        });
      }
      // Config files
      const configFiles = this.configFiles();
      if (configFiles.length > 0) {
        const configDir: SourceTreeNode = {
          name: 'config',
          path: 'config',
          type: 'dir',
          children: configFiles.map(f => ({
            name: f,
            path: `config/${f}`,
            type: 'file' as const,
            size: (this.configContents()[f] || '').length,
          })),
        };
        tree.push(configDir);
      }
    }

    // MCP Schema
    if (item.tool?.mcp_schema) {
      tree.push({
        name: 'schema.json',
        path: 'schema.json',
        type: 'file',
        size: JSON.stringify(item.tool.mcp_schema).length,
      });
    }

    // Agent Card
    if (item.agent?.agent_card) {
      tree.push({
        name: 'agent-card.json',
        path: 'agent-card.json',
        type: 'file',
        size: JSON.stringify(item.agent.agent_card).length,
      });
    }

    // Item metadata
    tree.push({
      name: 'metadata.json',
      path: 'metadata.json',
      type: 'file',
      size: JSON.stringify({
        item_id: item.item_id,
        name: item.name,
        type: item.type,
        description: item.description,
        version: item.version,
        status: item.status,
        source: item.source,
        provenance: item.provenance,
        reliability: item.reliability,
        discovery: item.discovery,
      }).length,
    });

    return tree;
  }

  private getSourceFileName(source?: string): string {
    if (!source) return 'integration.md';
    if (source.includes('npm')) return 'package.json';
    if (source.includes('pypi') || source.includes('pip')) return 'requirements.txt';
    if (source.includes('cargo') || source.includes('crates')) return 'Cargo.toml';
    if (source.includes('go')) return 'go.mod';
    return 'integration.md';
  }

  private loadSchema(): void {
    if (!this.item) return;
    this.schemaLoading.set(true);
    this.service.getSchema(this.item.item.item_id).subscribe({
      next: (data) => {
        this.schemaData.set(data.schema || null);
        this.agentCard.set(data.agent_card || null);
        this.schemaLoading.set(false);
      },
      error: () => {
        this.schemaLoading.set(false);
      },
    });
  }

  private loadProvenance(): void {
    if (!this.item) return;
    this.provenanceLoading.set(true);
    this.service.getProvenance(this.item.item.item_id).subscribe({
      next: (data) => {
        this.provenanceData.set(data);
        this.provenanceLoading.set(false);
      },
      error: () => {
        this.provenanceLoading.set(false);
      },
    });
  }

  onTreeNodeClick(node: SourceTreeNode): void {
    if (node.type === 'dir') {
      this.toggleDir(node.path);
    } else {
      this.openFile(node.path);
    }
  }

  isExpanded(path: string): boolean {
    return this.expandedDirs().has(path);
  }

  private toggleDir(path: string): void {
    const current = new Set(this.expandedDirs());
    if (current.has(path)) {
      current.delete(path);
    } else {
      current.add(path);
    }
    this.expandedDirs.set(current);
  }

  private openFile(path: string): void {
    this.currentFilePath.set(path);
    this.fileLoading.set(true);

    if (!this.item) return;

    // Handle synthetic files locally (no backend call)
    const syntheticContent = this.getSyntheticFileContent(path);
    if (syntheticContent !== null) {
      setTimeout(() => {
        this.fileLoading.set(false);
        this.setEditorContent(syntheticContent, this.getLanguageFromPath(path));
      }, 100);
      return;
    }

    // Try backend for real source files
    this.service.getSourceFile(this.item.item.item_id, path).subscribe({
      next: (data) => {
        this.fileLoading.set(false);
        // An empty file is still a successfully loaded source file.
        if (data.available && data.content !== null && data.content !== undefined) {
          this.setEditorContent(data.content, data.language || this.getLanguageFromPath(path));
        } else {
          this.setEditorContent(
            `// File not available\n// ${data.note || 'This file could not be loaded from the source repository.'}`,
            this.getLanguageFromPath(path)
          );
        }
      },
      error: () => {
        this.fileLoading.set(false);
        this.setEditorContent(
          `// Error loading file\n// ${path}`,
          this.getLanguageFromPath(path)
        );
      },
    });
  }

  private getSyntheticFileContent(path: string): string | null {
    if (!this.item) return null;
    const item = this.item.item;
    const artifacts = this.artifactsData();

    // Config files
    if (path.startsWith('config/')) {
      const filename = path.replace('config/', '');
      return this.configContents()[filename] || null;
    }

    // README / source preview
    if (artifacts?.source_preview.available && artifacts.source_preview.path === path) {
      return artifacts.source_preview.content || null;
    }
    if (path === 'README.md' && artifacts?.source_preview.available) {
      return artifacts.source_preview.content || null;
    }

    // Integration snippet
    if (artifacts?.integration.available && artifacts.integration.snippet) {
      const expectedName = this.getSourceFileName(artifacts.integration.source);
      if (path === expectedName) {
        const lang = this.getLanguageFromPath(path);
        return `# Integration Snippet\n\n${artifacts.integration.note ? `> ${artifacts.integration.note}\n\n` : ''}\`\`\`${lang === 'plaintext' ? '' : lang}\n${artifacts.integration.snippet}\n\`\`\`\n\n*Source: ${artifacts.integration.source || 'unknown'}*`;
      }
    }

    // MCP Schema
    if (path === 'schema.json' && item.tool?.mcp_schema) {
      return JSON.stringify(item.tool.mcp_schema, null, 2);
    }

    // Agent Card
    if (path === 'agent-card.json' && item.agent?.agent_card) {
      return JSON.stringify(item.agent.agent_card, null, 2);
    }

    // Item metadata
    if (path === 'metadata.json') {
      return JSON.stringify({
        item_id: item.item_id,
        name: item.name,
        type: item.type,
        description: item.description,
        version: item.version,
        status: item.status,
        source: item.source,
        provenance: item.provenance,
        reliability: item.reliability,
        discovery: item.discovery,
        tool: item.tool ? { server_id: item.tool.server_id, tool_name: item.tool.tool_name } : undefined,
        agent: item.agent ? {
          endpoint: item.agent.endpoint,
          skills: item.agent.skills,
          capabilities: item.agent.capabilities,
          declared_dependencies: item.agent.declared_dependencies,
        } : undefined,
      }, null, 2);
    }

    return null; // Not a synthetic file
  }

  private setEditorContent(content: string, language: string): void {
    if (!this.monacoEditor || !this.monaco) return;
    const model = this.monacoEditor.getModel();
    if (model) {
      this.monaco.editor.setModelLanguage(model, language);
      this.monacoEditor.setValue(content);
    }
  }

  private getLanguageFromPath(path: string): string {
    const ext = path.split('.').pop()?.toLowerCase() || '';
    const langMap: Record<string, string> = {
      ts: 'typescript',
      tsx: 'typescript',
      js: 'javascript',
      jsx: 'javascript',
      json: 'json',
      md: 'markdown',
      yaml: 'yaml',
      yml: 'yaml',
      toml: 'ini',
      py: 'python',
      rb: 'ruby',
      go: 'go',
      rs: 'rust',
      java: 'java',
      c: 'c',
      cpp: 'cpp',
      h: 'c',
      hpp: 'cpp',
      sh: 'shell',
      bash: 'shell',
      zsh: 'shell',
      fish: 'shell',
      dockerfile: 'dockerfile',
      makefile: 'makefile',
      tf: 'hcl',
      html: 'html',
      css: 'css',
      scss: 'scss',
      less: 'less',
      xml: 'xml',
      sql: 'sql',
      graphql: 'graphql',
      proto: 'protobuf',
    };
    if (path.toLowerCase() === 'dockerfile') return 'dockerfile';
    if (path.toLowerCase().startsWith('makefile')) return 'makefile';
    return langMap[ext] || 'plaintext';
  }

  setTab(tab: ViewCodeTab): void {
    this.activeTab.set(tab);
  }

  getConfigContent(filename: string): string {
    return this.configContents()[filename] || '';
  }

  onBackdropClick(event: MouseEvent): void {
    if ((event.target as HTMLElement).classList.contains('modal-backdrop')) {
      this.close.emit();
    }
  }
}
