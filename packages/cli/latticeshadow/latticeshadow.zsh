# Optional Zsh widgets. Loading this file leaves existing keys and completion alone.

function latticeshadow-ghost-paste() {
    (( $+commands[shadow] )) || return
    local raw_content ghost_text
    raw_content=$(shadow get-ghost-paste 2>/dev/null)
    [[ -n "$raw_content" ]] || return
    if [[ "$raw_content" == "{"* ]]; then
        ghost_text=$(python3 -c "import json, sys; print(json.loads(sys.argv[1]).get('text', ''))" "$raw_content" 2>/dev/null)
    else
        ghost_text="$raw_content"
    fi
    [[ -n "$ghost_text" ]] || return
    BUFFER="${BUFFER:0:$CURSOR}${ghost_text}${BUFFER:$CURSOR}"
    CURSOR=$((CURSOR + $#ghost_text))
    zle redisplay
}
zle -N latticeshadow-ghost-paste

function latticeshadow-loop-fix() {
    (( $+commands[shadow] )) || return
    local suggestion
    suggestion=$(shadow get-loop-fix 2>/dev/null)
    [[ -n "$suggestion" ]] || return
    BUFFER="${BUFFER:0:$CURSOR}${suggestion}${BUFFER:$CURSOR}"
    CURSOR=$((CURSOR + $#suggestion))
    zle redisplay
}
zle -N latticeshadow-loop-fix
