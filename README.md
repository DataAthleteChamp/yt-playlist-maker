# yt-playlist-maker

`ytpl.py` is a small command-line tool for making YouTube playlists and saving them to your account. It is a single Python file that uses only the standard library. It is built so AI coding agents such as Claude Code can make playlists for you when you ask in chat, and it works just as well by hand.

- Check videos (length, channel, title) and search YouTube with an API key.
- Save playlists to your own YouTube account after a one-time guided setup. Login uses OAuth with PKCE and needs no extra packages.
- Running `create` again is safe. It reuses your playlist with the same title and adds only the missing videos.
- Describe playlists as small JSON spec files.

## Requirements

Python 3.9 or newer. Nothing to `pip install`.

## Setup

**1. API key (for searching and checking videos).** In [Google Cloud Console](https://console.cloud.google.com), enable **YouTube Data API v3** and create an API key restricted to that API. Then run `cp .env.example .env` and put the key in `.env`.

**2. Saving to your account (one time, about 5 minutes).** Run:

```sh
python3 ytpl.py setup
```

It opens the four Google Cloud pages you need, waits for the file you download, moves it into place, and logs you in. Use the same Cloud project as your API key. On those pages:

1. Turn on **YouTube Data API v3**.
2. Under **Google Auth Platform**, click **Get started**. Use any app name, your email, and choose Audience **External**.
3. Under **Audience → Test users**, add your own Gmail.
4. Under **Clients**, click **Create client**, choose **Desktop app**, click **Create**, then **Download JSON**.

Then approve the Google login in your browser. Google warns that the app isn't verified; it is your own app, so click **Continue**.

YouTube does not let API keys write to an account, which is why saving needs this login. While your app is in Testing mode, Google ends the login after 7 days, and `python3 ytpl.py auth` logs you in again. To avoid that, click **Publish app** on the Audience page. The "not verified" warning stays, which is fine for personal use.

## Commands

| Command | What it does | Needs | Quota cost |
|---|---|---|---|
| `info` | check videos: length, channel, title | API key | 1 unit per 50 videos |
| `search "query"` | find videos | API key | 100 units |
| `anon` | temporary playlist link, **not saved** to your account | nothing | 0 |
| `setup` | one-time guided setup for saving playlists | - | 1 |
| `create` | save a playlist to your account, reusing one with the same title | login | 50, plus 50 per added video |
| `add` | append videos to one of your playlists | login | 50 per video |
| `mine` | list your playlists | login | 1 per 50 playlists |
| `auth` | log in again, for example after the 7-day expiry | client file | 0 |

The default daily quota is 10,000 units, which is about 190 added videos per day.

```sh
python3 ytpl.py search "market making explained" --max 5
python3 ytpl.py info --spec playlists/example.json
python3 ytpl.py create --spec playlists/example.json --dry-run
python3 ytpl.py create --spec playlists/example.json
python3 ytpl.py create --title "My list" --privacy unlisted -- VIDEO_ID https://youtu.be/VIDEO_ID
```

Videos can be bare ids or any common YouTube URL (`watch?v=`, `youtu.be/`, `shorts/`, `embed/`, `live/`). Put ids that start with `-` after `--`. Use `create --new` if you really want a second playlist with the same title.

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

`CLAUDE.md` tells agents how to use the tool: check video ids instead of inventing them, never print secrets, stay within the quota, make playlists private by default, and run `setup` for you while you click through Google's pages.

## Security

Git ignores `.env`, `client_secret*.json` and `.token.json`, and the tool saves the client and token files with `600` permissions. Never commit them.

## License

[MIT](LICENSE)
