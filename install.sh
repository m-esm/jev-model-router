#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
MODE="${1:-install}"
python3 - "$HERE/jev_router.py" "$SETTINGS" "$MODE" <<'PY'
import json, sys
from pathlib import Path
script, settings, mode = sys.argv[1:4]
p = Path(settings)
cfg = json.loads(p.read_text()) if p.exists() else {}
hooks = cfg.setdefault("hooks", {})
wanted = {
    "UserPromptSubmit": ("prompt", 8),
    "SessionStart": ("session", 5),
    "PostModelSwitch": ("switched", 5),
}
def ours(entry):
    return any("jev_router.py" in h.get("command", "") for h in entry.get("hooks", []))
for event, (sub, timeout) in wanted.items():
    entries = [e for e in hooks.get(event, []) if not ours(e)]
    if mode == "install":
        entries.append({"hooks": [{"type": "command", "command": f'python3 "{script}" {sub}', "timeout": timeout}]})
    if entries:
        hooks[event] = entries
    else:
        hooks.pop(event, None)
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(cfg, indent=2) + "\n")
print(f"{mode}: {p}")
PY
