-- AIena Knowledge Graph Schema
-- Database: aiena_knowledge_graph.db
-- Versione: 1.0 (prototipo)

-- ============================================================
-- TABELLA: entities
-- Memorizza tutte le entita conosciute (persone, aziende, enti, luoghi, appalti)
-- ============================================================
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Identificatori
    name TEXT NOT NULL,                      -- Nome normalizzato (uppercase, senza spazi extra)
    name_original TEXT NOT NULL,             -- Nome originale come trovato
    entity_type TEXT NOT NULL,               -- PERSONA | AZIENDA | ENTE_PUBBLICO | INDIRIZZO | APPALTO | ALTRO

    -- Identificatori unici italiani (opzionali)
    codice_fiscale TEXT,                     -- CF persona o P.IVA azienda (11 o 16 caratteri)
    partita_iva TEXT,                        -- P.IVA separata se diversa da CF
    cig TEXT,                                -- Codice Identificativo Gara (se appalto)

    -- Metadati
    metadata TEXT,                           -- JSON con dati aggiuntivi (indirizzo, date, ruoli)

    -- Tracking
    confidence REAL DEFAULT 1.0,             -- Confidenza identificazione (0.0-1.0)
    first_seen_at TEXT NOT NULL,             -- Timestamp prima apparizione
    last_updated_at TEXT NOT NULL,           -- Timestamp ultimo aggiornamento

    -- Flags
    is_suspicious INTEGER DEFAULT 0,         -- 1 se flaggato come sospetto
    investigation_count INTEGER DEFAULT 0,   -- Quante indagini menzionano questa entita

    UNIQUE(name, entity_type)
);

-- ============================================================
-- TABELLA: relations
-- Memorizza le relazioni tra entita (chi-cosa-quando-come)
-- ============================================================
CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Entita coinvolte
    entity1_id INTEGER NOT NULL,
    entity2_id INTEGER NOT NULL,

    -- Tipo relazione
    relation_type TEXT NOT NULL,             -- VINCE | AMMINISTRA | POSSIEDE | LAVORA_PER |
                                             -- COLLABORA | PAGA | RICEVE | LOCALIZZATO_IN |
                                             -- CORRELATO | SUCCESSORE_DI | CONTROLLATO_DA

    -- Dettagli
    description TEXT,                        -- Descrizione testuale della relazione
    weight REAL DEFAULT 1.0,                 -- Peso/importanza (1.0 = normale, >1 = forte)

    -- Provenienza
    source_type TEXT NOT NULL,               -- ANAC | SEGNALAZIONE | ARTICOLO | REGISTRO_IMPRESE | ALTRO
    source_url TEXT,                         -- URL fonte
    source_date TEXT,                        -- Data del documento fonte

    -- Tracking
    confidence REAL DEFAULT 1.0,             -- Confidenza relazione (0.0-1.0)
    created_at TEXT NOT NULL,

    -- Flags
    is_verified INTEGER DEFAULT 0,           -- 1 se verificata manualmente
    is_suspicious INTEGER DEFAULT 0,         -- 1 se la relazione e sospetta

    FOREIGN KEY (entity1_id) REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY (entity2_id) REFERENCES entities(id) ON DELETE CASCADE
);

-- ============================================================
-- TABELLA: entity_sources
-- Traccia da quali fonti proviene ogni informazione su un'entita
-- ============================================================
CREATE TABLE IF NOT EXISTS entity_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    entity_id INTEGER NOT NULL,

    -- Fonte
    source_type TEXT NOT NULL,               -- ANAC | SEGNALAZIONE | ARTICOLO | REGISTRO_IMPRESE | ALTRO
    source_name TEXT NOT NULL,               -- Nome descrittivo (es. "ANAC Gare 2025")
    source_url TEXT,                         -- URL
    source_date TEXT,                        -- Data documento

    -- Cosa abbiamo appreso
    info_type TEXT NOT NULL,                 -- ESISTENZA | INDIRIZZO | RUOLO | APPALTO | ALTRO
    info_value TEXT,                         -- Il dato specifico appreso

    -- Tracking
    extracted_at TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,

    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);

