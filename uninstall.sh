#!/usr/bin/env bash
# Remove the LaunchAgent, zsh hook and CLI symlink. Data in ~/.habit-scout is kept unless --purge.
set -euo pipefail
PLIST="$HOME/Library/LaunchAgents/com.habit-scout.daily.plist"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
rm -f "$PLIST" "$HOME/.local/bin/scout"
[ -f "$HOME/.zshrc" ] && sed -i '' '/# habit-scout$/d' "$HOME/.zshrc"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "${HABIT_SCOUT_HOME:-$HOME/.habit-scout}"
  echo "removed data"
fi
echo "uninstalled (accepted scripts in ~/.local/bin and ~/.claude/commands were left in place)"
