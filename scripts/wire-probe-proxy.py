import http.server, json, os, urllib.request, time
UP = "https://api.anthropic.com"
LOG = os.environ.get("WIRE_PROBE_LOG", "wire-probe.log")
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_POST(self): self.relay()
    def do_GET(self): self.relay()
    def relay(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n) if n else None
        row = {"t": time.strftime("%H:%M:%S"), "path": self.path}
        try:
            j = json.loads(body) if body else {}
            row.update({"model": j.get("model"), "output_config": j.get("output_config"), "thinking": j.get("thinking"), "messages": len(j.get("messages") or []), "sys_effort": [m.get("output_config") for m in (j.get("messages") or []) if m.get("role") == "system"]})
        except Exception: pass
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length", "accept-encoding")}
        req = urllib.request.Request(UP + self.path, data=body, headers=hdrs, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                data = r.read(); status = r.status; rh = r.headers
        except urllib.error.HTTPError as e:
            data = e.read(); status = e.code; rh = e.headers
        row["status"] = status
        if b'"usage"' in data:
            import re
            m = re.search(rb'"usage":\{[^}]*\}', data)
            if m: row["usage"] = m.group(0).decode()[:300]
        with open(LOG, "a") as f: f.write(json.dumps(row) + "\n")
        self.send_response(status)
        for k, v in rh.items():
            if k.lower() in ("content-length", "transfer-encoding", "content-encoding", "connection"): continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
http.server.ThreadingHTTPServer(("127.0.0.1", 18791), H).serve_forever()
