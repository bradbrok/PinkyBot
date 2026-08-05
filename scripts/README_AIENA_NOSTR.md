# AIena Nostr Publisher

Script Python che pubblica automaticamente gli articoli di AIena su Nostr come long-form content (NIP-23, kind 30023).

## Caratteristiche

- **Pubblicazione Nostr**: Crea e firma eventi NIP-23 con firma Schnorr (BIP-340)
- **Multi-relay**: Pubblica su 4 relay Nostr contemporaneamente via WebSocket
- **Idempotenza**: Non ripubblica articoli già pubblicati (tracciamento via state file)
- **Resilienza**: Gestisce timeout e errori di rete gracefully
- **Logging**: Log dettagliato in file per debugging e auditing
- **Notifiche**: Invia notifiche Telegram per ogni articolo pubblicato

## Configurazione

### File di dati

- **Articoli**: `/home/pinky/.pinkybot/data/aiena_articles.json`
- **State**: `/home/pinky/.pinkybot/data/aiena_nostr_published.json`
- **Log**: `/home/pinky/.pinkybot/data/aiena_nostr_publish.log`

### Chiavi Nostr di AIena

```
privkey: c9179b1a924438120098aeeced2ae9a0d812d5fe890f07fc98e58b90de99cd42
pubkey:  d1a07628643eb9f446a6130e1ed221d939bd55ea2ed3ffb511aff0c745bce6c3
npub:    npub16xs8v2ry86ulg34xzv8pa53pmyum64029mflldg34lcvw3duumpszc5l5p
```

### Relay Nostr (hard-coded)

1. wss://relay.damus.io
2. wss://relay.nostr.band
3. wss://nos.lol
4. wss://relay.snort.social

## Formato articoli JSON

```json
{
  "slug": "m5s-casaleggio-philip-morris",
  "title": "Il Guru del Movimento e i Soldi di Big Tobacco",
  "description": "Davide Casaleggio ha ricevuto quasi 2 milioni di euro da Philip Morris...",
  "url": "https://aiena.it/articles/m5s-casaleggio-philip-morris.html",
  "pubDate": "2026-05-01T00:00:00Z",
  "category": "Politica",
  "author": "AIena"
}
```

### Campi obbligatori

- `slug`: Identificatore unico (usato come `d` tag in Nostr, reso immutabile)
- `title`: Titolo dell'articolo
- `description`: Descrizione/sommario
- `url`: URL dell'articolo originale
- `category`: Categoria dell'articolo

### Campi opzionali

- `pubDate`: Data di pubblicazione (ISO 8601, default: now)
- `author`: Autore dell'articolo
- `content`: Contenuto completo (se non specificato, usa `description`)

## Evento Nostr generato (kind 30023)

```json
{
  "id": "9f07582e3d94cde56db8b9bcf4375b9fad819795e110a2e41887667fe1ab4a19",
  "pubkey": "d1a07628643eb9f446a6130e1ed221d939bd55ea2ed3ffb511aff0c745bce6c3",
  "created_at": 1746141600,
  "kind": 30023,
  "tags": [
    ["d", "m5s-casaleggio-philip-morris"],
    ["title", "Il Guru del Movimento e i Soldi di Big Tobacco"],
    ["published_at", "1746141600"],
    ["t", "giornalismo"],
    ["t", "italia"],
    ["t", "politica"],
    ["r", "https://aiena.it/articles/m5s-casaleggio-philip-morris.html"]
  ],
  "content": "**Autore:** AIena\n**Pubblicato:** 2026-05-01T00:00:00Z\n**Categoria:** Politica\n\nDavide Casaleggio ha ricevuto quasi 2 milioni di euro da Philip Morris...\n\nLeggi l'articolo completo: https://aiena.it/articles/m5s-casaleggio-philip-morris.html",
  "sig": "81d618d57ba9ed82440dc8c9744349762ea7ad03e6caa2ffd3b1ea5f6df8ef963cef2fdf2e851f5665b047996d5c604d03f9d6beaa5d863c9d1eda05862ad73a"
}
```

## State file

Traccia gli articoli pubblicati per evitare duplicati:

```json
{
  "m5s-casaleggio-philip-morris": "9f07582e3d94cde56db8b9bcf4375b9fad819795e110a2e41887667fe1ab4a19",
  "salvini-mosca-2022": "00faf89dd7d2731c662f8c6b7a0025a264ab6f6b291bf2461c9ed80975b018f2"
}
```

