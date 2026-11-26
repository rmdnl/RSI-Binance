import re

with open("dashboard.py", "r") as f:
    content = f.read()

match = re.search(r"<script>(.*?)</script>", content, re.DOTALL)
if match:
    js = match.group(1)
    with open("static/app.js", "w") as f:
        f.write(js)
    print("Extracted", len(js), "chars")
    content = re.sub(r"<script>.*?</script>", '<script src="/static/app.js"></script>', content, flags=re.DOTALL)
    with open("dashboard.py", "w") as f:
        f.write(content)
    print("Done")
else:
    print("Not found")