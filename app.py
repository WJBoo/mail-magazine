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
# STAGE_RE  = re.compile(r"^\[(?P<stage>本戦|予選)\]$")
# Put these near your other regexes
CATEGORY_RE = re.compile(r"^(男子シングルス|男子ダブルス|女子シングルス|女子ダブルス)")
# Accept [本戦] / [予選] (you already have STAGE_RE)
# STAGE_RE  = re.compile(r"^\[(?P<stage>本戦|予選)\]$")

def parse_bracket(text: str) -> List[Dict[str, Any]]:
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]

    sections: List[Dict[str, Any]] = []
    current_section: Optional[Dict[str, Any]] = None

    # For merging same player within a section
    player_index: Dict[str, Dict[str, Any]] = {}

    current_player: Optional[Dict[str, Any]] = None
    current_block: Optional[Dict[str, Any]] = None

    def normalize_category(header_title: str) -> str:
        """
        header_title is text after ◆, e.g. "男子シングルス本戦1R"
        returns just "男子シングルス" etc if found; else returns raw.
        """
        m = CATEGORY_RE.match(header_title.strip())
        return m.group(1) if m else header_title.strip()

    def flush_block():
        nonlocal current_block
        if current_player is not None and current_block is not None:
            # avoid appending empty blocks
            if current_block["lines"] or current_block["stage"]:
                current_player["blocks"].append(current_block)
        current_block = None

    def flush_player():
        nonlocal current_player
        flush_block()
        current_player = None

    def start_section(raw_title: str):
        nonlocal current_section, player_index, current_player, current_block

        flush_player()

        cat = normalize_category(raw_title)

        # If the next header is the same category, continue the same section
        # (this handles "◆男子シングルス本戦1R" then later "◆男子シングルス本戦2R")
        if current_section is not None and current_section.get("category") == cat:
            return

        current_section = {"category": cat, "players": []}
        sections.append(current_section)
        player_index = {}
        current_player = None
        current_block = None

    i = 0
    while i < len(lines):
        ln = lines[i].strip()

        if ln == "":
            i += 1
            continue

        hm = HEADER_RE.match(ln)
        if hm:
            start_section(hm.group("title"))
            i += 1
            continue

        if current_section is None:
            i += 1
            continue

        sm = STAGE_RE.match(ln)
        if sm:
            if current_player is None:
                i += 1
                continue
            flush_block()
            current_block = {"stage": sm.group("stage"), "lines": []}
            i += 1
            continue

        # Decide if this line starts a NEW player
        # Rule: if we have no current_player OR we just finished a player and next looks like a name.
        # In this format, a name line is followed by either [本戦]/[予選] OR a result line.
        if current_player is None:
            name = ln

            # Reuse existing player object in this section if same name appears again
            if name in player_index:
                current_player = player_index[name]
            else:
                current_player = {"name": name, "blocks": []}
                player_index[name] = current_player
                current_section["players"].append(current_player)

            current_block = None
            i += 1
            continue

        # Otherwise: treat as result line
        if current_block is None:
            # If user didn't write [本戦]/[予選], store lines under stage=""
            current_block = {"stage": "", "lines": []}

        current_block["lines"].append(ln)
        i += 1

    flush_player()

    # Remove empty sections
    sections = [s for s in sections if s.get("players")]
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

def merge_events_into_state(state: Dict[str, Any], events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    events: sections list in template shape:
      [{"category":..., "players":[{"name":..., "blocks":[{"stage":..., "lines":[...]}]}]}]
    Merges by:
      category + player + stage, de-dupe exact lines, preserve order.
    """
    # Index existing state
    sections = state.get("sections", [])
    sec_map: Dict[str, Dict[str, Any]] = {s["category"]: s for s in sections if "category" in s}

    def get_or_create_section(category: str) -> Dict[str, Any]:
        if category not in sec_map:
            s = {"category": category, "players": []}
            sec_map[category] = s
            sections.append(s)
        return sec_map[category]

    for ev_sec in events:
        category = ev_sec.get("category", "").strip()
        if not category:
            continue

        s = get_or_create_section(category)

        # player index within this section
        p_map: Dict[str, Dict[str, Any]] = {p["name"]: p for p in s.get("players", []) if "name" in p}

        for ev_p in ev_sec.get("players", []):
            name = (ev_p.get("name") or "").strip()
            if not name:
                continue

            if name not in p_map:
                p = {"name": name, "blocks": []}
                s["players"].append(p)
                p_map[name] = p
            p = p_map[name]

            # block index by stage
            b_map: Dict[str, Dict[str, Any]] = {b.get("stage",""): b for b in p.get("blocks", [])}

            for ev_b in ev_p.get("blocks", []):
                stage = (ev_b.get("stage") or "").strip()  # "本戦"/"予選"/""
                lines = [ln.strip() for ln in ev_b.get("lines", []) if ln.strip()]

                if stage not in b_map:
                    b = {"stage": stage, "lines": []}
                    p["blocks"].append(b)
                    b_map[stage] = b
                b = b_map[stage]

                existing = set(b.get("lines", []))
                for ln in lines:
                    if ln not in existing:
                        b["lines"].append(ln)
                        existing.add(ln)

    state["sections"] = sections
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
    events = parse_pair_headers(raw)   # <-- from the earlier message
    # OR, if you paste the bracket format with [本戦]/[予選]:
    # events = parse_bracket(raw)

    if not events:
        return PlainTextResponse("Parsed 0 sections. Check your pasted format.", status_code=400)

    # 2) Load cumulative state, merge today's events, save back
    state = load_state(GH_TOKEN, GH_OWNER, GH_REPO, GH_BRANCH)
    state["title"] = page_title
    state["last_updated"] = today
    state = merge_events_into_state(state, events)
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

