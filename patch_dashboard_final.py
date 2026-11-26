with open("dashboard.py", "r") as f:
    lines = f.readlines()

# 1. Add Path import after line with http.server import
for i, line in enumerate(lines):
    if line.strip() == "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer":
        lines.insert(i, "from pathlib import Path\n")
        break

# 2. Add STATIC_DIR after STATE_FILE line
for i, line in enumerate(lines):
    if "STATE_FILE=os.getenv" in line:
        lines.insert(i + 1, 'STATIC_DIR = Path(__file__).parent / "static"\n')
        break

# 3. Replace CDN URLs
for i, line in enumerate(lines):
    if "unpkg.com/react@18" in line:
        lines[i] = line.replace(
            'https://unpkg.com/react@18/umd/react.production.min.js',
            '/static/react.min.js'
        ).replace(
            'https://unpkg.com/react-dom@18/umd/react-dom.production.min.js',
            '/static/react-dom.min.js'
        ).replace(
            'https://unpkg.com/prop-types@15.8.1/prop-types.min.js',
            '/static/prop-types.min.js'
        ).replace(
            'https://unpkg.com/recharts@2.15.4/umd/Recharts.js',
            '/static/recharts.min.js'
        )
        break

# 4. Find Handler class and replace its do_GET method
handler_start = None
for i, line in enumerate(lines):
    if line.strip().startswith("class Handler"):
        handler_start = i
        break

if handler_start is not None:
    method_end = None
    for i in range(handler_start + 1, len(lines)):
        if lines[i] and not lines[i].startswith(" ") and not lines[i].startswith("\t") and not lines[i].startswith(" def "):
            method_end = i
            break
    if method_end is None:
        method_end = len(lines)

    new_handler = '''class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  # Serve static files
  if self.path.startswith("/static/"):
    file_path = STATIC_DIR / self.path[len("/static/"):]
    try:
      with open(file_path, "rb") as f:
        body = f.read()
      status = 200
      if file_path.suffix == ".js":
        ctype = "application/javascript; charset=utf-8"
      elif file_path.suffix == ".css":
        ctype = "text/css; charset=utf-8"
      else:
        ctype = "application/octet-stream"
    except (FileNotFoundError, OSError):
      body = b"not found"; status = 404; ctype = "text/plain"
  elif self.path == "/":
    body = PAGE.encode("utf-8"); status = 200; ctype = "text/html; charset=utf-8"
  elif self.path == "/api/status":
    try:
      with open(STATE_FILE, encoding="utf-8") as f:
        state = json.load(f)
      body = json.dumps(status_payload(state), allow_nan=False).encode()
      status = 200
    except FileNotFoundError:
      body = b'{"error":"state file not found"}'; status = 404
    except (OSError, ValueError, TypeError):
      body = b'{"error":"state file unreadable"}'; status = 503
    ctype = "application/json; charset=utf-8"
  else:
    body = b"not found"; status = 404; ctype = "text/plain"
  self.send_response(status)
  self.send_header("Content-Type", ctype)
  self.send_header("Cache-Control", "no-store")
  self.send_header("X-Content-Type-Options", "nosniff")
  self.send_header("Content-Length", str(len(body)))
  self.end_headers()
  self.wfile.write(body)
 def log_message(self,*args):pass
'''
    lines = lines[:handler_start] + [new_handler + "\n"] + lines[method_end:]

with open("dashboard.py", "w") as f:
    f.writelines(lines)

print("Patched")