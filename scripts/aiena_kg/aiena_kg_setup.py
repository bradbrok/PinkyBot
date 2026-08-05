#!/usr/bin/env python3
"""
AIena Knowledge Graph — Script di setup.

Esegue:
1. Installazione dipendenze (networkx)
2. Creazione database SQLite
3. Verifica funzionamento

Uso:
    python3 aiena_kg_setup.py [--force]

Flags:
    --force   Ricrea il database anche se esiste
"""
import subprocess
import sys
from pathlib import Path

# Paths
SCRIPT_DIR = Path(__file__).parent
DATA_DIR = Path("/home/pinky/.pinkybot/data")
DB_PATH = DATA_DIR / "aiena_knowledge_graph.db"
SCHEMA_PATH = SCRIPT_DIR / "schema.sql"
VENV_PIP = Path("/home/pinky/.pinkybot/.venv/bin/pip")


def install_dependencies():
    """Installa networkx nel virtualenv di PinkyBot."""
    print("[setup] Verifico dipendenze...")

    # Controlla se networkx e installato
    try:
        import networkx
        print(f"[setup] networkx gia installato: v{networkx.__version__}")
        return True
    except ImportError:
        pass

    print("[setup] Installo networkx...")

    pip_cmd = str(VENV_PIP) if VENV_PIP.exists() else "pip3"

    try:
        result = subprocess.run(
            [pip_cmd, "install", "networkx"],
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode == 0:
            print("[setup] networkx installato con successo")
            return True
        else:
            print(f"[setup] ERRORE installazione: {result.stderr}")
            return False
    except Exception as e:
        print(f"[setup] ERRORE: {e}")
        return False


def create_database(force: bool = False):
    """Crea il database SQLite con lo schema."""
    import sqlite3

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if DB_PATH.exists() and not force:
        print(f"[setup] Database esiste gia: {DB_PATH}")
        print("[setup] Usa --force per ricrearlo")
        return True

    if not SCHEMA_PATH.exists():
        print(f"[setup] ERRORE: schema non trovato: {SCHEMA_PATH}")
        return False

    print(f"[setup] Creo database: {DB_PATH}")

    try:
        # Elimina se esiste e force
        if DB_PATH.exists() and force:
            DB_PATH.unlink()
            print("[setup] Database precedente eliminato")

        # Crea connessione e esegui schema
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_PATH.read_text())
        conn.close()

        print("[setup] Database creato con successo")
        print(f"[setup] Dimensione: {DB_PATH.stat().st_size / 1024:.1f} KB")
        return True

    except Exception as e:
        print(f"[setup] ERRORE creazione database: {e}")
        return False


def verify_installation():
    """Verifica che tutto funzioni."""
    print("\n[setup] Verifica installazione...")

    try:
        # Import del modulo
        sys.path.insert(0, str(SCRIPT_DIR.parent))
        from aiena_kg import KnowledgeGraph

        # Crea istanza
        kg = KnowledgeGraph()

        # Test operazioni base
        entity_id = kg.add_entity("Test Entity", "ALTRO", {"test": True})
        print(f"[setup] Test add_entity: OK (id={entity_id})")

        entity = kg.get_entity(entity_id)
        assert entity is not None
        print(f"[setup] Test get_entity: OK")

        results = kg.search_entity("Test")
        assert len(results) >= 1
        print(f"[setup] Test search_entity: OK")

        stats = kg.get_stats()
        print(f"[setup] Test get_stats: OK (entities={stats['entities']})")

        # Cleanup test entity
        import sqlite3
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("DELETE FROM entities WHERE name = 'TEST ENTITY'")
        conn.commit()
        conn.close()
        print("[setup] Cleanup test data: OK")

        print("\n[setup] === INSTALLAZIONE COMPLETATA ===")
        print(f"[setup] Database: {DB_PATH}")
        print("[setup] Pronto per l'uso!")
        return True

    except Exception as e:
        print(f"[setup] ERRORE verifica: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Setup AIena Knowledge Graph"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Forza ricreazione database"
    )
    args = parser.parse_args()

    print("=" * 50)
    print("AIena Knowledge Graph — Setup")
    print("=" * 50)

    # Step 1: Dipendenze
    if not install_dependencies():
        sys.exit(1)

    # Step 2: Database
    if not create_database(force=args.force):
        sys.exit(1)

    # Step 3: Verifica
    if not verify_installation():
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
