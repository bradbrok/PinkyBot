#!/usr/bin/env node
/**
 * i18n-extract.js — Scan source files for $_() usages and sync en.json
 *
 * - Finds all $_('key') and $_("key") calls in .svelte and .js files under src/
 * - Adds missing keys to en.json with value ""
 * - Reports orphaned keys (in en.json but not in code) — warns, does NOT remove
 * - Exits with code 1 if new keys were added (signals CI that translations are needed)
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SRC_DIR = path.resolve(__dirname, '../src');
const EN_JSON = path.resolve(__dirname, '../src/locales/en.json');

// ---- File scanning --------------------------------------------------------

function walkDir(dir, exts, results = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walkDir(full, exts, results);
    } else if (exts.some(e => entry.name.endsWith(e))) {
      results.push(full);
    }
  }
  return results;
}

// ---- Key extraction -------------------------------------------------------

// Matches $_('key') or $_("key") — static string only
const STATIC_KEY_RE = /\$_\(\s*(['"])([a-zA-Z0-9_.]+)\1/g;

// Matches dynamic patterns we can't extract — warn about these
const DYNAMIC_KEY_RE = /\$_\(\s*(`[^`]*`|[^'")`][^)]*)\s*\)/g;

function extractKeysFromFile(filepath) {
  const raw = fs.readFileSync(filepath, 'utf8');

  // Strip single-line comments (// ...)
  // Strip block comments (/* ... */) and HTML comments (<!-- ... -->)
  const stripped = raw
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\/\/[^\n]*/g, '');

  const keys = new Set();
  const warnings = [];
  const relPath = path.relative(process.cwd(), filepath);

  // Find dynamic usages first and warn
  let dynMatch;
  const dynRe = new RegExp(DYNAMIC_KEY_RE.source, 'g');
  while ((dynMatch = dynRe.exec(stripped)) !== null) {
    const snippet = dynMatch[0].slice(0, 60).replace(/\n/g, ' ');
    warnings.push(`  ${relPath}: dynamic key skipped — ${snippet}`);
  }

  // Extract static keys
  let match;
  const staticRe = new RegExp(STATIC_KEY_RE.source, 'g');
  while ((match = staticRe.exec(stripped)) !== null) {
    keys.add(match[2]);
  }

  return { keys, warnings };
}

// ---- JSON helpers ---------------------------------------------------------

/**
 * Get a nested value from an object by dot-path.
 * Returns undefined if path doesn't exist.
 */
function getByPath(obj, keyPath) {
  const parts = keyPath.split('.');
  let cur = obj;
  for (const p of parts) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[p];
  }
  return cur;
}

/**
 * Set a nested value in an object by dot-path, creating intermediate objects.
 */
function setByPath(obj, keyPath, value) {
  const parts = keyPath.split('.');
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (cur[parts[i]] == null || typeof cur[parts[i]] !== 'object') {
      cur[parts[i]] = {};
    }
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}

/**
 * Collect all leaf key paths from a nested object as dot-separated strings.
 */
function collectPaths(obj, prefix = '', results = []) {
  for (const [k, v] of Object.entries(obj)) {
    const fullKey = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
      collectPaths(v, fullKey, results);
    } else {
      results.push(fullKey);
    }
  }
  return results;
}

// ---- Main -----------------------------------------------------------------

function main() {
  // 1. Scan all source files
  const files = walkDir(SRC_DIR, ['.svelte', '.js']);
  const allKeys = new Set();
  const allWarnings = [];

  for (const file of files) {
    // Skip locale files themselves
    if (file.includes('/locales/')) continue;

    const { keys, warnings } = extractKeysFromFile(file);
    keys.forEach(k => allKeys.add(k));
    allWarnings.push(...warnings);
  }

  if (allWarnings.length > 0) {
    console.warn('\nWarnings — dynamic keys skipped (cannot auto-extract):');
    allWarnings.forEach(w => console.warn(w));
  }

  console.log(`\nFound ${allKeys.size} static key(s) in source files.`);

  // 2. Load current en.json
  let enJson = {};
  if (fs.existsSync(EN_JSON)) {
    enJson = JSON.parse(fs.readFileSync(EN_JSON, 'utf8'));
  }

  const existingPaths = new Set(collectPaths(enJson));

  // 3. Find new keys (in code, not in en.json)
  const newKeys = [...allKeys].filter(k => !existingPaths.has(k)).sort();

  // 4. Find orphaned keys (in en.json, not in code)
  const orphaned = [...existingPaths].filter(k => !allKeys.has(k)).sort();

  // 5. Add new keys with empty string value
  if (newKeys.length > 0) {
    console.log(`\nAdding ${newKeys.length} new key(s) to en.json:`);
    for (const key of newKeys) {
      console.log(`  + ${key}`);
      setByPath(enJson, key, '');
    }
    fs.writeFileSync(EN_JSON, JSON.stringify(enJson, null, 2) + '\n', 'utf8');
    console.log(`\nWrote updated en.json.`);
  } else {
    console.log('\nen.json is up to date — no new keys to add.');
  }

  // 6. Report orphaned keys (warn only, do not remove)
  if (orphaned.length > 0) {
    console.warn(`\nOrphaned keys in en.json (not found in code) — review manually:`);
    orphaned.forEach(k => console.warn(`  ? ${k}`));
    console.warn('  (Not removed automatically — delete manually if safe.)');
  }

  // 7. Exit with code 1 if new keys were added (CI signal)
  if (newKeys.length > 0) {
    console.log('\nExiting with code 1 — new keys added, translations needed.');
    process.exit(1);
  }

  console.log('\nDone.');
}

main();
