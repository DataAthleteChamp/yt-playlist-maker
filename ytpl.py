#!/usr/bin/env python3
"""ytpl: make YouTube playlists from the command line. Agent-friendly, stdlib only.

Read-only commands (info, search, anon) only need YOUTUBE_API_KEY in .env.
Creating playlists in your account (create, add, mine) needs a one-time Google
login (`python3 ytpl.py auth`), because YouTube does not let API keys write.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
TOKEN_FILE = ROOT / ".token.json"
CLIENT_FILE = ROOT / "client_secret.json"

API = "https://www.googleapis.com/youtube/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube"
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


class CliError(Exception):
    """A user-facing error, printed without a traceback."""


# ---------------------------------------------------------------- plumbing

def load_env() -> None:
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def write_private(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o600)


def request_json(method: str, url: str, *, params=None, body=None, form=None, token=None) -> dict:
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise CliError(f"HTTP {exc.code}: {_error_text(exc.read())}") from None
    except urllib.error.URLError as exc:
        raise CliError(f"network error: {exc.reason}") from None
    return json.loads(raw) if raw else {}


def _error_text(raw: bytes) -> str:
    text = raw.decode(errors="replace")
    try:
        payload = json.loads(text)
    except ValueError:
        return text[:300]
    err = payload.get("error")
    if isinstance(err, dict):  # YouTube Data API error
        reasons = [e.get("reason") for e in err.get("errors", []) if e.get("reason")]
        return err.get("message", text) + (f" [{', '.join(reasons)}]" if reasons else "")
    if err:  # OAuth token endpoint error
        return f"{err} {payload.get('error_description', '')}".strip()
    return text[:300]


def api_key() -> str:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        raise CliError(f"YOUTUBE_API_KEY is missing; add it to {ENV_FILE}")
    return key


# ---------------------------------------------------------------- parsing

def video_id(text: str) -> str:
    """Accept a bare 11-character id or any common YouTube URL form."""
    text = text.strip()
    if VIDEO_ID.fullmatch(text):
        return text
    url = urllib.parse.urlparse(text if "://" in text else "https://" + text)
    host = url.netloc.lower().split(":")[0]
    for prefix in ("www.", "m.", "music."):
        host = host.removeprefix(prefix)
    candidate = ""
    if host == "youtu.be":
        candidate = url.path.strip("/").split("/")[0]
    elif host == "youtube.com":
        query = urllib.parse.parse_qs(url.query)
        if "v" in query:
            candidate = query["v"][0]
        else:
            parts = [p for p in url.path.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
                candidate = parts[1]
    if not VIDEO_ID.fullmatch(candidate):
        raise CliError(f"not a YouTube video id or URL: {text!r}")
    return candidate


def playlist_id(text: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(text).query)
    return query.get("list", [text.strip()])[0]


def iso_seconds(iso: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def fmt_seconds(total: int) -> str:
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def load_spec(path: str) -> dict:
    try:
        spec = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise CliError(f"can't read spec {path}: {exc}") from None
    spec["ids"] = [video_id(v["id"] if isinstance(v, dict) else v) for v in spec.get("videos", [])]
    return spec


def collect(args) -> tuple[list[str], dict]:
    spec = load_spec(args.spec) if args.spec else {}
    ids = spec.get("ids", []) + [video_id(v) for v in args.videos]
    ids = list(dict.fromkeys(ids))  # dedupe, keep order
    if not ids:
        raise CliError("no videos given: pass ids/URLs or --spec playlists/<name>.json")
    return ids, spec


# ---------------------------------------------------------------- read-only (API key)

def fetch_videos(ids: list[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for start in range(0, len(ids), 50):
        data = request_json("GET", f"{API}/videos", params={
            "part": "snippet,contentDetails,status",
            "id": ",".join(ids[start:start + 50]),
            "maxResults": 50,
            "key": api_key(),
        })
        for item in data.get("items", []):
            found[item["id"]] = item
    return found


def print_videos(ids: list[str], found: dict[str, dict]) -> None:
    total = 0
    for n, vid in enumerate(ids, 1):
        item = found.get(vid)
        if item is None:
            print(f"{n:>2}. {vid}  UNAVAILABLE (private, deleted or wrong id)")
            continue
        secs = iso_seconds(item["contentDetails"].get("duration", ""))
        total += secs
        channel = item["snippet"]["channelTitle"][:22]
        flag = "" if item.get("status", {}).get("embeddable", True) else "  [no embed]"
        print(f"{n:>2}. {vid}  {fmt_seconds(secs):>7}  {channel:<22}  {item['snippet']['title']}{flag}")
    ok = sum(1 for v in ids if v in found)
    print(f"-- {ok}/{len(ids)} available, total {fmt_seconds(total)} "
          f"(~{fmt_seconds(round(total / 1.5))} at 1.5x)")


def cmd_info(args) -> None:
    ids, _ = collect(args)
    print_videos(ids, fetch_videos(ids))


def cmd_search(args) -> None:
    data = request_json("GET", f"{API}/search", params={
        "part": "snippet",
        "type": "video",
        "q": args.query,
        "maxResults": max(1, min(args.max, 50)),
        "relevanceLanguage": args.lang,
        "key": api_key(),
    })
    ids = [it["id"]["videoId"] for it in data.get("items", []) if it.get("id", {}).get("videoId")]
    if not ids:
        print("no results")
        return
    print_videos(ids, fetch_videos(ids))


def cmd_anon(args) -> None:
    ids, _ = collect(args)
    if len(ids) > 50:
        raise CliError("an instant (no-login) playlist holds at most 50 videos")
    print("https://www.youtube.com/watch_videos?video_ids=" + ",".join(ids))
    print("Temporary playlist, no login needed. For a permanent one in your account: "
          "`create` (after the one-time `auth`).", file=sys.stderr)


# ---------------------------------------------------------------- login (OAuth, installed-app + PKCE)

def oauth_client() -> tuple[str, str]:
    cid = os.environ.get("GOOGLE_CLIENT_ID", "")
    secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if CLIENT_FILE.exists():
        block = json.loads(CLIENT_FILE.read_text())
        block = block.get("installed") or block.get("web") or {}
        cid, secret = block.get("client_id", cid), block.get("client_secret", secret)
    if not cid or not secret:
        raise CliError(f"no OAuth client: save the Desktop-app client JSON as {CLIENT_FILE.name} "
                       "(README.md, 'One-time login setup')")
    return cid, secret


def save_token(fresh: dict, old: dict | None = None) -> dict:
    merged = dict(old or {})
    merged.update(fresh)
    merged["expires_at"] = time.time() + int(fresh.get("expires_in", 3600))
    write_private(TOKEN_FILE, json.dumps(merged, indent=2))
    return merged


def access_token() -> str:
    if not TOKEN_FILE.exists():
        raise CliError("not logged in: run `python3 ytpl.py auth` once (see README.md)")
    tok = json.loads(TOKEN_FILE.read_text())
    if tok.get("expires_at", 0) - 60 > time.time():
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise CliError("login expired: run `python3 ytpl.py auth` again")
    cid, secret = oauth_client()
    try:
        fresh = request_json("POST", TOKEN_URL, form={
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        })
    except CliError as exc:
        raise CliError(f"{exc}\nlogin expired (apps in Testing mode expire after 7 days): "
                       "run `python3 ytpl.py auth` again") from None
    return save_token(fresh, tok)["access_token"]


def cmd_auth(args) -> None:
    cid, secret = oauth_client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib naming)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in query or "error" in query:
                result.update({k: v[0] for k, v in query.items()})
                ok = "code" in query
                msg = ("Got Google's answer. Close this tab and check the terminal." if ok
                       else f"Login failed: {query['error'][0]}")
                self.send_response(200)
            else:
                msg = "Waiting for Google login..."
                self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode())

        def log_message(self, *_):  # keep the terminal quiet
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    redirect = f"http://127.0.0.1:{server.server_port}"
    url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    print("Opening your browser for Google login. If it doesn't open, visit:\n" + url)
    webbrowser.open(url)
    deadline = time.time() + 300
    while not result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        raise CliError(f"login not completed: {result.get('error', 'timed out after 5 minutes')}")
    if result.get("state") != state:
        raise CliError("login state mismatch, aborting")
    save_token(request_json("POST", TOKEN_URL, form={
        "code": result["code"],
        "client_id": cid,
        "client_secret": secret,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
        "code_verifier": verifier,
    }))
    print(f"Logged in. Saved {TOKEN_FILE.name}; create/add/mine now work.")


# ---------------------------------------------------------------- write (needs login)

def insert_item(token: str, pid: str, vid: str) -> None:
    body = {"snippet": {"playlistId": pid, "resourceId": {"kind": "youtube#video", "videoId": vid}}}
    for attempt in range(3):
        try:
            request_json("POST", f"{API}/playlistItems", params={"part": "snippet"}, body=body, token=token)
            return
        except CliError as exc:
            if attempt == 2 or not re.match(r"HTTP (409|5\d\d)", str(exc)):
                raise
            time.sleep(1.5 * (attempt + 1))


def cmd_create(args) -> None:
    ids, spec = collect(args)
    title = args.title or spec.get("title")
    if not title:
        raise CliError("playlist needs a title: --title or 'title' in the spec")
    description = args.description if args.description is not None else spec.get("description", "")
    privacy = args.privacy or spec.get("privacy", "private")
    found = fetch_videos(ids) if os.environ.get("YOUTUBE_API_KEY") else {}
    if found:
        missing = [v for v in ids if v not in found]
        if missing:
            print("skipping unavailable videos: " + ", ".join(missing), file=sys.stderr)
            ids = [v for v in ids if v in found]
    print(f"Playlist '{title}' ({privacy}), {len(ids)} videos, ~{50 + 50 * len(ids)} quota units")
    if args.dry_run:
        print_videos(ids, found) if found else print("\n".join(ids))
        return
    token = access_token()
    playlist = request_json("POST", f"{API}/playlists", params={"part": "snippet,status"}, token=token, body={
        "snippet": {"title": title[:150], "description": description[:5000]},
        "status": {"privacyStatus": privacy},
    })
    pid = playlist["id"]
    failed = []
    for n, vid in enumerate(ids, 1):
        try:
            insert_item(token, pid, vid)
            print(f"  [{n}/{len(ids)}] added {vid}")
        except CliError as exc:
            failed.append(vid)
            print(f"  [{n}/{len(ids)}] FAILED {vid}: {exc}", file=sys.stderr)
    print(f"https://www.youtube.com/playlist?list={pid}")
    if failed:
        raise CliError(f"{len(failed)} video(s) not added: {', '.join(failed)}")


def cmd_add(args) -> None:
    ids, _ = collect(args)
    token = access_token()
    pid = playlist_id(args.playlist)
    for n, vid in enumerate(ids, 1):
        insert_item(token, pid, vid)
        print(f"  [{n}/{len(ids)}] added {vid}")
    print(f"https://www.youtube.com/playlist?list={pid}")


def cmd_mine(args) -> None:
    data = request_json("GET", f"{API}/playlists", token=access_token(), params={
        "part": "snippet,contentDetails,status", "mine": "true", "maxResults": 50,
    })
    for p in data.get("items", []):
        print(f"{p['id']}  {p['contentDetails']['itemCount']:>3} videos  "
              f"{p['status']['privacyStatus']:<8}  {p['snippet']['title']}")


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(line_buffering=True)  # keep stdout/stderr in order when piped to an agent
    load_env()
    parser = argparse.ArgumentParser(prog="ytpl", description="Make YouTube playlists (agent-friendly).")
    sub = parser.add_subparsers(dest="command", required=True)

    def videos_cmd(name: str, help_text: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("videos", nargs="*", help="video ids or YouTube URLs (put ids starting with '-' after --)")
        sp.add_argument("--spec", help="playlist spec JSON, see playlists/")
        return sp

    videos_cmd("info", "check videos: length, channel, title (API key, 1 unit per 50)").set_defaults(fn=cmd_info)

    sp = sub.add_parser("search", help="search YouTube (API key, 100 units)")
    sp.add_argument("query")
    sp.add_argument("--max", type=int, default=5)
    sp.add_argument("--lang", default="en")
    sp.set_defaults(fn=cmd_search)

    videos_cmd("anon", "instant temporary playlist link, no login (max 50)").set_defaults(fn=cmd_anon)
    sub.add_parser("auth", help="one-time Google login so create/add/mine work").set_defaults(fn=cmd_auth)

    sp = videos_cmd("create", "create a playlist in your account (login; 50 units + 50 per video)")
    sp.add_argument("--title")
    sp.add_argument("--description")
    sp.add_argument("--privacy", choices=["private", "unlisted", "public"])
    sp.add_argument("--dry-run", action="store_true", help="validate and show, don't create")
    sp.set_defaults(fn=cmd_create)

    sp = videos_cmd("add", "append videos to one of your playlists (login)")
    sp.add_argument("--playlist", required=True, help="playlist id or URL")
    sp.set_defaults(fn=cmd_add)

    sub.add_parser("mine", help="list your playlists (login)").set_defaults(fn=cmd_mine)

    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
