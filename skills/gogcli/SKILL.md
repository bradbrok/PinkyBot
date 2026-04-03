---
name: gogcli
description: Google Workspace access via gog CLI — Gmail, Calendar, Drive, Docs, Sheets, Slides, Contacts, Tasks. Use when the owner asks to read/send email, check calendar, manage Drive files, or interact with any Google Workspace service. Requires one-time OAuth setup per account.
---

# gogcli — Google Workspace CLI

## Installation

Already installed at `/opt/homebrew/bin/gog` (v0.12.0). Command is `gog`, not `gogcli`.

## Authentication Setup (one-time, per account)

```bash
# 1. Add OAuth credentials (download client_secret JSON from Google Cloud Console)
gog auth credentials add ~/Downloads/client_secret_XXXX.json

# 2. Authorize an account (opens browser)
gog auth add brad@example.com

# 3. Verify
gog auth list
gog auth status
```

Set a default account to avoid passing `--account` every time:
```bash
export GOG_ACCOUNT=brad@example.com
# Or in .env / shell profile
```

Multi-account: pass `--account user@example.com` per command, or use `gog auth alias` to set named shortcuts.

---

## Global Flags (use on any command)

| Flag | Description |
|---|---|
| `-j, --json` | JSON output — always use for scripting |
| `-p, --plain` | TSV output, no colors — stable for parsing |
| `--results-only` | In JSON mode, emit only the result (drops envelope) |
| `--select=field` | In JSON mode, extract specific fields (dot paths) |
| `-a, --account=EMAIL` | Override account for this command |
| `-n, --dry-run` | Preview without making changes |
| `-y, --force` | Skip confirmations |
| `--no-input` | Never prompt (CI-safe) |

**Always use `--json` or `--plain` when scripting.** Never parse color output.

---

## Gmail

### Search / Read

```bash
# Search emails (Gmail query syntax)
gog gmail search 'is:unread newer_than:7d' --json
gog gmail search 'from:boss@company.com subject:urgent' --max 10 --json
gog gmail search 'label:inbox is:unread' -j --results-only

# Get a specific message
gog gmail get <messageId> --json
gog gmail get <messageId> --format metadata --json   # headers only, no body

# List labels
gog gmail labels list --json
```

### Send

```bash
# Send plain text
gog gmail send --to recipient@example.com --subject "Subject" --body "Body text" -y

# Send HTML
gog gmail send --to recipient@example.com --subject "Subject" --body-html "<h1>Hello</h1>" -y

# Reply to a thread
gog gmail send --thread-id <threadId> --body "Reply text" --reply-all -y

# With attachment
gog gmail send --to user@example.com --subject "File" --body "See attached" --attach ~/file.pdf -y

# CC/BCC
gog gmail send --to primary@example.com --cc cc@example.com --subject "Hello" --body "Hi" -y
```

### Organize

```bash
# Apply/remove labels
gog gmail thread modify <threadId> --add-label STARRED --remove-label UNREAD

# Mark as read
gog gmail thread modify <threadId> --remove-label UNREAD

# Trash
gog gmail thread trash <threadId>
```

---

## Calendar

```bash
# List calendars
gog calendar calendars --json

# List upcoming events (primary calendar)
gog calendar events --json
gog calendar events --max 10 --json
gog calendar events <calendarId> --max 5 --json

# Search events
gog calendar events --query "standup" --json

# Create an event
gog calendar create primary \
  --title "Team Sync" \
  --start "2026-04-03T10:00:00" \
  --end "2026-04-03T11:00:00" \
  --attendees colleague@example.com \
  --description "Weekly sync" -y --json

# All-day event
gog calendar create primary --title "Holiday" --start "2026-04-07" --all-day -y

# Delete event
gog calendar delete primary <eventId> -y
```

---

## Drive

```bash
# List files in root
gog drive ls --json

# List files in a folder
gog drive ls --folder <folderId> --json

# Search Drive
gog drive search "quarterly report" --json
gog drive search "mimeType='application/pdf'" --json

# Download a file (auto-exports Google Docs → docx, Sheets → xlsx)
gog drive download <fileId> --output ~/Downloads/

# Upload a file
gog drive upload ~/report.pdf --parent <folderId> --json

# Upload and convert to Google Doc/Sheet
gog drive upload ~/data.csv --convert-to sheet --json

# Replace file content (preserves share link)
gog drive upload ~/new_report.pdf --replace <existingFileId> --json

# Create folder
gog drive mkdir "My Reports" --parent <parentFolderId> --json
```

---

## Docs

```bash
# Read a Google Doc
gog docs read <docId> --json          # structured JSON
gog docs read <docId> --plain         # plain text

# Export as file
gog docs download <docId> --format docx --output ~/doc.docx
gog docs download <docId> --format pdf --output ~/doc.pdf
```

---

## Sheets

```bash
# Read a spreadsheet
gog sheets read <sheetId> --json
gog sheets read <sheetId> --sheet "Sheet2" --json

# Append a row
gog sheets append <sheetId> --data "Value1,Value2,Value3" --sheet "Sheet1"

# Write to a specific range
gog sheets write <sheetId> --range "A1:C1" --data "Col1,Col2,Col3"
```

---

## Contacts

```bash
gog contacts list --json
gog contacts search "Brad" --json
gog contacts get <contactId> --json
```

---

## Tasks

```bash
# List task lists
gog tasks lists --json

# List tasks
gog tasks list <taskListId> --json
gog tasks list <taskListId> --show-completed --json

# Create a task
gog tasks create <taskListId> --title "Review report" --due "2026-04-05" --json

# Complete a task
gog tasks complete <taskListId> <taskId> -y
```

---

## Agent Best Practices

### Always use `--json` for scripting
```bash
# Good
gog gmail search 'is:unread' --json | python3 -m json.tool

# Extract just email subjects
gog gmail search 'is:unread' --json --select "messages.snippet"
```

### Use `--dry-run` before destructive actions
```bash
gog calendar delete primary <eventId> --dry-run   # Preview before deleting
gog drive upload file.pdf --replace <id> --dry-run
```

### Use `--no-input` in automated contexts
```bash
gog gmail send --to user@example.com --subject "Auto" --body "Hi" -y --no-input
```

### Check auth before API calls
```bash
gog auth status  # Quick health check
gog auth list    # List configured accounts
```

### Error handling pattern
```bash
result=$(gog gmail send ... 2>&1)
if echo "$result" | grep -q "error"; then
  echo "Send failed: $result"
else
  echo "Sent successfully"
fi
```

---

## Boundaries

**Requires approval before:**
- Sending emails to external recipients
- Deleting files from Drive
- Deleting calendar events
- Sharing Drive files/folders

**Autonomous (no approval needed):**
- Reading email, calendar, Drive, Docs, Sheets
- Creating calendar events on the owner's calendar
- Uploading files to Drive (owner's account)
- Appending to Sheets the owner uses for tracking
- Creating draft emails (if supported)

---

## Auth Setup Checklist

If auth isn't set up yet, tell the owner:
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create/select a project
3. Enable APIs: Gmail, Calendar, Drive, Docs, Sheets APIs
4. Create OAuth 2.0 credentials → Desktop app → Download JSON
5. Run `gog auth credentials add ~/Downloads/client_secret_*.json`
6. Run `gog auth add your@gmail.com`
7. Complete the browser OAuth flow
8. Test: `gog calendar calendars --json`
