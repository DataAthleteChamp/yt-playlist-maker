# yt-playlist-maker

`ytpl.py` is a small command-line tool for making YouTube playlists. It is a single Python file that uses only the standard library. It is built so AI coding agents such as Claude Code can make playlists for you when you ask in chat, and it works just as well by hand.

- Check videos (length, channel, title) and search YouTube with an API key.
- Get an instant playlist link with no login at all.
- Create real playlists in your account after a one-time Google login. This uses OAuth with PKCE and needs no extra packages.
- Describe playlists as small JSON spec files.

## Requirements

Python 3.9 or newer. Nothing to `pip install`.

## Setup

1. Get an API key. In [Google Cloud Console](https://console.cloud.google.com), enable **YouTube Data API v3**, then go to Credentials → Create credentials → **API key**. Restrict the key to YouTube Data API v3.
2. Run `cp .env.example .env` and put your key in `.env`.

That is all you need for `info`, `search` and `anon`.

### One-time login (only for `create`, `add` and `mine`)

YouTube does not let API keys write to an account, so creating playlists needs OAuth:

1. In the same Cloud project, open the OAuth consent screen (Google Auth Platform). Choose **External**, and add your Google account under **Audience → Test users**.
2. Go to Credentials → Create credentials → **OAuth client ID**. Pick **Desktop app**, then **Download JSON**. Save the file as `client_secret.json` next to `ytpl.py`.
3. Run `python3 ytpl.py auth` and approve access in the browser.

While the app is in Testing mode, Google expires the login after 7 days. When that happens, run `auth` again.

## Commands

| Command | What it does | Needs | Quota cost |
|---|---|---|---|
| `info` | check videos: length, channel, title | API key | 1 unit per 50 videos |
| `search "query"` | find videos | API key | 100 units |
| `anon` | instant temporary playlist link | nothing | 0 |
| `auth` | one-time Google login | OAuth client | 0 |
| `create` | create a playlist in your account | login | 50 + 50 per video |
| `add` | append videos to one of your playlists | login | 50 per video |
| `mine` | list your playlists | login | 1 |

The default daily quota is 10,000 units, which is about 190 added videos per day.

```sh
python3 ytpl.py search "binary search explained" --max 5
python3 ytpl.py info --spec playlists/example.json
python3 ytpl.py anon --spec playlists/example.json
python3 ytpl.py create --spec playlists/example.json --dry-run
python3 ytpl.py create --title "My list" --privacy unlisted -- VIDEO_ID https://youtu.be/VIDEO_ID
```

Videos can be bare ids or any common YouTube URL (`watch?v=`, `youtu.be/`, `shorts/`, `embed/`, `live/`). Put ids that start with `-` after `--`.

## Playlist specs

```json
{
  "title": "My playlist",
  "description": "optional",
  "privacy": "private",
  "videos": [{"id": "VIDEO_ID", "note": "why it's here"}]
}
```

Git ignores everything in `playlists/` except `example.json`, so your own lists stay private.

## Using it from an AI agent

`CLAUDE.md` tells agents how to use the tool: check video ids instead of inventing them, never print secrets, stay within the quota, and make playlists private by default. Ask your agent for a playlist and it can do the rest.

## Security

Git ignores `.env`, `client_secret*.json` and `.token.json`, and the tool saves the token file with `600` permissions. Never commit these files.

## License

[MIT](LICENSE)
