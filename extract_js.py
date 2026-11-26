import re

with open("dashboard.py", "r") as f:
    content = f.read()

# Extract the inline script between <script>...</script>
match = re.search(r"<script>(.*?)</script>", content, re.DOTALL)
if match:
    js_code = match.group(1)
    # Remove HTML entities
    js_code = js_code.replace("&", "&").replace("<", "<").replace(">", ">").replace(""", '"').replace("'", "'")
    with open("static/app.js", "w") as f:
        f.write(js_code)
    print("Extracted", len(js_code), "chars to static/app.js")
    # Replace script tag in PAGE
    content = re.sub(r"<script>.*?</script>", '<script src="/static/app.js"></script>', content, flags=re.DOTALL)
    with open("dashboard.py", "w") as f:
        f.write(content)
    print("Updated dashboard.py")
else:
    print("No script found")