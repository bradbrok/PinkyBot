#!/bin/bash
# FTP Deploy Verify
# Uso: ftp_deploy_verify.sh <file_locale> <path_ftp> <url_http>
# Esempio: ftp_deploy_verify.sh /var/www/aiena.it/index.html /index.html https://aiena.it/index.html

LOCAL_FILE="$1"
FTP_PATH="$2"
HTTP_URL="$3"
FTP_USER="aiena.it:Ugh6ooth"
FTP_HOST="ftp.aiena.it"

if [ -z "$LOCAL_FILE" ] || [ -z "$FTP_PATH" ] || [ -z "$HTTP_URL" ]; then
  echo "Usage: $0 <file_locale> <path_ftp> <url_http>"
  exit 1
fi

# 1. Deploy FTP
UPLOAD=$(curl -s -w "%{http_code}" -T "$LOCAL_FILE" "ftp://${FTP_HOST}${FTP_PATH}" --user "$FTP_USER" -o /dev/null)

if [ "$UPLOAD" != "226" ] && [ "$UPLOAD" != "000" ]; then
  echo "FTP_ERROR: upload failed (code: $UPLOAD)"
  exit 1
fi

# 2. Verifica HTTP (attendi max 10s)
sleep 2
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$HTTP_URL")

if [ "$HTTP_CODE" = "200" ]; then
  echo "OK: $HTTP_URL live ($HTTP_CODE)"
  exit 0
else
  echo "WARN: deploy ok ma HTTP $HTTP_CODE su $HTTP_URL"
  exit 2
fi
