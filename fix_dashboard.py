import re

with open("dashboard.py", "r") as f:
    content = f.read()

# Find the inline script and extract it
match = re.search(r"<script>(.*?)</script>", content, re.DOTALL)
if match:
    js_code = match.group(1)
    # Fix HTML entities
    js_code = js_code.replace("&", "&")
    js_code = js_code.replace("<", "<")
    js_code = js_code.replace(">", ">")
    js_code = js_code.replace(""", '"')
    js_code = js_code.replace("'", "'")
    with open("static/app.js", "w") as f:
        f.write(js_code)
    print("Extracted", len(js_code), "chars")

    # Replace the script tag
    old_pattern = r"<script>.*?</script>"
    new_tag = '<script src="/static/app.js"></script>'
    content = re.sub(old_pattern, new_tag, content, flags=re.DOTALL)
    with open("dashboard.py", "w") as f:
        f.write(content)
    print("Updated")
else:
    print("Not found")