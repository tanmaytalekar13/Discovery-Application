import { HttpClient } from '@angular/common/http';
import { CommonModule } from '@angular/common';
import { Component, inject } from '@angular/core';
import { catchError, map, of } from 'rxjs';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [CommonModule],
  template: `
    <main>
      <p class="eyebrow">Agentic Discovery Platform</p>
      <h1>Find the right agentic building block.</h1>
      <p class="status" [class.offline]="(status$ | async) === 'offline'">
        Backend: {{ status$ | async }}
      </p>
    </main>
  `,
  styles: [
    `
      :host { display: block; min-height: 100vh; }
      main { max-width: 760px; margin: 0 auto; padding: 12vh 2rem; }
      .eyebrow { color: #d5653f; font: 700 0.75rem/1.2 monospace; letter-spacing: 0.08em; text-transform: uppercase; }
      h1 { max-width: 12ch; color: #132d35; font: 700 clamp(3rem, 8vw, 6.5rem)/0.92 Georgia, serif; margin: 2rem 0; }
      .status { color: #246b53; font-weight: 700; }
      .offline { color: #b13c35; }
    `,
  ],
})
export class AppComponent {
  private readonly http = inject(HttpClient);
  readonly status$ = this.http.get<{ status: string }>('/health').pipe(
    map((health) => (health.status === 'ok' ? 'connected' : 'offline')),
    catchError(() => of('offline')),
  );
}
