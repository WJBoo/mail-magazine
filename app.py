import os
import re
import base64
import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Tuple

import requests
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape


# ----------------------------
# Parsing
# ----------------------------

HEADER_RE = re.compile(r"^◆(?P<title>.+)$")
# Example header: 男子シングルス本戦1R / 女子ダブルス予選F
SECTION_RE = re.compile(
    r"^(?P<category>男子シングルス|男子ダブルス|女子シングルス|女子ダブルス)"
    r"(?P<stage>予選|本戦)"
    r"(?P<round>.*)$"
)

# Match score line (very permissive):
# 4-6/6(4)-7 杉本一樹(明治大学)
SCORELINE_RE = re.compile(
    r"^(?P<score>.+?)\s+(?P<opp>.+?)(?:\((?P<aff>.+)\))?$"
)

def normalize_round(raw: str) -> str:
    s = raw.strip()
    # Optional normalization: "1R" -> "1R", "F" -> "F", "SF" -> "SF"
    return s if s else ""

def parse_results(text: str) -> List[Dict[str, Any]]:
    """
    Returns a list of sections:
    [
      {
        "category": "...",
        "stage": "...",
        "round": "...",
        "entries": [
           {"name": "...", "score": "...", "opponent": "...", "opponent_affiliation": "..."}
        ]
      }, ...
    ]
    """
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n")]
    # Keep blank lines as separators but remove leading/trailing blanks
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()

    sections: List[Dict[str, Any]] = []
    i = 0
    current: Optional[Dict[str, Any]] = None

    def start_section(title_line: str) -> Dict[str, Any]:
        m = SECTION_RE.match(title_line)
        if not m:
            # Fallback: store as raw title
            return {"category": title_line, "stage": "", "round": "", "entries": []}
        return {
            "category": m.group("category"),
            "stage": m.group("stage"),
            "round": normalize_round(m.group("round")),
            "entries": [],
        }

    while i < len(lines):
        ln = lines[i]

        if ln == "":
            i += 1
            continue

        hm = HEADER_RE.match(ln)
        if hm:
            # New section
            title = hm.group("title").strip()
            current = start_section(title)
            sections.append(current)
            i += 1
            continue

        if current is None:
            # Ignore anything before the first header
            i += 1
            continue

        # Expect participant name line then score line
        name = ln
        # Look ahead for score line
        j = i + 1
        while j < len(lines) and lines[j].strip() == "":
            j += 1
        if j >= len(lines):
            break

        score_ln = lines[j].strip()
        sm = SCORELINE_RE.match(score_ln)
        if not sm:
            # If the next line isn't a score line, skip this line
            i += 1
            continue

        entry = {
            "name": name,
            "score": sm.group("score").strip(),
            "opponent": sm.group("opp").strip(),
            "opponent_affiliation": (sm.group("aff") or "").strip(),
        }
        current["entries"].append(entry)

        i = j + 1

    # Drop empty sections (no entries)
    sections = [s for s in sections if s.get("entries")]
    return sections


# ----------------------------
# Rendering
# ----------------------------

env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html"])
)

def render_latest_html(
    sections: List[Dict[str, Any]],
    title: str = "結果速報",
    updated_date: Optional[str] = None
) -> str:
    tmpl = env.get_template("individual_results.html")
    return tmpl.render(
        title=title,
        updated_date=updated_date or dt.date.today().isoformat(),
        sections=sections,
    )


# ----------------------------
# GitHub publish (Contents API)
# ----------------------------

def github_put_file(
    owner: str,
    repo: str,
    path: str,
    content_bytes: bytes,
    message: str,
    token: str,
    branch: str = "main"
) -> None:
    """
    Create or update a file via GitHub Contents API.
    """
    api = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "results-publisher",
    }

    # Check if file exists to get sha
    sha = None
    r = requests.get(api, headers=headers, params={"ref": branch}, timeout=20)
    if r.status_code == 200:
        sha = r.json().get("sha")
    elif r.status_code != 404:
        raise RuntimeError(f"GitHub GET failed {r.status_code}: {r.text}")

    payload = {
        "message": message,
        "content": base64.b64encode(content_bytes).decode("utf-8"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    w = requests.put(api, headers=headers, json=payload, timeout=30)
    if w.status_code not in (200, 201):
        raise RuntimeError(f"GitHub PUT failed {w.status_code}: {w.text}")


# ----------------------------
# FastAPI admin
# ----------------------------

app = FastAPI()

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_OWNER = os.environ.get("GITHUB_OWNER", "wjboo")
GH_REPO = os.environ.get("GITHUB_REPO", "mail-magazine")
GH_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
PUBLISH_PATH = os.environ.get("PUBLISH_PATH", "latest.html")

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/admin", response_class=HTMLResponse)
def admin():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Publish Results</title>
  <style>
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:24px;line-height:1.4;}
    textarea{width:100%;height:320px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:14px;}
    input,button{font-size:16px;padding:8px 10px;}
    .row{margin:12px 0;}
    .hint{color:#444;font-size:13px;}
  </style>
</head>
<body>
  <h1>Publish Results</h1>
  <form method="post" action="/publish">
    <div class="row">
      <label>Password</label><br/>
      <input type="password" name="password" required />
    </div>
    <div class="row">
      <label>Title (optional)</label><br/>
      <input type="text" name="title" placeholder="例：2025年 ○○大会 結果速報" style="width:100%;" />
    </div>
    <div class="row">
      <label>Results text</label><br/>
      <textarea name="raw" required placeholder="Paste the results here..."></textarea>
      <div class="hint">Format: ◆セクション名 → 選手名 → スコア 相手(所属)</div>
    </div>
    <div class="row">
      <button type="submit">Publish to GitHub Pages</button>
    </div>
  </form>
</body>
</html>
"""

@app.post("/publish")
def publish(raw: str = Form(...), password: str = Form(...), title: str = Form("")):
    if ADMIN_PASSWORD and password != ADMIN_PASSWORD:
        return PlainTextResponse("Unauthorized", status_code=401)

    if not GH_TOKEN:
        return PlainTextResponse("Missing GITHUB_TOKEN env var", status_code=500)

    sections = parse_results(raw)
    if not sections:
        return PlainTextResponse("Parsed 0 sections. Check input format.", status_code=400)

    html = render_latest_html(
        sections=sections,
        title=title.strip() or "結果速報",
        updated_date=dt.date.today().strftime("%Y-%m-%d")
    ).encode("utf-8")

    # Publish latest.html
    github_put_file(
        owner=GH_OWNER,
        repo=GH_REPO,
        path=PUBLISH_PATH,
        content_bytes=html,
        message=f"Update {PUBLISH_PATH} ({dt.datetime.now().isoformat(timespec='seconds')})",
        token=GH_TOKEN,
        branch=GH_BRANCH,
    )

    # Optional archive copy
    archive_path = f"archive/{dt.date.today().isoformat()}.html"
    github_put_file(
        owner=GH_OWNER,
        repo=GH_REPO,
        path=archive_path,
        content_bytes=html,
        message=f"Archive results ({dt.date.today().isoformat()})",
        token=GH_TOKEN,
        branch=GH_BRANCH,
    )

    return HTMLResponse(
        f"""<p>Published: <code>{PUBLISH_PATH}</code> and <code>{archive_path}</code></p>
            <p>Wix embed URL: <code>https://{GH_OWNER}.github.io/{GH_REPO}/{PUBLISH_PATH}</code></p>"""
    )
