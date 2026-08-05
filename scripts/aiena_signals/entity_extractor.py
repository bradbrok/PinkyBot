#!/usr/bin/env python3
"""
AIena Entity Extractor — Estrae entita da testo italiano.

Estrae:
- Nomi di aziende (SRL, SPA, SCARL, SCRL, SAPA, etc.)
- Codici fiscali (16 caratteri alfanumerici)
- Partite IVA (11 cifre)
- CIG (10 caratteri alfanumerici)
- Enti pubblici (ASL, ASP, Comune, Provincia, Ospedale, etc.)
- Persone (pattern "Nome Cognome" con maiuscole)
- Luoghi italiani (comuni, province, regioni)

Decisioni architetturali:
- Regex + liste pattern invece di NER ML (no dipendenze pesanti)
- Normalizzazione aggressiva per matching
- Confidenza basata su contesto
- Pronto per upgrade a spaCy in produzione
"""
import re
from dataclasses import dataclass
from typing import Any

# ============================================================
# Pattern e liste
# ============================================================

# Suffissi aziende italiane
COMPANY_SUFFIXES = [
    r"\bS\.?R\.?L\.?\b",
    r"\bS\.?P\.?A\.?\b",
    r"\bS\.?A\.?S\.?\b",
    r"\bS\.?N\.?C\.?\b",
    r"\bS\.?A\.?P\.?A\.?\b",
    r"\bS\.?C\.?A\.?R\.?L\.?\b",
    r"\bS\.?C\.?R\.?L\.?\b",
    r"\bS\.?C\.?P\.?A\.?\b",
    r"\bS\.?O\.?C\.?\s*COOP\.?\b",
    r"\bCOOP\.?\b",
    r"\bCONSORZIO\b",
    r"\bASSOCIAZIONE\b",
    r"\bFONDAZIONE\b",
]

# Pattern per estrarre nome azienda completo
COMPANY_PATTERN = re.compile(
    r"([A-Z][A-Za-z0-9\s\-\']+(?:" +
    "|".join(COMPANY_SUFFIXES) +
    r"))",
    re.IGNORECASE
)

# Codice fiscale italiano (16 caratteri)
CF_PATTERN = re.compile(
    r"\b([A-Z]{6}\d{2}[A-EHLMPRST]\d{2}[A-Z]\d{3}[A-Z])\b",
    re.IGNORECASE
)

# Partita IVA italiana (11 cifre, spesso preceduta da "P.IVA" o "IT")
PIVA_PATTERN = re.compile(
    r"(?:P\.?\s*IVA|PARTITA\s*IVA|IT)?\s*:?\s*(\d{11})\b",
    re.IGNORECASE
)

# CIG - Codice Identificativo Gara (10 caratteri alfanumerici)
CIG_PATTERN = re.compile(
    r"\b(?:CIG|C\.I\.G\.?)\s*:?\s*([A-Z0-9]{10})\b",
    re.IGNORECASE
)

# CUP - Codice Unico Progetto
CUP_PATTERN = re.compile(
    r"\b(?:CUP|C\.U\.P\.?)\s*:?\s*([A-Z][A-Z0-9]{14})\b",
    re.IGNORECASE
)

# Enti pubblici italiani
PUBLIC_ENTITY_PATTERNS = [
    r"\bASL\s+(?:di\s+)?([A-Za-z\s]+\d*)\b",
    r"\bASP\s+(?:di\s+)?([A-Za-z\s]+)\b",
    r"\bAUSL\s+(?:di\s+)?([A-Za-z\s]+)\b",
    r"\bOSPEDALE\s+(?:(?:DI|DEL|DELLA|DELL\')\s+)?([A-Za-z\s\']+)\b",
    r"\bPOLICLINICO\s+(?:(?:DI|DEL)\s+)?([A-Za-z\s]+)\b",
    r"\bCOMUNE\s+(?:DI\s+)?([A-Za-z\s\']+)\b",
    r"\bPROVINCIA\s+(?:DI\s+)?([A-Za-z\s]+)\b",
    r"\bREGIONE\s+([A-Za-z\s\-]+)\b",
    r"\bMINISTERO\s+(?:(?:DELL?[AEO]?\'?)\s+)?([A-Za-z\s]+)\b",
    r"\bAGENZIA\s+(?:(?:DELL?[AEO]?\'?)\s+)?([A-Za-z\s]+)\b",
    r"\bISPETTORATO\s+([A-Za-z\s]+)\b",
    r"\bQUESTURA\s+(?:DI\s+)?([A-Za-z\s]+)\b",
    r"\bPREFETTURA\s+(?:DI\s+)?([A-Za-z\s]+)\b",
    r"\bTRIBUNALE\s+(?:DI\s+)?([A-Za-z\s]+)\b",
    r"\bPROCURA\s+(?:(?:DELLA REPUBBLICA\s+)?DI\s+)?([A-Za-z\s]+)\b",
    r"\bUNIVERSIT[AÀ]\s+(?:(?:DEGLI STUDI\s+)?DI\s+)?([A-Za-z\s]+)\b",
]

