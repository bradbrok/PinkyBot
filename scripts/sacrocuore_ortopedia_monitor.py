#!/usr/bin/env python3
"""
Monitor multi-struttura — VISITA ORTOPEDICA CHIRURGIA DELLA MANO
Verona: Sacro Cuore Negrar + Clinica San Francesco
Cron ogni 5 minuti. Notifica ogni check (trovato o no).
"""
import requests
import json
from datetime import datetime, timedelta
import sqlite3

NRE = "050A10267231133"
CF  = "FRTMRK75M03L781B"
TELEGRAM_CHAT_ID = "32405655"
LOG_FILE = "/home/pinky/.pinkybot/scripts/sacrocuore_ortopedia_monitor.log"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
}

# Strutture da monitorare
STRUTTURE = [
    {
        'nome': 'Sacro Cuore Negrar',
        'db': 'tt_sacrocuore_prod',
        'activity_id': 'sc167320b3dd5ec4',
        'activity_name': 'VISITA ORTOPEDICA CHIRURGIA DELLA MANO PRIMA',
        'url': 'https://www.sacrocuore.it/prenota-online/',
        'use_nre': True,
    },
    {
        'nome': 'Clinica San Francesco Verona',
        'db': 'tt_afea_san_francesco',
        'activity_id': 'sc15eb02d01f2932',   # PRIMA VISITA ORTOPEDICA PER LA MANO
        'activity_name': 'PRIMA VISITA ORTOPEDICA PER LA MANO',
        'url': 'https://www.ghcspa.com/it/clinicasanfrancesco/prenota-visita',
        'use_nre': True,
    },
    {
        'nome': 'Clinica San Francesco (alt)',
        'db': 'tt_afea_san_francesco',
        'activity_id': 'sc15fdc80330976f',   # PRIMA VISITA ORTOPEDICA MANO/GOMITO
        'activity_name': 'PRIMA VISITA ORTOPEDICA MANO/GOMITO',
        'url': 'https://www.ghcspa.com/it/clinicasanfrancesco/prenota-visita',
        'use_nre': True,
    },
]

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
    token = get_telegram_token()
    if not token:
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, },
            timeout=10
        )
        if r.status_code == 200:
            log("✉️ Telegram OK")
        else:
            log(f"⚠️ Telegram {r.status_code}")
    except Exception as e:
        log(f"⚠️ Telegram: {e}")

def check_struttura(s):
    today = datetime.now().strftime("%Y-%m-%d")
    end = (datetime.now() + timedelta(days=180)).strftime("%Y-%m-%d")
    db = s['db']
    h = {**HEADERS, 'Referer': f'https://app.tuotempo.com/mop/index.php?dbName={db}'}
    params = {
        'activityid': s['activity_id'],
        'start_date': today,
        'end_date': end,
        'version': '1.1', 'lang': 'it',
        'application': 'MOP', 'client': 'desktop',
    }
    if s['use_nre']:
        params['nre'] = NRE
        params['fiscal_code'] = CF
    try:
        r = requests.get(
            f"https://app.tuotempo.com/api/v3/{db}/availabilities",
            params=params, headers=h, timeout=20
        )
        data = r.json()
        ret = data.get('return', {})
        if isinstance(ret, dict):
            avails = ret.get('results', {}).get('availabilities', [])
            return avails
    except Exception as e:
        log(f"⚠️ {s['nome']}: {e}")
    return []

def main():
    log("=== Check avviato ===")
    ts = datetime.now().strftime("%d/%m/%Y %H:%M")

    found_any = False
    results = []

    for s in STRUTTURE:
        avails = check_struttura(s)
        log(f"{s['nome']}: {len(avails)} slot")
        if avails:
            found_any = True
            slot_lines = []
            for av in avails[:3]:
                date = av.get('date', av.get('start_date', '?'))
                time_val = av.get('time', av.get('start_time', ''))
                slot_lines.append(f"  📅 {date} {time_val}")
            results.append((s, slot_lines))

    if found_any:
        msg_parts = [f"🚨 SLOT LIBERO! [{ts}]\n🦴 Visita Ortopedica - Chirurgia della Mano\n👤 Mirko Feriotti\n"]
        for s, slots in results:
            msg_parts.append(f"\n{s['nome']}:\n" + "\n".join(slots))
            msg_parts.append(f"\n👉 <a href=\"{s['url']}\">Prenota subito</a>")
            msg_parts.append(f"\nNRE: {NRE} | CF: {CF}")
        msg_parts.append("\n\n⏰ Fai in fretta!")
        send_telegram("\n".join(msg_parts))
    else:
        # Notifica "nessun slot" come richiesto da Mirko
        strutture_status = " | ".join([f"{s['nome'].split()[0]}: ❌" for s in STRUTTURE[:2]])
        msg = f"⏱ [{ts}] {strutture_status}\nNessun slot ortopedia mano. Ricontrollo tra 5 min."
        send_telegram(msg)

    log("=== Check completato ===")

if __name__ == '__main__':
    main()
