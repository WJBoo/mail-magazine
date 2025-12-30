import os
import re
import json
import base64
import datetime as dt
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
# ADD:
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

    # Remove accidental whitespace/newlines
    client_b64 = re.sub(r"\s+", "", client_b64)
    token_b64  = re.sub(r"\s+", "", token_b64)

    client_info = json.loads(base64.b64decode(client_b64).decode("utf-8"))
    token_info  = json.loads(base64.b64decode(token_b64).decode("utf-8"))

    installed = client_info.get("installed") or client_info.get("web") or {}
    if not installed:
        raise RuntimeError("credentials.json missing 'installed' or 'web' block")

    # token.json must contain refresh_token for server use
    if not token_info.get("refresh_token"):
        raise RuntimeError(
            "token.json has no refresh_token. Recreate token.json with prompt='consent' and gmail.send scope."
        )

    # Build creds with refresh config in the constructor (no property-setting)
    creds = Credentials(
        token=token_info.get("token"),
        refresh_token=token_info.get("refresh_token"),
        token_uri=token_info.get("token_uri") or installed.get("token_uri") or "https://oauth2.googleapis.com/token",
        client_id=token_info.get("client_id") or installed.get("client_id"),
        client_secret=token_info.get("client_secret") or installed.get("client_secret"),
        scopes=SCOPES,
    )

    # Force refresh so we definitely have an access token for Authorization header
    creds.refresh(Request())

    # Optional: one-time debug in logs (remove later)
    print("GMAIL AUTH DEBUG:", {"has_token": bool(creds.token), "expired": creds.expired})

    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    msg = MIMEText(html, "html", "utf-8")
    msg["To"] = to_email
    msg["Subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    service.users().messages().send(userId=from_user, body={"raw": raw}).execute()

def normalize_zenkaku(s: str) -> str:
    if not s:
        return s

    # full-width digits -> ASCII digits
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))

    # normalize punctuation BUT DO NOT touch "ー" (prolonged sound mark)
    s = (s.replace("／", "/")
          .replace("　", " ")
          .replace("（", "(").replace("）", ")"))

    # normalize minus variants to ASCII hyphen for parsing (not "ー")
    for ch in ["－", "−", "―", "–", "—", "‐", "-"]:
        s = s.replace(ch, "-")

    return s


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
TEAM_HEADER_RE = re.compile(r"^\s*【(?P<gender>男子|女子)】\s*(?P<title>.+?)\s*$")
TEAM_LINE_RE = re.compile(
    r"^\s*(?P<slot>D\d|S\d)\s+"
    r"(?P<team>.+?)\s+"
    r"(?P<score>[0-9０-９\-/／()（）－−―–—‐ー]+)\s+"
    r"(?P<opp>.+?)\s*$"
)


def did_home_team_win(score: str) -> bool:
    """
    Determines whether the LEFT side (Keio) won the match.
    score examples:
      6-3/6-2
      6-2/2-6/7-10
      6(3)-7/6-4/6-2
    """
    score = normalize_zenkaku(score)
    sets = score.split("/")

    home_sets = 0
    away_sets = 0

    for s in sets:
        # extract numbers only: "6(3)-7" -> ["6","7"]
        nums = re.findall(r"\d+", s)
        if len(nums) < 2:
            continue

        a, b = int(nums[0]), int(nums[1])
        if a > b:
            home_sets += 1
        else:
            away_sets += 1

    return home_sets > away_sets

def compute_team_score(lines: list) -> dict:
    """
    lines = mens.lines or womens.lines
    Returns: {"home": 5, "away": 4}
    """
    home = 0
    away = 0

    for row in lines:
        if row.get("kind") != "match":
            continue

        score = row.get("score", "")
        if not score:
            continue

        if did_home_team_win(score):
            home += 1
        else:
            away += 1

    return {"home": home, "away": away}

def build_final_line(home: int, away: int, home_name="慶應義塾大学") -> str:
    if home > away:
        return f"計{home}-{away}を持ちまして、{home_name}の勝ちが決定致しました。"
    elif home < away:
        return f"計{away}-{home}を持ちまして、相手校の勝ちが決定致しました。"
    else:
        return f"計{home}-{away}の引き分けとなりました。"