# Regioni italiane (per normalizzazione)
ITALIAN_REGIONS = {
    "PIEMONTE", "VALLE D'AOSTA", "LOMBARDIA", "TRENTINO-ALTO ADIGE",
    "VENETO", "FRIULI-VENEZIA GIULIA", "LIGURIA", "EMILIA-ROMAGNA",
    "TOSCANA", "UMBRIA", "MARCHE", "LAZIO", "ABRUZZO", "MOLISE",
    "CAMPANIA", "PUGLIA", "BASILICATA", "CALABRIA", "SICILIA", "SARDEGNA"
}

# Province italiane principali (per riconoscimento luoghi)
ITALIAN_PROVINCES = {
    "ROMA", "MILANO", "NAPOLI", "TORINO", "PALERMO", "GENOVA",
    "BOLOGNA", "FIRENZE", "BARI", "CATANIA", "VENEZIA", "VERONA",
    "MESSINA", "PADOVA", "TRIESTE", "TARANTO", "BRESCIA", "PARMA",
    "REGGIO CALABRIA", "MODENA", "REGGIO EMILIA", "PERUGIA",
    "LIVORNO", "RAVENNA", "CAGLIARI", "FOGGIA", "RIMINI", "FERRARA",
    "SALERNO", "CROTONE", "COSENZA", "CATANZARO", "VIBO VALENTIA",
    "SASSARI", "SIRACUSA", "PESCARA", "MONZA", "LECCE", "ANCONA",
    "BERGAMO", "FORLÌ", "BOLZANO", "TRENTO", "NOVARA", "PIACENZA",
    "UDINE", "AREZZO", "LATINA", "CASERTA", "POTENZA", "MATERA",
}

# Pattern nome persona (Nome Cognome con maiuscole)
PERSON_PATTERN = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b"
)

# Parole da escludere (falsi positivi comuni)
EXCLUDE_WORDS = {
    "LA", "IL", "LO", "LE", "GLI", "UN", "UNA", "DEL", "DELLA", "DELLO",
    "DELLE", "DEGLI", "AL", "ALLA", "ALLO", "ALLE", "AGLI", "DA", "DAL",
    "DALLA", "DALLO", "DALLE", "DAGLI", "DI", "SU", "SUL", "SULLA", "SULLO",
    "SULLE", "SUGLI", "IN", "CON", "PER", "TRA", "FRA", "COME", "CHE", "CHI",
    "QUESTO", "QUESTA", "QUELLO", "QUELLA", "QUANTO", "QUANTA",
}


# ============================================================
# Dataclass per entita estratte
# ============================================================

@dataclass
class ExtractedEntity:
    """Rappresenta un'entita estratta dal testo."""
    name: str                    # Nome estratto
    entity_type: str             # PERSONA | AZIENDA | ENTE_PUBBLICO | LUOGO | CIG | CF | PIVA
    raw_match: str               # Match originale nel testo
    confidence: float            # Confidenza estrazione (0.0-1.0)
    start_pos: int               # Posizione inizio nel testo
    end_pos: int                 # Posizione fine nel testo
    metadata: dict[str, Any]     # Dati aggiuntivi

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "entity_type": self.entity_type,
            "raw_match": self.raw_match,
            "confidence": self.confidence,
            "start_pos": self.start_pos,
            "end_pos": self.end_pos,
            "metadata": self.metadata,
        }


# ============================================================
# Classe principale
# ============================================================

