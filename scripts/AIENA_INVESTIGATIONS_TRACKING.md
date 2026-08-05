# AIena Investigations Tracking System

## Overview

Sistema automatico di monitoraggio delle indagini per AIena. Scansiona i DIARY.md delle indagini e invia report giornalieri tramite broker interno.

## Components

**Script**: `/home/pinky/.pinkybot/scripts/aiena_investigations_daily_review.py`

**Cron**: Esecuzione giornaliera alle 08:00
```
0 8 * * * /home/pinky/.pinkybot/.venv/bin/python3 /home/pinky/.pinkybot/scripts/aiena_investigations_daily_review.py >> /home/pinky/.pinkybot/data/logs/aiena_investigations.log 2>&1
```

**Log**: `/home/pinky/.pinkybot/data/logs/aiena_investigations.log`

**Source Directory**: `/home/pinky/.pinkybot/data/agents/aiena/indagini/`

## Functionality

Lo script esegue tre verifiche ogni volta che viene lanciato:

### 1. Pending Actions Vecchie (>24 ore)
- Scansiona tutti i DIARY.md delle indagini
- Estrae righe con pattern `- [ ]` (pending)
- Estrae data dalla riga o dalla sezione precedente
- Segnala azioni pendenti da più di 24 ore

**Parsing**: Supporta formati:
- `[YYYY-MM-DD]` (tra parentesi)
- `YYYY-MM-DD` (plain date)
- Pattern `## YYYY-MM-DD` (sezioni Markdown)

### 2. DIARY.md Mancante
- Controlla se ogni cartella in `indagini/` ha un DIARY.md
- Segnala cartelle senza diary (caso non inizializzato)

### 3. Casi Inattivi (>7 giorni)
- Controlla mtime di ogni DIARY.md
- Se non modificato da >7 giorni, segnala inattività
- Utile per identificare indagini stagnanti

## Output

**Report Format**:
```
📋 DAILY REVIEW INDAGINI — [DATA]

⚠️ PENDING ACTIONS (>24h):
• [caso]: [azione] [[data]] ([giorni]gg)

🔴 DIARY.md MANCANTE:
• indagini/[cartella]/

💤 CASI INATTIVI (>7gg):
• [caso]: ultima modifica [data] ([giorni]gg)

[N] pending, [M] alert, [K] inattivi
```

**Invio**: Via HTTP POST al broker interno:
- URL: `http://localhost:8888/broker/send`
- From: `satoshi`
- To: `aiena`
- Message: Report formattato

**Comportamento Silenzioso**: Se non ci sono problemi, lo script non invia alcun report (zero noise).

## Uso Manuale

```bash
# Dry-run (stampa su stdout, non invia)
python3 aiena_investigations_daily_review.py --dry-run

# Esecuzione normale (invia al broker)
python3 aiena_investigations_daily_review.py
```

## DIARY.md Structure

Ogni indagine deve avere un file `DIARY.md` con struttura:

```markdown
# Titolo Caso

## YYYY-MM-DD

Descrizione della giornata.

- [x] Azione completata
- [ ] Azione pending [YYYY-MM-DD]
- [ ] Altra azione [YYYY-MM-DD]

## YYYY-MM-DD

...
```

**Regole**:
- Sezioni marcate con `## YYYY-MM-DD`
- Azioni pending con `- [ ]` (checkbox vuoto)
- Data della azione: nella stessa riga (tra parentesi) oppure dalla sezione precedente
- Le azioni completate `- [x]` sono ignorate

## Logging

Tutte le operazioni vengono logate in `/home/pinky/.pinkybot/data/logs/aiena_investigations.log`:

```
2026-05-06 19:16:27,965 [INFO] === Avvio Daily Review Indagini ===
2026-05-06 19:16:28,228 [WARNING] Caso inattivo: caso-freddo (10gg)
2026-05-06 19:16:28,228 [INFO] Pending action old: test-caso - - [ ] Contattare testimone principale...
2026-05-06 19:16:28,229 [INFO] Daily review completata con successo
```

## Test

Directory test: `/home/pinky/.pinkybot/data/agents/aiena/indagini/test-caso/`

Con DIARY.md contenente:
- Pending actions da 5-16 giorni fa
- Utile per validare il parsing e il report format

Esecuzione test:
```bash
python3 aiena_investigations_daily_review.py --dry-run
```

Output atteso: Report con 4 pending actions, 0 alert, 0 inattivi.

## Implementation Notes

- **Stdlib only**: os, re, json, datetime, pathlib, urllib
- **Error handling**: Continua anche se un DIARY.md è corrotto
- **Timezone**: Usa system timezone (deve essere configurato correttamente nel server)
- **Atomicity**: Nessuno stato persistente tra esecuzioni
- **Performance**: O(n) dove n = numero di righe in tutti i DIARY.md

## Future Enhancements

- Alert via Telegram per casi critici (action molto vecchia)
- Dashboard web per visualizzare status indagini
- Integration con Git per versioning DIARY.md
- Categorizzazione per urgenza/priorità