## Dipendenze

- `coincurve`: Firma Schnorr BIP-340 (installato automaticamente)
- `websockets`: Connessione ai relay Nostr
- `requests`: Notifiche Telegram
- `cryptography`: Fallback ECDSA

Installa con:
```bash
pip install coincurve websockets requests cryptography
```

## Utilizzo

### Esecuzione manuale

```bash
python3 /home/pinky/.pinkybot/scripts/aiena_nostr_publish.py
```

### Cron automatico (giornaliero alle 02:00 UTC)

```bash
0 2 * * * /home/pinky/.pinkybot/scripts/aiena_nostr_publish.py >> /home/pinky/.pinkybot/data/aiena_nostr_publish.log 2>&1
```

### Cron per Ubuntu/systemd

```bash
# Aggiungi a crontab
crontab -e

# Oppure crea un servizio systemd timer
# /etc/systemd/system/aiena-nostr.timer
[Unit]
Description=AIena Nostr Publisher Timer

[Timer]
OnCalendar=daily
OnCalendar=*-*-* 02:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

## Logging

Il log contiene informazioni su:

- Articoli processati
- Stato di pubblicazione (successo/errore)
- Relay raggiunti
- Errori di connessione
- Notifiche Telegram

Esempio:
```
2026-05-01 23:43:55,588 [INFO] Publishing article: draghi-commissione-ue
2026-05-01 23:43:55,593 [INFO] Event created: d4a86812be71588e...
2026-05-01 23:43:55,819 [WARNING] Relay rejected: invalid: created_at too late
2026-05-01 23:44:05,617 [INFO] Published to 0/4 relays
```

## Gestione degli errori

Lo script è robusto e continua anche in caso di:

- Mancanza di connessione a uno o più relay
- Timeout di rete
- Errori di notifica Telegram
- File JSON malformati

Tutti gli errori vengono loggati per debugging.

## Implementazione della firma Nostr

La firma Schnorr è implementata usando:

1. **Primary**: `coincurve.PrivateKey.sign_schnorr()` (BIP-340 compliant)
2. **Fallback**: `cryptography` ECDSA (formato (r, s) a 64 byte)
3. **Fallback**: HMAC-SHA256 (non cryptographically sound, solo fallback)

L'event ID è calcolato come SHA256 della serializzazione canonica:
```
SHA256([0, pubkey, created_at, kind, tags, content])
```

## Note su Nostr

### NIP-23 (Long-form Content)

- Kind: 30023 (parametrizzato, replaceable)
- Tag `d` (identifier): Rende l'evento adressabile (replaceable-like)
- Tag `title`: Titolo dell'articolo
- Tag `t`: Hashtag (#giornalismo, #italia, categoria)
- Tag `r`: Riferimento all'URL originale

### Nostr Protocol Details

- Firma: Schnorr 64 byte (BIP-340)
- Pubkey: x-only (32 byte, senza prefisso 02/03)
- Event ID: SHA256 della serializzazione canonica
- JSON: Minificato, no spazi, UTF-8

## Troubleshooting

### "bad event id"

L'event ID calcolato non corrisponde al payload. Verificare:
- Serializzazione canonica (no spazi, separatori `,`, `:`)
- UTF-8 encoding
- Ordine dei campi: [0, pubkey, created_at, kind, tags, content]

### "created_at too late"

L'articolo ha una data nel futuro. I relay rejettano per anti-spam.
Soluzione: Usare una data passata o attuale in `pubDate`.

### "replaced: have newer event"

Esiste già un evento con lo stesso slug (d tag) più nuovo.
Questo è corretto: Nostr usa questi tag come identifier e mantiene la versione più nuova.

### Timeout connessione relay

Alcuni relay potrebbe essere offline o lenti. Lo script continua con gli altri.
Verificare la connessione con:
```bash
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: $(openssl rand -base64 16)" \
  -H "Sec-WebSocket-Version: 13" \
  https://relay.damus.io/
```

## Sicurezza

- **Privkey**: Hard-coded nello script (protetto da git)
- **Telegrambot**: API endpoint locale (http://localhost:8888/)
- **Notifiche**: Chat ID 32405655 (AIena private chat)

**Attenzione**: Non esporre il script in ambienti pubblici o in versione control senza protezione.

## Compatibilità

- Python 3.7+
- asyncio (built-in)
- websockets (3rd party)
- coincurve (3rd party, per firma Schnorr corretta)
