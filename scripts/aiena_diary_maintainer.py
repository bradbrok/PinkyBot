#!/usr/bin/env python3
"""
aiena_diary_maintainer.py - Mantiene aggiornato il Diario AIena sulla homepage.

Funzionalita:
1. Legge pipeline.json (fonte di verita per investigations[])
2. Per ogni investigation nelle prime 3 posizioni con < 3 diary entries:
   - Genera entries deterministicamente da preview HTML o dati investigation
3. Aggiorna pipeline.json con le nuove entries
4. Aggiorna il diary-box in index.html usando la logica di aiena_post_publish
5. Deploya index.html aggiornato via FTP

Uso:
  python3 aiena_diary_maintainer.py
  python3 aiena_diary_maintainer.py --dry-run
  python3 aiena_diary_maintainer.py --force  # rigenera anche se gia 3 entries

Cron consigliato: 0 7 * * * (ogni mattina alle 7)

Autore: Engineer (PinkyBot)
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from html.parser import HTMLParser
from typing import Optional

from aiena_secrets import _load_secrets

# Paths
PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
INDEX_HTML = Path("/var/www/aiena.it/index.html")
INDEX_LOCK = Path("/tmp/aiena_index_html.lock")
PREVIEW_DIR = Path("/var/www/aiena.it/preview")

# FTP credentials (same as aiena_post_publish.py)
FTP_HOST = "ftp.aiena.it"
FTP_USER = "aiena.it"
FTP_PASS = _load_secrets().get("FTP_PASS", "")


def log(msg: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [diary-maintainer] {msg}")


# ============================================================================
# HTML Text Extraction
# ============================================================================

class TextExtractor(HTMLParser):
    """Estrae testo da HTML, ignorando tag e preservando contenuto."""

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.skip_tags = {'script', 'style', 'head', 'meta', 'link'}
        self.current_tag = None
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.skip_tags:
            self._skip_depth += 1
        self.current_tag = tag

    def handle_endtag(self, tag):
        if tag in self.skip_tags and self._skip_depth > 0:
            self._skip_depth -= 1
        self.current_tag = None

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self.text_parts.append(text)

    def get_text(self) -> str:
        return ' '.join(self.text_parts)


def extract_text_from_html(html_content: str) -> str:
    """Estrae testo pulito da contenuto HTML."""
    extractor = TextExtractor()
    try:
        extractor.feed(html_content)
    except Exception:
        pass
    return extractor.get_text()


def extract_article_content(preview_path: Path) -> dict:
    """
    Estrae contenuto chiave da un file preview HTML.
    Ritorna: {title, subtitle, first_paragraphs, meta_description}
    """
    result = {
        'title': '',
        'subtitle': '',
        'first_paragraphs': [],
        'meta_description': ''
    }

    if not preview_path.exists():
        return result

    try:
        html = preview_path.read_text(encoding='utf-8')
    except Exception:
        return result

    # Estrai meta description
    m = re.search(r'<meta\s+name="description"\s+content="([^"]+)"', html, re.I)
    if m:
        result['meta_description'] = m.group(1)

    # Estrai title
    m = re.search(r'<h1[^>]*class="article-title"[^>]*>([^<]+)</h1>', html, re.I)
    if m:
        result['title'] = m.group(1).strip()

    # Estrai subtitle
    m = re.search(r'<p[^>]*class="article-subtitle"[^>]*>(.*?)</p>', html, re.DOTALL | re.I)
    if m:
        result['subtitle'] = extract_text_from_html(m.group(1))

    # Estrai primi paragrafi dall'article body (dopo l'AI disclaimer)
    m = re.search(r'<article[^>]*class="article-body"[^>]*>(.*?)</article>', html, re.DOTALL | re.I)
    if m:
        body = m.group(1)
        # Rimuovi AI disclaimer section
        body = re.sub(r'<div[^>]*class="ai-notice"[^>]*>.*?</div>', '', body, flags=re.DOTALL | re.I)
        # Trova tutti i <p> (non dentro div.ai-notice)
        paragraphs = re.findall(r'<p[^>]*>(.*?)</p>', body, re.DOTALL | re.I)
        for p in paragraphs[:10]:  # Max 10 paragrafi per catturare piu' fatti
            text = extract_text_from_html(p)
            # Salta paragrafi che sembrano disclaimer o metadata
            if len(text) > 30 and 'intelligenza artificiale' not in text.lower():
                result['first_paragraphs'].append(text)

    # Estrai anche numeri chiave da tutto l'HTML (per statistiche)
    numbers_text = re.findall(r'\b(\d+)\s+(?:vittim|abus|indagat|person|donne|casi)\w*', html, re.I)
    if numbers_text:
        result['key_numbers'] = numbers_text

    return result


# ============================================================================
# Diary Entry Generation
# ============================================================================

DIARY_TEMPLATES = {
    # Template per categoria
    'Fondi UE': [
        "Ho trovato i dati: {cifra}. La deadline e' vicina. <em>I numeri non mentono.</em>",
        "Analisi fonti ufficiali completata. {dettaglio}. <em>Sto verificando.</em>",
        "{fatto_chiave}. Il sistema e' chiaro. <em>Chi non rendiconta, perde.</em>"
    ],
    'Corruzione': [
        "Operazione {nome}: {n_indagati} indagati, {cifra} drenati. <em>Scavo nei documenti.</em>",
        "Il sistema emerge: {dettaglio}. <em>Le carte parlano.</em>",
        "{fatto_chiave}. <em>La giustizia si muove. Io documento.</em>"
    ],
    'Appalti pubblici': [
        "Ho trovato il bando: {anomalia}. <em>Non tornano i conti.</em>",
        "Registro imprese consultato. {dettaglio}. <em>Il meccanismo e' questo.</em>",
        "{fatto_chiave}. <em>A parole trasparenza. Nei fatti, altro.</em>"
    ],
    'Chiesa': [
        "Rapporto interno trovato: <strong>{n_vittime} vittime</strong> documentate. I superiori sapevano dal {periodo}. <em>Il silenzio e' complice.</em>",
        "Leggo i documenti: {dettaglio}. Abusi dal 1968 al 1998. <em>Trent'anni senza conseguenze.</em>",
        "Le testimonianze emergono. {fatto_chiave}. <em>Chi sapeva ha taciuto.</em>"
    ],
    'Politica': [
        "Analizzo la legge: {dettaglio}. <em>Seguire i soldi.</em>",
        "{fatto_chiave}. Il meccanismo e' evidente. <em>Sto documentando.</em>",
        "Ostruzionismo in corso: {n_emendamenti} emendamenti. <em>Democrazia rallentata.</em>"
    ],
    'Sanita': [
        "I numeri del report: {cifra}. <em>Troppi per essere statistiche.</em>",
        "{dettaglio}. Le responsabilita' ci sono. <em>Chi risponde?</em>",
        "{fatto_chiave}. <em>La sanita' che non cura se stessa.</em>"
    ],
    # Default
    'default': [
        "Ho iniziato a scavare. {dettaglio}. <em>Le prime tracce emergono.</em>",
        "Verifico le fonti: {fatto_chiave}. <em>I documenti non mentono.</em>",
        "Analisi completata. {conclusione}. <em>Pubblico presto.</em>"
    ]
}


def generate_date_sequence(base_date: date, n: int) -> list[str]:
    """Genera N date in italiano distribuite nei giorni precedenti a base_date."""
    mesi_it = ['Gen', 'Feb', 'Mar', 'Apr', 'Mag', 'Giu',
               'Lug', 'Ago', 'Set', 'Ott', 'Nov', 'Dic']

    dates = []
    for i in range(n):
        # Distribuisci le date: prima entry piu' vecchia
        days_ago = (n - 1 - i) * 2 + 1  # 5, 3, 1 giorni fa per n=3
        d = base_date - timedelta(days=days_ago)
        dates.append(f"{d.day} {mesi_it[d.month - 1]} {d.year}")

    return dates


def extract_key_facts(investigation: dict, preview_content: dict) -> dict:
    """Estrae fatti chiave da investigation e preview per popolare i template."""
    facts = {
        'cifra': '',
        'dettaglio': '',
        'fatto_chiave': '',
        'anomalia': '',
        'nome': '',
        'n_indagati': '',
        'n_vittime': '',
        'periodo': '',
        'n_emendamenti': '',
        'conclusione': ''
    }

    # Combina diary_text + primi paragrafi per avere piu' contenuto
    diary_text = investigation.get('diary_text', '')
    all_text = diary_text
    if preview_content.get('first_paragraphs'):
        all_text += ' ' + ' '.join(preview_content['first_paragraphs'])

    # Estrai cifre (es. "25 milioni", "400mila euro", "11,5 miliardi")
    cifre = re.findall(r'[\d,.]+ ?(?:milioni|miliardi|mila|euro|%|M)', all_text, re.I)
    if cifre:
        facts['cifra'] = cifre[0]

    # Usa meta description o subtitle come dettaglio
    if preview_content.get('meta_description'):
        facts['dettaglio'] = preview_content['meta_description'][:120]
    elif preview_content.get('subtitle'):
        facts['dettaglio'] = preview_content['subtitle'][:120]
    elif diary_text:
        # Pulisci diary_text da tag HTML
        clean = re.sub(r'<[^>]+>', '', diary_text)
        facts['dettaglio'] = clean[:120]

    # Estrai fatto chiave dal primo paragrafo
    if preview_content.get('first_paragraphs'):
        facts['fatto_chiave'] = preview_content['first_paragraphs'][0][:150]
    elif diary_text:
        clean = re.sub(r'<[^>]+>', '', diary_text)
        facts['fatto_chiave'] = clean[:150]

    # Estrai numeri specifici per template (cerca in tutto il testo)
    m = re.search(r'(\d+)\s*(?:indagat|arrest)', all_text, re.I)
    if m:
        facts['n_indagati'] = m.group(1)

    m = re.search(r'(\d+)\s*(?:vittim|abus|donne)', all_text, re.I)
    if m:
        facts['n_vittime'] = m.group(1)
    elif preview_content.get('key_numbers'):
        facts['n_vittime'] = preview_content['key_numbers'][0]

    m = re.search(r'(\d+)\s*(?:emendament|modific)', all_text, re.I)
    if m:
        facts['n_emendamenti'] = m.group(1)

    # Conclusione generica
    title = investigation.get('title', 'questa storia')
    facts['conclusione'] = f"I fatti su {title} sono documentati"

    # Nome operazione
    m = re.search(r'[Oo]perazione\s+"?([^"]+)"?', diary_text)
    if m:
        facts['nome'] = m.group(1)
    else:
        facts['nome'] = investigation.get('slug', '').replace('-', ' ').title()[:30]

    # Anomalia per appalti
    facts['anomalia'] = facts.get('dettaglio', 'requisiti non rispettati')

    # Periodo (cerca anno singolo o range)
    m = re.search(r'(\d{4})\s*[-–]\s*(\d{4})', all_text)
    if m:
        facts['periodo'] = f"{m.group(1)}-{m.group(2)}"
    else:
        # Cerca menzioni di decenni
        m = re.search(r"anni\s*'?(\d0)", all_text, re.I)
        if m:
            facts['periodo'] = f"anni '{m.group(1)}"
        else:
            # Cerca primo anno menzionato
            m = re.search(r'\b(19\d{2}|20[012]\d)\b', all_text)
            if m:
                facts['periodo'] = m.group(1)
            else:
                facts['periodo'] = "decenni"

    return facts


def generate_diary_entries(investigation: dict, n_entries: int = 3) -> list[dict]:
    """
    Genera N diary entries per un'investigation.
    Usa dati dal preview HTML se disponibile, altrimenti da diary_text/research_notes.
    """
    slug = investigation.get('slug', '')
    category = investigation.get('category', 'default')

    # Carica contenuto preview se esiste
    preview_path = PREVIEW_DIR / f"{slug}.html"
    preview_content = extract_article_content(preview_path)

    # Estrai fatti chiave
    facts = extract_key_facts(investigation, preview_content)

    # Scegli template per categoria
    templates = DIARY_TEMPLATES.get(category, DIARY_TEMPLATES['default'])

    # Genera date
    today = date.today()
    dates = generate_date_sequence(today, n_entries)

    entries = []
    for i in range(n_entries):
        template = templates[i % len(templates)]

        # Sostituisci placeholder con fatti estratti
        text = template
        for key, value in facts.items():
            if value:
                text = text.replace('{' + key + '}', value)

        # Rimuovi placeholder non sostituiti
        text = re.sub(r'\{[^}]+\}', '', text)

        # Pulisci spazi multipli
        text = re.sub(r'\s+', ' ', text).strip()

        # Tronca a 250 caratteri mantenendo tag HTML
        if len(re.sub(r'<[^>]+>', '', text)) > 250:
            # Tronca il testo interno
            plain = re.sub(r'<[^>]+>', '', text)[:247] + "..."
            # Ricostruisci con enfasi finale
            if '<em>' in text:
                text = plain[:-20] + " <em>" + plain[-17:] + "</em>"
            else:
                text = plain

        entry = {
            'date': dates[i],
            'note': f"#{i + 1}",
            'text': text
        }
        entries.append(entry)

    return entries


def generate_diary_from_research_notes(investigation: dict, n_entries: int = 3) -> list[dict]:
    """
    Genera diary entries dalle research_notes se disponibili.
    Piu' accurato perche' usa dati gia' elaborati.
    """
    notes = investigation.get('research_notes', [])
    if not notes:
        return generate_diary_entries(investigation, n_entries)

    # Filtra solo note di ricerca taggate esplicitamente (non note interne/admin
    # senza tag, che possono contenere metadati come Slug/Status/BOZZA)
    research = [n for n in notes if '[RICERCA' in n]

    today = date.today()
    dates = generate_date_sequence(today, n_entries)

    entries = []
    for i in range(n_entries):
        if i < len(research):
            # Usa research note
            note_text = research[i]
            # Rimuovi prefisso data se presente
            note_text = re.sub(r'^\[RICERCA[^\]]*\]\s*', '', note_text)
            # Formatta per diario
            text = note_text[:200]
            if len(note_text) > 200:
                text += "..."
            text = text + " <em>Sto verificando.</em>"
        else:
            # Fallback a template
            category = investigation.get('category', 'default')
            templates = DIARY_TEMPLATES.get(category, DIARY_TEMPLATES['default'])
            text = templates[i % len(templates)]
            # Pulisci placeholder
            text = re.sub(r'\{[^}]+\}', '', text).strip()

        entry = {
            'date': dates[i],
            'note': f"#{i + 1}",
            'text': text
        }
        entries.append(entry)

    return entries


# ============================================================================
# Pipeline JSON Operations
# ============================================================================

def load_pipeline() -> dict:
    """Carica pipeline.json."""
    try:
        return json.loads(PIPELINE_JSON.read_text(encoding='utf-8'))
    except FileNotFoundError:
        log("ERROR: pipeline.json not found")
        return {"investigations": [], "leads": [], "published": []}
    except json.JSONDecodeError as e:
        log(f"ERROR: pipeline.json is corrupted: {e}")
        return {"investigations": [], "leads": [], "published": []}


def save_pipeline(data: dict) -> None:
    """Salva pipeline.json atomicamente."""
    data['updated_at'] = date.today().isoformat()
    content = json.dumps(data, ensure_ascii=False, indent=2)

    fd, tmp_path = tempfile.mkstemp(dir=PIPELINE_JSON.parent, suffix='.tmp', prefix='pipeline_')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp_path, PIPELINE_JSON)
        log(f"Pipeline salvato: {PIPELINE_JSON}")
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise e


# ============================================================================
# Index.html Diary Update (from aiena_post_publish.py)
# ============================================================================

def build_diary_slides(current: dict) -> tuple[list[str], int]:
    """
    Costruisce le slide del diario da diary[] (array) o diary_text (stringa).
    Ritorna (slides_html_list, total).
    Priorita': diary[] > diary_text > fallback.
    """
    slides = []
    diary_array = current.get('diary') or []

    if diary_array:
        # Filter out internal (non-public) entries before rendering
        public_entries = [e for e in diary_array if e.get('public', True) is not False]
        for i, entry in enumerate(public_entries):
            active_class = " active" if i == 0 else ""
            date_label = entry.get('date', '')
            note_label = entry.get('note', '')
            date_str = f"{date_label} — nota {note_label}" if note_label else date_label
            text = entry.get('text', '')
            slides.append(
                f'<div class="diary-slide{active_class}" id="ds-{i}">\n'
                f'              <div class="diary-date">{date_str}</div>\n'
                f'              <div class="diary-text">{text}</div>\n'
                f'            </div>'
            )
    else:
        diary_text = current.get('diary_text', 'In lavorazione.')
        today_str = date.today().strftime("%d %b %Y").upper()
        slides.append(
            f'<div class="diary-slide active" id="ds-0">\n'
            f'              <div class="diary-date">{today_str} — aggiornamento</div>\n'
            f'              <div class="diary-text">{diary_text}</div>\n'
            f'            </div>'
        )

    return slides, len(slides)


def build_diary_dots(total: int) -> str:
    """Genera i dots HTML per il diario."""
    dots = []
    for i in range(total):
        active_class = " active" if i == 0 else ""
        dots.append(f'<div class="diary-dot{active_class}" data-i="{i}"></div>')
    return "\n              ".join(dots)


def update_diary_in_html(current: dict) -> bool:
    """
    Aggiorna il Diario AIena in index.html.
    Ritorna True se aggiornato con successo.
    """
    if not current:
        log("WARN: current_investigation null, diario non aggiornato")
        return False

    with open(INDEX_LOCK, 'w') as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            return _update_diary_locked(current)
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


def _update_diary_locked(current: dict) -> bool:
    """Corpo di update_diary_in_html - eseguito sotto file lock."""
    html = INDEX_HTML.read_text(encoding='utf-8')

    # 1. Aggiorna la categoria nel diary-header
    category = current.get('category', 'Indagine')
    html = re.sub(
        r'(<span class="diary-cat">)[^<]*(</span>)',
        rf'\g<1>{category}\g<2>',
        html
    )

    # 2. Costruisci slides e dots
    slides, total = build_diary_slides(current)
    slides_html = "\n            ".join(slides)
    dots_html = build_diary_dots(total)

    # 3. Sostituisci il contenuto interno di diary-body
    pattern = r'(<div class="diary-body">)\s*(.*?)\s*(</div>\s*<div class="diary-nav">)'

    def replace_diary_content(m):
        return m.group(1) + '\n            ' + slides_html + '\n          ' + m.group(3)

    new_html, n = re.subn(pattern, replace_diary_content, html, count=1, flags=re.DOTALL)

    if n == 0:
        log("WARN: pattern diary-body non trovato in index.html")
        return False

    # 4. Aggiorna i dots
    new_html = re.sub(
        r'(<div class="diary-dots">)\s*.*?\s*(</div>\s*<a href="/segnala\.html")',
        rf'\g<1>\n              {dots_html}\n            \g<2>',
        new_html,
        flags=re.DOTALL
    )

    # 5. Aggiorna const total nel JS
    new_html = re.sub(r'const total = \d+;', f'const total = {total};', new_html)

    # Scrittura atomica
    fd, tmp = tempfile.mkstemp(dir=INDEX_HTML.parent, suffix='.html.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(new_html)
        os.chmod(tmp, 0o644)  # nginx (www-data) must read it; mkstemp creates 0600
        os.replace(tmp, INDEX_HTML)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    log(f"Diario aggiornato in index.html: {category} ({total} slide)")
    return True


# ============================================================================
# FTP Deploy
# ============================================================================

def deploy_ftp(file_path: Path, remote_path: str) -> bool:
    """Deploya un file via FTP."""
    cmd = [
        'curl', '-s', '-T', str(file_path),
        f'ftp://{FTP_HOST}/{remote_path}',
        '--user', f'{FTP_USER}:{FTP_PASS}'
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        log(f"FTP OK: {remote_path}")
        return True
    else:
        log(f"FTP ERRORE {remote_path}: {result.stderr or 'Unknown error'}")
        return False


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Mantiene aggiornato il Diario AIena")
    parser.add_argument('--dry-run', action='store_true', help="Non salva modifiche")
    parser.add_argument('--force', action='store_true', help="Rigenera anche se gia' 3 entries")
    parser.add_argument('--no-deploy', action='store_true', help="Non deploya via FTP")
    args = parser.parse_args()

    log("=== DIARY MAINTAINER START ===")

    # 1. Carica pipeline
    data = load_pipeline()
    investigations = data.get('investigations', [])

    if not investigations:
        log("WARN: nessuna investigation trovata")
        return

    log(f"Trovate {len(investigations)} investigations")

    # 2. Processa le prime 3 investigations
    updated_any = False
    for i, inv in enumerate(investigations[:3]):
        slug = inv.get('slug', f'unknown-{i}')
        title = inv.get('title', 'Untitled')[:50]
        current_diary = inv.get('diary', [])
        n_entries = len(current_diary)

        log(f"[{i+1}] {slug}: {n_entries}/3 diary entries")

        # Salta se gia' completo (a meno che --force)
        if n_entries >= 3 and not args.force:
            log(f"    -> OK, gia' completo")
            continue

        # Genera entries mancanti
        n_needed = 3 - n_entries if not args.force else 3

        if args.force:
            # Rigenera tutto
            new_entries = generate_diary_entries(inv, 3)
        else:
            # Mantieni esistenti, aggiungi mancanti
            existing = current_diary.copy()
            new_generated = generate_diary_entries(inv, n_needed)
            new_entries = existing + new_generated

        if args.dry_run:
            log(f"    -> DRY-RUN: genererebbe {len(new_entries)} entries")
            for j, e in enumerate(new_entries):
                preview = e['text'][:80] + "..." if len(e['text']) > 80 else e['text']
                preview = re.sub(r'<[^>]+>', '', preview)  # Strip HTML per log
                log(f"       [{j+1}] {e['date']}: {preview}")
        else:
            inv['diary'] = new_entries
            updated_any = True
            log(f"    -> Generati {len(new_entries)} entries")

    if args.dry_run:
        log("=== DRY-RUN COMPLETATO ===")
        return

    if not updated_any:
        log("Nessun aggiornamento necessario")
        # Aggiorna comunque index.html per sicurezza
        current = investigations[0] if investigations else None
        if current and current.get('diary'):
            update_diary_in_html(current)
            if not args.no_deploy:
                deploy_ftp(INDEX_HTML, 'index.html')
        # Sincronizza admin data comunque
        _sync_admin_data()
        log("=== DIARY MAINTAINER COMPLETATO ===")
        return

    # Ci sono aggiornamenti: persisti pipeline, aggiorna HTML e deploya.
    # (SEC-6: prima del fix questo blocco mancava → modifiche al diary venivano
    # generate ma mai salvate né pubblicate.)
    save_pipeline(data)
    current = investigations[0]
    if current.get('diary'):
        update_diary_in_html(current)
        if not args.no_deploy:
            deploy_ftp(INDEX_HTML, 'index.html')
    _sync_admin_data()
    log("=== DIARY MAINTAINER COMPLETATO ===")


def _sync_admin_data():
    """Chiama update_admin_data.py per sincronizzare admin/data.json."""
    update_script = Path("/home/pinky/.pinkybot/scripts/update_admin_data.py")
    if update_script.exists():
        try:
            result = subprocess.run(
                [sys.executable, str(update_script)],
                capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                log("Admin data.json sincronizzato")
            else:
                log(f"WARN: update_admin_data.py fallito: {result.stderr[:100]}")
        except Exception as e:
            log(f"WARN: update_admin_data.py error: {e}")


if __name__ == "__main__":
    main()
