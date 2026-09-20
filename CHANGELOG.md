# Changelog

Notable changes, newest first. This project moves quickly; entries are
grouped by cycle rather than by version number.

## 2.0 — ground-up rebuild (current)

Version 2 is a complete rewrite. The beta was a single-purpose per-tag
proof-checker; this shares its name and its core gesture and almost
nothing else — the state engine, the file layer, the save format, every
tool, and the whole interface were written from scratch. What follows is
what the rebuild is, grouped by area. Later sections below record the
cycles this was assembled across.

### The core, rebuilt

- **Image-first review loop.** See an image, judge one tag on it (Yes /
  No / Skip), and the caption is rewritten to disk instantly and
  atomically — a crash can never leave a half-written file. Walk a
  single tag across every image that carries it, or work image by image.
- **Auto-confirm and filters.** Auto-confirm pre-answers Yes for tags the
  tagger already applied so you only check the doubtful ones; filters
  narrow the queue to just the images still undecided for a tag.
- **Real completion tracking.** A tag is "done" only when every image
  carrying it has been decided; a percentage reports exactly that.
- **Multi-folder loading.** Several folders load and walk as one dataset,
  with the loaded set shown in the window title.

### Tag intelligence

- **Tag Referencer.** Post counts for any tag across four dated Danbooru
  snapshots (2017, Pony-era 2023, Illustrious/NoobAI 2024, current), a
  one-line drift verdict (stable / renamed / too-new), two-way alias
  resolution, co-occurrence, and a compare mode for "are these two tags
  interchangeable?". Fully offline; the optional online half adds the
  Danbooru wiki, example galleries and a post viewer, off by default.
- **Pruning Advisor.** Ranks every tag by how worthwhile it is to cut, in
  three tiers, explaining every verdict — era-aware rarity, broad/narrow
  family detection, and export (including an opt-in briefing for a
  language model). Read-only by design.
- **Multi-tokenizer token counting.** Counts each caption with the
  *actual* tokeniser the trainer uses, because the right ruler differs by
  model: CLIP for **SDXL / Illustrious** (75 / 150 / 225), T5-XXL for
  **Flux.1**, Qwen3 for **Flux.2 Klein** (which also covers Krea 2 and
  Anima), and Mistral's Tekken for **Flux.2 Dev**. The Flux tokenisers
  load only when selected, so they cost nothing at startup; the Mistral
  counter is a from-scratch pure-Python reader validated token-for-token
  against Mistral's own library, adding ~3 MB rather than the ~80 MB the
  official library would.

### Dataset health & statistics

- **Health Check** independently flags: missing caption file, empty
  caption, unusual or unreadable resolution, not-perfectly-square (off by
  default), and stray caption files.
- **Statistics** built around the dataset: caption-length distribution
  against the token limits, tags-per-image density, tag frequency, and
  your vocabulary measured against the base-model snapshot you chose.
- **Meta info inspector.** Right-click any image for "Meta info…" to read
  the Stable-Diffusion generation metadata embedded in it — prompt,
  negative prompt and settings — parsed for Automatic1111 / Forge and
  ComfyUI, and read from EXIF for JPEG/WebP, in a two-column view with
  copy buttons. Kept separate from "Properties…" so the dense generation
  data never crowds the quick facts.

### Bulk editing, auditing, and rules

- **Batch tag edits:** rename (with automatic merge), split, delete, and
  duplicate-cleanup across the whole dataset; underscore/space
  conversion with a preview and a format-breaking-edit guard. Each is
  atomic and a single undo.
- **Tag audit** against a bundled 201,269-tag Danbooru database, with an
  exceptions list for deliberate tags.
- **Conflict rules** — exclusion (tags that must not co-occur) and
  requirement (a tag that must accompany another), alias-aware — with a
  violation scanner that changes nothing until you choose.

### Image Editor (standalone)

- Batch and one-by-one cropping to **Kohya-style aspect-ratio buckets**
  (2048 / 1536 / 1024 / 768 / 512), colour jitter, and a report of what
  each image needs — writing to a separate output folder, never touching
  originals. **Right-click drag pans** the image in the one-by-one view,
  and the input and output folder pickers each remember their own
  location independently.

### Sessions, safety, and interface

- **Sessions** save and restore every decision, tag-status change,
  filter/sort, front-locked token and the exact walk position; autosave
  runs on a timer and by action count; a changed-on-disk dataset is
  reconciled automatically rather than breaking; saves are atomic.
- **Undo** covers every destructive action, batch operations included,
  with an "undo everything" option and an action log.