def append_team_final_line(block: Dict[str, Any], home_name="慶應義塾大学"):
    if not block["lines"]:
        return

    score = compute_team_score(block["lines"])
    final_line = build_final_line(score["home"], score["away"], home_name)

    block["lines"].append({
        "kind": "note",
        "text": final_line
    })


def parse_team_report(text: str) -> Dict[str, Any]:
    lines = [
        normalize_zenkaku(ln.rstrip())
        for ln in (text or "").replace("\r\n", "\n").split("\n")
    ]

    out = {
        "mens": {"title": "", "lines": []},
        "womens": {"title": "", "lines": []},
    }

    current = None  # "mens" or "womens"

    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue

        hm = TEAM_HEADER_RE.match(ln)
        if hm:
            # 🔴 IMPORTANT PART ①: close previous block
            if current is not None:
                append_team_final_line(out[current])

            gender = hm.group("gender")
            title = hm.group("title").strip()
            current = "mens" if gender == "男子" else "womens"
            out[current]["title"] = title
            continue

        if current is None:
            continue

        m = TEAM_LINE_RE.match(ln)
        if not m:
            out[current]["lines"].append({
                "kind": "note",
                "text": ln
            })
            continue

        slot = m.group("slot").strip()
        team = m.group("team").strip()
        score = m.group("score").strip()
        opp = m.group("opp").strip()

        score_disp = score.replace("-", "－").replace("/", "／")

        out[current]["lines"].append({
            "kind": "match",
            "slot": slot,
            "team": team,
            "score": score_disp,
            "opp": opp,
            "display": [
                f"{slot} {team}",
                f"{score_disp} {opp}",
            ],
        })

    # 🔴 IMPORTANT PART ②: close last block after loop
    if current is not None:
        append_team_final_line(out[current])

    return out



HEADER_RE = re.compile(
    r"^[◆◇\s]*"
    r"(?P<category>男子シングルス|男子ダブルス|女子シングルス|女子ダブルス)"
    r"\s*(?P<stage>本戦|予選)\s*(?P<round>.+?)\s*$"
)


def parse_score_line(score_line: str):
    """
    Returns (score, opp) or None if not parseable.
    Accepts:
      6-3/6-2 南拓海(関西大学)
      7-6(3)/6-3 後藤...(...)
    Also works if there are odd unicode punctuation characters.
    """
    s = (score_line or "").strip()
    if not s:
        return None

    # Try strict regex first
    m = SCORE_RE.match(s)
    if m:
        return m.group("score"), m.group("opp")

    # Fallback: split at first whitespace and validate left token “looks like a score”
    parts = s.split(None, 1)
    if len(parts) != 2:
        return None

    left, right = parts[0].strip(), parts[1].strip()

    # “looks like score”: contains at least one digit and only score-ish chars
    if any(ch.isdigit() for ch in left) and re.fullmatch(r"[0-9\-/()]+", left):
        return left, right

    return None


SCORE_RE = re.compile(r"^(?P<score>[0-9０-９\-/／()（）－−ー―]+)\s+(?P<opp>.+)$")


def parse_daily_results(text: str) -> List[Dict[str, Any]]:
    lines = [
        normalize_zenkaku(ln.strip())
        for ln in text.replace("\r\n", "\n").split("\n")
        if ln.strip()
    ]

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

            parsed = parse_score_line(score_line)
            if parsed:
                score, opp = parsed
                section["players"].append({
                    "name": name,
                    "blocks": [{
                        "stage": stage,
                        "lines": [f"{round_} {score} {opp}"]
                    }]
                })
                i += 2
            else:
                # If score_line didn't parse, do NOT jump by 2.
                # Move forward by 1 so we don't skip headers / desync.
                i += 1


        sections.append(section)

    return sections