class EntityExtractor:
    """
    Estrattore di entita da testo italiano.

    Esempio:
        extractor = EntityExtractor()
        entities = extractor.extract("L'azienda Edilizia Calabria SRL di Mario Rossi...")
        for e in entities:
            print(f"{e.entity_type}: {e.name} (conf: {e.confidence})")
    """

    def __init__(self):
        # Compila pattern enti pubblici
        self.public_entity_patterns = [
            re.compile(p, re.IGNORECASE) for p in PUBLIC_ENTITY_PATTERNS
        ]

    def normalize_name(self, name: str) -> str:
        """Normalizza un nome per matching."""
        # Strip e collassa spazi
        n = " ".join(name.split())
        # Rimuove punteggiatura finale
        n = re.sub(r"[.,;:]+$", "", n)
        return n

    def extract_companies(self, text: str) -> list[ExtractedEntity]:
        """Estrae nomi di aziende."""
        entities = []

        for match in COMPANY_PATTERN.finditer(text):
            raw = match.group(0)
            name = self.normalize_name(raw)

            # Calcola confidenza
            conf = 0.9 if any(re.search(s, raw, re.I) for s in COMPANY_SUFFIXES) else 0.6

            entities.append(ExtractedEntity(
                name=name.upper(),
                entity_type="AZIENDA",
                raw_match=raw,
                confidence=conf,
                start_pos=match.start(),
                end_pos=match.end(),
                metadata={}
            ))

        return entities

    def extract_codici_fiscali(self, text: str) -> list[ExtractedEntity]:
        """Estrae codici fiscali."""
        entities = []

        for match in CF_PATTERN.finditer(text):
            cf = match.group(1).upper()

            entities.append(ExtractedEntity(
                name=cf,
                entity_type="CF",
                raw_match=match.group(0),
                confidence=0.95,
                start_pos=match.start(),
                end_pos=match.end(),
                metadata={"tipo": "codice_fiscale"}
            ))

        return entities

    def extract_partite_iva(self, text: str) -> list[ExtractedEntity]:
        """Estrae partite IVA."""
        entities = []

        for match in PIVA_PATTERN.finditer(text):
            piva = match.group(1)

            # Verifica checksum base (prima cifra != 0)
            if piva[0] == "0" and piva[1] == "0":
                continue

            entities.append(ExtractedEntity(
                name=piva,
                entity_type="PIVA",
                raw_match=match.group(0),
                confidence=0.9,
                start_pos=match.start(),
                end_pos=match.end(),
                metadata={"tipo": "partita_iva"}
            ))

        return entities

    def extract_cig(self, text: str) -> list[ExtractedEntity]:
        """Estrae CIG."""
        entities = []

        for match in CIG_PATTERN.finditer(text):
            cig = match.group(1).upper()

            entities.append(ExtractedEntity(
                name=cig,
                entity_type="CIG",
                raw_match=match.group(0),
                confidence=0.95,
                start_pos=match.start(),
                end_pos=match.end(),
                metadata={"tipo": "cig"}
            ))

        return entities

    def extract_public_entities(self, text: str) -> list[ExtractedEntity]:
        """Estrae enti pubblici."""
        entities = []

        for pattern in self.public_entity_patterns:
            for match in pattern.finditer(text):
                raw = match.group(0)
                name = self.normalize_name(raw)

                # Determina sottotipo
                subtype = "ENTE"
                raw_upper = raw.upper()
                if "OSPEDALE" in raw_upper or "ASL" in raw_upper or "ASP" in raw_upper:
                    subtype = "SANITARIO"
                elif "COMUNE" in raw_upper:
                    subtype = "COMUNE"
                elif "PROVINCIA" in raw_upper:
                    subtype = "PROVINCIA"
                elif "REGIONE" in raw_upper:
                    subtype = "REGIONE"
                elif "MINISTERO" in raw_upper:
                    subtype = "MINISTERO"
                elif "TRIBUNALE" in raw_upper or "PROCURA" in raw_upper:
                    subtype = "GIUDIZIARIO"

                entities.append(ExtractedEntity(
                    name=name.upper(),
                    entity_type="ENTE_PUBBLICO",
                    raw_match=raw,
                    confidence=0.85,
                    start_pos=match.start(),
                    end_pos=match.end(),
                    metadata={"sottotipo": subtype}
                ))

        return entities

    def extract_persons(self, text: str) -> list[ExtractedEntity]:
        """
        Estrae possibili nomi di persone.

        NOTA: Alta probabilita di falsi positivi. Usa con cautela.
        """
        entities = []

        for match in PERSON_PATTERN.finditer(text):
            raw = match.group(1)

            # Filtra falsi positivi
            words = raw.split()

            # Deve avere almeno 2 parole
            if len(words) < 2:
                continue

            # Non deve contenere parole comuni
            if any(w.upper() in EXCLUDE_WORDS for w in words):
                continue

            # Non deve essere una provincia/regione nota
            if raw.upper() in ITALIAN_PROVINCES | ITALIAN_REGIONS:
                continue

            # Confidenza bassa per nomi di persona (molti falsi positivi)
            entities.append(ExtractedEntity(
                name=raw,
                entity_type="PERSONA",
                raw_match=raw,
                confidence=0.5,
                start_pos=match.start(),
                end_pos=match.end(),
                metadata={}
            ))

        return entities

    def extract_places(self, text: str) -> list[ExtractedEntity]:
        """Estrae luoghi italiani menzionati."""
        entities = []
        text_upper = text.upper()

        # Cerca regioni
        for region in ITALIAN_REGIONS:
            if region in text_upper:
                idx = text_upper.find(region)
                entities.append(ExtractedEntity(
                    name=region,
                    entity_type="LUOGO",
                    raw_match=text[idx:idx + len(region)],
                    confidence=0.9,
                    start_pos=idx,
                    end_pos=idx + len(region),
                    metadata={"tipo": "regione"}
                ))

        # Cerca province
        for province in ITALIAN_PROVINCES:
            if province in text_upper:
                idx = text_upper.find(province)
                entities.append(ExtractedEntity(
                    name=province,
                    entity_type="LUOGO",
                    raw_match=text[idx:idx + len(province)],
                    confidence=0.85,
                    start_pos=idx,
                    end_pos=idx + len(province),
                    metadata={"tipo": "provincia"}
                ))

        return entities

    def extract(
        self,
        text: str,
        include_persons: bool = True,
        min_confidence: float = 0.0
    ) -> list[ExtractedEntity]:
        """
        Estrae tutte le entita da un testo.

        Args:
            text: Testo da analizzare
            include_persons: Includi nomi di persona (alta probabilita falsi positivi)
            min_confidence: Confidenza minima per inclusione

        Returns:
            Lista di entita estratte, deduplicate e ordinate per posizione
        """
        all_entities = []

        # Estrai ogni tipo
        all_entities.extend(self.extract_companies(text))
        all_entities.extend(self.extract_codici_fiscali(text))
        all_entities.extend(self.extract_partite_iva(text))
        all_entities.extend(self.extract_cig(text))
        all_entities.extend(self.extract_public_entities(text))
        all_entities.extend(self.extract_places(text))

        if include_persons:
            all_entities.extend(self.extract_persons(text))

        # Filtra per confidenza
        filtered = [e for e in all_entities if e.confidence >= min_confidence]

        # Deduplica (stesso nome e tipo)
        seen = set()
        unique = []
        for e in filtered:
            key = (e.name, e.entity_type)
            if key not in seen:
                seen.add(key)
                unique.append(e)

        # Ordina per posizione nel testo
        unique.sort(key=lambda x: x.start_pos)

        return unique

    def extract_to_dict(
        self,
        text: str,
        **kwargs
    ) -> list[dict]:
        """Estrae entita e restituisce come lista di dict."""
        entities = self.extract(text, **kwargs)
        return [e.to_dict() for e in entities]


