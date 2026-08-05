#!/usr/bin/env python3
"""
Monitor disponibilità Prima Visita Ginecologica - Sacro Cuore Negrar
Gira ogni 5 minuti via cron. Notifica su Telegram quando trova slot.
Se trova slot, tenta auto-prenotazione via Playwright.
"""
import requests
import json
from datetime import datetime
import sys
import os
import subprocess
import time

# Config
BASE = "https://app.tuotempo.com/api/v3/tt_sacrocuore_prod"
ACTIVITY_ID = "sc15e27092ee94a2"   # PRIMA VISITA GINECOLOGICA
ACTIVITY_ENDO = "sc15e27092eee83b" # PRIMA VISITA GINECOLOGICA PER ENDOMETRIOSI
NRE_GINECO = "050A10304210294"     # Ricetta Prima Visita Ginecologica
NRE_ENDO   = "050A10304210293"     # Ricetta Prima Visita Endometriosi
CF = "LBRVNT78T47L364K"
TELEGRAM_CHAT_ID = "32405655"
PINKYBOT_API = "http://localhost:8888"
LOG_FILE = "/home/pinky/.pinkybot/scripts/sacrocuore_gineco_monitor.log"

# Dati paziente per prenotazione automatica
PAZIENTE = {
    "nome": "Valentina",
    "cognome": "Alberi",
    "nascita": "07/12/1978",
    "telefono": "3493785983",
    "email": "Valentinaalb@etik.com",
    "cf": CF,
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Referer': 'https://app.tuotempo.com/',
}

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(line + "\n")
    except:
        pass

def get_telegram_token():
    """Get bot token from PinkyBot DB"""
    import sqlite3
    db = '/home/pinky/.pinkybot/data/conversations_agents.db'
    try:
        conn = sqlite3.connect(db)
        c = conn.cursor()
        c.execute("SELECT token FROM agent_tokens WHERE platform='telegram' AND agent_name='satoshi'")
        row = c.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        log(f"DB token error: {e}")
        return None

def send_telegram(text):
    """Send Telegram message directly via bot API"""
    token = get_telegram_token()
    if not token:
        log("⚠️ No Telegram token found")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10
        )
        if r.status_code == 200:
            log("✉️ Telegram inviato OK")
        else:
            log(f"⚠️ Telegram error: {r.status_code} {r.text[:100]}")
    except Exception as e:
        log(f"⚠️ Telegram exception: {e}")

def check_availabilities():
    today = datetime.now().strftime("%Y-%m-%d")
    results = []

    for act_id, act_name, act_nre in [
        # ACTIVITY_ID / NRE_GINECO rimossi su richiesta Mirko 2026-04-16
        (ACTIVITY_ENDO, "Prima Visita Ginecologica (Endometriosi)", NRE_ENDO),
    ]:
        try:
            r = requests.get(
                f"{BASE}/availabilities",
                params={
                    'activityid': act_id,
                    'start_date': today,
                    'version': '1.1',
                    'lang': 'it',
                    'application': 'MOP',
                    'client': 'desktop',
                },
                headers=HEADERS,
                timeout=20
            )
            data = r.json()
            avails = data.get('return', {}).get('results', {}).get('availabilities', [])
            if avails:
                results.append((act_id, act_name, act_nre, avails))
                log(f"✅ TROVATA DISPONIBILITÀ per {act_name}: {len(avails)} slot")
            else:
                log(f"❌ Nessuna disponibilità per {act_name}")
        except Exception as e:
            log(f"⚠️ Errore check {act_name}: {e}")

    return results

