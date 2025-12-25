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

# Email Code
import os, base64, json, re
from email.mime.text import MIMEText

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

def send_gmail_html(to_email: str, subject: str, html: str, from_user: str = "me"):
    client_b64 = os.environ.get("GMAIL_CLIENT_B64", "")
    token_b64  = os.environ.get("GMAIL_TOKEN_B64", "")
    if not client_b64 or not token_b64:
        raise RuntimeError("Missing GMAIL_CLIENT_B64 or GMAIL_TOKEN_B64 env var")

    # strip whitespace/newlines just in case Render wrapped it
    client_b64 = re.sub(r"\s+", "", client_b64)
    token_b64  = re.sub(r"\s+", "", token_b64)

    client_info = json.loads(base64.b64decode(client_b64).decode("utf-8"))
    token_info  = json.loads(base64.b64decode(token_b64).decode("utf-8"))

    installed = client_info.get("installed") or client_info.get("web") or {}
    if not installed:
        raise RuntimeError("credentials.json missing 'installed' or 'web' block")

    # Build creds from token.json
    creds = Credentials.from_authorized_user_info(token_info, scopes=SCOPES)

    # Ensure refresh configuration exists (needed on servers)
    creds.client_id = creds.client_id or installed.get("client_id")
    creds.client_secret = creds.client_secret or installed.get("client_secret")
    creds.token_uri = creds.token_uri or installed.get("token_uri", "https://oauth2.googleapis.com/token")

    # IMPORTANT: server must be able to refresh
    # If refresh_token is missing, you will never get an access token on Render.
    if not creds.refresh_token:
        raise RuntimeError(
            "token.json has no refresh_token. Re-create token.json with InstalledAppFlow and gmail.send scope."
        )

    # Force refresh to guarantee Authorization header is sent
    creds.refresh(Request())

    # Debug once in Render logs (remove later)
    print("GMAIL AUTH DEBUG:",
          {"has_token": bool(creds.token), "expired": creds.expired, "scopes": creds.scopes})

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    msg = MIMEText(html, "html", "utf-8")
    msg["To"] = to_email
    msg["Subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service.users().messages().send(userId=from_user, body={"raw": raw}).execute()


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
HEADER_RE = re.compile(
    r"^(?P<category>男子シングルス|男子ダブルス|女子シングルス|女子ダブルス)"
    r"(?P<stage>本戦|予選)"
    r"(?P<round>.+)$"
)

SCORE_RE = re.compile(
    r"^(?P<score>[0-9\-/()]+)\s+(?P<opp>.+)$"
)

def parse_daily_results(text: str) -> List[Dict[str, Any]]:
    lines = [ln.strip() for ln in text.replace("\r\n", "\n").split("\n") if ln.strip()]

    sections = []
    i = 0

    while i < len(lines):
        # ---- HEADER ----
        hm = HEADER_RE.match(lines[i])
        if not hm:
            i += 1
            continue

        category = hm.group("category")
        stage = hm.group("stage")
        round_ = hm.group("round")

        section = {
            "category": category,
            "players": []
        }

        i += 1

        # ---- PLAYER + SCORE PAIRS ----
        while i + 1 < len(lines) and not HEADER_RE.match(lines[i]):
            name = lines[i]
            score_line = lines[i + 1]

            sm = SCORE_RE.match(score_line)
            if sm:
                section["players"].append({
                    "name": name,
                    "blocks": [{
                        "stage": stage,
                        "lines": [
                            f"{round_} {sm.group('score')} {sm.group('opp')}"
                        ]
                    }]
                })

            i += 2

        sections.append(section)

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
# 4) JSON Stuff
# ============================================================
STATE_PATH = "data/state.json"
PUBLISH_LATEST = "latest.html"

def load_state(token: str, owner: str, repo: str, branch: str) -> Dict[str, Any]:
    txt = github_get_file_text(owner, repo, STATE_PATH, token, branch=branch)
    if not txt:
        return {"title": "結果速報", "last_updated": "", "sections": []}
    try:
        data = json.loads(txt)
        if not isinstance(data, dict):
            return {"title": "結果速報", "last_updated": "", "sections": []}
        data.setdefault("title", "結果速報")
        data.setdefault("last_updated", "")
        data.setdefault("sections", [])
        return data
    except json.JSONDecodeError:
        return {"title": "結果速報", "last_updated": "", "sections": []}

def save_state(state: Dict[str, Any], token: str, owner: str, repo: str, branch: str) -> None:
    b = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    github_put_file(
        owner=owner, repo=repo, path=STATE_PATH,
        content_bytes=b,
        message=f"Update {STATE_PATH} ({dt.datetime.now().isoformat(timespec='seconds')})",
        token=token, branch=branch
    )

def merge_into_state(state: Dict[str, Any], daily_sections: List[Dict[str, Any]]) -> Dict[str, Any]:
    sec_map = {s["category"]: s for s in state.get("sections", [])}

    for ds in daily_sections:
        cat = ds["category"]

        if cat not in sec_map:
            sec_map[cat] = {"category": cat, "players": []}
            state["sections"].append(sec_map[cat])

        sec = sec_map[cat]
        player_map = {p["name"]: p for p in sec["players"]}

        for dp in ds["players"]:
            name = dp["name"]

            if name not in player_map:
                player_map[name] = {"name": name, "blocks": []}
                sec["players"].append(player_map[name])

            player = player_map[name]

            for db in dp["blocks"]:
                stage = db["stage"]

                block = next((b for b in player["blocks"] if b["stage"] == stage), None)
                if not block:
                    block = {"stage": stage, "lines": []}
                    player["blocks"].append(block)

                for ln in db["lines"]:
                    if ln not in block["lines"]:
                        block["lines"].append(ln)

    return state



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
    if ADMIN_PASSWORD and password != ADMIN_PASSWORD:
        return PlainTextResponse("Unauthorized", status_code=401)

    if not GH_TOKEN:
        return PlainTextResponse("Missing GITHUB_TOKEN env var", status_code=500)

    today = dt.date.today().isoformat()
    page_title = title.strip() or "結果速報"

    # 1) Parse today's paste into "events"
    # IMPORTANT: use the parser matching your input format:
    events = events = parse_daily_results(raw)   # <-- from the earlier message
    # OR, if you paste the bracket format with [本戦]/[予選]:
    # events = parse_bracket(raw)

    if not events:
        return PlainTextResponse("Parsed 0 sections. Check your pasted format.", status_code=400)

    # 2) Load cumulative state, merge today's events, save back
    state = load_state(GH_TOKEN, GH_OWNER, GH_REPO, GH_BRANCH)
    state["title"] = page_title
    state["last_updated"] = today
    state = merge_into_state(state, events)
    save_state(state, GH_TOKEN, GH_OWNER, GH_REPO, GH_BRANCH)

    # 3) Render cumulative results (NOT just today's)
    html = render_bracket_html(
        sections=state["sections"],
        title=state["title"],
        updated_date=state["last_updated"],
    ).encode("utf-8")

    # 4) Publish latest + archive snapshot
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
        message=f"Archive cumulative results ({today})",
        token=GH_TOKEN, branch=GH_BRANCH
    )
    send_gmail_html(
    to_email="wboo@college.harvard.edu",
    subject=f"{page_title}（{today}）",
    html=html.decode("utf-8")
    )


    return HTMLResponse(
        f"""
        <p>Updated cumulative state and published cumulative page.</p>
        <ul>
          <li><code>{STATE_PATH}</code></li>
          <li><code>{PUBLISH_LATEST}</code></li>
          <li><code>{archive_path}</code></li>
        </ul>
        <p>Latest URL: <code>https://{GH_OWNER}.github.io/{GH_REPO}/{PUBLISH_LATEST}</code></p>
        """
    )

