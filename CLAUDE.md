# Agent guide: yt-playlist-maker

The user asks for YouTube playlists in chat, and this folder is the tool for making them. Run it as `python3 ytpl.py <command>`. It finds `.env` by itself, so you can run it from any directory. Spec paths are relative to where you run it.

## Workflow

1. **Pick videos.** Only use ids you found with `search` or checked with `info`. Never invent video ids.
2. **Write a spec** to `playlists/<slug>.json` with `title`, `description`, `privacy` and `videos: [{id, note}]`.
3. **Check it** with `python3 ytpl.py info --spec playlists/<slug>.json` and drop anything marked UNAVAILABLE.
4. **Save it to the user's account.**
   - If `.token.json` exists, run `create --spec ...` and give the user the playlist URL it prints. Running it again is safe, because it reuses the playlist with the same title and adds only missing videos.
   - If not, the user hasn't set up saving yet. Start `python3 ytpl.py setup` **in the background**. It opens four Google Cloud pages in the user's browser, waits up to 15 minutes for the downloaded `client_secret_*.json`, then opens the Google login. Tell the user what to do: enable the API, Get started (Audience: External), add themselves as a test user, create a Desktop app client and Download JSON, then click Continue and Allow on the login. When setup finishes, run `create`.
   - The `anon` link is only a temporary playlist that is not saved to the account. Say so if you share it.

## Rules

- Never print, cat or echo `.env`, `client_secret*.json` or `.token.json`.
- Default privacy is `private` unless the user asks for something else.
- Quota is 10,000 units per day. `search` costs 100, `create` costs 50 plus 50 per added video, and `info` costs 1 per 50 videos. Don't loop searches.
- Video ids that start with `-` must go after `--`, or be passed as URLs.
- If a login has expired (Testing mode lasts 7 days), the user has to run `python3 ytpl.py auth`, which opens a browser.
