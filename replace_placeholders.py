import re
import json

with open("dashboard.py", "r") as f:
    content = f.read()

match_tf = re.search(r"RSI_TF_COLORS\s*=\s*(\{.*?\})", content, re.DOTALL)
match_dash = re.search(r"RSI_PAIR_DASHES\s*=\s*(\[.*?\])", content, re.DOTALL)
match_oversold = re.search(r"OVERSOLD_FALLBACK\s*=\s*([\d.]+)", content)

tf_colors = json.loads(match_tf.group(1)) if match_tf else {"15m": "#38bdf8", "1h": "#fbbf24", "4h": "#a78bfa"}
dashes = json.loads(match_dash.group(1)) if match_dash else ["", "6 3", "2 3", "8 3 2 3"]
oversold_fb = float(match_oversold.group(1)) if match_oversold else 25.0

match = re.search(r"<script>(.*?)</script>", content, re.DOTALL)
if match:
    js = match.group(1)
    js = js.replace("__TF_COLORS__", json.dumps(tf_colors))
    js = js.replace("__DASHES__", json.dumps(dashes))
    js = js.replace("__OVERSOLD_FALLBACK__", json.dumps(oversold_fb))
    js = js.replace("&", "&")
    js = js.replace("<", "<")
    js = js.replace(">", ">")
    js = js.replace("""", "\"")
    js = js.replace("'", "'")
    with open("static/app.js", "w") as f:
        f.write(js)
    print("Created app.js with placeholders replaced")
    content = re.sub(r"<script>.*?</script>", '<script src="/static/app.js"></script>', content, flags=re.DOTALL)
    with open("dashboard.py", "w") as f:
        f.write(content)
    print("Done")
else:
    print("Not found")