# ============================================================
# Test self-contained
# ============================================================

if __name__ == "__main__":
    print("=== Test EntityExtractor ===\n")

    extractor = EntityExtractor()

    # Testo di test
    test_text = """
    Segnalazione ricevuta il 4 maggio 2026.

    L'azienda Edilizia Calabria SRL (P.IVA 12345678901) con sede a Crotone
    ha vinto l'appalto CIG 1234567890 per la manutenzione dell'Ospedale
    San Giovanni di Dio di Crotone.

    L'amministratore delegato Mario Rossi (CF RSSMRA80A01H501Z) risulta
    coinvolto in precedenti indagini della Procura di Catanzaro.

    La gara e stata bandita dall'ASP di Crotone, Regione Calabria.

    Fonti: Comune di Crotone, Provincia di Crotone.
    """

    print("Testo di test:")
    print("-" * 60)
    print(test_text.strip())
    print("-" * 60)

    # Estrazione
    print("\nEntita estratte:\n")
    entities = extractor.extract(test_text, min_confidence=0.3)

    for e in entities:
        print(f"  [{e.entity_type}] {e.name}")
        print(f"    Confidenza: {e.confidence:.0%}")
        print(f"    Match: '{e.raw_match}'")
        if e.metadata:
            print(f"    Metadata: {e.metadata}")
        print()

    # Statistiche
    print("Statistiche:")
    by_type: dict[str, int] = {}
    for e in entities:
        by_type[e.entity_type] = by_type.get(e.entity_type, 0) + 1

    for t, c in sorted(by_type.items()):
        print(f"  {t}: {c}")

    print(f"\nTotale: {len(entities)} entita")

    print("\n=== Test completati ===")
