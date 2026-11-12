# DT-Manager

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # then put your real bot token in .env
python main.py
```

Run it from this directory (`server_dbs/` and `fonts/` are relative
to wherever the process is started, same as the original script).

## Layout

```
main.py             entry point - Bot subclass, cog auto-loading, shared aiohttp session
cogs/
  admin.py           owner-only hot-swap panel: load / reload / unload / refresh_all (+ bonus: cogs)
  events.py          on_ready, on_guild_join, on_message, the DM-only-.stats check, error logging
  economy.py         help, balance, setbio, stats, daily, work, give
  casino.py          coinflip, blackjack
  shop.py            shop (role purchases)
  fun.py             /fakemessage, plus font management (addfont/listfonts/removefont/refreshfonts)
  blacktea.py         .blacktea word game
utils/
  database.py        per-guild sqlite access, all async-safe (asyncio.to_thread)
  achievements.py     achievement definitions + unlock/announce logic
  helpers.py          error_embed, check_bet
  imaging.py           /fakemessage's renderer + the font-fallback system
  wordgame.py           dictionary/combo loading + answer validation for .blacktea
fonts/               drop .ttf/.otf files here (or use .addfont)
data/                dictionary.txt + combos.txt for .blacktea (see below)
```

## Hot-swapping (owner only)

```
.load <name>         load a cog that isn't currently loaded
.unload <name>        unload a running cog
.reload <name>         reload a cog - e.g. after editing/replacing its file
.refresh_all             reload every loaded cog + re-sync slash commands
.cogs                     bonus: shows what's loaded vs. sitting in cogs/ unused
```

`<name>` can be the short form (`casino`) or the full dotted path
(`cogs.casino`). If a file has a syntax error or throws on import, the
full traceback is posted in chat (or attached as a `.txt` if it's too
long for a message) and the bot keeps running on whatever was loaded
before - `reload_extension` only swaps in the new version once it's
confirmed to load cleanly.

`load`/`unload`/`reload` only touch one cog. `refresh_all` walks every
currently-loaded cog and attempts each independently, so one broken
file doesn't block the rest from refreshing.

## Fonts (owner only)

```
.addfont          attach a .ttf/.otf to this command - saved to fonts/, loaded immediately
.listfonts          see what's loaded and in what priority order
.removefont <file>    delete a font from fonts/
.refreshfonts          re-scan fonts/ (for files dropped in by hand)
```

`/fakemessage` resolves each character against every loaded font, in
priority order (`app_font.ttf` first if present, then the rest
alphabetically), and uses whichever font actually has a glyph for that
character. This is what fixes fancy-Unicode text (𝖇𝖔𝖑𝖉 𝖋𝖗𝖆𝖐𝖙𝖚𝖗,
𝕕𝕠𝕦𝕓𝕝𝕖-𝕤𝕥𝕣𝕦𝕔𝕜, sᴍᴀʟʟ ᴄᴀᴘs, etc.) rendering as empty boxes: no single
"looks like Discord" font covers those Unicode blocks, but you can
upload one that does (e.g. **Noto Sans Math** or **Symbola**, both
well known for broad coverage of exactly this kind of text) and it'll
be used automatically wherever the primary font comes up short. A
character with no coverage in *any* loaded font still renders via the
primary font (typically a small placeholder box) rather than vanishing
silently.

## Blacktea (`.blacktea`)

A live word game: post `.blacktea` in a channel, everyone who reacts
✅ within 30 seconds joins (remove the reaction to change your mind).
Once it starts, players get pinged in a shuffled order with 3 random
letters and 10 seconds to reply with one real word containing them, in
order, anywhere in the word. Miss the timer and lose a life (everyone
starts with 2); the last player standing wins. A word can only be used
once per game. The last 3 seconds of each turn get 3️⃣ 2️⃣ 1️⃣ reactions
on the prompt.

Two data files drive it, both plain text and both hot-reloaded by
`.reload blacktea` (no restart needed to edit either one):

- **`data/combos.txt`** - one 3-letter combo per line, `#` for
  comments. This is what gets asked each round.
- **`data/dictionary.txt`** - one valid word per line. This is what
  actually validates an answer.

Both were generated, not hand-typed: `data/dictionary.txt` merges a
~370k-word English list with a first-names list (373,598 entries after
merging and dedup), and `data/combos.txt` was produced by scanning
that dictionary for every 3-letter sequence with at least 200 matching
words, so every combo in the file is guaranteed answerable within the
timer without being trivial. `data/combo_word_preview.txt` is a
generated, human-readable reference in the `(combo:xyz/words:"...",
...)` format - **not read by the bot** - for eyeballing what a combo
matches before deciding whether to keep it in `combos.txt`.

Why answers are checked against a full dictionary instead of a fixed
word list per combo (as literally described in the original ask): a
hand-maintained per-combo list can never be complete, and an
incomplete one means real, valid words get wrongly rejected. Checking
live against `dictionary.txt` means any word that's actually in there
is accepted no matter which combo it's answering, and adding a missed
word later is a one-line edit to one file instead of finding and
updating every combo it happens to match. If you'd rather have tighter
control over exactly which words are acceptable per combo (e.g. a
themed or difficulty-tuned game), `data/combo_word_preview.txt` is
already close to that shape and `check_answer()` in `utils/wordgame.py`
is a single, small function to point at a per-combo list instead.

Both data files are plain text - the dictionary is large but it's
still just a list you can open and edit. It's sourced from a large,
crowd-maintained word list rather than hand-curated, so it leans
permissive (it'll accept some obscure/technical words) and, rarely,
may contain a scraping artifact; delete lines from `dictionary.txt` for
anything you don't want accepted.

## What else changed from the original script

- **Token**: was hardcoded in source (`bot.run("...")`); now read from
  `DISCORD_BOT_TOKEN` in `.env` / the real environment. Rotate your bot
  token in the Discord Developer Portal if the old hardcoded one was
  ever committed or shared anywhere.
- **Blocking I/O**: every sqlite3 call (which is fully synchronous) ran
  directly on the event loop in the original - see the comments at the
  top of `utils/database.py` for the full breakdown, including the two
  hot paths (`on_message`, `on_guild_join`'s member loop) that got
  extra consolidation, not just a blocking->non-blocking wrap.
- **/fakemessage**: opened a new `aiohttp.ClientSession` per call; now
  shares one session for the bot's lifetime. All the Pillow work
  (decoding, drawing, compositing, PNG encoding) is CPU-bound and now
  runs in a worker thread instead of inline on the event loop - see
  the comments at the top of `utils/imaging.py`.
- **`BlackjackView` / `ShopView`**: were defined fresh inside their
  command function on every invocation (closing over `ctx`); now
  module-level classes that take `ctx` explicitly.

## Left exactly as-is (flagging, not fixing, since it wasn't asked for)

- `ACHIEVEMENTS` defines `natural_21_win` and `natural_21_dealer`, but
  nothing in the original code actually calls
  `send_achievement_unlock(..., "natural_21_win")` or
  `"natural_21_dealer"` anywhere - they're defined but never triggered.
  Possibly intentional scaffolding for a feature you haven't wired up
  yet; left untouched either way.
- `MEDIA_ROLE_ID` / `STREAMING_ROLE_ID` are still hardcoded guild-specific
  role IDs in `cogs/shop.py`, same as the original.
- `server_dbs/` and `fonts/app_font.ttf` are still plain relative paths
  (resolved against the process's working directory, not the file's
  location) - same behavior as the original script.
