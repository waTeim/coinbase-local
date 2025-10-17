#!/usr/bin/env bash
set -euo pipefail

# --- Ensure `python` exists (alias to python3 if missing) ---
if ! command -v python >/dev/null 2>&1; then
  sudo update-alternatives --install /usr/bin/python python /usr/bin/python3 1
fi

# --- npm/npx: fix cache perms and sane user paths ---
mkdir -p "$HOME/.npm" "$HOME/.cache" "$HOME/.config" "$HOME/.npm-global/bin"
sudo chown -R "$(id -u)":"$(id -g)" "$HOME/.npm" "$HOME/.cache" "$HOME/.config" "$HOME/.npm-global"

# Use user-owned cache & global prefix to avoid EACCES
npm config set cache "$HOME/.npm" --location=global
npm config set prefix "$HOME/.npm-global" --location=global

# Ensure global npm bin is on PATH for future shells
if ! grep -q 'npm-global/bin' "$HOME/.bashrc" 2>/dev/null; then
  echo 'export PATH="$HOME/.npm-global/bin:$PATH"' >> "$HOME/.bashrc"
fi

pip install -r requirements.txt

# --- Optional: sanity checks (won't fail the build if one is missing) ---
echo "== Tool versions =="
( python --version || true )
( go version || true )
( node -v || true )
( npm -v || true )
( npx -v || true )

echo "Setup complete."
