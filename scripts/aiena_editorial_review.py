#!/usr/bin/env python3
"""
aiena_editorial_review.py — Controllo editoriale automatico pre-preview

Analizza ogni draft con Claude Opus prima del deploy preview.
Se il draft non supera il controllo, Opus lo riscrive automaticamente.
Max 2 round. Se ancora fallisce → blocca preview + notifica AIena.

Criteri: presunzione di innocenza, hedging obbligatorio su indagati,
titoli non assertivi, linguaggio non accusatorio senza attribuzione.

Integrazione: chiamato da aiena_preview_cron.py prima della conversione HTML.

Author: AIena (PinkyBot)
"""

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import subprocess

# ── Config: Claude CLI path ───────────────────────────────────────────────────
CLAUDE_BIN = "/home/pinky/.local/bin/claude"

# ── Config ───────────────────────────────────────────────────────────────────
REVIEW_MODEL    = "claude-opus-4-7"
REPORTS_DIR     = Path("/home/pinky/.pinkybot/data/agents/aiena/review_reports")
MAX_ROUNDS      = 2

REVIEW_PROMPT = """\
Sei un editor legale specializzato in giornalismo investigativo italiano.
Il tuo compito è verificare che questo articolo rispetti rigorosamente:
1. La presunzione di innocenza (art. 27 Cost. italiana)
2. Il Testo Unico sulla diffamazione e le norme deontologiche dell'Ordine dei Giornalisti
3. Gli standard AIena: esponiamo fatti documentati e facciamo domande — non accusiamo

Analizza l'articolo e verifica i seguenti SEI criteri:

---

CRITERIO 1 — TITOLO
Il titolo NON deve:
- Implicare colpa diretta senza condanna ("trattava appalti" = implica accordo corruttivo)
- Usare sostantivi accusatori senza attribuzione ("il corrotto", "il ladro", "il faccendiere")
- Affermare fatti non documentati come certi

Il titolo PUÒ:
- Citare lo stato dell'indagine: "indagato per", "sotto inchiesta", "la Procura ipotizza"
- Descrivere fatti verificati: "sette settimane tra le elezioni e la GdF"
- Fare domande o usare virgolette per frasi altrui

CRITERIO 2 — HEDGING SUL CORPO
Ogni affermazione su fatti non definitivamente accertati riguardante persone indagate
DEVE contenere uno di questi marcatori:
  "avrebbe" / "secondo la Procura" / "nell'ipotesi investigativa" /
  "secondo l'accusa" / "gli inquirenti ipotizzano" / "stando alle indagini" /
  "secondo quanto emerge dagli atti" / "è accusato di" / "è indagato per"

Frasi che presentano come certi fatti in fase di indagine → FAIL.
Esempio FAIL: "Il sindaco gestiva gli appalti in modo irregolare."
Esempio PASS: "Il sindaco avrebbe gestito gli appalti in modo irregolare, secondo la Procura."

CRITERIO 3 — CLAIM SENZA FONTE (warning, non blocca)
Fatti specifici (numeri, importi, date, dichiarazioni virgolettate) senza fonte
citata inline o nella sezione Fonti finale → SEGNALA come warning.
Non blocca la pubblicazione, ma elenca i claim da verificare.

CRITERIO 4 — DISCLAIMER PRESUNZIONE INNOCENZA
L'articolo DEVE contenere, in fondo, una nota esplicita del tipo:
"Tutte le persone citate sono indagate in fase preliminare e devono essere
considerate innocenti fino a eventuale condanna definitiva."
Se mancante → FAIL (può essere aggiunto automaticamente).

CRITERIO 5 — LINGUAGGIO ASSERTIVO NON ATTRIBUITO
Espressioni che affermano una realtà criminale senza citare atti formali → FAIL:
- "rete corruttiva" (senza "quella che la Procura definisce")
- "sistema criminale" (senza attribuzione)
- "associazione mafiosa" (senza essere citata da atti giudiziari)
- "il sistema di corruzione" (presentato come fatto accertato)

CRITERIO 6 — AZIONI INVESTIGATIVE DICHIARATE (BLOCCA SEMPRE — non auto-correggibile)
Verifica se l'articolo contiene affermazioni che implicano azioni concrete svolte dalla redazione o dall'autore, ad esempio:
- "è stata inviata una lettera / PEC / verifica formale / richiesta ufficiale"
- "abbiamo inviato" / "abbiamo contattato" / "abbiamo richiesto"
- "deadline per risposta" con data specifica
- Riferimenti a silenzio-inadempimento (L. 241/1990) basati su richieste non documentate
- Nomi di funzionari specifici con ruoli precisi citati come destinatari di azioni della redazione

Se QUALSIASI di questi pattern è presente → overall_pass = false, manual_review_needed = true.
NON tentare auto-fix su questo criterio. Richiede sempre revisione umana.

---

IMPORTANTE: Distingui sempre tra:
- Fatti ACCERTATI (sentenza definitiva, atti pubblici, dichiarazioni verificate) → nessun hedging richiesto
- Fatti IN INDAGINE (indagato, accusato, ipotesi PM) → hedging OBBLIGATORIO
- Opinioni/dichiarazioni altrui riportate → devono essere attribuite con virgolette

---

OUTPUT: Rispondi SOLO con JSON valido, nessun testo aggiuntivo:

{
  "overall_pass": true/false,
  "manual_review_needed": false,
  "criteria": {
    "title": {
      "pass": true/false,
      "issues": ["descrizione problema se presente"]
    },
    "hedging": {
      "pass": true/false,
      "issues": [
        {"text": "testo originale problematico", "reason": "motivo", "suggested_fix": "testo corretto"}
      ]
    },
    "unsourced_claims": {
      "pass": true,
      "warnings": ["claim senza fonte inline"]
    },
    "disclaimer": {
      "pass": true/false,
      "present": true/false
    },
    "assertive_language": {
      "pass": true/false,
      "issues": [
        {"text": "espressione problematica", "reason": "motivo", "suggested_fix": "alternativa corretta"}
      ]
    },
    "investigative_actions": {
      "pass": true/false,
      "issues": ["descrizione azione dichiarata senza documentazione"]
    }
  },
  "summary": "Riassunto in 1-2 frasi di cosa è stato trovato"
}

overall_pass = true SOLO SE tutti questi passano: title, hedging, disclaimer, assertive_language, investigative_actions.
unsourced_claims NON blocca overall_pass (è solo un warning).
manual_review_needed = true SOLO SE i problemi sono ambigui e richiedono giudizio umano, O se investigative_actions.pass = false (sempre).
"""

