import json, os, subprocess, sys
usage = "measure.py <name> <ladder-json> <forced-levels> <prompt> [prompt ...]"
if len(sys.argv) < 5:
    print(usage); sys.exit(2)
repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
work = os.environ.get("MEASURE_DIR", os.path.join(os.getcwd(), "measure-out"))
os.makedirs(work, exist_ok=True)
name, ladder, force = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3]
cfg = os.path.join(work, f"{name}.router.json")
json.dump({"ladder": ladder}, open(cfg, "w"))
env = dict(os.environ); env.pop("CLAUDECODE", None)
env.update({"JEV_ROUTER_CONFIG": cfg, "JEV_ROUTER_FORCE": force, "JEV_ROUTER_STATE": os.path.join(work, "state-" + name)})
argv = ["claude", "-p", "--plugin-dir", repo, "--model", os.environ.get("MEASURE_MODEL", "opus"), "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", "--permission-mode", "bypassPermissions"]
p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=work, env=env)
def send(text):
    p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"); p.stdin.flush()
    first = True
    while True:
        line = p.stdout.readline()
        if not line:
            print("session ended early:", p.stderr.read()[-800:]); sys.exit(1)
        ev = json.loads(line)
        if ev.get("type") == "assistant" and first:
            u = ev["message"].get("usage", {}); first = False
            print(f"  model={ev['message'].get('model')} cache_create={u.get('cache_creation_input_tokens')} cache_read={u.get('cache_read_input_tokens')}")
        if ev.get("type") == "result":
            print("  result:", ev.get("subtype"), "|", (ev.get("result") or "")[:60].replace("\n", " ")); return
for i, t in enumerate(sys.argv[4:]):
    print(f"turn {i + 1}: {t[:60]}"); send(t)
p.stdin.close(); p.wait(timeout=30)
log = os.path.join(work, "state-" + name, "log.jsonl")
print(open(log).read() if os.path.exists(log) else "no plugin log")
