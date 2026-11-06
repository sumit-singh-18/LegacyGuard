#!/bin/sh
# Installs the LegacyGuard plugin into Claude Code for this user. Run it once; run it again to update.
# It copies this folder to ~/.legacyguard/package and registers it with Claude Code's own plugin installer.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
DEST="${LEGACYGUARD_HOME:-$HOME/.legacyguard}/package"

if ! command -v claude >/dev/null 2>&1; then
  echo "Claude Code was not found. Install it first (https://claude.com/claude-code), then run this file again."
  exit 1
fi
if [ ! -f "$HERE/legacyguard/.claude-plugin/plugin.json" ]; then
  echo "This file must stay in the folder it came in, next to the legacyguard folder."
  exit 1
fi

mkdir -p "$DEST"
rm -rf "$DEST/legacyguard" "$DEST/.claude-plugin"
cp -R "$HERE/legacyguard" "$HERE/.claude-plugin" "$DEST/"

claude plugin uninstall legacyguard@legacyguard >/dev/null 2>&1 || true
claude plugin marketplace remove legacyguard >/dev/null 2>&1 || true
claude plugin marketplace add "$DEST"
claude plugin install legacyguard@legacyguard

echo
echo "LegacyGuard is installed."
echo "Open a terminal in any Python project and type:  claude"
echo "The first start in a project takes about a minute while the guard sets itself up."
