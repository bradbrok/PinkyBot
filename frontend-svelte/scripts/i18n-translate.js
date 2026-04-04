#!/usr/bin/env node
/**
 * i18n-translate.js — Machine-translate empty locale keys via DeepL Free API
 *
 * - Reads src/locales/en.json as the source of truth
 * - For each other locale (ru, es, uk, ja, zh, ko):
 *   - Finds keys where value is "" (untranslated)
 *   - Calls DeepL Free API to fill them in
 * - Requires DEEPL_API_KEY env var — skips gracefully if missing
 * - Batches requests to avoid hammering the API
 */

import fs from 'fs';
import path from 'path';
import https from 'https';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const LOCALES_DIR = path.resolve(__dirname, '../src/locales');

// DeepL target language codes keyed by locale filename (without .json)
const DEEPL_LANG = {
  ru: 'RU',
  es: 'ES',
  uk: 'UK',
  ja: 'JA',
  zh: 'ZH',
  ko: 'KO',
};

// How many strings to send per DeepL request
const BATCH_SIZE = 25;

// Delay between batches in ms — be a good citizen
const BATCH_DELAY_MS = 300;

// ---- Helpers ---------------------------------------------------------------

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Collect all leaf key paths and values from a nested object.
 * Returns array of { path: 'a.b.c', value: '...' }
 */
function collectLeaves(obj, prefix = '', results = []) {
  for (const [k, v] of Object.entries(obj)) {
    const fullKey = prefix ? `${prefix}.${k}` : k;
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
      collectLeaves(v, fullKey, results);
    } else {
      results.push({ path: fullKey, value: v });
    }
  }
  return results;
}

function getByPath(obj, keyPath) {
  const parts = keyPath.split('.');
  let cur = obj;
  for (const p of parts) {
    if (cur == null || typeof cur !== 'object') return undefined;
    cur = cur[p];
  }
  return cur;
}

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

// ---- DeepL API -------------------------------------------------------------

/**
 * Translate an array of strings via DeepL Free API.
 * Returns array of translated strings in the same order.
 */
function deepLTranslate(texts, targetLang, apiKey) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({
      text: texts,
      target_lang: targetLang,
      source_lang: 'EN',
    });

    const options = {
      hostname: 'api-free.deepl.com',
      path: '/v2/translate',
      method: 'POST',
      headers: {
        'Authorization': `DeepL-Auth-Key ${apiKey}`,
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
      },
    };

    const req = https.request(options, res => {
      let data = '';
      res.on('data', chunk => { data += chunk; });
      res.on('end', () => {
        if (res.statusCode !== 200) {
          reject(new Error(`DeepL API error ${res.statusCode}: ${data}`));
          return;
        }
        try {
          const parsed = JSON.parse(data);
          resolve(parsed.translations.map(t => t.text));
        } catch (e) {
          reject(new Error(`DeepL parse error: ${e.message}`));
        }
      });
    });

    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// ---- Per-locale translation ------------------------------------------------

async function translateLocale(localeName, enLeaves, apiKey) {
  const localeFile = path.join(LOCALES_DIR, `${localeName}.json`);
  const targetLang = DEEPL_LANG[localeName];

  // Load existing locale file (or start empty)
  let localeData = {};
  if (fs.existsSync(localeFile)) {
    localeData = JSON.parse(fs.readFileSync(localeFile, 'utf8'));
  }

  // Find keys that are empty in the locale file
  const toTranslate = enLeaves.filter(({ path: p, value: enVal }) => {
    if (!enVal) return false; // skip if English value is also empty
    const existing = getByPath(localeData, p);
    return existing === '' || existing === undefined || existing === null;
  });

  if (toTranslate.length === 0) {
    console.log(`  ${localeName}: nothing to translate.`);
    return;
  }

  console.log(`  ${localeName} (${targetLang}): translating ${toTranslate.length} key(s)...`);

  // Process in batches
  for (let i = 0; i < toTranslate.length; i += BATCH_SIZE) {
    const batch = toTranslate.slice(i, i + BATCH_SIZE);
    const texts = batch.map(item => item.value);

    try {
      const translated = await deepLTranslate(texts, targetLang, apiKey);
      for (let j = 0; j < batch.length; j++) {
        setByPath(localeData, batch[j].path, translated[j]);
        console.log(`    [${localeName}] ${batch[j].path} = "${translated[j]}"`);
      }
    } catch (err) {
      console.error(`    Error translating batch for ${localeName}: ${err.message}`);
      // Continue with next batch — partial success is better than none
    }

    if (i + BATCH_SIZE < toTranslate.length) {
      await sleep(BATCH_DELAY_MS);
    }
  }

  // Write updated locale file
  fs.writeFileSync(localeFile, JSON.stringify(localeData, null, 2) + '\n', 'utf8');
  console.log(`  ${localeName}: wrote ${localeFile}`);
}

// ---- Main ------------------------------------------------------------------

async function main() {
  const apiKey = process.env.DEEPL_API_KEY;

  if (!apiKey) {
    console.warn('\nWarning: DEEPL_API_KEY not set — skipping machine translation.');
    console.warn('Set DEEPL_API_KEY to your DeepL Free API key and re-run.');
    process.exit(0);
  }

  // Load en.json as source
  const enFile = path.join(LOCALES_DIR, 'en.json');
  if (!fs.existsSync(enFile)) {
    console.error(`en.json not found at ${enFile}`);
    process.exit(1);
  }
  const enJson = JSON.parse(fs.readFileSync(enFile, 'utf8'));
  const enLeaves = collectLeaves(enJson);

  console.log(`\nSource: ${enLeaves.length} key(s) in en.json`);
  console.log(`Locales to process: ${Object.keys(DEEPL_LANG).join(', ')}\n`);

  for (const localeName of Object.keys(DEEPL_LANG)) {
    await translateLocale(localeName, enLeaves, apiKey);
  }

  console.log('\nTranslation pass complete.');
}

main().catch(err => {
  console.error('Fatal:', err.message);
  process.exit(1);
});
