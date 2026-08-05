#!/usr/bin/env python3
"""
ANAC OSINT - Cerca appalti e contratti pubblici italiani
Fonte: portale ANAC + BDNCP + MePA
Parte del toolkit OSINT di AIena.
"""

import argparse
import json
import sys

import requests


def search_anac(query: str, ente: str | None = None) -> dict:
    result = {
        "source": "ANAC - Autorità Nazionale Anticorruzione",
        "query": query,
        "ente": ente,
        "appalti": [],
        "errors": [],
        "links": {}
    }

    headers = {"User-Agent": "Mozilla/5.0 (investigative-osint/1.0)"}
    q_enc = requests.utils.quote(query)

    # Prova API ANAC CKAN opendata
    try:
        search_url = f"https://dati.anticorruzione.it/api/3/action/package_search?q={q_enc}&rows=5"
        r = requests.get(search_url, timeout=15, headers=headers)
        if r.status_code == 200:
            try:
                data = r.json()
                if data.get("success"):
                    for d in data.get("result", {}).get("results", [])[:5]:
                        result["appalti"].append({
                            "titolo": d.get("title"),
                            "organizzazione": d.get("organization", {}).get("title"),
                            "url": f"https://dati.anticorruzione.it/opendata/dataset/{d.get('name')}",
                            "note": d.get("notes", "")[:200]
                        })
            except Exception:
                pass
        else:
            result["errors"].append(f"API ANAC status: {r.status_code}")
    except Exception as e:
        result["errors"].append(f"API ANAC: {e}")

    # Link diretti sempre utili per AIena
    result["links"] = {
        "ricerca_trasparenza": f"https://www.anticorruzione.it/-/ricerca-contratti-pubblici?q={q_enc}",
        "opendata_portal": f"https://dati.anticorruzione.it/opendata?q={q_enc}",
        "google_appalti": f"site:anticorruzione.it \"{query}\" appalto OR gara OR contratto",
        "gazzetta_ufficiale": f"site:gazzettaufficiale.it \"{query}\" appalto",
        "trasparenza_pa": f"site:amministrazionetrasparente.it \"{query}\"",
    }

    if ente:
        result["links"]["ente_specifico"] = (
            f"https://www.anticorruzione.it/-/ricerca-contratti-pubblici?q={q_enc}&denominazioneStazioneAppaltante={requests.utils.quote(ente)}"
        )

    return result


def main():
    parser = argparse.ArgumentParser(description="ANAC OSINT per AIena")
    parser.add_argument("--query", required=True, help="Keyword (ente, azienda, oggetto gara)")
    parser.add_argument("--ente", help="Filtra per ente appaltante")
    args = parser.parse_args()

    data = search_anac(args.query, args.ente)
    print(json.dumps(data, indent=2, ensure_ascii=False))

    print("\n" + "="*60)
    print("ANAC — Link per indagine:")
    for k, v in data["links"].items():
        print(f"  [{k}] {v}")
    if data["appalti"]:
        print(f"\nDataset trovati: {len(data['appalti'])}")
        for a in data["appalti"]:
            print(f"  → {a.get('titolo', '?')} ({a.get('organizzazione', '?')})")


if __name__ == "__main__":
    main()
