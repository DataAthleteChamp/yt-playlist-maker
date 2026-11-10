#!/usr/bin/env python3
"""ytpl: make YouTube playlists from the command line. Agent-friendly, stdlib only.

Read-only commands (info, search, anon) only need YOUTUBE_API_KEY in .env.
Saving playlists to your account (create, add, mine) needs a one-time Google
login, because YouTube does not let API keys write. Run `python3 ytpl.py setup`
once and it walks you through it.
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
import shutil
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
DOWNLOADS = Path.home() / "Downloads"

API = "https://www.googleapis.com/youtube/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube"
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
TEST_USERS_URL = "https://console.cloud.google.com/auth/audience"

SETUP_STEPS = [
    ("Turn on YouTube Data API v3: click Enable (if the button says Manage, it is already on).",
     "https://console.cloud.google.com/apis/library/youtube.googleapis.com"),
    ("Set up Google Auth Platform: click Get started, enter any app name (for example ytpl) and your "
     "email, choose Audience: External, then Create.",
     "https://console.cloud.google.com/auth/overview"),
    ("Add yourself as a test user: Audience, then Test users, then Add users, enter your Gmail, then Save.",
     TEST_USERS_URL),
    ("Create the client: Clients, then Create client, Application type: Desktop app, then Create, then "
     "Download JSON in the dialog that appears.",
     "https://console.cloud.google.com/auth/clients"),
]


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
    print("This is a temporary playlist and is NOT saved to your account. To save playlists to your "
          "account, run `python3 ytpl.py setup` once, then use `create`.", file=sys.stderr)


# ---------------------------------------------------------------- login (OAuth, installed-app + PKCE)

def client_file() -> Path | None:
    """The OAuth client JSON in this folder, if any (Google names downloads client_secret_<id>.json)."""
    if CLIENT_FILE.exists():
        return CLIENT_FILE
    found = sorted(ROOT.glob("client_secret*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0] if found else None


def read_client(path: Path) -> tuple[str, str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise CliError(f"can't read {path.name}: {exc}") from None
    if not isinstance(data, dict) or "installed" not in data:
        kind = next(iter(data), "unknown") if isinstance(data, dict) else "unknown"
        raise CliError(f"{path.name} is a '{kind}' client, but a 'Desktop app' client is needed "
                       "(Google Auth Platform: Clients, then Create client, then Desktop app)")
    block = data["installed"]
    if not block.get("client_id") or not block.get("client_secret"):
        raise CliError(f"{path.name} is missing client_id or client_secret")
    return block["client_id"], block["client_secret"]


def oauth_client() -> tuple[str, str]:
    path = client_file()
    if path:
        return read_client(path)
    cid = os.environ.get("GOOGLE_CLIENT_ID", "")
    secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    if cid and secret:
        return cid, secret
    raise CliError("no Google login client yet: run `python3 ytpl.py setup` once (about 5 minutes)")


def save_token(fresh: dict, old: dict | None = None) -> dict:
    merged = dict(old or {})
    merged.update(fresh)
    merged["expires_at"] = time.time() + int(fresh.get("expires_in", 3600))
    write_private(TOKEN_FILE, json.dumps(merged, indent=2))
    return merged


def access_token() -> str:
    if not TOKEN_FILE.exists():
        raise CliError("not logged in to YouTube yet: run `python3 ytpl.py setup` once")
    tok = json.loads(TOKEN_FILE.read_text())
    if tok.get("expires_at", 0) - 60 > time.time():
        return tok["access_token"]
    if not tok.get("refresh_token"):
        raise CliError("login expired: run `python3 ytpl.py auth`")
    cid, secret = oauth_client()
    try:
        fresh = request_json("POST", TOKEN_URL, form={
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": tok["refresh_token"],
            "grant_type": "refresh_token",
        })
    except CliError as exc:
        raise CliError(f"{exc}\nyour login expired (apps in Testing mode expire after 7 days): "
                       "run `python3 ytpl.py auth`") from None
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
    print("Opening Google login in your browser. Google may warn that the app isn't verified; "
          "it is your own app, so click Continue, then allow YouTube access.")
    print("If the browser doesn't open, visit:\n" + url)
    webbrowser.open(url)
    deadline = time.time() + 300
    while not result and time.time() < deadline:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        error = result.get("error", "timed out after 5 minutes")
        raise CliError(f"login not completed ({error}). If Google said 'Access blocked', add your Google "
                       f"account as a test user at {TEST_USERS_URL}, then run `python3 ytpl.py auth`.")
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
    print(f"Logged in. Saved {TOKEN_FILE.name}; create, add and mine now work.")


def find_client() -> Path | None:
    """A client JSON in this folder, or the newest one in ~/Downloads."""
    path = client_file()
    if path:
        return path
    if DOWNLOADS.is_dir():
        downloads = list(DOWNLOADS.glob("client_secret*.json"))
        if downloads:
            return max(downloads, key=lambda p: p.stat().st_mtime)
    return None


def cmd_setup(args) -> None:
    if not os.environ.get("YOUTUBE_API_KEY"):
        print(f"note: no YOUTUBE_API_KEY in {ENV_FILE.name}. info and search need it; saving playlists doesn't.")
    if TOKEN_FILE.exists() and not args.force:
        try:
            count = len(my_playlists(access_token()))
            print(f"Already set up: logged in, {count} playlists in your account. Use --force to redo it.")
            return
        except CliError as exc:
            print(f"The saved login no longer works ({exc}). Setting up again.")
    source = find_client()
    if source is None:
        print("One-time setup: create your own Google login client (about 5 minutes).")
        print("Use the same Google Cloud project as your API key (project picker at the top of each page).\n")
        for n, (text, url) in enumerate(SETUP_STEPS, 1):
            print(f"{n}. {text}\n   {url}")
        if not args.no_browser:
            for _, url in SETUP_STEPS:
                webbrowser.open(url)
        wait = f"{args.wait // 60} min" if args.wait >= 60 else f"{args.wait} s"
        print(f"\nWaiting up to {wait} for client_secret_*.json in {DOWNLOADS} "
              "(or in this folder). Press Ctrl+C to stop.", flush=True)
        deadline = time.time() + args.wait
        while source is None and time.time() < deadline:
            time.sleep(2)
            source = find_client()
        if source is None:
            raise CliError("no client file appeared; finish the steps above, then run setup again")
    read_client(source)  # validate before moving it into place
    if source != CLIENT_FILE:
        shutil.move(str(source), CLIENT_FILE)
        CLIENT_FILE.chmod(0o600)
        print(f"Moved {source.name} here as {CLIENT_FILE.name} (private, git-ignored).")
    cmd_auth(args)
    count = len(my_playlists(access_token()))
    print(f"All set: logged in, {count} playlists in your account. "
          "Next: python3 ytpl.py create --spec playlists/<name>.json")


# ---------------------------------------------------------------- write (needs login)

def my_playlists(token: str) -> list[dict]:
    items: list[dict] = []
    page = None
    while True:
        params = {"part": "snippet,contentDetails,status", "mine": "true", "maxResults": 50}
        if page:
            params["pageToken"] = page
        data = request_json("GET", f"{API}/playlists", params=params, token=token)
        items += data.get("items", [])
        page = data.get("nextPageToken")
        if not page:
            return items


def playlist_video_ids(token: str, pid: str) -> set[str]:
    ids: set[str] = set()
    page = None
    while True:
        params = {"part": "contentDetails", "playlistId": pid, "maxResults": 50}
        if page:
            params["pageToken"] = page
        data = request_json("GET", f"{API}/playlistItems", params=params, token=token)
        ids.update(item["contentDetails"]["videoId"] for item in data.get("items", []))
        page = data.get("nextPageToken")
        if not page:
            return ids


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


def add_videos(token: str, pid: str, ids: list[str]) -> list[str]:
    failed = []
    for n, vid in enumerate(ids, 1):
        try:
            insert_item(token, pid, vid)
            print(f"  [{n}/{len(ids)}] added {vid}")
        except CliError as exc:
            failed.append(vid)
            print(f"  [{n}/{len(ids)}] FAILED {vid}: {exc}", file=sys.stderr)
    return failed


def cmd_create(args) -> None:
    ids, spec = collect(args)
    title = (args.title or spec.get("title") or "").strip()
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
    if args.dry_run:
        print(f"Would save '{title}' ({privacy}) with {len(ids)} videos, ~{50 + 50 * len(ids)} quota units")
        print_videos(ids, found) if found else print("\n".join(ids))
        return
    token = access_token()
    wanted = title.lower()
    existing = None if args.new else next(
        (p for p in my_playlists(token) if p["snippet"]["title"].strip().lower() == wanted), None)
    if existing:
        pid = existing["id"]
        have = playlist_video_ids(token, pid)
        todo = [v for v in ids if v not in have]
        print(f"Updating your existing playlist '{existing['snippet']['title']}': "
              f"{len(have)} videos there, adding {len(todo)}.")
    else:
        playlist = request_json("POST", f"{API}/playlists", params={"part": "snippet,status"}, token=token, body={
            "snippet": {"title": title[:150], "description": description[:5000]},
            "status": {"privacyStatus": privacy},
        })
        pid, todo = playlist["id"], ids
        print(f"Created playlist '{title}' ({privacy}). Adding {len(todo)} videos.")
    failed = add_videos(token, pid, todo)
    print(f"https://www.youtube.com/playlist?list={pid}")
    if failed:
        raise CliError(f"{len(failed)} video(s) not added: {', '.join(failed)}. Run the same command again to retry.")


def cmd_add(args) -> None:
    ids, _ = collect(args)
    token = access_token()
    pid = playlist_id(args.playlist)
    failed = add_videos(token, pid, ids)
    print(f"https://www.youtube.com/playlist?list={pid}")
    if failed:
        raise CliError(f"{len(failed)} video(s) not added: {', '.join(failed)}")


def cmd_mine(args) -> None:
    for p in my_playlists(access_token()):
        print(f"{p['id']}  {p['contentDetails']['itemCount']:>3} videos  "
              f"{p['status']['privacyStatus']:<8}  {p['snippet']['title']}")


# ---------------------------------------------------------------- CLI

def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
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

    videos_cmd("anon", "temporary playlist link, NOT saved to your account (max 50)").set_defaults(fn=cmd_anon)

    sp = sub.add_parser("setup", help="one-time guided setup so playlists save to your account")
    sp.add_argument("--wait", type=int, default=900, help="seconds to wait for the downloaded client file")
    sp.add_argument("--no-browser", action="store_true", help="print the setup links instead of opening them")
    sp.add_argument("--force", action="store_true", help="redo setup even if already logged in")
    sp.set_defaults(fn=cmd_setup)

    sub.add_parser("auth", help="log in again (for example after the 7-day expiry)").set_defaults(fn=cmd_auth)

    sp = videos_cmd("create", "save a playlist to your account; reuses one with the same title")
    sp.add_argument("--title")
    sp.add_argument("--description")
    sp.add_argument("--privacy", choices=["private", "unlisted", "public"])
    sp.add_argument("--new", action="store_true", help="always create a new playlist, even if the title exists")
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