def parse_tomorrow_text(tomorrow_text: str) -> List[Dict[str, str]]:
    """
    Accepts flexible input like:
      山田太郎（男子シングルス）
      対 佐藤次郎（9:00、3番コート）

      鈴木花子（女子シングルス）
      対 田中愛（10:30、1番コート）

    Returns:
      [{"name":"山田太郎", "event":"男子シングルス", "opponent":"佐藤次郎", "time":"9:00", "court":"3番コート"}, ...]
    """
    t = (tomorrow_text or "").strip()
    if not t:
        return []

    # split into blocks by blank lines
    blocks = re.split(r"\n\s*\n+", t)
    out: List[Dict[str, str]] = []

    # name line: "山田太郎（男子シングルス）" or "山田太郎 (男子シングルス)"
    name_re = re.compile(r"^(?P<name>.+?)\s*[（(]\s*(?P<event>.+?)\s*[)）]\s*$")
    # match line: "対 佐藤（9:00、3番コート）"
    vs_re = re.compile(r"^対\s*(?P<opp>.+?)\s*[（(]\s*(?P<time>[^、,]+)\s*[、,]\s*(?P<court>.+?)\s*[)）]\s*$")

    for b in blocks:
        lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue

        m1 = name_re.match(lines[0])
        m2 = vs_re.match(lines[1])

        if not (m1 and m2):
            continue

        out.append({
            "name": m1.group("name"),
            "event": m1.group("event"),
            "opponent": m2.group("opp"),
            "time": m2.group("time"),
            "court": m2.group("court"),
        })

    return out

def tomorrow_player_names(tomorrow_matches: List[Dict[str, str]]) -> List[str]:
    """
    Returns unique player names scheduled for tomorrow.
    """
    seen = set()
    names = []
    for m in tomorrow_matches:
        name = m.get("name")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names

def sanitize_tag(tag: str) -> str:
    tag = (tag or "").strip()
    if not tag:
        raise ValueError("tournament_name (tag) is empty")
    # ファイル名として危ない文字だけ潰す（日本語は残す）
    tag = re.sub(r"[\\/:\*\?\"<>\|]+", "_", tag)
    tag = re.sub(r"\s+", "_", tag)
    return tag

def tournament_state_path(tag: str) -> str:
    return f"{TOURNAMENT_DIR}/{sanitize_tag(tag)}.json"

def is_mens_category(cat: str) -> bool:
    return (cat or "").startswith("男子")

def is_womens_category(cat: str) -> bool:
    return (cat or "").startswith("女子")

def split_sections_by_gender(sections: List[Dict[str, Any]]):
    left = [s for s in sections if is_mens_category(s.get("category", ""))]
    right = [s for s in sections if is_womens_category(s.get("category", ""))]
    return left, right


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
    updated_date: Optional[str] = None,
    announcement_title: str = "",
    tournament_link: str = "",
    venue_name: str = "",
    tomorrow_matches: Optional[List[Dict[str, str]]] = None,
    special_message: str = "",
) -> str:
    tmpl = env.get_template("bracket_template.html")
    return tmpl.render(
        title=title,
        updated_date=updated_date or dt.date.today().isoformat(),
        sections=sections,
        announcement_title=announcement_title,
        tournament_link=tournament_link,
        venue_name=venue_name,
        tomorrow_matches=tomorrow_matches or [],
        special_message=special_message,
    )

    def normalize_zenkaku(s: str) -> str:
        if not s:
            return s
        # convert full-width digits to ASCII digits
        trans = str.maketrans("０１２３４５６７８９", "0123456789")
        s = s.translate(trans)
    
        # normalize common full-width punctuation
        s = (s.replace("／", "/")
              .replace("－", "-")
              .replace("−", "-")
              .replace("ー", "-")
              .replace("―", "-")
              .replace("（", "(")
              .replace("）", ")")
              .replace("　", " "))  # full-width space
    
        return s


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
TOURNAMENT_DIR = "data/tournaments"
STATE_PATH = "data/state.json"
PUBLISH_LATEST = "latest.html"

def load_state_at_path(token: str, owner: str, repo: str, branch: str, path: str) -> Dict[str, Any]:
    txt = github_get_file_text(owner, repo, path, token, branch=branch)
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

def save_state_at_path(state: Dict[str, Any], token: str, owner: str, repo: str, branch: str, path: str) -> None:
    b = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
    github_put_file(
        owner=owner, repo=repo, path=path,
        content_bytes=b,
        message=f"Update {path} ({dt.datetime.now().isoformat(timespec='seconds')})",
        token=token, branch=branch
    )


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
  <h1>Preview Results</h1>
  <form method="post" action="/preview">
    <div class="row">
      <label>Password (optional)</label><br/>
      <input type="password" name="password" />
    </div>
    <div class="row">
  <label>レポート形式</label><br/>
  <select name="report_type" style="width:100%;padding:8px 10px;font-size:16px;">
    <option value="individual" selected>個人戦（現行）</option>
    <option value="team">団体戦（男子/女子 左右）</option>
  </select>
