#!/usr/bin/env bash
# Install habit-scout: CLI symlink, zsh hook, hourly LaunchAgent (does the real run once/day).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
DATA="${HABIT_SCOUT_HOME:-$HOME/.habit-scout}"
PLIST="$HOME/Library/LaunchAgents/com.habit-scout.daily.plist"

CLAUDE="${CLAUDE_BIN:-$(command -v claude || true)}"
[ -z "$CLAUDE" ] && CLAUDE="$(zsh -ic 'command -v claude' 2>/dev/null || true)"
if [ -z "$CLAUDE" ]; then
  echo "claude CLI not found; set CLAUDE_BIN=/path/to/claude and re-run" >&2
  exit 1
fi

mkdir -p "$DATA/logs" "$HOME/.local/bin" "$HOME/Library/LaunchAgents"
chmod 700 "$DATA"
chmod +x "$DIR/bin/scout"
ln -sf "$DIR/bin/scout" "$HOME/.local/bin/scout"

if ! grep -qF "# habit-scout" "$HOME/.zshrc" 2>/dev/null; then
  printf '\nsource "%s/shell/habit-scout.zsh"  # habit-scout\n' "$DIR" >> "$HOME/.zshrc"
  echo "added hook to ~/.zshrc"
fi

sed -e "s#__DIR__#$DIR#g" \
    -e "s#__DATA__#$DATA#g" \
    -e "s#__PATH__#$(dirname "$CLAUDE"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin#g" \
    "$DIR/launchd/com.habit-scout.daily.plist" > "$PLIST"
launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "installed. claude: $CLAUDE"
echo "daily run after 18:00 (config: $DATA/config.json -> run_after_hour). logs: $DATA/logs/scout.log"
echo "open a new terminal, then try: scout candidates"
