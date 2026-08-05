#!/bin/bash
# i18n Audit — Scansiona index.html per elementi senza data-i18n
# Notifica Mirko su Telegram se trova problemi.
# Uso: bash i18n_audit.sh

SITE_DIR="/var/www/bitcoinmarket.net"
CHAT_ID="32405655"

ISSUES=$(python3 << 'PYEOF'
import re

with open('/var/www/bitcoinmarket.net/index.html', 'r', encoding='utf-8') as f:
    content = f.read()

italian_indicators = ['il ', 'la ', 'le ', 'di ', 'del ', 'per ', 'con ', 'che ', 'una ', 'uno ', 'sono ', 'nei ', 'agli ']
missing = []

for m in re.finditer(r'<(h[1-6]|p|span|a|li|td|th|button|label)([^>]*)>([^<]{10,})</', content):
    attrs = m.group(2)
    text = m.group(3).strip()
    if 'data-i18n' in attrs:
        continue
    if not text or text.startswith('http') or text.startswith('$') or text.startswith('{') or text.isdigit():
        continue
    if any(ind in text.lower() for ind in italian_indicators):
        missing.append(text[:70])

for item in missing[:15]:
    print(item)
PYEOF
)

if [ -z "$ISSUES" ]; then
    exit 0  # Tutto OK — nessuna notifica
fi

COUNT=$(echo "$ISSUES" | wc -l)

# Notifica via API Pinky
curl -s -X POST http://localhost:8888/messages/outbound \
  -H "Content-Type: application/json" \
  -d "{
    \"chat_id\": \"$CHAT_ID\",
    \"platform\": \"telegram\",
    \"text\": \"⚠️ i18n audit: trovati $COUNT elementi senza data-i18n su index.html\\n\\n$(echo "$ISSUES" | head -10 | sed 's/$/\\n/' | tr -d '\n')\\nLi fixo adesso.\"
  }" 2>/dev/null

# Auto-fix: lancia fix se ci sono problemi
# (opzionale — per ora solo notifica)
exit 0
