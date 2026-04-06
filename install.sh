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
if claude auth status 2>/dev/null | grep -q '"loggedIn":true\|loggedIn.*true\|authenticated'; then
  ok "Claude authenticated"
else
  echo ""
  echo -e "  ${YELLOW}Not logged in to Claude.${RESET}"
  echo -e "  Run ${YELLOW}claude login${RESET} after installation, or set ${YELLOW}ANTHROPIC_API_KEY${RESET} in your environment."
  echo ""
fi

# ── 5. Clone or update PinkyBot ────────────────────────────────────────────────
step "Installing PinkyBot to $INSTALL_DIR..."

TAG=$(curl -s https://api.github.com/repos/bradbrok/PinkyBot/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
if [ -z "$TAG" ]; then
  echo -e "  ${YELLOW}Warning: no releases found — falling back to main branch.${RESET}"
  TAG="main"
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  echo -e "  ${DIM}Existing install found — updating to $TAG...${RESET}"
  git -C "$INSTALL_DIR" fetch --depth 1 origin --tags
  git -C "$INSTALL_DIR" checkout "$TAG" 2>/dev/null || git -C "$INSTALL_DIR" checkout "tags/$TAG"
else
  git clone --branch "$TAG" --depth 1 "$REPO" "$INSTALL_DIR"
fi
ok "PinkyBot $TAG ready"

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

# ── 8. Set PINKY_SESSION_SECRET if not already set ────────────────────────────
SHELL_RC="$HOME/.bashrc"
[[ -n "$ZSH_VERSION" || "$SHELL" == *zsh* ]] && SHELL_RC="$HOME/.zshrc"

if [ -z "$PINKY_SESSION_SECRET" ] && ! grep -q "PINKY_SESSION_SECRET" "$SHELL_RC" 2>/dev/null; then
  PINKY_SESSION_SECRET_VAL=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || openssl rand -hex 32 2>/dev/null || head -c 32 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 32)
  echo "export PINKY_SESSION_SECRET=\"$PINKY_SESSION_SECRET_VAL\"" >> "$SHELL_RC"
  export PINKY_SESSION_SECRET="$PINKY_SESSION_SECRET_VAL"
  ok "Generated PINKY_SESSION_SECRET and saved to $SHELL_RC"
else
  ok "PINKY_SESSION_SECRET already configured"
fi

# ── 9. Done ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}  PinkyBot installed successfully!${RESET}"
echo ""
echo -e "  ${BOLD}1. Start the server:${RESET}"
echo -e "     ${YELLOW}pinky --mode api --port 8888${RESET}"
echo ""
echo -e "  ${BOLD}2. Open the dashboard:${RESET}"
echo -e "     ${YELLOW}http://localhost:8888${RESET}"
echo ""
echo -e "  ${BOLD}3. Complete onboarding:${RESET}"
echo -e "     Set a password, create your agent, and connect Telegram."
echo ""
if ! claude auth status 2>/dev/null | grep -q '"loggedIn":true\|loggedIn.*true\|authenticated'; then
  echo -e "  ${YELLOW}⚠  Claude not authenticated.${RESET} Run one of:"
  echo -e "     ${YELLOW}claude login${RESET}   (Claude Max/Pro subscription)"
  echo -e "     ${YELLOW}export ANTHROPIC_API_KEY=sk-ant-...${RESET}   (API key)"
  echo ""
fi
echo -e "  ${DIM}Docs & help: https://pinkybot.ai${RESET}"
echo ""
