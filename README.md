# YouTube playlist → Apple Books audiobook

Paste any channel URL in `download_playlists.py` (`CHANNEL_URL`), then:

```bash
./download.sh refresh          # fetch & cache playlist list (only when you want an update)
./download.sh                  # interactive: pick numbers, all, or refresh
./download.sh list             # show cached playlists
./download.sh download 1 3-5   # download specific playlists
./download.sh download all     # download every cached playlist
./download.sh test             # smoke test: 1 episode only
```

## How it maps to Apple Books

| YouTube | Books |
|---------|--------|
| Playlist | One book (folder of `.m4b` chapters) |
| Episode | One chapter (numbered `001`, `002`, …) |
| Videos not in a playlist | Ignored |

Tags set per chapter: album = playlist title, track number, genre = Audiobook, media kind = Audiobook.

## Output

```
downloads/
  <Channel Name>/
    playlists_cache.json
    Surah Yusuf/
      001 - Episode title.m4b
      002 - Episode title.m4b
```

## Single video

```bash
./download_video.sh
# prompts: Paste YouTube video URL:
# or:
./download_video.sh "https://www.youtube.com/watch?v=..."
```

Saves to `downloads/Single Videos/`.

## Switch channel

Edit the top of `download_playlists.py`:

```python
CHANNEL_URL = "https://www.youtube.com/@SomeOtherChannel"
```

Then run `./download.sh refresh`.
