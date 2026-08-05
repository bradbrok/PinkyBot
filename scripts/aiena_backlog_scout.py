#!/usr/bin/env python3
"""
AIena Backlog Scout — trigger giornaliero lun-ven alle 10:30.
Ogni giorno sceglie un'idea dai leads (a rotazione) e chiede ad AIena
di trovare 1-2 lead freschi su di essa, aggiornando pipeline.json.
Il weekend non gira (ci pensa aiena_weekly_trigger.py con ricerca profonda).
"""
import json
import requests
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from pinky_daemon.auth import build_internal_auth_headers, resolve_signing_secret

AIENA_ENDPOINT = "http://localhost:8888/agents/aiena/message"
PIPELINE_JSON = Path("/var/www/aiena.it/data/pipeline.json")
STATE_FILE = Path("/home/pinky/.pinkybot/data/aiena_backlog_scout_state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_index": -1}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def trigger_aiena(message: str):
    try:
        _secret = resolve_signing_secret()
        _auth_hdrs = build_internal_auth_headers(
            _secret, agent_name="satoshi", method="POST", path="/agents/aiena/message"
        ) if _secret else {}
        resp = requests.post(
            AIENA_ENDPOINT,
            json={"from_agent": "satoshi", "message": message},
            headers={"Content-Type": "application/json", **_auth_hdrs},
            timeout=10
        )
        print(f"[backlog-scout] AIena triggered: {resp.status_code}")
    except Exception as e:
        print(f"[backlog-scout] Errore trigger AIena: {e}")


