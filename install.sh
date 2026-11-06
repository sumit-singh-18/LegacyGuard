#!/bin/sh
# Installs the LegacyGuard plugin into Claude Code for this user, for people who downloaded the files as a zip.
# With GitHub access the two lines in README.md do the same without this file. Run it once; run it again to update.
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

# The marketplace is named after the team; the plugin keeps its name. Copies installed under the old
# marketplace name (legacyguard) are removed first, so an update from an older zip ends with one install.
for old in legacyguard turnback-lab; do
  claude plugin uninstall "legacyguard@$old" >/dev/null 2>&1 || true
  claude plugin marketplace remove "$old" >/dev/null 2>&1 || true
done
claude plugin marketplace add "$DEST"
claude plugin install legacyguard@turnback-lab

echo
echo "LegacyGuard is installed."
echo "Open a terminal in any Python project and type:  claude"
echo "The first start in a project takes about a minute while the guard sets itself up."
