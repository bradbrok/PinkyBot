#!/usr/bin/env python3
"""
OpenPolis Search - Cerca dati su politici italiani
Parte del toolkit OSINT di AIena.
"""

import argparse
import json
import re
import sys
from typing import Any
from urllib.parse import quote

import requests


def search_openpolis(name: str) -> dict[str, Any]:
    """
    Cerca informazioni su un politico italiano tramite OpenPolis.

    OpenPolis non ha API pubblica ufficiale, quindi usiamo:
    1. Scraping leggero delle pagine pubbliche
    2. API indirette dove disponibili
    3. Fallback su suggerimenti di ricerca
    """
    results = {
        "source": "OpenPolis - Osservatorio Politico",
        "query": name,
        "politico": None,
        "incarichi": [],
        "presenze": None,
        "dichiarazioni": [],
        "finanziamenti": [],
        "errors": [],
        "links": []
    }

    # Normalizza il nome per la ricerca
    name_normalized = name.strip().lower()
    name_url = quote(name)

    # URL principali OpenPolis
    urls = {
        "parlamento18": f"https://parlamento18.openpolis.it/parlamentare/{name_url}",
        "parlamento19": f"https://parlamento19.openpolis.it/parlamentare/{name_url}",
        "voisietequi": f"https://voisietequi.openpolis.it/candidato/{name_url}",
        "search": f"https://www.openpolis.it/?s={name_url}"
    }

    results["links"] = [
        {"name": "Ricerca OpenPolis", "url": urls["search"]},
        {"name": "Parlamento XVIII Legislatura", "url": urls["parlamento18"]},
        {"name": "Parlamento XIX Legislatura", "url": urls["parlamento19"]},
    ]

    headers = {
        "User-Agent": "AIena-OSINT/1.0 (Investigative Journalism Tool)"
    }

    # Prova a ottenere dati dal sito parlamento
    for leg_name, leg_url in [("XIX", urls["parlamento19"]), ("XVIII", urls["parlamento18"])]:
        try:
            # Costruisci URL con slug del nome
            name_slug = "-".join(name.lower().split())
            test_url = f"https://parlamento{leg_name.lower()}.openpolis.it/parlamentare/{name_slug}"

            response = requests.get(test_url, headers=headers, timeout=15, allow_redirects=True)

            if response.status_code == 200:
                html = response.text

                # Estrai informazioni base dal HTML
                politico_info = extract_politico_info(html, name)
                if politico_info:
                    results["politico"] = politico_info
                    results["politico"]["legislatura"] = leg_name
                    results["politico"]["url"] = test_url
                    break

        except requests.exceptions.RequestException as e:
            results["errors"].append(f"Errore connessione {leg_name}: {str(e)}")
        except Exception as e:
            results["errors"].append(f"Errore parsing {leg_name}: {str(e)}")

    # Cerca su Camera.it per dati patrimoniali
    try:
        camera_results = search_camera_patrimoni(name)
        if camera_results:
            results["dichiarazioni"] = camera_results
    except Exception as e:
        results["errors"].append(f"Errore ricerca Camera.it: {str(e)}")

    # Aggiungi suggerimenti di ricerca manuale
    results["manual_search"] = {
        "openpolis": f"https://www.openpolis.it/?s={name_url}",
        "camera_anagrafe": "https://www.camera.it/leg19/28",
        "senato_composizione": "https://www.senato.it/leg/19/BGT/Schede/Attsen/Sena.html",
        "google_dork": f'site:openpolis.it "{name}"',
        "wikipedia": f"https://it.wikipedia.org/wiki/{name_url.replace('%20', '_')}"
    }

    return results


