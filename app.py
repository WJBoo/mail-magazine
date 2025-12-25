import os
import re
import json
import base64
import datetime as dt
from typing import List, Dict, Any, Optional

import requests
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape


# ============================================================
# 1) PARSING (new “bracket + multi-line per player” format)
# ============================================================
# Input format you want to paste (example):
# ◆男子シングルス
# 菅谷優作
# [本戦]
# 1R bye
# 2R 6-2/3-6/11-9 大下翔希(近畿大学)
# ...
#
# (blank line)
# 次の選手名
# [本戦]
# ...
#
# ◆女子シングルス
# ...

HEADER_RE = re.compile(r"^◆(?P<title>.+)$")
STAGE_RE = re.compile(r"^\[(?P<stage>本戦|予選)\]$")
# "round line" is anything non-empty that is not header/stage
# We keep it as plain text (already includes opponent/affiliation if present)

def parse_bracket(text: str) -> List[Dict[str, Any]]:
    """
    Returns:
    sections = [
      {
        "category": "男子シングルス",
        "players": [
          {"name":"菅谷優作","stage":"本戦","lines":["1R bye","2R 6-2/... 大下翔希(近畿大学)", ...]},
          ...
        ]
      }, ...
    ]
    """
    raw_lines = text.replace("\r\n", "\n").split("\n")
    lines = [ln.rstrip() for ln in raw_lines]

    # trim leading/trailing blank lines
    while lines and lines[0].strip() == "":
        lines.pop(0)
    while lines and lines[-1].strip() == "":
        lines.pop()

    sections: List[Dict[str, Any]] = []
    current_section: Optional[Dict[str, Any]] = None
    current_player: Optional[Dict[str, Any]] = None

    def flush_player():
        nonlocal current_player, current_section
        if current_section is None or current_player is None:
            return
        # keep only if has any meaningful content
        if current_player.get("name") and (current_player.get("lines") or current_player.get("stage")):
            current_section["players"].append(current_player)
        current_player = None

    def start_section(title: str):
        nonlocal current_section, current_player
        # flush previous player and section
        flush_player()
        if current_section is not None and current_section.get("players"):
            sections.append(current_section)
        current_section = {"category": title.strip(), "players": []}
        current_player = None

    i = 0
    while i < len(lines):
        ln = lines[i].strip()

        if ln == "":
            # blank line ends a player block (if we already started one)
            flush_player()
            i += 1
            continue

        hm = HEADER_RE.match(ln)
        if hm:
            start_section(hm.group("title"))
            i += 1
            continue

        if current_section is None:
            # ignore anything before first header
            i += 1
            continue

        sm = STAGE_RE.match(ln)
        if sm:
            if current_player is None:
                # stage without a player name: create placeholder
                current_player = {"name": "", "stage": sm.group("stage"), "lines": []}
            else:
                current_player["stage"] = sm.group("stage")
            i += 1
            continue

        # Otherwise it's either a player name (if no current_player) or a line of that player
        if current_player is None:
            current_player = {"name": ln, "stage": "", "lines": []}
        else:
            # If we have a player but no stage and no lines yet, and this line looks like a name,
            # we still treat it as a line unless a blank line separated it.
            current_player["lines"].append(ln)

        i += 1

    # flush at end
    flush_player()
    if current_section is not None and current_section.get("players"):
        sections.append(current_section)

    return sections


# ============================================================
# 2) RENDERING
# ============================================================
env = Environment(
    loader=FileSystemLoader("templates"),
    autoescape=select_autoescape(["html"])
)

def render_bracket_html(
    sections: List[Dict[str, Any]],
    title: str = "結果速報",
    updated_date: Optional[str] = None
) -> str:
    tmpl = env.get_template("bracket_template.html")  # <-- use the template I gave you
    return tmpl.render(
        title=title,
        updated_date=updated_date or dt.date.today().isoformat(),
        sections=sections,
    )


# ============================================================
# 3) GITHUB HELPERS (read + write files via Contents API)
# ============================================================
def gh_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "results-publisher",
    }

def github_get_file_text(owner: str, repo: str, path: str, token: str, branch: str = "main") -> Optional[str]:
    api = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    r = requests.get(api, headers=gh_headers(token), params={"ref": branch}, timeout=20)
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"GitHub GET failed {r.status_code}: {r.text}")
    content_b64 = r.json()["content"]
    return base64.b64decode(content_b64).decode("utf-8")

def github_put_file(
    owner: str,
    repo: str,
    path: str,
    content_bytes: bytes,
    message: str,
    token: str,
    branch: str = "main"
) -> None:
    api = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = gh_headers(token)

    # get sha if exists
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


# ============================================================
# 4) “ADD NEW ROWS EACH DAY” STORAGE MODEL
#    - Keep a JSON log in the repo: data/days.json
#    - Each day append: {"date":"YYYY-MM-DD","title":"...","sections":[...]}
#    - Re-render:
#        (a) latest.html (today)
#        (b) archive/YYYY-MM-DD.html
#        (c) optional: index.html listing all days
# ============================================================
DATA_PATH = "data/days.json"
PUBLISH_LATEST = "latest.html"

def load_days(token: str, owner: str, repo: str, branch: str) -> List[Dict[str, Any]]:
    txt = github_get_file_text(owner, repo, DATA_PATH, token, branch=branch)
    if not txt:
        return []
    try:
        data = json.loads(txt)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []

def save_days(days: List[Dict[str, Any]], token: str, owner: str, repo: str, branch: str) -> None:
    b = json.dumps(days, ensure_ascii=False, indent=2).encode("utf-8")
    github_put_file(
        owner=owner, repo=repo, path=DATA_PATH,
        content_bytes=b,
        message=f"Update {DATA_PATH} ({dt.datetime.now().isoformat(timespec='seconds')})",
        token=token, branch=branch
    )

def render_index_html(days: List[Dict[str, Any]]) -> str:
    """
    Simple list page (optional). Create templates/index.html if you want nicer styling.
    """
    items = []
    for d in sorted(days, key=lambda x: x.get("date",""), reverse=True):
        date = d.get("date","")
        title = d.get("title","結果速報")
        items.append(f'<li><a href="archive/{date}.html">{date} — {title}</a></li>')
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>結果一覧</title></head>
<body><h1>結果一覧</h1><ul>{"".join(items)}</ul></body></html>"""


# ============================================================
# 5) FASTAPI APP
# ============================================================
app = FastAPI()

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")  # if empty => no password check
GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GH_OWNER = os.environ.get("GITHUB_OWNER", "wjboo")
GH_REPO = os.environ.get("GITHUB_REPO", "mail-magazine")
GH_BRANCH = os.environ.get("GITHUB_BRANCH", "main")

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
    textarea{width:100%;height:360px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:14px;}
    input,button{font-size:16px;padding:8px 10px;}
    .row{margin:12px 0;}
    .hint{color:#444;font-size:13px;}
  </style>
</head>
<body>
  <h1>Publish Results</h1>
  <form method="post" action="/publish">
    <div class="row">
      <label>Password (optional)</label><br/>
      <input type="password" name="password" />
    </div>
    <div class="row">
      <label>Title (optional)</label><br/>
      <input type="text" name="title" placeholder="例：2025年 ○○大会 結果速報" style="width:100%;" />
    </div>
    <div class="row">
      <label>Results text</label><br/>
      <textarea name="raw" required placeholder="Paste the bracket-style results here..."></textarea>
      <div class="hint">Format: ◆種目 → 選手名 → [本戦/予選] → 複数行(1R bye / 2R ...)</div>
    </div>
    <div class="row">
      <button type="submit">Publish</button>
    </div>
  </form>
</body>
</html>
"""

@app.post("/publish")
def publish(raw: str = Form(...), password: str = Form(""), title: str = Form("")):
    # password behavior:
    # - if ADMIN_PASSWORD is empty, anyone can publish
    # - if set, the user must input it
    if ADMIN_PASSWORD and password != ADMIN_PASSWORD:
        return PlainTextResponse("Unauthorized", status_code=401)

    if not GH_TOKEN:
        return PlainTextResponse("Missing GITHUB_TOKEN env var", status_code=500)

    today = dt.date.today().isoformat()
    sections = parse_bracket(raw)
    if not sections:
        return PlainTextResponse("Parsed 0 sections. Check your pasted format.", status_code=400)

    page_title = title.strip() or "結果速報"

    # 1) Append today to JSON log (or replace if same date already exists)
    days = load_days(GH_TOKEN, GH_OWNER, GH_REPO, GH_BRANCH)
    days = [d for d in days if d.get("date") != today]
    days.append({"date": today, "title": page_title, "sections": sections})
    save_days(days, GH_TOKEN, GH_OWNER, GH_REPO, GH_BRANCH)

    # 2) Render today HTML from template
    html = render_bracket_html(
        sections=sections,
        title=page_title,
        updated_date=today
    ).encode("utf-8")

    # 3) Publish latest.html + archive copy
    github_put_file(
        owner=GH_OWNER, repo=GH_REPO, path=PUBLISH_LATEST,
        content_bytes=html,
        message=f"Update {PUBLISH_LATEST} ({dt.datetime.now().isoformat(timespec='seconds')})",
        token=GH_TOKEN, branch=GH_BRANCH
    )
    archive_path = f"archive/{today}.html"
    github_put_file(
        owner=GH_OWNER, repo=GH_REPO, path=archive_path,
        content_bytes=html,
        message=f"Archive results ({today})",
        token=GH_TOKEN, branch=GH_BRANCH
    )

    # 4) Optional: publish an index page listing all days
    index_html = render_index_html(days).encode("utf-8")
    github_put_file(
        owner=GH_OWNER, repo=GH_REPO, path="index.html",
        content_bytes=index_html,
        message=f"Update index.html ({dt.datetime.now().isoformat(timespec='seconds')})",
        token=GH_TOKEN, branch=GH_BRANCH
    )

    return HTMLResponse(
        f"""
        <p>Published:</p>
        <ul>
          <li><code>{PUBLISH_LATEST}</code></li>
          <li><code>{archive_path}</code></li>
          <li><code>{DATA_PATH}</code></li>
          <li><code>index.html</code></li>
        </ul>
        <p>Latest URL: <code>https://{GH_OWNER}.github.io/{GH_REPO}/{PUBLISH_LATEST}</code></p>
        <p>Archive URL: <code>https://{GH_OWNER}.github.io/{GH_REPO}/{archive_path}</code></p>
        """
    )