- **Network off by default** — nothing leaves the machine unless Danbooru
  lookups are enabled, and even then only a tag name or post id is sent.
- **Seven themes**, fully rebindable shortcuts, co-occurrence hints while
  walking, and robust caption reading (UTF-8, then CP932, UTF-16 by BOM)
  that never overwrites a file it cannot decode.
- **Calmer top bar.** The old path strip (directory label, Browse, Full
  Reset) was retired; loading and Full Reset moved into the File menu,
  and the loaded-dataset identity — including multi-folder loads — is
  carried by the window title.

## Network cycle

Added an opt-in half that reads reference material from Danbooru. The
dataset half still makes no network requests, and nothing about a
dataset ever leaves the machine.

- **Tag Referencer** — era drift verdicts across four shipped
  snapshots, offline tag search, the Danbooru wiki rendered in-app,
  the staff-curated example galleries, and a **compare mode** that
  puts two tags side by side with their post counts and how much of
  their usual company they share.
- **Post Browser** — uncurated search, for seeing how a tag is used in
  practice rather than only the examples an editor chose.
- **Content blacklist** — one editable, pre-filled list governing
  images fetched from Danbooru. Your own dataset is never filtered.
- **Bookmarks** for posts, tags and searches, in plain JSON beside the
  settings so they stay readable if the program will not start.
- **Discovery** — one button that looks up a random tag, filtered by
  scope and minimum post count.
- **Tag Pruning Advisor** — ranks tags worth cutting when captions
  press against a token limit, in three tiers, explaining every
  verdict. Read-only by design.
- **Dataset marker** — renames over-limit images and their captions
  with a `!` prefix so they sort to the top of a folder.
- **Diagnostic log viewer** under Help, over the crash log the program
  was already writing.

### Fixes worth naming

- Captions saved in CP932 (Windows Notepad's ANSI mode on a Japanese
  system) were read lossily and written back as UTF-8, destroying the
  original text. Decoding now tries UTF-8, then CP932, with UTF-16
  recognised by its byte-order mark, and a write is refused outright
  if the existing content could not be decoded.
- A caption wrapped across lines produced one tag containing a line
  break instead of two tags.
- Blacklisted tags were reachable through their aliases, and tags
  containing a colon (`:d`, `:o`) could not be blacklisted at all.

## 2.0.0

First public release of the rebuild. Version 1 was the original
prototype, released separately under MIT; this is a different codebase
under different terms, which is what the major version says.

### Dataset health

- **Statistics window rebuilt around the dataset, not the walk.** A new
  tab charts caption length against the token limits that actually
  bite, tags per image (uneven caption density is a real flaw nothing
  else surfaces), how often each tag appears, and your vocabulary
  measured against the base-model snapshot you selected. Folders and
  their kohya repeat counts appear only when there is more than one
  folder or a repeat to report — a panel that is always present and
  usually blank reads as broken.
- **Co-occurrence now compares instead of listing rare tags.** The old
  "least often appears with" pane was noise by construction: the bottom
  of a long tail is arbitrary. It is replaced by two panes showing
  where your captions and Danbooru's habits diverge — pairings you use
  far more than the site does (the signature of what you are teaching,
  or a correlation about to be baked into it), and pairings the base
  model expects that your captions leave unwritten.

### Speed

Three separate faults, each found by measuring rather than reading:

- The image cache swept its whole directory after **every** write. The
  cost was the measurement, not the deletion — learning the total size
  means stat-ing every file, and that happened before anything could be
  deleted. A cache well under its cap paid ~100 ms per write and threw
  nothing away. Eviction now runs on an idle timer.
- Every keypress restyled **every** row in the queue. A decision changes
  two rows; on a 1,200-image queue it restyled 1,201. Now 3, and flat
  as the dataset grows: 55 ms per keypress at 2,400 images became 5 ms.
- `Path.relative_to` was about 40% of the time to open a folder,
  building 24,000 intermediate objects. Folder open is roughly a third
  faster.

### Interface

- A left-edge accent bar marks the row the program is working on,
  distinct from the click highlight. Its colour is derived per theme so
  it clears 3.5:1 contrast against every row state in all seven themes
  — two of which fail with their own accent unmodified.
- Preferences groups carry internal headings, and the export settings,
  which had been split across ninety lines of unrelated controls, are
  together.
- Downloaded posts can be named by timestamp, tags or artist rather
  than post id alone, and downloading no longer opens a folder window.
- Group mode starts the group walk immediately instead of waiting for a
  header to be clicked.