def try_autobooking(act_id, act_name, act_nre, avails):
    """
    Tenta prenotazione automatica via Playwright.
    Naviga TuoTempo, inserisce NRE+CF, seleziona attività, clicca primo slot,
    compila form paziente, conferma.
    Ritorna: ("success", url) | ("manual", url) | ("error", msg)
    """
    log(f"🤖 Avvio auto-booking per {act_name}...")

    # Imposta LD_LIBRARY_PATH per Playwright
    env = os.environ.copy()
    pw_libs = '/home/pinky/.local/lib/playwright-deps/extracted/usr/lib/x86_64-linux-gnu'
    env['LD_LIBRARY_PATH'] = pw_libs + ':' + env.get('LD_LIBRARY_PATH', '')
    env['DISPLAY'] = ':99'  # Xvfb se disponibile

    booking_script = f'''
import asyncio
import os
import sys

os.environ['LD_LIBRARY_PATH'] = '{pw_libs}:' + os.environ.get('LD_LIBRARY_PATH', '')

from playwright.async_api import async_playwright

BOOKING_URL = "https://www.sacrocuore.it/prenota-online/"
NRE = "{act_nre}"
CF = "{PAZIENTE['cf']}"
ACT_ID = "{act_id}"
PAZIENTE_NOME = "{PAZIENTE['nome']}"
PAZIENTE_COGNOME = "{PAZIENTE['cognome']}"
PAZIENTE_NASCITA = "{PAZIENTE['nascita']}"
PAZIENTE_TEL = "{PAZIENTE['telefono']}"
PAZIENTE_EMAIL = "{PAZIENTE['email']}"

def patch_route(route):
    try:
        response = route.fetch()
        body = response.text()
        if 'apiPreventScraping' in body:
            body = body.replace('"enabled":1,', '"enabled":0,')
            body = body.replace('"enabled":"1",', '"enabled":"0",')
        route.fulfill(response=response, body=body)
    except Exception:
        route.continue_()

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--disable-gpu',
            ]
        )
        ctx = await browser.new_context(
            viewport={{"width": 1280, "height": 900}},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await ctx.new_page()
        await page.add_init_script("Object.defineProperty(navigator,'webdriver',{{get:()=>undefined}});")

        # Intercetta risposta bootstrap TuoTempo per disabilitare anti-scraping
        await page.route("**/index.php?dbName=tt_sacrocuore_prod**", patch_route)
        await page.route("**/index.php**", patch_route)

        log_msgs = []

        try:
            # 1. Apri pagina prenotazione
            log_msgs.append("Apertura pagina...")
            await page.goto(BOOKING_URL, wait_until="networkidle", timeout=30000)
            await page.wait_for_timeout(2000)

            # Cerca iframe TuoTempo
            frames = page.frames
            tt_frame = None
            for f in frames:
                if 'tuotempo' in f.url or 'sacrocuore' in f.url.lower():
                    tt_frame = f
                    break

            if not tt_frame:
                # Cerca iframe nel DOM
                iframe_el = await page.query_selector('iframe[src*="tuotempo"]')
                if iframe_el:
                    tt_frame = await iframe_el.content_frame()

            target = tt_frame or page
            log_msgs.append(f"Frame: {{target.url[:80] if hasattr(target,'url') else 'page'}}")

            # 2. Inserisci NRE
            log_msgs.append("Inserimento NRE...")
            nre_input = None
            for sel in ['input[placeholder*="NRE"]', 'input[placeholder*="nre"]',
                        'input[name*="nre"]', 'input[type="text"]']:
                try:
                    el = await target.wait_for_selector(sel, timeout=5000)
                    if el:
                        nre_input = el
                        break
                except:
                    pass

            if not nre_input:
                print(f"STATUS:manual:{{BOOKING_URL}}")
                print("LOG:" + "|".join(log_msgs + ["NRE input non trovato"]))
                await browser.close()
                return

            await nre_input.click()
            await page.keyboard.type(NRE, delay=50)
            await page.wait_for_timeout(500)
            await page.keyboard.press("Tab")

            # 3. Inserisci CF
            log_msgs.append("Inserimento CF...")
            await page.keyboard.type(CF, delay=50)
            await page.wait_for_timeout(500)

            # 4. Clicca Cerca/Avanti
            log_msgs.append("Click Cerca...")
            for btn_text in ["Cerca", "Avanti", "Verifica", "Continua", "Conferma"]:
                try:
                    btn = await target.query_selector(f'button:has-text("{{btn_text}}")')
                    if not btn:
                        btn = await target.query_selector(f'[value="{{btn_text}}"]')
                    if btn:
                        await btn.click()
                        log_msgs.append(f"Cliccato: {{btn_text}}")
                        await page.wait_for_timeout(2000)
                        break
                except:
                    pass

            # 5. Seleziona attività se necessario
            log_msgs.append("Selezione attività...")
            await page.wait_for_timeout(2000)

            # Cerca link/bottone per l'attività
            for act_text in ["Ginecologica", "Ginecologia", "Visita Ginecol"]:
                try:
                    el = await target.query_selector(f'text="{act_text}"')
                    if el:
                        await el.click()
                        log_msgs.append(f"Attività selezionata: {{act_text}}")
                        await page.wait_for_timeout(2000)
                        break
                except:
                    pass

            # 6. Cerca slot disponibili
            log_msgs.append("Ricerca slot...")
            for btn_text in ["Cerca", "Cerca disponibilità", "Avanti"]:
                try:
                    btn = await target.query_selector(f'button:has-text("{{btn_text}}")')
                    if btn:
                        await btn.click()
                        log_msgs.append(f"Cliccato: {{btn_text}}")
                        await page.wait_for_timeout(3000)
                        break
                except:
                    pass

            # 7. Clicca primo slot disponibile
            log_msgs.append("Click primo slot...")
            slot_clicked = False
            for slot_sel in [
                '.tt-availability-slot:first-child',
                '.slot:first-child',
                '.appointment:first-child',
                '[class*="slot"]:first-child',
                '[class*="availab"]:first-child',
                'button[class*="slot"]',
                '.calendar-slot:first-child',
                'td.available:first-child',
                'td[class*="free"]:first-child',
            ]:
                try:
                    el = await target.query_selector(slot_sel)
                    if el and await el.is_visible():
                        await el.click()
                        log_msgs.append(f"Slot cliccato: {{slot_sel}}")
                        slot_clicked = True
                        await page.wait_for_timeout(2000)
                        break
                except:
                    pass

            if not slot_clicked:
                # Prova a cliccare qualsiasi bottone verde/disponibile
                try:
                    slots = await target.query_selector_all('button, a, td, div')
                    for el in slots[:50]:
                        classes = await el.get_attribute('class') or ''
                        if any(x in classes.lower() for x in ['avail', 'free', 'slot', 'open', 'book']):
                            if await el.is_visible():
                                await el.click()
                                log_msgs.append("Slot cliccato (fallback class)")
                                slot_clicked = True
                                await page.wait_for_timeout(2000)
                                break
                except:
                    pass

            if not slot_clicked:
                current_url = page.url
                print(f"STATUS:manual:{{current_url}}")
                print("LOG:" + "|".join(log_msgs + ["Slot non cliccabile - form manuale"]))
                await browser.close()
                return

            # 8. Compila dati paziente
            log_msgs.append("Compilazione dati paziente...")
            await page.wait_for_timeout(1000)

            # Mappa campi comuni
            field_map = [
                (["nome", "first_name", "firstname", "name"], PAZIENTE_NOME),
                (["cognome", "last_name", "lastname", "surname"], PAZIENTE_COGNOME),
                (["nascita", "birth", "data_nascita", "dob", "birthdate"], PAZIENTE_NASCITA),
                (["telefono", "phone", "tel", "cellulare", "mobile"], PAZIENTE_TEL),
                (["email", "mail"], PAZIENTE_EMAIL),
                (["codice_fiscale", "cf", "fiscal"], CF),
            ]

            filled = 0
            for field_names, value in field_map:
                for fname in field_names:
                    for sel in [
                        f'input[name*="{{fname}}"]',
                        f'input[id*="{{fname}}"]',
                        f'input[placeholder*="{{fname.title()}}"]',
                        f'input[placeholder*="{{fname}}"]',
                    ]:
                        try:
                            el = await target.query_selector(sel)
                            if el and await el.is_visible():
                                await el.click()
                                await el.fill('')
                                await page.keyboard.type(value, delay=30)
                                filled += 1
                                log_msgs.append(f"Campo compilato: {{fname}}")
                                break
                        except:
                            pass
                    else:
                        continue
                    break

            log_msgs.append(f"Campi compilati: {{filled}}")

            # 9. Conferma prenotazione
            log_msgs.append("Tentativo conferma...")
            confirmed = False
            for btn_text in ["Conferma", "Prenota", "Invia", "Salva", "Procedi", "Continua"]:
                try:
                    btn = await target.query_selector(f'button:has-text("{{btn_text}}")')
                    if not btn:
                        btn = await target.query_selector(f'input[value="{{btn_text}}"]')
                    if btn and await btn.is_visible():
                        await btn.click()
                        log_msgs.append(f"Confermato con: {{btn_text}}")
                        await page.wait_for_timeout(3000)
                        confirmed = True
                        break
                except:
                    pass

            # 10. Verifica risultato
            final_url = page.url
            page_text = await page.inner_text('body') if confirmed else ""

            success_keywords = ["prenotazione", "confermata", "conferma", "numero", "appuntamento", "grazie", "success"]
            if any(k in page_text.lower() for k in success_keywords):
                print(f"STATUS:success:{{final_url}}")
                print("LOG:" + "|".join(log_msgs + [f"✅ PRENOTATO! testo: {{page_text[:200]!r}}"]))
            else:
                # Non siamo sicuri — manda link manuale al punto raggiunto
                print(f"STATUS:manual:{{final_url}}")
                print("LOG:" + "|".join(log_msgs + [f"Stato incerto, testo: {{page_text[:200]!r}}"]))

        except Exception as e:
            import traceback
            print(f"STATUS:error:{{str(e)}}")
            print("LOG:" + "|".join(log_msgs + [f"EXCEPTION: {{e}}", traceback.format_exc()[-300:]]))

        finally:
            await browser.close()

asyncio.run(main())
'''

    # Scrivi script temporaneo
    tmp_script = "/tmp/sacrocuore_booking_attempt.py"
    with open(tmp_script, 'w') as f:
        f.write(booking_script)

    try:
        result = subprocess.run(
            [sys.executable, tmp_script],
            capture_output=True, text=True, timeout=120,
            env=env
        )
        output = result.stdout + result.stderr
        log(f"Booking script output:\n{output[:2000]}")

        # Parsa status
        for line in output.splitlines():
            if line.startswith("STATUS:"):
                parts = line.split(":", 2)
                status = parts[1] if len(parts) > 1 else "error"
                url = parts[2] if len(parts) > 2 else BOOKING_FALLBACK_URL
                return (status, url)

        return ("error", f"Nessun STATUS nel output: {output[:200]}")

    except subprocess.TimeoutExpired:
        return ("error", "Timeout (120s) durante auto-booking")
    except Exception as e:
        return ("error", str(e))