def main():
    now = datetime.now(ZoneInfo("Europe/Rome"))
    dow = now.weekday()  # 5=Sab, 6=Dom

    if dow in (5, 6):
        print(f"[backlog-scout] Weekend — skip (ci pensa aiena_weekly_trigger)")
        return

    try:
        data = json.loads(PIPELINE_JSON.read_text())
    except Exception as e:
        print(f"[backlog-scout] Errore lettura pipeline.json: {e}")
        return

    backlog = data.get("leads", [])
    if not backlog:
        print("[backlog-scout] Backlog vuoto — nulla da fare")
        return

    # Rotazione: prendi il prossimo item a rotazione
    state = load_state()
    last_idx = state.get("last_index", -1)
    current_idx = (last_idx + 1) % len(backlog)
    item = backlog[current_idx]

    title = item.get("title", "")
    description = item.get("description", "")
    category = item.get("category", "")
    existing_notes = item.get("research_notes", [])
    notes_str = "\n".join(f"- {n}" for n in existing_notes) if existing_notes else "Nessuna ancora."

    # Selezione fonti per categoria
    source_map = {
        "Sanita":              ["ANAC (dati.anticorruzione.it)", "CONSIP (consip.it)", "Corte dei Conti (banchedati.corteconti.it)", "Trasparenza.gov.it", "OCCRP Aleph (aleph.occrp.org)"],
        "PNRR":                ["OpenCoesione (opencoesione.gov.it)", "OpenBDAP (openbdap.rgs.mef.gov.it)", "ItaliaDomai.gov.it", "OpenPNRR (openpnrr.it)", "ANAC"],
        "Patrimonio":          ["Agenzia Entrate / Catasto (agenziaentrate.gov.it — richiede SPID Mirko)", "Corte dei Conti", "dati.gov.it", "ANAC"],
        "Corruzione":          ["ANAC", "OCCRP Aleph (aleph.occrp.org)", "OpenSanctions (opensanctions.org)", "Corte dei Conti"],
        "Politica":            ["Openpolis (openpolis.it)", "Camera open data (dati.camera.it)", "OpenSanctions (PEP check)", "ICIJ Offshore Leaks (offshoreleaks.icij.org)"],
        "Politica estera":     ["OCCRP Aleph", "OpenSanctions", "ICIJ Offshore Leaks", "OpenCorporates (opencorporates.com)"],
        "Pubblica Amministrazione": ["ANAC", "Trasparenza.gov.it", "Corte dei Conti", "FOIA/Accesso Civico (foia.gov.it — inviare richiesta formale se necessario)"],
        "Fondi UE":            ["OpenCoesione", "OpenBDAP", "OCCRP Aleph", "EU Audit Reports (eca.europa.eu)"],
        "Appalti pubblici":    ["ANAC (CIG lookup)", "CONSIP", "Corte dei Conti", "Trasparenza.gov.it"],
        "Chiesa":              ["OCCRP Aleph", "OpenCorporates", "Registro Imprese (registroimprese.it)"],
    }
    # Fonti borderline gratuite — sempre da controllare (con supervisione Mirko se trova qualcosa)
    DARK_WEB_SOURCES = [
        "DDoSecrets (ddosecrets.org) — CONFERMATO ITALIANO: 1,35GB Polizia di Stato, intercettazioni Telecom Italia, email politico Gallina",
        "WikiLeaks Cablegate (wikileaks.org/plusd) — CONFERMATO ITALIANO: migliaia di cablogrammi su Berlusconi/Putin/ENI/Gazprom/NSA/Camorra. Cerca prefissi ROME/NAPLES/MILAN",
        "Archive.org / Wayback Machine (web.archive.org) — pagine/documenti rimossi PA, versioni precedenti siti istituzionali",
        "Have I Been Pwned (haveibeenpwned.com) — breach italiani: SIAE, Ferrovie, Giglio. Verifica account soggetti indagati",
        "RECAP/CourtListener (courtlistener.com) — CONFERMATO ITALIANO: ENI/Saipem ($240M FCPA), 'ndrangheta USA. Atti giudiziari federali USA gratuiti",
        "FCPA Clearinghouse Stanford (fcpa.stanford.edu) — corruzione internazionale aziende italiane. Gratuito.",
        "Shodan free tier (shodan.io) — CONFERMATO ITALIANO: 62k+ IP PA italiani esposti. Cerca country:IT. Solo osservazione passiva.",
    ]
    fonti_categoria = source_map.get(category, ["ANAC", "OpenData (dati.gov.it)", "OCCRP Aleph (aleph.occrp.org)", "Corte dei Conti"])
    fonti_str = "\n".join(f"  - {f}" for f in fonti_categoria)
    dark_str = "\n".join(f"  - {f}" for f in DARK_WEB_SOURCES)

    msg = (
        f"Backlog scout giornaliero — {now.strftime('%A %d %b')}.\n\n"
        f"Indaga brevemente questa storia dal backlog:\n"
        f"**Titolo:** {title}\n"
        f"**Categoria:** {category}\n"
        f"**Descrizione:** {description}\n\n"
        f"Note già raccolte:\n{notes_str}\n\n"
        f"Obiettivo: trova 1-2 lead NUOVI e concreti (fonti, dati aperti, documenti, nomi).\n\n"
        f"Fonti PRIORITARIE per questa categoria ({category}):\n{fonti_str}\n\n"
        f"🌑 DARK WEB — controlla SEMPRE (per ogni categoria):\n{dark_str}\n\n"
        f"Fonti aggiuntive sempre disponibili: Registro Imprese (registroimprese.it), "
        f"OpenCorporates, Openpolis, dati.gov.it.\n\n"
        f"NOTA su fonti speciali:\n"
        f"- Catasto/SPID: se serve verifica patrimonio immobiliare, aggiungi 'RICHIESTA_MIRKO: verifica catastale su [nome + CF]' nelle research_notes — Satoshi avviserà Mirko\n"
        f"- FOIA: se nessuna fonte pubblica ha il documento, aggiungi 'RICHIESTA_MIRKO: FOIA a [ente] per [documento]' — Satoshi valuterà\n\n"
        f"Quando finisci:\n"
        f"1. Aggiungi i lead trovati a `research_notes` di questo item in /var/www/aiena.it/data/pipeline.json\n"
        f"2. Se trovi qualcosa di significativo che fa salire la priorità, cambia `status` da 'idea' a 'ricerca'\n"
        f"3. Dopo aver aggiornato pipeline.json, esegui SEMPRE: "
        f"`import subprocess; subprocess.run(['/home/pinky/.pinkybot/.venv/bin/python3', "
        f"'/home/pinky/.pinkybot/scripts/aiena_admin_rebuild.py'], timeout=60)`\n"
        f"4. Nessuna notifica a Mirko — lavoro silenzioso. "
        f"ECCEZIONE: se hai aggiunto 'RICHIESTA_MIRKO:' nelle notes, invia a Satoshi via send_to_agent('satoshi') per coordinamento\n\n"
        f"Tieni il lavoro compatto: 10-15 minuti, poi aggiorna pipeline.json, rebuilda il pannello admin e stop."
    )

    print(f"[backlog-scout] Item scelto [{current_idx}]: {title}")
    trigger_aiena(msg)

    # Salva stato rotazione
    state["last_index"] = current_idx
    state["last_run"] = now.isoformat()
    state["last_item"] = title
    save_state(state)

    print(f"[backlog-scout] Done — prossimo: {backlog[(current_idx + 1) % len(backlog)]['title']}")


if __name__ == "__main__":
    main()
