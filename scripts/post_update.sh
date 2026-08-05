#!/bin/bash
# post_update.sh — Master post-update hook for PinkyBot local customizations.
# Called automatically by /admin/update after every git pull + rebuild.
# Runs all patch scripts in order. Each patch must be idempotent.
#
# Usage: bash scripts/post_update.sh

set -e
SCRIPTS_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== PinkyBot post-update hook ==="

# 1. Account switch feature (Claude personal ↔ work)
if [ -f "$SCRIPTS_DIR/patch_account_switch.sh" ]; then
    echo "--- Applying: patch_account_switch.sh"
    bash "$SCRIPTS_DIR/patch_account_switch.sh"
else
    echo "--- SKIP: patch_account_switch.sh not found"
fi

# 2. Dream runner WAL->TRUNCATE fix (mirrors #797/#220 for conversations_agents.db)
if [ -f "$SCRIPTS_DIR/patch_dream_wal.sh" ]; then
    echo "--- Applying: patch_dream_wal.sh"
    bash "$SCRIPTS_DIR/patch_dream_wal.sh"
else
    echo "--- SKIP: patch_dream_wal.sh not found"
fi

echo "=== post-update hook complete ==="
