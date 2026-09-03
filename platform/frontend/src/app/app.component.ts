import { Component } from '@angular/core';
import { DiscoveryPageComponent } from './discovery/discovery-page.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [DiscoveryPageComponent],
  template: `<app-discovery-page></app-discovery-page>`,
})
export class AppComponent {}
