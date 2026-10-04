"""Reproduce the initial model roster from a pinned pre-roster source revision."""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
from datetime import date
from pathlib import Path

BASE = "00c61d40d18f5152ed2b75db76cb2406b63194e1"


def _literal(node: ast.AST, bindings: dict) -> object:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return bindings[node.id]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        values = [_literal(item, bindings) for item in node.elts]
        return {ast.List: list, ast.Tuple: tuple, ast.Set: set}[type(node)](values)
    if isinstance(node, ast.Dict):
        return {_literal(key, bindings): _literal(value, bindings)
                for key, value in zip(node.keys, node.values, strict=True)}
    raise ValueError(f"Unsupported source literal: {ast.dump(node)}")


def _source(root: Path, commit: str, path: str) -> ast.Module:
    result = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=root,
                            check=True, capture_output=True, text=True)
    return ast.parse(result.stdout)


def generate(root: Path, base: str, updated: str) -> dict:
    if date.fromisoformat(updated).isoformat() != updated:
        raise ValueError("updated must be a canonical YYYY-MM-DD date")
    commit = subprocess.run(["git", "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"],
                            cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    registry = _source(root, commit, "src/pinky_daemon/agent_registry.py")
    model_class = next(node for node in registry.body
                       if isinstance(node, ast.ClassDef) and node.name == "AgentRegistry")
    bindings = {}
    for node in model_class.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in {
                    "_SONNET_5_DESCRIPTION", "_MODEL_SEEDS",
                }:
                    bindings[target.id] = _literal(node.value, bindings)
    seeds = bindings["_MODEL_SEEDS"]
    bindings = {}
    for node in _source(root, commit, "src/pinky_daemon/pricing.py").body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = _literal(node.value, bindings)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "RATE_TABLE":
                bindings["RATE_TABLE"] = _literal(node.value, bindings)
    rates = bindings["RATE_TABLE"]
    one_million = next(_literal(node.value, {}) for node in
                       _source(root, commit, "src/pinky_daemon/streaming_session.py").body
                       if isinstance(node, ast.Assign) and any(
                           isinstance(target, ast.Name) and target.id == "_1M_MODELS"
                           for target in node.targets))
    models = []
    for seed in seeds:
        provider, mid, display, description, tier, context, one_m, inp, out, cached, thinking, sort = seed
        rate = rates[mid]
        if (inp, out, cached) != (rate["input"], rate["output"], rate["cache_read"]):
            raise ValueError(f"Source rates disagree for {mid}")
        if bool(one_m) != (mid in one_million):
            raise ValueError(f"Source context flags disagree for {mid}")
        models.append({
            "provider": provider, "model_id": mid, "display_name": display,
            "description": description, "tier": tier, "context_window": context,
            "is_1m": bool(one_m), "pricing": {
                "input": inp, "output": out, "cached_input": cached,
                "cache_write_5m": rate["cache_write_5m"],
                "cache_write_1h": rate["cache_write_1h"],
            },
            "supports_thinking": bool(thinking), "active": True, "sort_order": sort,
        })
    return {"schema": "pinky-model-roster/1", "revision": 1, "updated": updated, "models": models}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=BASE)
    parser.add_argument("--updated", default="2026-10-04")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    document = generate(Path(__file__).resolve().parents[1], arguments.base, arguments.updated)
    arguments.output.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")


if __name__ == "__main__":
    main()