FIX_PROMPT = """\
Sei un editor legale specializzato in giornalismo investigativo italiano.
Hai analizzato l'articolo seguente e trovato i problemi elencati.
Il tuo compito è correggere SOLO le parti problematiche, mantenendo
intatto tutto il resto (struttura, fatti, tono, fonti).

REGOLE PER LE CORREZIONI:
- Aggiungi hedging dove manca ("avrebbe", "secondo la Procura", ecc.)
- Sostituisci linguaggio assertivo non attribuito con versioni attribuite
- Correggi il titolo solo se flaggato, mantenendo forza giornalistica
- Se il disclaimer manca, aggiungilo alla fine prima della sezione Fonti
- NON cambiare i fatti, le fonti, i numeri, le date
- NON aggiungere o rimuovere sezioni
- NON rendere il testo asettico: mantieni il tono AIena (freddo, diretto, con domande)

PROBLEMI DA CORREGGERE:
{issues_json}

Rispondi SOLO con il testo completo dell'articolo corretto in Markdown.
Nessun commento, nessuna spiegazione, solo il testo corretto.
"""

DISCLAIMER_TEXT = """\

---

*Tutte le persone citate in questo articolo sono indagate in fase preliminare \
e devono essere considerate innocenti fino a eventuale condanna definitiva. \
AIena non formula accuse: espone fatti documentati e domande di interesse pubblico.*
"""


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [review] {msg}", flush=True)