-- ============================================================
-- TABELLA: anomaly_flags
-- Memorizza le anomalie rilevate dal detector
-- ============================================================
CREATE TABLE IF NOT EXISTS anomaly_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Riferimento entita o appalto
    entity_id INTEGER,                       -- Entita coinvolta (opzionale)
    cig TEXT,                                -- CIG appalto (opzionale)

    -- Anomalia
    anomaly_type TEXT NOT NULL,              -- RIBASSO_ANOMALO | ZERO_CONCORRENZA | VINCITORE_RICORRENTE |
                                             -- AZIENDA_NEONATA | VARIANTE_ECCESSIVA | CONNESSIONE_SOSPETTA
    anomaly_score REAL NOT NULL,             -- Score 0.0-1.0 (1.0 = molto anomalo)
    description TEXT NOT NULL,               -- Descrizione leggibile

    -- Dati supporto
    data_json TEXT,                          -- JSON con dati raw che supportano l'anomalia

    -- Tracking
    detected_at TEXT NOT NULL,
    reviewed INTEGER DEFAULT 0,              -- 1 se gia vista
    dismissed INTEGER DEFAULT 0,             -- 1 se scartata come falso positivo

    -- Riferimento indagine
    pipeline_slug TEXT,                      -- Se aggiunta al backlog

    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE SET NULL
);

-- ============================================================
-- TABELLA: investigation_links
-- Collega entita/anomalie alle indagini in pipeline.json
-- ============================================================
CREATE TABLE IF NOT EXISTS investigation_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Riferimenti
    pipeline_slug TEXT NOT NULL,             -- slug dell'indagine in pipeline.json
    entity_id INTEGER,                       -- Entita collegata
    anomaly_id INTEGER,                      -- Anomalia collegata
    ticket_code TEXT,                        -- Segnalazione collegata

    -- Tipo link
    link_type TEXT NOT NULL,                 -- MENTIONS | INVESTIGATES | ORIGINATED_FROM

    -- Tracking
    created_at TEXT NOT NULL,

    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE SET NULL,
    FOREIGN KEY (anomaly_id) REFERENCES anomaly_flags(id) ON DELETE SET NULL
);

-- ============================================================
-- INDICI per performance
-- ============================================================

-- Ricerche per nome/tipo
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_cf ON entities(codice_fiscale);
CREATE INDEX IF NOT EXISTS idx_entities_piva ON entities(partita_iva);
CREATE INDEX IF NOT EXISTS idx_entities_cig ON entities(cig);
CREATE INDEX IF NOT EXISTS idx_entities_suspicious ON entities(is_suspicious);

-- Ricerche relazioni
CREATE INDEX IF NOT EXISTS idx_relations_entity1 ON relations(entity1_id);
CREATE INDEX IF NOT EXISTS idx_relations_entity2 ON relations(entity2_id);
CREATE INDEX IF NOT EXISTS idx_relations_type ON relations(relation_type);
CREATE INDEX IF NOT EXISTS idx_relations_source ON relations(source_type);

-- Ricerche fonti
CREATE INDEX IF NOT EXISTS idx_sources_entity ON entity_sources(entity_id);
CREATE INDEX IF NOT EXISTS idx_sources_type ON entity_sources(source_type);

-- Ricerche anomalie
CREATE INDEX IF NOT EXISTS idx_anomalies_cig ON anomaly_flags(cig);
CREATE INDEX IF NOT EXISTS idx_anomalies_type ON anomaly_flags(anomaly_type);
CREATE INDEX IF NOT EXISTS idx_anomalies_score ON anomaly_flags(anomaly_score DESC);
CREATE INDEX IF NOT EXISTS idx_anomalies_reviewed ON anomaly_flags(reviewed, dismissed);

-- Ricerche investigation links
CREATE INDEX IF NOT EXISTS idx_invlinks_slug ON investigation_links(pipeline_slug);
CREATE INDEX IF NOT EXISTS idx_invlinks_entity ON investigation_links(entity_id);
CREATE INDEX IF NOT EXISTS idx_invlinks_ticket ON investigation_links(ticket_code);
