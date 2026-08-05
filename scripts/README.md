# AIena.it — Ecosistema Scripts

Guida in italiano. Spiega cosa fa ogni parte del sistema, come si collegano, e perché esistono.
Nessun codice. Solo spiegazioni.

---

## Cos'è questo sistema

AIena è una testata giornalistica automatizzata. Il sito è `aiena.it`. Gli articoli vengono scritti da un agente AI (Segugio), approvati da Mirko tramite un pannello di amministrazione, e pubblicati automaticamente da questi script.

Tutto gira su un server Linux con cron job (compiti programmati) che eseguono i vari script a orari fissi.

---

## Il Pipeline: come nasce un articolo

```
SEGNALE → LEAD → INDAGINE → ARTICOLO SCRITTO → PREVIEW → APPROVAZIONE → PUBBLICAZIONE
```

### 1. SEGNALE
Fonti esterne (Bluesky, RSS, ANAC, etc.) vengono monitorate automaticamente.
Script responsabile: `aiena_signals/aiena_signal_processor.py`

Quando trova qualcosa di interessante (score ≥ 60), lo trasforma in un **lead**.

### 2. LEAD
Un lead è un'idea di indagine. Viene salvato nel file `pipeline.json` nella sezione `leads[]`.
Ha una priorità (altissima / alta / media / bassa) e uno stato (es. "nuovo", "valutando").

Per aggiungere un lead manualmente si usa: `aiena_notify_lead.py`
Per aggiungerlo via API si usa: `POST http://localhost:8888/api/leads/create`

### 3. INDAGINE
Quando Mirko decide di trasformare un lead in un'indagine vera, questa passa nella sezione
`investigations[]` del pipeline. Da qui viene assegnata a Segugio per la scrittura.

### 4. ARTICOLO SCRITTO + PREVIEW
Segugio scrive l'articolo. Satoshi lo carica come file HTML in `/var/www/aiena.it/preview/`
e registra l'approvazione pendente su Supabase (database cloud).

Il file è visibile all'URL: `aiena.it/preview/nome-articolo.html`

### 5. APPROVAZIONE
Mirko va su `admin.aiena.it` e clicca "Approva" sull'articolo in preview.
Questo aggiorna il record su Supabase con `status = 'approved'`.

⛔ L'approvazione avviene SOLO tramite pannello admin. Mai tramite Telegram o altri canali.

### 6. PUBBLICAZIONE
Il cron alle 09:30 di martedì esegue `aiena_auto_publish.py`.
Questo script legge Supabase, trova l'articolo approvato, e lo pubblica sul sito.

---

## Il File pipeline.json

È il "cervello" del sistema. Contiene:

- `leads[]` — idee di indagine non ancora iniziate
- `investigations[]` — indagini in corso
- `published[]` — articoli già pubblicati
- `archived[]` — materiale archiviato
- `events[]` — log di eventi (chi ha fatto cosa e quando)
- `urgente_card` — eventuale articolo "fuori programma" attivo in homepage

Vive su due posti:
- In locale: `/var/www/aiena.it/data/pipeline.json` (originale)
- Su FTP: `aiena.it/data/pipeline.json` (copia pubblica per il sito)

---

## Il Pannello Admin

URL: `admin.aiena.it`

È un'interfaccia web che Mirko usa per:
- Vedere gli articoli in preview
- Approvare o rifiutare articoli
- Vedere lo stato di AIena (heartbeat, ultimo articolo, etc.)

Legge i dati da `aiena.it/admin/data.json` — un file JSON generato dagli script.

### Come si aggiorna data.json
Lo script `update_admin_data.py` scansiona le cartelle preview/ e articles/, legge
lo stato del sistema, e genera il file JSON. Poi lo manda via FTP al sito.

Viene eseguito:
- Automaticamente ogni volta che Satoshi carica un nuovo articolo
- Dal cron ogni 47 minuti (`aiena_admin_rebuild.py`)

---

## Pubblicazione Normale

Script: `aiena_auto_publish.py`
Cron: martedì alle 09:30

Cosa fa:
1. Controlla se c'è un articolo approvato su Supabase
2. Sposta il file da `preview/` a `articles/`
3. Aggiorna la homepage (hero + griglia "In corso")
4. Aggiorna l'archivio
5. Manda tutto via FTP
6. Timbratura OTS (blockchain timestamp per autenticità)
7. Notifica Mirko su Telegram

---

## Fuori Programma (Articoli Urgenti)

Script: `aiena_urgente_processor.py`
Cron: ogni 2 minuti

Serve per articoli che non possono aspettare il martedì.

Come funziona:
1. Mirko (o uno script) imposta su Supabase `status = 'urgente_pending'`
2. Il cron lo intercetta entro 2 minuti
3. Pubblica l'articolo come normale
4. Aggiunge una "urgente card" sopra la griglia in homepage (dura 72 ore)

**Limite:** esiste solo un posto per la urgente card. Se ne arriva una seconda, sovrascrive la prima.

---

## Distribuzione sui Social

Dopo la pubblicazione, `aiena_post_publish.py` coordina la distribuzione:

- **Bluesky**: `aiena_bsky_publisher.py` posta l'articolo su @aiena-it.bsky.social
- **Nostr**: `aiena_nostr_publish.py` posta sui relay decentralizzati
- **X (Twitter)**: `aiena_x_publisher.py` (se configurato)

---

## Segnali e Monitoraggio Fonti

Questi script girano in background e cercano nuove storie:

