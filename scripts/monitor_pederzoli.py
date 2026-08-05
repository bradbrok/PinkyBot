#!/usr/bin/env python3
"""
monitor_pederzoli.py — Monitora disponibilità Prima visita ortopedica - chirurgia della mano
Ospedale Pederzoli. Notifica Mirko su Telegram appena ci sono slot SSN/Agevolato.

Flow:
  1. Home page → seleziona esame "Prima visita ortopedica - chirurgia della mano" (ID 771)
  2. Naviga a disponibilita-esame
  3. Clicca FILTRO SEDI → FILTRA
  4. Attendi caricamento → cerca slot disponibili
  5. Se trovato → notifica Telegram
"""
import sys, os, json, logging, re
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "pederzoli_monitor.log", mode="a"),
    ]
)
logger = logging.getLogger(__name__)

HOME_URL = "https://portalepaziente.ospedalepederzoli.it/"
EXAM_VALUE = "771"  # Prima visita ortopedica - chirurgia della mano
STATE_FILE = PROJECT_ROOT / "data" / "pederzoli_last_state.json"
TELEGRAM_CHAT_ID = "32405655"


def load_last_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"slots_found": False, "last_check": None, "details": ""}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def check_availability():
    """
    Naviga il portale Pederzoli, seleziona esame chirurgia mano,
    controlla disponibilità SSN. Ritorna dict con risultato.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox', '--ignore-certificate-errors',
                  '--disable-blink-features=AutomationControlled']
        )
        context = browser.new_context(
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
            ignore_https_errors=True,
            viewport={'width': 1280, 'height': 900}
        )
        page = context.new_page()

        # Track proxy/API calls
        api_calls = []
        page.on('response', lambda r: api_calls.append({
            'url': r.url, 'status': r.status,
            'body': r.text()[:800] if r.status == 200 and 'proxy' in r.url else ''
        }) if 'proxy' in r.url else None)

        try:
            # ── Step 1: Home page ────────────────────────────────────────
            logger.info("Step 1: Home page")
            page.goto(HOME_URL, wait_until='networkidle', timeout=30000)
            page.wait_for_timeout(2000)

            # ── Step 2: Seleziona esame via JS ───────────────────────────
            logger.info(f"Step 2: Selezione esame {EXAM_VALUE}")
            selected = page.evaluate(f"""
                (() => {{
                    // Cerca il select prestazioni (name=icd9 o quello con più opzioni)
                    var sel = document.querySelector('select[name=icd9]');
                    if (!sel) {{
                        var selects = document.querySelectorAll('select');
                        var maxOpts = 0;
                        for (var s of selects) {{
                            if (s.options.length > maxOpts) {{ maxOpts = s.options.length; sel = s; }}
                        }}
                    }}
                    if (sel) {{
                        sel.value = '{EXAM_VALUE}';
                        sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                        var idx = sel.selectedIndex;
                        var txt = idx >= 0 ? sel.options[idx].text : 'unknown';
                        return 'ok:' + sel.value + ' text=' + txt + ' opts=' + sel.options.length;
                    }}
                    return 'not_found';
                }})()
            """)
            logger.info(f"Exam select: {selected}")
            page.wait_for_timeout(1000)

            # ── Step 3: Invia form ───────────────────────────────────────
            # Find and submit the search form
            logger.info("Step 3: Submit form")
            submit_done = False

            # Handle any confirmation dialog that might appear
            confirmed = {'done': False}
            def handle_dialog(dialog):
                logger.info(f"Dialog appeared (dismissing): {dialog.message[:80]}")
                dialog.accept()
                confirmed['done'] = True
            page.on('dialog', handle_dialog)

            # Try submitting the form
            form = page.query_selector('.c-search-bar__form')
            if form:
                page.evaluate('document.querySelector(".c-search-bar__form").dispatchEvent(new Event("submit", {bubbles: true, cancelable: true}))')
                page.wait_for_timeout(2000)
                submit_done = True
                logger.info("Form submitted")

            # Aspetta il modal custom del portale (class c-modal)
            page.wait_for_timeout(1500)
            modal = page.query_selector('.c-modal')
            if modal:
                logger.info("Modal Confirm trovato — checkbox + Conferma")
                # 1. Check il checkbox (obbligatorio per far scattare il callback)
                cb = modal.query_selector('[type=checkbox]')
                if cb:
                    page.evaluate('(el) => el.click()', cb)
                    page.wait_for_timeout(500)
                    logger.info("Checkbox checked")
                # 2. Clicca Conferma (.second button)
                confirm_btn = modal.query_selector('button.second')
                if confirm_btn:
                    page.evaluate('(el) => el.click()', confirm_btn)
                    logger.info("Conferma cliccato")
            else:
                logger.info("Nessun modal — form già processato")

            # Aspetta navigazione verso disponibilita-esame (callback JS lo fa)
            try:
                page.wait_for_url('**/disponibilita-esame**', timeout=12000)
                logger.info(f"Navigato a: {page.url}")
            except PwTimeout:
                logger.warning(f"Navigation timeout — URL attuale: {page.url}")
                # Se non ha navigato, prova direct navigation (ultima risorsa)
                if 'disponibilita-esame' not in page.url:
                    logger.warning("Fallback: navigazione diretta")
                    page.goto(f"{HOME_URL.rstrip('/')}/disponibilita-esame?sedi=1&virtualSites=1&cp=B",
                              wait_until='networkidle', timeout=20000)

            # Aspetta che il calendario carichi (dopo navigazione)
            page.wait_for_timeout(5000)
            logger.info(f"URL attuale: {page.url}")

            # ── Step 4: Chiudi popup "Informazione prezzi" (popup2) ──────
            # Dopo la navigazione appare popup2 con avviso prezzi.
            # Lo chiudiamo via jQuery (non ha class .first — ha solo "Indietro").
            try:
                page.evaluate("if (typeof $ !== 'undefined' && $('#popup2').length) { $('#popup2').hide(); }")
                page.wait_for_timeout(500)
                logger.info("popup2 chiuso via jQuery")
            except Exception as e:
                logger.debug(f"popup2 close: {e}")

            # ── Step 5: Seleziona B - BREVE nel select priorità SSN ──────
            # FIX: usa page.select_option() di Playwright (triggera eventi framework Vue/React)
            # invece di dispatchEvent nativo che veniva ignorato.
            # DOPPIA strategia: prima Playwright nativo, poi navigazione diretta con cp=B.
            breve_applied = False
            try:
                # Strategia 1: Playwright select_option — triggera eventi framework correttamente
                ssn_selects = page.query_selector_all('select.select-priorita, select.classePriorita')
                for sel_handle in ssn_selects:
                    try:
                        page.select_option(sel_handle, 'B')
                        logger.info(f"B-BREVE select_option: ok via Playwright native")
                        breve_applied = True
                        break
                    except Exception as e2:
                        logger.debug(f"select_option failed on element: {e2}")

                if not breve_applied:
                    # Fallback: trova qualsiasi select con opzione B-BREVE
                    result = page.evaluate("""
                        (() => {
                            var selects = document.querySelectorAll('select');
                            for (var sel of selects) {
                                for (var opt of sel.options) {
                                    if (opt.value === 'B' && opt.text.includes('BREVE')) {
                                        return sel.id || sel.className || 'found';
                                    }
                                }
                            }
                            return 'not found';
                        })()
                    """)
                    if result != 'not found':
                        page.select_option(f'select', 'B')
                        breve_applied = True
                        logger.info(f"B-BREVE select fallback: ok ({result})")

                if breve_applied:
                    page.wait_for_timeout(12000)  # CRITICO: aspetta 12s per API + reload
                    logger.info("Calendario B-BREVE caricato via select_option")
                else:
                    logger.warning("B-BREVE select non trovato — provo navigazione diretta con cp=B")

            except Exception as e:
                logger.warning(f"B-BREVE select failed: {e}")

            # Strategia 2: se select non funziona, naviga direttamente con &cp=B nell'URL
            if not breve_applied:
                try:
                    current_url = page.url
                    if 'cp=B' not in current_url:
                        # Aggiungi cp=B all'URL corrente e ricarica
                        new_url = current_url + ('&' if '?' in current_url else '?') + 'cp=B'
                        page.goto(new_url, wait_until='networkidle', timeout=20000)
                        page.wait_for_timeout(5000)
                        logger.info(f"Navigato con cp=B: {page.url}")
                        breve_applied = True
                except Exception as e:
                    logger.warning(f"Navigazione cp=B fallita: {e}")

            # ── Screenshot finale ────────────────────────────────────────
            page.screenshot(path=str(PROJECT_ROOT / "data" / "pederzoli_screenshot.png"), full_page=True)
            text = page.inner_text('body')
            logger.info(f"Testo finale (1000 chars): {text[:1000]}")

            # ── Analisi disponibilità ────────────────────────────────────
            text_lower = text.lower()

            # ── Analisi SSN column specifica ─────────────────────────────
            # Il portale mostra DUE colonne: SSN/AGEVOLATO (sinistra, class titolo-dispo-ssn)
            # e PRIVATO (destra). Controlliamo SOLO la colonna SSN.
            # La colonna SSN ha un div con class "titolo-dispo-ssn".
            # Saliamo al contenitore padre per avere tutto il testo SSN.
            ssn_col_text = page.evaluate("""
                (() => {
                    var ssnTitle = document.querySelector('.titolo-dispo-ssn');
                    if (!ssnTitle) return '';
                    // Risali al contenitore padre della colonna SSN
                    var el = ssnTitle;
                    for (var i = 0; i < 8; i++) {
                        el = el.parentElement;
                        if (!el) break;
                        var txt = (el.innerText || '').trim();
                        // Il contenitore SSN deve avere almeno 50 chars ma non tutta la pagina
                        // Stop prima che diventi la pagina intera (>8000 chars)
                        if (txt.length > 80 && txt.length < 8000 && txt.includes('SSN')) {
                            return txt.slice(0, 3000);
                        }
                    }
                    // Fallback: usa testo diretto del genitore immediato
                    return ssnTitle.parentElement ? ssnTitle.parentElement.innerText.slice(0, 3000) : '';
                })()
            """)
            logger.info(f"SSN col text (300): {ssn_col_text[:300]}")

            # Se SSN col trovata, usa quella; altrimenti usa testo intera pagina
            # MA: tronca il testo al separatore PRIVATO per escludere la colonna destra
            if not ssn_col_text:
                # Fallback: tronca testo al primo "PRIVATO" per usare solo colonna sinistra
                privato_idx = text.upper().find('\nPRIVATO\n')
                ssn_col_text = text[:privato_idx] if privato_idx > 0 else text
                logger.info(f"SSN col fallback (PRIVATO split at {privato_idx}): {ssn_col_text[:200]}")

            ssn_col_lower = ssn_col_text.lower()

            has_slots_keywords = [
                'prenota appuntamento',
                "scegli l'orario",
                'scegli orario',
                'seleziona orario',
                'disponibile il',
                'appuntamento disponibile',
                'primo disponibile',
                'prenota ora',
                'prima disponibilità',    # ← testo del calendario Pederzoli
                'mostra orari',           # ← bottone MOSTRA ORARI
            ]

            no_slots_keywords_list = [
                'nessuna disponibilità',
                'nessun appuntamento',
                'non disponibile',
                'nessun risultato',
                "lista d'attesa",
                'non ci sono disponibilità',
                'disponibilità esaurite',   # ← messaggio Pederzoli SSN vuoto
                'torna alla homepage',      # ← appare solo quando SSN è vuoto
            ]

            # Date patterns — rileva mesi/date nel calendario SSN
            date_pattern = re.compile(
                r'\b\d{1,2}[/\-]\d{1,2}[/\-]\d{4}\b|'
                r'\b(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)\s+\d{4}\b',
                re.IGNORECASE
            )
            date_matches = date_pattern.findall(ssn_col_text)

            has_slots = any(kw in ssn_col_lower for kw in has_slots_keywords) or bool(date_matches)
            no_slots = any(kw in ssn_col_lower for kw in no_slots_keywords_list)

            # API OVERRIDE — se proxy restituisce data:[0] non ci sono slot SSN reali
            # Questo blocca i falsi positivi da date del calendario PRIVATO (colonna destra)
            api_data_zero = any(
                '"data":[0]' in ac.get('body', '') or '{"data":[0]' in ac.get('body', '')
                for ac in api_calls
            )
            if api_data_zero:
                has_slots = False
                no_slots = True
                logger.info("API override: data=[0] confermato → nessuno slot SSN reale")

            # "torna alla homepage" o "disponibilità esaurite" indica SSN vuoto — override
            elif no_slots:
                has_slots = False
                logger.info("SSN vuoto confermato (no_slots keyword trovato)")

            logger.info(f"has_slots={has_slots}, no_slots={no_slots}, date={date_matches[:3]}")

            # Log API calls
            if api_calls:
                for ac in api_calls[:5]:
                    logger.info(f"API: {ac['status']} {ac['url'][:80]}: {ac['body'][:100]}")

            return {
                'page_text': text[:3000],
                'has_slots': has_slots,
                'no_slots': no_slots,
                'date_matches': date_matches[:10],
                'timestamp': datetime.now().isoformat(),
            }

        except PwTimeout as e:
            logger.error(f"Timeout: {e}")
            return {'error': str(e), 'has_slots': False, 'no_slots': False}
        except Exception as e:
            logger.error(f"Errore: {e}", exc_info=True)
            return {'error': str(e), 'has_slots': False, 'no_slots': False}
        finally:
            browser.close()


def get_telegram_token():
    """Read Telegram bot token from agent_tokens DB."""
    import sqlite3
    db_path = PROJECT_ROOT / "data" / "conversations_agents.db"
    try:
        conn = sqlite3.connect(str(db_path))
        row = conn.execute(
            "SELECT token FROM agent_tokens WHERE platform='telegram' AND agent_name='satoshi'"
        ).fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"get_telegram_token error: {e}")
        return None


def send_telegram(message):
    """Send Telegram notification via Satoshi bot token."""
    import urllib.request as ureq

    bot_token = get_telegram_token()
    if not bot_token:
        logger.error("Bot token non trovato in agent_tokens DB")
        return False

    payload = json.dumps({
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown'
    })
    req = ureq.Request(
        f'https://api.telegram.org/bot{bot_token}/sendMessage',
        data=payload.encode(),
        headers={'Content-Type': 'application/json'}
    )
    try:
        resp = ureq.urlopen(req, timeout=10)
        logger.info(f"Telegram inviato: {resp.status}")
        return True
    except Exception as e:
        logger.error(f"Telegram errore: {e}")
        return False


def main():
    logger.info("=== Pederzoli Monitor CHECK ===")

    last_state = load_last_state()
    result = check_availability()

    if 'error' in result:
        logger.error(f"Check fallito: {result['error']}")
        return

    page_text = result.get('page_text', '')
    has_slots = result.get('has_slots', False)
    no_slots = result.get('no_slots', False)
    date_matches = result.get('date_matches', [])

    was_available = last_state.get('slots_found', False)

    if has_slots and not was_available:
        date_info = f"\nDate trovate: {', '.join(date_matches[:5])}" if date_matches else ""
        msg = (
            f"🏥 *SLOT SSN DISPONIBILE — Pederzoli Chirurgia Mano!*\n\n"
            f"Prima visita ortopedica - chirurgia della mano\n\n"
            f"Prenota subito:\n"
            f"https://portalepaziente.ospedalepederzoli.it/"
            f"{date_info}\n\n"
            f"Cerca: *Prima visita ortopedica - chirurgia della mano*"
        )
        ok = send_telegram(msg)
        logger.info(f"NOTIFICA {'INVIATA' if ok else 'FALLITA'}")
        save_state({'slots_found': True, 'last_check': result['timestamp'], 'details': page_text[:300]})

    elif has_slots and was_available:
        logger.info("Slot già notificati — nessun nuovo avviso")
        save_state({'slots_found': True, 'last_check': result['timestamp'], 'details': page_text[:300]})

    elif no_slots:
        logger.info("Nessuna disponibilità confermata")
        if was_available:
            send_telegram("ℹ️ Pederzoli Chirurgia Mano: slot non più disponibili.")
        save_state({'slots_found': False, 'last_check': result['timestamp'], 'details': 'Nessuna disponibilità'})

    else:
        logger.info(f"Stato ambiguo — page: {page_text[:200]}")
        save_state({'slots_found': False, 'last_check': result['timestamp'], 'details': f'ambiguous: {page_text[:200]}'})

    logger.info("=== CHECK DONE ===")


if __name__ == "__main__":
    main()
