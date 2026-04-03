#!/usr/bin/env bash
set -e

# PinkyBot Installer
# Usage: curl -fsSL https://pinkybot.ai/install.sh | bash

REPO="https://github.com/bradbrok/PinkyBot.git"
INSTALL_DIR="$HOME/.pinkybot"
BOLD="\033[1m"
DIM="\033[2m"
YELLOW="\033[33m"
GREEN="\033[32m"
RED="\033[31m"
RESET="\033[0m"

banner() {
  echo ""
  echo -e "${YELLOW}${BOLD}  ██████╗ ██╗███╗   ██╗██╗  ██╗██╗   ██╗${RESET}"
  echo -e "${YELLOW}${BOLD}  ██╔══██╗██║████╗  ██║██║ ██╔╝╚██╗ ██╔╝${RESET}"
  echo -e "${YELLOW}${BOLD}  ██████╔╝██║██╔██╗ ██║█████╔╝  ╚████╔╝ ${RESET}"
  echo -e "${YELLOW}${BOLD}  ██╔═══╝ ██║██║╚██╗██║██╔═██╗   ╚██╔╝  ${RESET}"
  echo -e "${YELLOW}${BOLD}  ██║     ██║██║ ╚████║██║  ██╗   ██║   ${RESET}"
  echo -e "${YELLOW}${BOLD}  ╚═╝     ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝   ╚═╝   ${RESET}"
  echo ""
  echo -e "  ${DIM}Your personal AI sidekick — pinkybot.ai${RESET}"
  echo ""
}

step() { echo -e "${YELLOW}▸${RESET} $1"; }
ok()   { echo -e "${GREEN}✓${RESET} $1"; }
err()  { echo -e "${RED}✗${RESET} $1"; exit 1; }

banner

# ── 1. Check Python ────────────────────────────────────────────────────────────
step "Checking Python 3.11+..."
PYTHON=""
for cmd in python3 python; do
  if command -v "$cmd" &>/dev/null; then
    VER=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null)
    MAJOR=$(echo "$VER" | cut -d. -f1)
    MINOR=$(echo "$VER" | cut -d. -f2)
    if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 11 ]; then
      PYTHON="$cmd"
      ok "Found Python $VER ($cmd)"
      break
    fi
  fi
done
[ -z "$PYTHON" ] && err "Python 3.11+ required. Install from https://python.org"

# ── 2. Check Git ───────────────────────────────────────────────────────────────
step "Checking git..."
command -v git &>/dev/null || err "git required. Install from https://git-scm.com"
ok "Found $(git --version)"

# ── 3. Check Claude Code ───────────────────────────────────────────────────────
step "Checking Claude Code..."
if ! command -v claude &>/dev/null; then
  echo ""
  echo -e "  ${DIM}Claude Code not found. Installing...${RESET}"
  curl -fsSL https://claude.ai/install.sh | bash
  # Re-source PATH in case it was just installed
  export PATH="$HOME/.claude/bin:$PATH"
  command -v claude &>/dev/null || err "Claude Code install failed. Try: curl -fsSL https://claude.ai/install.sh | bash"
fi
ok "Found Claude Code"

# ── 4. Check Claude auth ───────────────────────────────────────────────────────
step "Checking Claude authentication..."
if ! claude auth status 2>/dev/null | grep -q '"loggedIn":true\|loggedIn.*true\|authenticated'; then
  echo ""
  echo -e "  ${DIM}Not logged in to Claude. Running: claude login${RESET}"
  echo ""
  claude login
fi
ok "Claude authenticated"

# ── 5. Clone or update PinkyBot ────────────────────────────────────────────────
step "Installing PinkyBot to $INSTALL_DIR..."
if [ -d "$INSTALL_DIR/.git" ]; then
  echo -e "  ${DIM}Existing install found — updating...${RESET}"
  git -C "$INSTALL_DIR" pull --ff-only origin main
else
  git clone "$REPO" "$INSTALL_DIR"
fi
ok "PinkyBot source ready"

# ── 6. Create venv + install ───────────────────────────────────────────────────
step "Setting up Python environment..."
cd "$INSTALL_DIR"
if [ ! -d ".venv" ]; then
  "$PYTHON" -m venv .venv
fi
source .venv/bin/activate
pip install --quiet --upgrade pip
pip install --quiet -e ".[all]"
ok "Dependencies installed"

# ── 7. Create wrapper script ───────────────────────────────────────────────────
step "Installing pinky command..."
WRAPPER="$HOME/.local/bin/pinky"
mkdir -p "$HOME/.local/bin"
cat > "$WRAPPER" <<SCRIPT
#!/usr/bin/env bash
source "$INSTALL_DIR/.venv/bin/activate"
exec python -m pinky_daemon "\$@"
SCRIPT
chmod +x "$WRAPPER"

# Add to PATH if not already
if [[ ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
  SHELL_RC="$HOME/.bashrc"
  [[ -n "$ZSH_VERSION" || "$SHELL" == *zsh* ]] && SHELL_RC="$HOME/.zshrc"
  echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
  export PATH="$HOME/.local/bin:$PATH"
fi
ok "pinky command installed"

# ── 8. Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}  PinkyBot installed successfully!${RESET}"
echo ""
echo -e "  Start the server:"
echo -e "  ${YELLOW}pinky --mode api --port 8888${RESET}"
echo ""
echo -e "  Then open: ${YELLOW}http://localhost:8888${RESET}"
echo ""
echo -e "  ${DIM}Docs: https://pinkybot.ai/docs${RESET}"
echo ""
