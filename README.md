# voxmark

A self-hosted daemon that keeps your ebook and audiobook reading progress in sync
across formats, without any publisher, DRM, or vendor account involved.

If you own the same book as both an EPUB and an audiobook (in a self-hosted library
like [BookOrbit](https://github.com/COMICFEED/bookorbit-app), Audiobookshelf, Kavita,
etc.), voxmark watches your progress in one format and automatically finds and sets
the matching position in the other, so you can put your phone down mid-chapter in the
audiobook and pick the paperback-equivalent spot right back up in the ebook reader.

## Why this exists

Commercial audiobook/ebook bundles use text-alignment tricks for this exact feature,
locked to their own store and their own file formats. This does the same thing for
a self-hosted library: local files, local database, no cloud dependency, no account
requirement, and it works on any book pair you already own in both formats.

## How it works

1. **Watch.** Polls your library's Postgres database for recent `audiobook_progress`
   or `reading_progress` (ebook) updates, debounced (default 30s of inactivity) so it
   only fires once you've actually stopped at a spot, not on every scrub.
2. **Transcribe.** When a debounced audiobook position is detected, it slices a short
   audio clip around the current playback position with `ffmpeg` and transcribes it
   locally with [faster-whisper](https://github.com/guillaumekln/faster-whisper) (CPU,
   `tiny` model by default: fast enough to run inline, no GPU required).
3. **Match.** Parses the paired EPUB's actual spine/chapter text, and uses fuzzy string
   matching ([RapidFuzz](https://github.com/rapidfuzz/RapidFuzz)) to find the same
   sentence in the ebook. It generates a standard `epubcfi()` location string and an
   overall percentage, then writes that back to the ebook's `reading_progress` row.
4. **Reverse direction too.** When you update your position in the ebook instead, it
   estimates the equivalent character offset, pulls the nearby sentence, and searches
   for that same sentence's timestamp inside the audiobook using Whisper's
   word-level timestamps, then writes the matched playback position back to
   `audiobook_progress`.
5. **Auto-pairing.** Book pairs are found automatically: same author(s) + a normalized
   title match (bracketed edition tags and punctuation stripped), so "Foo: A Novel"
   and "Foo [Unabridged]" are recognized as the same underlying book.

No manual chapter mapping, no pre-baked alignment file, no publisher cooperation
required: it listens to real audio and reads the real EPUB text every time.

## Requirements

- Python 3.11+
- PostgreSQL database matching the schema below (this was built against
  [BookOrbit](https://github.com/COMICFEED/bookorbit-app)'s schema; adapt the table/
  column names in `voxmark_daemon.py` if your library server differs)
- `ffmpeg` on PATH
- CPU is enough (`faster-whisper` on `tiny` + `float32`); a GPU is not required but
  will make transcription faster if `faster-whisper` is configured for it

Expected tables (adjust to your library's actual schema if it differs):
- `audiobook_progress` (user_id, book_id, percentage, current_file_id, position_seconds, updated_at)
- `reading_progress` (book_file_id, user_id, percentage, cfi, updated_at)
- `book_files`, `book_metadata`, `book_authors`, `authors`, `books` for lookups

## Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configure

All configuration is via environment variables, no secrets in the script:

| Variable | Default | Purpose |
| --- | --- | --- |
| `VOXMARK_DB_URL` | `postgresql://user:password@localhost:5432/bookorbit` | Postgres connection string |
| `VOXMARK_EBOOKS_PATH` | `/mnt/media/media/books/ebooks` | Host filesystem root your ebook files live under |
| `VOXMARK_AUDIOBOOKS_PATH` | `/mnt/media/media/books/audiobooks` | Host filesystem root your audiobook files live under |

The daemon translates your library server's internal container paths
(`/books/...`, `/audiobooks/...`) into these host paths before touching files
directly, since the transcription/parsing steps run outside your library's own
container.

## Run

```bash
VOXMARK_DB_URL="postgresql://user:pass@host:5432/dbname" python3 voxmark_daemon.py
```

Or install as a systemd service, see `voxmark.service.example`.

## Tuning

- `DEBOUNCE_SECONDS` (default 30): how long a position must sit idle before voxmark
  treats it as "the user stopped here" and runs a sync
- `POLL_INTERVAL` (default 5): how often the DB is polled for new progress rows
- Whisper model size: change `WhisperModel("tiny", ...)` to `base`/`small` for higher
  transcription accuracy at the cost of more CPU time per sync
- Match confidence threshold: fuzzy match acceptance is gated at a 70% RapidFuzz score
  in both directions; raise it if you see false-positive chapter matches on very
  repetitive text (e.g. dialogue-heavy books), lower it if legitimate matches are being
  rejected

## Limitations

- Requires the audiobook and ebook to be genuinely the same text (abridged vs.
  unabridged editions, or heavily adapted "dramatized" versions, will not match
  reliably since the actual words differ)
- Chapter/EPUB parsing assumes a standard EPUB2/3 spine structure; heavily
  nonstandard EPUBs may fail to parse
- This is a homelab tool built against one specific library server's schema. Treat the
  SQL in `voxmark_daemon.py` as a reference implementation to adapt to your own setup,
  not a drop-in for every self-hosted book server.

## License

MIT, see `LICENSE`.