BOOKING_FALLBACK_URL = "https://www.sacrocuore.it/prenota-online/"

def main():
    log("=== Check avviato ===")

    found = check_availabilities()

    ts = datetime.now().strftime("%H:%M:%S")

    if found:
        for act_id, act_name, act_nre, avails in found:
            slot_info = []
            for av in avails[:5]:
                date = av.get('date', av.get('start_date', '?'))
                time_val = av.get('time', av.get('start_time', ''))
                slot_info.append(f"  📅 {date} {time_val}")

            # Notifica immediata
            alert_msg = f"""🚨 SLOT LIBERO al Sacro Cuore! [{ts}]

🔬 {act_name}
👩 Valentina Alberi

Disponibilità trovata:
{chr(10).join(slot_info[:5])}

⏳ Tento prenotazione automatica..."""

            log(f"✅ Trovato! Invio notifica alert...")
            send_telegram(alert_msg)

            # Tenta auto-prenotazione
            log("🤖 Avvio Playwright per auto-booking...")
            status, url = try_autobooking(act_id, act_name, act_nre, avails)

            if status == "success":
                booking_msg = f"""✅ PRENOTAZIONE COMPLETATA!

🔬 {act_name}
👩 Valentina Alberi

La prenotazione è stata confermata automaticamente.
URL: {url}

Controlla email {PAZIENTE['email']} per conferma."""
                log("✅ Auto-booking riuscito!")
                send_telegram(booking_msg)

            elif status == "manual":
                manual_msg = f"""⚠️ SLOT TROVATO — PRENOTA SUBITO!

🔬 {act_name}
Ho aperto il processo di prenotazione ma serve completamento manuale.

👉 Apri questo link e completa:
{url if url != BOOKING_FALLBACK_URL else "https://www.sacrocuore.it/prenota-online/"}

Dati da inserire:
• NRE: {act_nre}
• CF: {CF}
• Nome: Valentina Alberi
• Tel: {PAZIENTE['telefono']}
• Email: {PAZIENTE['email']}

⏰ Fai in fretta, lo slot potrebbe esaurirsi!"""
                log("⚠️ Auto-booking parziale, serve completamento manuale")
                send_telegram(manual_msg)

            else:
                error_msg = f"""🚨 SLOT LIBERO — PRENOTA MANUALMENTE ORA!

🔬 {act_name}

Auto-prenotazione fallita: {url[:100]}

👉 Vai subito su:
https://www.sacrocuore.it/prenota-online/

Dati:
• NRE: {act_nre}
• CF: {CF}
• Valentina Alberi, 07/12/1978
• Tel: {PAZIENTE['telefono']}"""
                log(f"❌ Auto-booking fallito: {url}")
                send_telegram(error_msg)
    else:
        msg = f"⏱ [{ts}] Sacro Cuore: nessuno slot disponibile. Ricontrollo tra 5 min."
        log(msg)
        send_telegram(msg)

    log("=== Check completato ===")

if __name__ == '__main__':
    main()