| Script | Cosa monitora |
|--------|---------------|
| `aiena_signal_processor.py` | Bluesky, RSS, fonti varie — valuta e crea lead |
| `aiena_backlog_scout.py` | Controlla il backlog delle indagini in attesa |
| `aiena_source_monitor.py` | Monitora fonti specifiche configurate |
| `aiena_bsky_signal_extractor.py` | Estrae segnali da Bluesky |
| `aiena_enriched_scout.py` | Arricchisce i lead con dati aggiuntivi |

---

## Ticket e Segnalazioni

I lettori possono mandare segnalazioni attraverso il sito. Queste vengono salvate su Supabase
nella tabella `ticket_messages`.

- `aiena_ticket_dispatcher.py` — smista i ticket nuovi
- `aiena_ticket_notifier.py` / `aiena_tickets_notifier.py` — notifica AIena
- `supabase_ticket_reply.py` — permette ad AIena di rispondere ai ticket
- `aiena_tips_notifier.py` — gestisce le segnalazioni anonime (tips)

AIena legge e risponde ai ticket in prima persona (tono giornalistico, non da chatbot).

---

## Monitoraggi Speciali

| Script | Cosa fa |
|--------|---------|
| `sacrocuore_gineco_monitor.py` | Monitora disponibilità appuntamenti ginecologia Sacro Cuore |
| `sacrocuore_ortopedia_monitor.py` | Monitora disponibilità appuntamenti ortopedia Sacro Cuore |
| `email_monitor.py` | Monitora inbox email per nuovi messaggi |
| `crontab_health_check.py` | Verifica che i cron job siano attivi e funzionanti |

---

## Report e Manutenzione

| Script | Quando gira | Cosa fa |
|--------|-------------|---------|
| `aiena_daily_brief.py` | Mattina ogni giorno | Report quotidiano a Mirko |
| `aiena_daily_report.py` | Fine giornata | Riepilogo attività |
| `aiena_friday_report.py` | Venerdì | Report settimanale |
| `aiena_health_monitor.py` | Ogni ora | Controlla che il sito sia online |
| `aiena_heartbeat.py` | Ogni 5 min | Segnala che AIena è attiva |
| `aiena_pipeline_backup.py` | Ogni giorno | Backup di pipeline.json |
| `aiena_git_backup.py` | Periodicamente | Backup su git |
| `aiena_validate_feed.py` | Periodicamente | Valida che RSS e feed siano corretti |

---

## Strumenti di Supporto

| Script | Cosa fa |
|--------|---------|
| `aiena_notify_lead.py` | Aggiunge un lead manualmente al pipeline (command line) |
| `aiena_ots_stamp.py` | Timbra un file su blockchain OpenTimestamps (prova di esistenza) |
| `aiena_ots_verify.py` | Verifica la validità di un timestamp OTS |
| `generate_card_v3.py` | Genera l'immagine di copertina per un articolo (social card) |
| `aiena_update_ticker.py` | Aggiorna il ticker di notizie in homepage |
| `aiena_stats_generator.py` | Genera statistiche del sito |
| `aiena_rss_update.py` | Aggiorna il feed RSS del sito |
| `affiliate_link_watchdog.py` | Controlla che i link affiliati siano funzionanti |
| `aiena_indagini_rebuild.py` | Rigenera la pagina indagini.html da zero |
| `aiena_weekly_trigger.py` | Trigger settimanale per review indagini |
| `aiena_investigations_daily_review.py` | Review giornaliera dello stato delle indagini |
| `aiena_stale_approver.py` | Gestisce approvazioni rimaste in sospeso troppo a lungo |

---

## API Lead (nuovo — maggio 2026)

Endpoint HTTP per aggiungere lead in modo sicuro:

- **URL**: `POST http://localhost:8888/api/leads/create`
- **Chi lo usa**: `aiena_notify_lead.py`, `aiena_signal_processor.py`, o chiunque voglia aggiungere un lead
- **Cosa fa**: scrive atomicamente nel pipeline.json, deduplica, notifica via Telegram, aggiorna il pannello admin
- **Vantaggio**: punto di ingresso unico — nessuno scrive direttamente nel JSON a mano

---

## Infrastruttura FTP

Il sito è su hosting FTP. Gli script usano `ftplib` di Python per caricare i file.
Le credenziali FTP sono in variabili d'ambiente o nel file di configurazione locale.

Script che fanno deploy via FTP:
- `aiena_auto_publish.py`
- `aiena_urgente_processor.py`
- `update_admin_data.py`
- `aiena_admin_rebuild.py`
- `aiena_rss_update.py`

---

## Cartelle Importanti

```
/var/www/aiena.it/           ← sito locale (masterfile)
  index.html                 ← homepage
  articles/                  ← articoli pubblicati
  preview/                   ← articoli in attesa di approvazione
  admin/index.html           ← pannello admin (master)
  data/pipeline.json         ← cervello del sistema

/var/www/aiena-admin/        ← pannello admin servito da nginx (copia)
  index.html

/home/pinky/.pinkybot/scripts/   ← tutti gli script
/home/pinky/.pinkybot/data/logs/ ← log di tutto
```

---

## Supabase (Database Cloud)

Tabelle usate:

| Tabella | Cosa contiene |
|---------|---------------|
| `article_approvals` | Articoli in preview/approvati/pubblicati |
| `ticket_messages` | Segnalazioni e messaggi dei lettori |

URL progetto: `fwyjxolljcogblvwvfca.supabase.co`

---

*Documentazione scritta da Satoshi — 7 maggio 2026*
