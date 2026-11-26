with open("dashboard.py", "r") as f:
    content = f.read()

# 1. Add Path import after existing imports
old_imports = "from __future__ import annotations\nimport json, math, os, time\nfrom typing import Any, TypeGuard\nfrom http.server import BaseHTTPRequestHandler, ThreadingHTTPServer"
new_imports = """from __future__ import annotations
import json, math, os, time
from pathlib import Path
from typing import Any, TypeGuard
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer"""
content = content.replace(old_imports, new_imports)

# 2. Add STATIC_DIR after STATE_FILE
old_static = 'HOST=os.getenv("DASHBOARD_HOST","127.0.0.1");PORT=int(os.getenv("DASHBOARD_PORT","8060"));STATE_FILE=os.getenv("STATE_FILE","state.json")'
new_static = 'HOST=os.getenv("DASHBOARD_HOST","127.0.0.1");PORT=int(os.getenv("DASHBOARD_PORT","8060"));STATE_FILE=os.getenv("STATE_FILE","state.json")\nSTATIC_DIR = Path(__file__).parent / "static"'
content = content.replace(old_static, new_static)

# 3. Update CDN URLs to local static paths
old_cdn = '<script src="https://unpkg.com/react@18/umd/react.production.min.js"></script><script src="https://unpkg.com/react-dom@18/umd/react-dom.production.min.js"></script><script src="https://unpkg.com/prop-types@15.8.1/prop-types.min.js"></script><script src="https://unpkg.com/recharts@2.15.4/umd/Recharts.js"></script>'
new_cdn = '<script src="/static/react.min.js"></script><script src="/static/react-dom.min.js"></script><script src="/static/prop-types.min.js"></script><script src="/static/recharts.min.js"></script>'
content = content.replace(old_cdn, new_cdn)

# 4. Replace Handler do_GET method
old_handler = """class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  if self.path=='/':body=PAGE.encode('utf-8');status=200;ctype='text/html; charset=utf-8'
  elif self.path=='/api/status':
   try:
    with open(STATE_FILE,encoding='utf-8') as f:state=json.load(f)
    body=json.dumps(status_payload(state),allow_nan=False).encode();status=200
   except FileNotFoundError:body=b'{"error":"state file not found"}';status=404
   except (OSError,ValueError,TypeError):body=b'{"error":"state file unreadable"}';status=503
   ctype='application/json; charset=utf-8'
  else:body=b'not found';status=404;ctype='text/plain'
  self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)"""

new_handler = """class Handler(BaseHTTPRequestHandler):
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
  self.wfile.write(body)"""

content = content.replace(old_handler, new_handler)

with open("dashboard.py", "w") as f:
    f.write(content)

print("Updated")