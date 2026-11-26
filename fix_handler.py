with open("dashboard.py", "r") as f:
    lines = f.readlines()

# Find the Handler class and replace its do_GET method
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

# Find class Handler line and replace from there to end of file
start = None
for i, line in enumerate(lines):
    if line.strip().startswith("class Handler"):
        start = i
        break

if start is not None:
    # Find the end of the file (or next class/function at module level)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i] and not lines[i].startswith(" ") and not lines[i].startswith("\t") and not lines[i].startswith(" def "):
            end = i
            break
    lines = lines[:start] + [new_handler + "\n"]
    with open("dashboard.py", "w") as f:
        f.writelines(lines)
    print("Handler updated")
else:
    print("Handler not found")