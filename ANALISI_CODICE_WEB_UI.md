# Analisi Dettagliata - Codice Web UI PinkyBot

## 🏗️ Architettura Generale

La web UI di PinkyBot è costruita con **Svelte 5 SPA** (Single Page Application) e segue questi principi:

- **Frontend**: `frontend-svelte/` — Interfaccia utente reattiva
- **Backend**: `src/pinky_daemon/` — API FastAPI
- **Router**: `svelte-spa-router` — Client-side routing (hash-based)
- **Styling**: Inline styles (no CSS framework) + monospace aesthetic
- **i18n**: `svelte-i18n` — Supporto multilingue

---

## 📄 File Principale: `App.svelte`

### Funzione
È il componente root dell'intera applicazione. Gestisce:
1. **Routing** — Mappa URL → Pagine
2. **Autenticazione** — Rileva pagine login/setup vs. applicazione principale
3. **Page Detection** — Distingue tra pagine pubbliche e autenticate

### Flow Logico

```
detectPage()
  ├─ Legge window.location.pathname/hash
  └─ Ritorna: { auth: 'login'|'setup'|'', landing: boolean }

App Mount (onMount)
  ├─ Inizializza authPage e isLanding
  ├─ Attiva event listeners per popstate/hashchange
  └─ Aggiorna automaticamente quando l'URL cambia

Rendering Condizionale
  ├─ Se pagina login → <Login />
  ├─ Se pagina setup → <Setup />
  ├─ Se landing → <Landing />
  └─ Altrimenti → <Layout><Router /></Layout>
```

### Route Map (15 rotte totali)

| Rotta | Componente | Descrizione |
|-------|-----------|-------------|
| `/` | Dashboard | Pagina principale |
| `/dashboard` | Dashboard | Alias di `/` |
| `/chat` | Chat | Chat generale |
| `/chat/:agent` | Chat | Chat con agente specifico |
| `/agents` | Agents | Gestione agenti (fleet consolidato) |
| `/fleet` | Agents | Redirect a `/agents` |
| `/tasks` | Tasks | Gestione task |
| `/research` | Research | Ricerca e analisi |
| `/knowledge-base` | KnowledgeBase | Knowledge base |
| `/memories` | Memories | Memoria degli agenti |
| `/people` | People | Gestione persone |
| `/analytics` | Analytics | Dashboard analitico |
| `/settings` | Settings | Impostazioni |
| `/presentations` | Presentations | Presentazioni |
| `/projects/:id` | ProjectHub | Hub progetti specifici |

---

## 🎨 File: `Layout.svelte`

### Funzione
Fornisce la **struttura shell** dell'app:
- Sidebar navigazione
- Main content area
- Toast notifications
- Gestione tema (dark/light)

### Componenti Principali

#### 1. **Sidebar Navigation** (240px fixed, sticky top)
```svelte
<aside class="sidebar">
  ├─ Branding (PINKY. + versione)
  ├─ Navigation Links (11 link principali)
  ├─ Active Agents List (mostra agenti streaming)
  └─ Footer (theme toggle, logout, status)
</aside>
```

**Navigation Links:**
- Dashboard
- Chat
- Agents (gestione fleet)
- Tasks
- Research
- Knowledge Base
- Presentations
- People
- Memories
- Analytics
- Settings

#### 2. **Agent Status Indicator**
```
agent.streaming === true  → Dot verde (alive)
agent.streaming === false → Dot grigio (idle)
```
Permette di vedere a colpo d'occhio quali agenti sono attivi.

#### 3. **Main Content Area**
```svelte
<main class="main-content">
  <slot /> <!-- Componente pagina corrente -->
</main>
```

### Flow di Inizializzazione (onMount)

```javascript
onMount(async () => {
  // 1. Carica dati in parallelo
  const [root, auth, agentsResp] = await Promise.all([
    api('GET', '/api'),              // Info server (versione)
    api('GET', '/auth/status'),      // Status autenticazione
    api('GET', '/agents')            // Lista agenti
  ]);

  // 2. Estrae dati
  statusText = `v${root.version}`;
  authenticated = !!auth.authenticated;
  agents = agentsResp?.agents || [];

  // 3. First-run onboarding check
  if (authenticated) {
    const obs = await api('GET', '/system/onboarding-status');
    if (!obs.onboarding_completed && !obs.has_agents) {
      window.location.hash = '#/onboarding';
    }
  }
});
```

### Responsive Design

**Desktop (>1024px)**
- Sidebar sempre visibile (240px)
- Mobile hamburger nascosto