</div>

    <div class="row">
      <label>Title (optional)</label><br/>
      <input type="text" name="title" placeholder="例：2025年 ○○大会 結果速報" style="width:100%;" />
    </div>
    <div class="row">
  <label>大会名</label><br/>
  <input type="text"
         name="tournament_name"
         placeholder="例：2025関東大学リーグ"
         style="width:100%;" />
</div>

<div class="row">
  <label>日次タイトル（何日目 + 案内など）</label><br/>
  <input type="text"
         name="day_title"
         placeholder="例：5日目結果報告、及び6日目のご案内"
         style="width:100%;" />
</div>
    
    <div class="row">
      <label>大会詳細リンク</label><br/>
      <input type="text"
             name="tournament_link"
             placeholder="https://kantotennisgakuren.r-cms.jp/..."
             style="width:100%;" />
    </div>

    
    <div class="row">
      <label>会場名</label><br/>
      <input type="text" name="venue_name"
             placeholder="例：慶應義塾大学 日吉キャンパス"
             style="width:100%;" />
    </div>
    
    <div class="row">
      <label>明日の予定（1行=1試合）</label><br/>
      <textarea name="tomorrow_text"
                placeholder="例：
    山田太郎（男子シングルス）
    対 佐藤次郎（9:00、3番コート）
    
    鈴木花子（女子シングルス）
    対 田中愛（10:30、1番コート）"
                style="width:100%;height:140px;"></textarea>
      <div class="hint">空行区切りでもOK。行数の制限なし。</div>
    </div>

    
    <div class="row">
      <label>特別メッセージ</label><br/>
      <textarea name="special_message"
                placeholder="例：応援よろしくお願いします！"
                style="width:100%;height:80px;"></textarea>
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

@app.post("/preview", response_class=HTMLResponse)
def preview(
    raw: str = Form(...),
    password: str = Form(""),
    title: str = Form(""),
    announcement_title: str = Form(""),
    tournament_link: str = Form(""),
    venue_name: str = Form(""),
    special_message: str = Form(""),
    tomorrow_text: str = Form(""),
    tournament_name: str = Form(""),
    day_title: str = Form(""),
    report_type: str = Form("individual"),
):
    if ADMIN_PASSWORD and password != ADMIN_PASSWORD:
        return PlainTextResponse("Unauthorized", status_code=401)

    # normalize
    report_type = (report_type or "individual").strip()

    tournament_name = tournament_name.strip()
    day_title = day_title.strip()
    announcement_title = "｜".join(x for x in [tournament_name, day_title] if x)

    tomorrow_matches = parse_tomorrow_text(tomorrow_text)
    tomorrow_names = tomorrow_player_names(tomorrow_matches)

    # ============================================================
    # TEAM REPORT MODE
    # ============================================================
    if report_type == "team":
        team = parse_team_report(raw)

        ctx = dict(
            raw=raw,
            title=title.strip() or "結果速報",
            tournament_name=tournament_name,
            day_title=day_title,
            announcement_title=announcement_title,
            tournament_link=tournament_link.strip(),
            venue_name=venue_name.strip(),
            tomorrow_matches=tomorrow_matches,
            tomorrow_names=tomorrow_names,
            special_message=special_message.strip(),
            report_type="team",
            team=team,
        )

        header_html = env.get_template("email_header.html").render(**ctx)
        left_html = env.get_template("team_left.html").render(team=team)
        right_html = env.get_template("team_right.html").render(team=team)

        return env.get_template("preview.html").render(
            header_html=header_html,
            left_html=left_html,
            right_html=right_html,
            ctx=json.dumps(ctx, ensure_ascii=False),
        )

    # ============================================================
    # INDIVIDUAL REPORT MODE (existing behavior)
    # ============================================================
    events = parse_daily_results(raw)
    if not events:
        return PlainTextResponse("Parsed 0 sections.", status_code=400)

    ctx = dict(
        raw=raw,
        title=title.strip() or "結果速報",
        tournament_name=tournament_name,
        day_title=day_title,
        announcement_title=announcement_title,
        tournament_link=tournament_link.strip(),
        venue_name=venue_name.strip(),
        tomorrow_matches=tomorrow_matches,
        tomorrow_names=tomorrow_names,
        special_message=special_message.strip(),
        sections=events,
        report_type="individual",
    )

    header_html = env.get_template("email_header.html").render(**ctx)
    left_sections, right_sections = split_sections_by_gender(events)

    left_html = env.get_template("column_left.html").render(sections=left_sections)
    right_html = env.get_template("column_right.html").render(sections=right_sections)

    return env.get_template("preview.html").render(
        header_html=header_html,
        left_html=left_html,
        right_html=right_html,
        ctx=json.dumps(ctx, ensure_ascii=False),
    )