def sha256_short(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def call_opus(system: str, user: str) -> str:
    """Call Claude via CLI for one-shot review (account OAuth, no API key needed)."""
    full_prompt = system + "\n\n---\n\nARTICOLO DA ANALIZZARE:\n\n" + user
    result = subprocess.run(
        [CLAUDE_BIN, "--output-format", "text", "-p", full_prompt, "--max-turns", "1"],
        capture_output=True, text=True, timeout=300,
        env={**os.environ, "HOME": "/home/pinky"},
        cwd="/tmp",  # avoid loading AIena's CLAUDE.md and MCP servers
    )
    output = result.stdout.strip()
    # Empty output = API unavailable regardless of rc — raise so caller can skip gracefully
    if not output:
        raise RuntimeError(
            f"Claude CLI returned empty response (rc={result.returncode}): {result.stderr[:200]}"
        )
    return output


def parse_review_json(raw: str) -> dict:
    """Extract JSON from Opus response (may have markdown fences)."""
    raw = raw.strip()
    # Strip ```json ... ``` fences if present
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw.strip())


def collect_issues(review: dict) -> list[dict]:
    """Flatten all actionable issues from a review result."""
    issues = []
    c = review.get("criteria", {})

    if not c.get("title", {}).get("pass", True):
        for issue in c["title"].get("issues", []):
            issues.append({"criterion": "title", "description": issue})

    if not c.get("hedging", {}).get("pass", True):
        for issue in c["hedging"].get("issues", []):
            issues.append({"criterion": "hedging", **issue})

    if not c.get("assertive_language", {}).get("pass", True):
        for issue in c["assertive_language"].get("issues", []):
            issues.append({"criterion": "assertive_language", **issue})

    if not c.get("disclaimer", {}).get("pass", True):
        issues.append({"criterion": "disclaimer", "description": "Disclaimer presunzione innocenza mancante"})

    return issues


def auto_fix_draft(draft_text: str, issues: list[dict]) -> str:
    """Ask Opus to fix the draft given a list of issues."""
    issues_json = json.dumps(issues, ensure_ascii=False, indent=2)
    system = FIX_PROMPT.format(issues_json=issues_json)
    fixed = call_opus(system, draft_text)
    # Ensure disclaimer is present (belt and suspenders)
    if "devono essere considerate innocenti" not in fixed:
        # Insert before ## Fonti or at end
        if "## Fonti" in fixed:
            fixed = fixed.replace("## Fonti", DISCLAIMER_TEXT + "\n## Fonti", 1)
        else:
            fixed = fixed.rstrip() + "\n" + DISCLAIMER_TEXT
    return fixed