def extract_politico_info(html: str, name: str) -> dict[str, Any] | None:
    """Estrae informazioni base dal HTML della pagina OpenPolis."""
    info = {}

    # Cerca il nome nel titolo
    title_match = re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE)
    if title_match:
        info["nome_pagina"] = title_match.group(1).strip()

    # Cerca gruppo parlamentare
    gruppo_match = re.search(r'gruppo[:\s]+([^<]+)', html, re.IGNORECASE)
    if gruppo_match:
        info["gruppo"] = gruppo_match.group(1).strip()[:100]

    # Cerca circoscrizione
    circ_match = re.search(r'circoscrizione[:\s]+([^<]+)', html, re.IGNORECASE)
    if circ_match:
        info["circoscrizione"] = circ_match.group(1).strip()[:100]

    # Cerca percentuale presenze
    presenze_match = re.search(r'presenze[:\s]+(\d+[\.,]?\d*)\s*%', html, re.IGNORECASE)
    if presenze_match:
        info["presenze_percentuale"] = presenze_match.group(1)

    # Cerca numero voti
    voti_match = re.search(r'(\d+)\s*vot[io]', html, re.IGNORECASE)
    if voti_match:
        info["voti_espressi"] = voti_match.group(1)

    if info:
        info["nome_cercato"] = name
        return info

    return None


def search_camera_patrimoni(name: str) -> list[dict[str, Any]]:
    """Cerca dichiarazioni patrimoniali su Camera.it."""
    results = []

    # L'anagrafe patrimoniale della Camera ha URL strutturati
    # ma richiede navigazione. Forniamo i link diretti.

    name_parts = name.split()
    if len(name_parts) >= 2:
        results.append({
            "tipo": "Anagrafe patrimoniale Camera",
            "descrizione": "Dichiarazioni dei redditi e situazione patrimoniale",
            "url": "https://www.camera.it/leg19/28",
            "nota": f"Cerca manualmente: {name}"
        })

        results.append({
            "tipo": "Senato - Composizione",
            "descrizione": "Scheda senatore con dichiarazioni",
            "url": "https://www.senato.it/leg/19/BGT/Schede/Attsen/Sena.html",
            "nota": f"Cerca manualmente: {name}"
        })

    return results


def print_summary(results: dict[str, Any]) -> None:
    """Stampa un riepilogo human-readable dei risultati."""
    print("\n" + "=" * 60)
    print("OPENPOLIS SEARCH - Risultati")
    print("=" * 60)
    print(f"Politico cercato: {results['query']}")
    print(f"Fonte: {results['source']}")
    print("-" * 60)

    if results["errors"]:
        print("\nAVVISI:")
        for err in results["errors"]:
            print(f"  ! {err}")

    if results["politico"]:
        print("\nINFORMAZIONI TROVATE:")
        pol = results["politico"]
        for key, value in pol.items():
            if key != "nome_cercato":
                print(f"  {key}: {value}")
    else:
        print("\nNessuna informazione diretta trovata.")
        print("Potrebbe essere necessaria una ricerca manuale.")

    if results["dichiarazioni"]:
        print("\nDICHIARAZIONI PATRIMONIALI:")
        for dich in results["dichiarazioni"]:
            print(f"  - {dich['tipo']}")
            print(f"    URL: {dich['url']}")
            if dich.get("nota"):
                print(f"    Nota: {dich['nota']}")

    if results["links"]:
        print("\nLINK UTILI:")
        for link in results["links"]:
            print(f"  - {link['name']}: {link['url']}")

    if results.get("manual_search"):
        print("\nRICERCA MANUALE:")
        for source, url in results["manual_search"].items():
            print(f"  {source}: {url}")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Cerca informazioni su politici italiani via OpenPolis",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python3 openpolis.py --name "Giorgia Meloni"
  python3 openpolis.py --name "Matteo Salvini"
  python3 openpolis.py --name "Giuseppe Conte" --json-only
        """
    )
    parser.add_argument("--name", "-n", required=True, help="Nome del politico da cercare")
    parser.add_argument("--json-only", "-j", action="store_true", help="Output solo JSON, no summary")

    args = parser.parse_args()

    results = search_openpolis(args.name)

    # Output JSON
    print(json.dumps(results, indent=2, ensure_ascii=False))

    # Summary human-readable (se non json-only)
    if not args.json_only:
        print_summary(results)

    sys.exit(0)


if __name__ == "__main__":
    main()