@app.post("/publish_final", response_class=HTMLResponse)
def publish_final(
    ctx: str = Form(...),
    header_html: str = Form(""),
    left_html: str = Form(""),
    right_html: str = Form(""),
):
    ctx = json.loads(ctx)
    today = dt.date.today().isoformat()

    # ---- GitHub / "DB" ----
    tournament_tag = (ctx.get("tournament_name") or "").strip()
    state_path = tournament_state_path(tournament_tag)

    state = load_state_at_path(GH_TOKEN, GH_OWNER, GH_REPO, GH_BRANCH, state_path)
    state["title"] = ctx["title"]
    state["last_updated"] = today

    # meta
    state["tournament_tag"] = tournament_tag
    if ctx.get("report_type") == "team":
        state.setdefault("team_days", [])
        state["team_days"].append({"date": today, "team": ctx["team"]})
        state["tournament_link"] = (ctx.get("tournament_link") or "").strip()
        state["venue_name"] = (ctx.get("venue_name") or "").strip()

    if ctx.get("report_type") == "team":
        state.setdefault("team_days", [])
        state["team_days"].append({
            "date": today,
            "mens": ctx["team"].get("mens", {}),
            "womens": ctx["team"].get("womens", {}),
        })
    else:
        state = merge_into_state(state, ctx["sections"])

    # save accumulated dataset
    save_state_at_path(state, GH_TOKEN, GH_OWNER, GH_REPO, GH_BRANCH, state_path)

    # ---- Render from accumulated state (ALL results thus far) ----
    all_sections = state.get("sections", [])
    left_sections, right_sections = split_sections_by_gender(all_sections)
    
    if ctx.get("report_type") == "team":
        team_days = state.get("team_days", [])
        left_html  = env.get_template("team_left.html").render(team_days=team_days)
        right_html = env.get_template("team_right.html").render(team_days=team_days)
    else:
        all_sections = state.get("sections", [])
        left_sections, right_sections = split_sections_by_gender(all_sections)
        left_html  = env.get_template("column_left.html").render(sections=left_sections)
        right_html = env.get_template("column_right.html").render(sections=right_sections)


    html = env.get_template("bracket_wrapper.html").render(
        title=ctx["title"],
        updated_date=today,
        header_html=header_html,
        left_html=left_html,
        right_html=right_html,
    ).encode("utf-8")

    tag_safe = sanitize_tag(tournament_tag)

    github_put_file(
        owner=GH_OWNER,
        repo=GH_REPO,
        path=f"latest_{tag_safe}.html",
        content_bytes=html,
        message=f"Update latest ({today})",
        token=GH_TOKEN,
        branch=GH_BRANCH
    )

    github_put_file(
        owner=GH_OWNER,
        repo=GH_REPO,
        path=f"archive/{tag_safe}/{today}.html",
        content_bytes=html,
        message=f"Archive ({today})",
        token=GH_TOKEN,
        branch=GH_BRANCH
    )

    # ---- Email subject ----
    subject_core = "｜".join(
        x for x in [
            ctx.get("tournament_name", "").strip(),
            ctx.get("day_title", "").strip(),
        ]
        if x
    ) or ctx.get("title", "結果速報")

    send_gmail_html(
        to_email="wboo@college.harvard.edu",
        subject=f"{subject_core}（{today}）",
        html=html.decode("utf-8"),
    )

    send_gmail_html(
        to_email="gtknt3010@gmail.com",
        subject=f"{subject_core}（{today}）",
        html=html.decode("utf-8"),
    )

    return HTMLResponse("<h2>送信完了しました。</h2>")