**Mobile (<1024px)**
- Hamburger button top-left (fixed)
- Sidebar hidden di default
- Sidebar slide-in on click (overlay scrim)
- Chiudi sidebar: click overlay, ESC key, click link

### Styling Key Points

**Theme System**
```css
--surface-1, --surface-2          /* Background colors */
--text-primary, --text-muted      /* Text colors */
--primary-container               /* Action buttons */
--yellow (brand color)            /* PINKY. branding */
--green                           /* Agent alive indicator */
--overlay-scrim                   /* Mobile overlay */
```

**Z-index Stack**
```
50 - Mobile hamburger
45 - Sidebar
40 - Sidebar overlay
```

---

## 🔄 Data Flow

### Caricamento Iniziale
```
App Mount
  ↓
detectPage() → determina se è pagina pubblica o autenticata
  ↓
Se autenticata → Layout Mount
  ↓
Layout carica: /api, /auth/status, /agents in parallelo
  ↓
Aggiorna stato (statusText, authenticated, agents)
  ↓
Se first-run + no agents → redirect onboarding
  ↓
Render Router con route corrente
```

### Navigazione Runtime
```
User clicca link
  ↓
updatePath() ascolta hashchange event
  ↓
currentPath store si aggiorna
  ↓
Template re-renders (sidebar mostra active state)
  ↓
Router monta nuovo componente pagina
```

---

## 📦 Dipendenze Critiche

| Dipendenza | Utilizzo | Critico |
|-----------|----------|---------|
| `svelte` | Framework UI | Sì |
| `svelte-spa-router` | Client-side routing | Sì |
| `svelte-i18n` | Multilingual UI | No (fallback: English) |
| `svelte/store` | State management (reactive) | Sì |

---

## ⚠️ Punti Critici da Monitorare

1. **Error Handling**: agents API fallisce silenziosamente (`.catch()`)
   - L'app continua anche se non carica agenti
   - Utente vede sidebar senza agenti attivi

2. **First-run Redirect**: Onboarding redirect avviene dopo Layout mount
   - Peut causare flash di contenuto se lento
   - Dovrebbe essere cached dopo primo onboarding

3. **Mobile UX**: Overlay scrim non previene scroll del body
   - Considerar aggiungere `body { overflow: hidden }` quando sidebar aperta

4. **Auth State**: Non c'è refresh automatico del token
   - Session può scadere senza notifica
   - Dovrebbe fare re-check periodico

---

## 🎯 Flusso Utente Standard

### First-Time User
```
Load App
  → No agents → Redirect onboarding
  → Complete onboarding (crea agente)
  → Redirect dashboard
```

### Logged-in User
```
Load App
  → Load agents list
  → Show sidebar con agenti attivi
  → Display dashboard
  → Naviga tramite sidebar links
```

### Mobile User
```
Load App (viewport <1024px)
  → Hamburger visible
  → Click hamburger
  → Sidebar slide-in con overlay
  → Click link
  → Sidebar auto-close
  → Render pagina
```

---

## 🔧 Suggerimenti Implementativi per Mirko

### Se aggiungere nuova pagina:
1. Crea file in `frontend-svelte/src/pages/NuovaPagina.svelte`
2. Importa in `App.svelte`
3. Aggiungi rotta in `routes` object
4. Aggiungi link in `Layout.svelte` navLinks

### Se modificare styling:
- Usa variabili CSS definite in `app.css`
- Mantieni monospace aesthetic (font-family consistency)
- Rispetta z-index stack

### Se aggiungere agente monitoring:
- Sfrutta già presente `agent.streaming` boolean
- Estendi indicator con tooltip mostrando status dettagliato

---

## 📊 Summary Architetturale

```
┌─────────────────────────────────────────┐
│ App.svelte (Router + Auth Detection)    │
└───────────────┬─────────────────────────┘
                │
    ┌───────────┴────────────┐
    │                        │
┌───▼───────────────┐  ┌─────▼──────────┐
│ Layout.svelte     │  │ Public Pages   │
│ (Sidebar Shell)   │  │ (Login/Setup)  │
└───┬───────────────┘  └────────────────┘
    │
    └──► Router → Pages (Dashboard, Chat, Agents, etc.)
         │
         └──► API calls to /api/* endpoints
```

---

**Documento generato:** 2026-07-24  
**Versione:** PinkyBot Frontend Svelte 5 SPA  
**Per:** Mirko (Analisi strutturale e architetturale)
