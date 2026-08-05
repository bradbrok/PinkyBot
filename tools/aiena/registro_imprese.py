#!/usr/bin/env python3
"""
Registro Imprese OSINT - Cerca società e persone nel registro italiano
Fonti: OpenCorporates (free tier), link diretti a portali pubblici
Parte del toolkit OSINT di AIena.
"""

import argparse
import json

import requests


def search_registro(query: str, tipo: str = "azienda") -> dict:
    result = {
        "source": "Registro Imprese / OpenCorporates",
        "query": query,
        "tipo": tipo,
        "aziende": [],
        "errors": [],
        "links": {}
    }

    headers = {"User-Agent": "Mozilla/5.0 (investigative-osint/1.0)"}
    q_enc = requests.utils.quote(query)

    # OpenCorporates API free (senza key, rate limited)
    try:
        r = requests.get(
            "https://api.opencorporates.com/v0.4/companies/search",
            params={"q": query, "jurisdiction_code": "it", "per_page": 10},
            timeout=15,
            headers=headers
        )
        if r.status_code == 200:
            data = r.json()
            companies = data.get("results", {}).get("companies", [])
            for c in companies[:5]:
                co = c.get("company", {})
                result["aziende"].append({
                    "nome": co.get("name"),
                    "numero_registro": co.get("company_number"),
                    "stato": co.get("current_status"),
                    "tipo": co.get("company_type"),
                    "indirizzo": co.get("registered_address_in_full"),
                    "data_costituzione": co.get("incorporation_date"),
                    "url_opencorporates": co.get("opencorporates_url"),
                })
        else:
            result["errors"].append(f"OpenCorporates API: {r.status_code} — richiede API key per query avanzate")
    except Exception as e:
        result["errors"].append(f"OpenCorporates: {e}")

    # Link diretti a portali pubblici italiani (sempre generati)
    result["links"] = {
        "opencorporates_web": f"https://opencorporates.com/companies/it?q={q_enc}",
        "registro_imprese_it": f"https://www.registroimprese.it/ricerca-libera?denominazione={q_enc}",
        "infoimprese": f"https://www.infoimprese.it/ricerca-aziende?q={q_enc}",
        "visura_camerale": f"https://www.visura.co/ricerca/{q_enc}",
        "consob_emittenti": f"https://www.consob.it/web/area-pubblica/consulta-elenchi?q={q_enc}",
        "google_bilanci": f"site:registroimprese.it OR site:infoimprese.it \"{query}\"",
        "google_soci": f"\"{query}\" soci amministratori bilancio filetype:pdf",
    }

    # Per persone fisiche: aggiungi ricerca specifica
    if tipo == "persona":
        result["links"]["google_persona"] = (
            f"\"{query}\" site:government.it OR site:camera.it OR site:senato.it"
        )
        result["links"]["openpolis"] = f"https://www.openpolis.it/persona/{q_enc}/"

    return result


def main():
    parser = argparse.ArgumentParser(description="Registro Imprese OSINT per AIena")
    parser.add_argument("--query", required=True, help="Nome azienda o persona da cercare")
    parser.add_argument("--tipo", choices=["azienda", "persona"], default="azienda")
    args = parser.parse_args()

    data = search_registro(args.query, args.tipo)
    print(json.dumps(data, indent=2, ensure_ascii=False))

    print("\n" + "="*60)
    if data["aziende"]:
        print(f"Aziende trovate: {len(data['aziende'])}")
        for a in data["aziende"]:
            print(f"  → {a['nome']} | {a.get('stato','?')} | {a.get('numero_registro','?')}")
            print(f"    {a.get('url_opencorporates','')}")
    else:
        print("Nessun risultato API — usa i link per ricerca manuale:")
    for k, v in data["links"].items():
        print(f"  [{k}] {v}")


if __name__ == "__main__":
    main()
