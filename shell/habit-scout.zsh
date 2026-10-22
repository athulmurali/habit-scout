# habit-scout shell integration (sourced from ~/.zshrc by install.sh)
#  - logs each command with its cwd, exit code and terminal (vscode / iTerm / ...) to ~/.habit-scout/shell.log
#  - loads aliases/functions you accepted via `scout accept`
#  - puts ~/.local/bin (where `scout` and accepted scripts live) on PATH
[[ -o interactive ]] || return 0

typeset -g _HS_DIR="${HABIT_SCOUT_HOME:-$HOME/.habit-scout}"
[[ -d $_HS_DIR ]] || mkdir -p "$_HS_DIR"
typeset -U path
path=("$HOME/.local/bin" $path)
[[ -f $_HS_DIR/aliases.zsh ]] && source "$_HS_DIR/aliases.zsh"

zmodload -F zsh/datetime p:EPOCHSECONDS

_hs_preexec() { _hs_cmd=$1; _hs_ts=$EPOCHSECONDS; _hs_pwd=$PWD; }
_hs_precmd() {
  local ec=$?
  [[ -n $_hs_cmd ]] || return 0
  local c=${_hs_cmd//$'\n'/\\n}
  print -r -- "$_hs_ts	$ec	$_hs_pwd	${TERM_PROGRAM:-}	${c//$'\t'/ }" >> "$_HS_DIR/shell.log"
  _hs_cmd=
}

autoload -Uz add-zsh-hook
add-zsh-hook preexec _hs_preexec
# run first so no other precmd hook clobbers $?
precmd_functions=(_hs_precmd ${precmd_functions:#_hs_precmd})
