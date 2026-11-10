# Agent guide: yt-playlist-maker

The user asks for YouTube playlists in chat, and this folder is the tool for making them. Run it as `python3 ytpl.py <command>`. It finds `.env` by itself, so you can run it from any directory. Spec paths are relative to where you run it.

## Workflow

1. **Pick videos.** Only use ids you found with `search` or checked with `info`. Never invent video ids.
2. **Write a spec** to `playlists/<slug>.json` with `title`, `description`, `privacy` and `videos: [{id, note}]`.
3. **Check it** with `python3 ytpl.py info --spec playlists/<slug>.json` and drop anything marked UNAVAILABLE.
4. **Deliver the playlist.**
   - If `.token.json` exists, run `create --spec ...` and give the user the playlist URL it prints.
   - If not, give the `anon --spec ...` link. Tell the user that a permanent playlist needs the one-time setup in README.md: `client_secret.json` plus `python3 ytpl.py auth`. `auth` opens a browser, so the user has to run it themselves.

## Rules

- Never print, cat or echo `.env`, `client_secret*.json` or `.token.json`.
- Default privacy is `private` unless the user asks for something else.
- Quota is 10,000 units per day. `search` costs 100, `create` costs 50 plus 50 per video, and `info` costs 1 per 50 videos. Don't loop searches.
- Video ids that start with `-` must go after `--`, or be passed as URLs.
- Don't create duplicate playlists. If unsure, check `mine` first.