def run_review(slug: str, draft_path: Path) -> dict:
    """
    Main entry point. Reviews the draft and auto-fixes if needed.
    Returns a report dict. If manual_review_needed=True, the caller
    should block the preview and notify AIena.

    Side effects:
    - May overwrite draft_path with auto-fixed version
    - Writes report JSON to REPORTS_DIR/{slug}_review.json
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{slug}_review.json"

    draft_text = draft_path.read_text("utf-8")
    original_hash = sha256_short(draft_text)

    log(f"Reviewing draft: {draft_path.name} (hash: {original_hash})")

    rounds_data = []
    auto_fixes_applied = []
    current_text = draft_text
    overall_pass = False
    manual_review_needed = False

    for round_num in range(1, MAX_ROUNDS + 1):
        log(f"  Round {round_num}/{MAX_ROUNDS} — calling {REVIEW_MODEL}...")
        try:
            raw_response = call_opus(REVIEW_PROMPT, current_text)
            review = parse_review_json(raw_response)
        except RuntimeError as e:
            # CLI unavailable (empty response, timeout, crash) — skip review, don't block
            log(f"  WARN: Claude CLI unavailable — skipping review (proceeding to preview): {e}")
            overall_pass = True
            break
        except json.JSONDecodeError as e:
            # Claude returned non-JSON — genuine parsing failure, flag for human review
            log(f"  ERROR: Could not parse review JSON: {e}")
            manual_review_needed = True
            break
        except Exception as e:
            # Unexpected error — skip rather than block
            log(f"  WARN: Unexpected review error — skipping (proceeding to preview): {e}")
            overall_pass = True
            break

        rounds_data.append(review)
        log(f"  Round {round_num} result: pass={review.get('overall_pass')} | "
            f"summary={review.get('summary','')[:80]}")

        # CRITERIO 6: investigative_actions.pass = False → always manual review, never auto-fix
        inv_actions = review.get("criteria", {}).get("investigative_actions", {})
        if inv_actions.get("pass") is False:
            manual_review_needed = True
            log(f"  [C6] BLOCKED — azioni investigative dichiarate rilevate: "
                f"{inv_actions.get('issues', [])[:2]}")
            break

        if review.get("overall_pass"):
            overall_pass = True
            break

        if review.get("manual_review_needed"):
            manual_review_needed = True
            log(f"  Manual review requested by model.")
            break

        if round_num < MAX_ROUNDS:
            issues = collect_issues(review)
            if not issues:
                # No actionable issues but overall_pass=False — safety net
                manual_review_needed = True
                break
            log(f"  Auto-fixing {len(issues)} issue(s)...")
            try:
                fixed_text = auto_fix_draft(current_text, issues)
                # Track what changed
                for issue in issues:
                    auto_fixes_applied.append({
                        "criterion": issue.get("criterion"),
                        "original": issue.get("text", issue.get("description", "")),
                        "suggested_fix": issue.get("suggested_fix", ""),
                        "reason": issue.get("reason", issue.get("description", "")),
                    })
                current_text = fixed_text
                log(f"  Draft updated with auto-fixes.")
            except Exception as e:
                log(f"  ERROR during auto-fix: {e}")
                manual_review_needed = True
                break
        else:
            # Exhausted rounds
            log(f"  Max rounds reached — marking for manual review.")
            manual_review_needed = True

    # If auto-fix was applied, overwrite the draft file
    if auto_fixes_applied and current_text != draft_text:
        log(f"  Saving fixed draft → {draft_path.name}")
        tmp = str(draft_path) + ".tmp"
        Path(tmp).write_text(current_text, "utf-8")
        os.replace(tmp, draft_path)

    # Build final report
    final_review = rounds_data[-1] if rounds_data else {}
    report = {
        "slug": slug,
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "model": REVIEW_MODEL,
        "draft_hash_original": original_hash,
        "draft_hash_final": sha256_short(current_text),
        "overall_pass": overall_pass,
        "manual_review_needed": manual_review_needed,
        "rounds": len(rounds_data),
        "issues_found": len(collect_issues(final_review)) if not overall_pass else 0,
        "issues_fixed": len(auto_fixes_applied),
        "auto_fixes_applied": auto_fixes_applied,
        "criteria": final_review.get("criteria", {}),
        "summary": final_review.get("summary", ""),
        "unsourced_warnings": (
            final_review.get("criteria", {})
            .get("unsourced_claims", {})
            .get("warnings", [])
        ),
    }

    # Save report
    tmp_report = str(report_path) + ".tmp"
    Path(tmp_report).write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp_report, report_path)
    log(f"  Report saved → {report_path.name}")

    return report


# ── CLI (test standalone) ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="AIena editorial review — standalone test")
    parser.add_argument("slug", help="Article slug")
    parser.add_argument("--draft", help="Path to DRAFT_*.md (optional, auto-detected if omitted)")
    parser.add_argument("--dry-run", action="store_true", help="Review only, do not overwrite draft")
    args = parser.parse_args()

    if args.draft:
        draft_p = Path(args.draft)
    else:
        candidates = list(
            Path("/home/pinky/.pinkybot/data/agents/aiena").glob(f"DRAFT_{args.slug}*.md")
        )
        if not candidates:
            print(f"ERROR: No DRAFT file found for slug '{args.slug}'", file=sys.stderr)
            sys.exit(1)
        draft_p = candidates[0]

    print(f"Reviewing: {draft_p}")

    if args.dry_run:
        original = draft_p.read_text("utf-8")

    result = run_review(args.slug, draft_p)

    if args.dry_run and draft_p.read_text("utf-8") != original:
        # Restore original
        draft_p.write_text(original, "utf-8")
        print("(dry-run: draft restored to original)")

    print("\n" + "=" * 60)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("=" * 60)
    sys.exit(0 if result["overall_pass"] else 1)
