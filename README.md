# Deepbooru TagWalker

**A fast, image-first way to review and fix the tags across an entire LoRA dataset — one image at a time.**

You see an image. You see one of its tags. You press **Yes** or **No**. The caption file is rewritten instantly, and the next image appears. That is the whole loop, and it is built to be fast enough to take a folder of a thousand images from "auto-tagged and messy" to "checked and clean" in one sitting.

If you have played Civitai's *Knights of the New Order* image-rating game, the rhythm is familiar: image up, quick decision, next — except here you are cleaning your own training data instead of moderating a queue.

![TagWalker](images/1_hero.png)

---

## The core loop

This is the part that matters, so it is not hidden below.

**Pick a tag. Judge it everywhere.** Click a tag in the list and TagWalker walks you through every image that carries it, asking one question: *does this tag belong on this image?* **Yes**, **No**, **Skip**. Because you are judging the *same tag* across many images, your eye stays calibrated and you move quickly — far faster than opening caption files one by one.

**Or go image by image.** Prefer to work through images rather than tags? Do that instead — review each image's full caption in turn.

**Every answer writes to disk immediately.** No "save" step, no batch commit to forget. The `.txt` next to the image is updated the moment you decide, and the write is atomic, so a crash can never leave a half-written caption.

**Two settings make it much faster:**
- **Auto-confirm** pre-answers *Yes* for tags the auto-tagger already applied, leaving you to check only what it might have gotten wrong.
- **Filters** narrow the queue to just the images still undecided for a tag — turning a full review into a quick spotting pass.

**Nothing is guesswork.** A completion percentage tracks how much of the dataset you have actually decided, so "done" is a fact, not a feeling. Stop whenever you like — the number just makes it a choice.

---

## Move through images fast

<details>
<summary><b>The image view, the queue, and batch actions</b> (click to expand)</summary>

<br>

**The image**
- Zoom by scrolling (it zooms toward your cursor), drag to pan, one key to refit, double-click or Esc to close the zoom. An on-screen hint shows the controls.
- See any image's dimensions, size, and format.

**The queue (the list of images)**
- **Sort** by name (A→Z, Z→A) or by tag count (most / fewest).
- **Filter** by decision state (pending, done, skipped) or by a selected tag — e.g. show only the images still pending for one tag.
- **Show or hide** images that have no caption file yet.
- **Grid view:** see the queue grouped (for example by subfolder) as a grid of up to 25 images, for quick batch decisions.
- **Select many at once** — a handful of images or the entire queue — and act on them together, as a single undo step.
- **Scroll to navigate:** hold a key and scroll over the queue to step through images one by one.

**The tag list**
- Every tag in the dataset, grouped by folder, each with its image count.
- Sort tags within a folder by name or by how many images use them.
- Click any tag to start walking it. Mark a tag "done" by hand when you have finished with it.
- **Front-lock trigger tokens:** pin your trigger words to the front of every caption, in the order you choose.

**Keyboard-driven**
- Yes, No, Skip, Back — all on keys, all rebindable in Settings. The whole review loop can be done without the mouse.

</details>

---

## Know your tags before you train

These are the tools that make TagWalker more than a fast editor — they tell you things about your captions that you cannot see by looking, and they matter most right before you start a training run. The important ones are first.

<details open>
<summary><b>Does your base model even know this tag? (the Tag Referencer)</b></summary>

<br>

Danbooru's tag vocabulary changes over time. Tags get renamed, retired, or invented years after your base model finished training — and a tag your model has never seen just wastes caption space.

Look up any tag and see how many Danbooru posts used it across **four points in time**: 2017, the Pony era (2023), the Illustrious/NoobAI era (2024), and now. For example, `hair_intakes` went 353 → 54,519 → 109,971 → 198,624 posts — so an older base model barely saw it. `ai-generated` went 0 → 0 → 6,202 → 15,010 — so on anything trained before 2024, it is a word the model has never met.

A one-line verdict tells you whether a tag is stable, was renamed, or is too new for your base model. The Danbooru wiki page and example images load alongside it (optional, off by default), so you can see what a tag actually means rather than guessing from its name.

**Compare mode** puts two tags side by side — their post counts and how much of their usual company they share — to answer the real question: *are these two tags interchangeable, or not?*

All of this works fully offline from bundled data. The example images are the only part that touches the network, and only if you turn it on.

![Tag Referencer](images/2_referencer.png)

</details>

<details open>
<summary><b>Which tags should you cut? (the Pruning Advisor)</b></summary>

<br>

When your captions run long and you need to trim, this ranks every tag by how worthwhile it is to remove — in three tiers — and **explains each verdict**. It changes nothing on its own; it hands you a sorted list to act on.

It knows things a plain word-counter does not:
- **A rare tag your base model already knows is still useful.** `elf` on three images cannot teach the model what an elf is — but it does not need to. It names a feature so that feature attaches to *the tag* instead of to your trigger word. That works from a single image.
- **A broad tag covered by narrower ones is redundant.** If every image with `weapon` also has `sword` or `gun`, the broad tag is mostly dead weight — and it shows you the images where the broad tag stands alone, so you can check before cutting.
- **Rarity is judged against the era your base model came from**, not against your small dataset.

You can export the ranked report, and optionally a briefing block that hands the data to a language model to answer follow-up questions.

![Pruning Advisor](images/3_pruning.png)

</details>

<details>
<summary><b>Count tokens with the right ruler for your trainer</b></summary>

<br>

Captions have a token limit, and past it the tail is silently cut — worse, with caption shuffling on, a *different* tail is cut every step, so the model gets contradictory supervision. TagWalker counts each caption's length using the **actual tokeniser your trainer uses**, because the correct ruler depends on the model:

- **SDXL / Illustrious** — CLIP (limits 75 / 150 / 225)
- **Flux.1** — T5-XXL
- **Flux.2 [klein]** — Qwen3
- **Flux.2 [dev]** — Mistral (Tekken)

Pick your target and the limit, warning thresholds, and per-tag costs all follow it. Over-limit captions get a marker that sorts them to the top of the folder. Counting a Flux caption with CLIP's ruler was quietly wrong before; this gets it right. (The Flux tokenisers load only when selected, so they add nothing to startup.)

![Dataset health](images/4_stats.png)

</details>

<details>
<summary><b>See the shape of the whole dataset (Statistics & Health Check)</b></summary>

<br>

**Health Check** scans the whole folder and flags, each independently:
- Images with **no caption file** at all.
- Caption files that exist but are **empty** — easy to miss by eye in a thousand-image set.
- Images at an **unusual resolution** (too small to bucket, or an extreme aspect ratio) or that cannot be read.
- Images that are **not perfectly square** (for non-bucketed training; off by default).
- **Stray caption files** with no matching image.

**Statistics** shows the dataset at a glance: headline numbers, a tag-frequency bar chart, which tags appear together, and how caption lengths sit against the token limit.

</details>

---

## Fix things in bulk

<details>
<summary><b>Batch tag operations, auditing, and conflict rules</b> (click to expand)</summary>

<br>

Every operation here is atomic and undoable as a single step. Your original **images are never modified** — the only writes to your dataset are caption files.

**Batch tag edits**
- **Rename** a tag across every image, merging automatically where the new name already exists.
- **Split** one tag into several, or **delete** a tag everywhere it appears.
- **Clean up duplicate tags** within captions across the whole dataset.
- **Convert** tag word-separators between underscores and spaces in bulk, with a preview you tick and a guard against format-breaking changes.

**Tag audit**
- Check every tag against a bundled **201,269-tag Danbooru database** to catch typos, deprecated tags, and made-up ones — with fixes you apply and undo in one shot.
- Keep an **exceptions list** of tags the audit should never flag (studio names, original-character tags, deliberate conventions).

**Conflict rules**
- Define tags that **must not appear together** (like `indoors` with `outdoors`) or tags that **require another** (like `cat_ears` needing `animal_ears`). Rules understand aliases.
- Scan the dataset for violations and fix them from one dialog — with a zoomable image viewer, checkboxes to remove several tags at once, and per-rule batch controls. Nothing changes until you choose.

</details>

<details>
<summary><b>Crop and prep images to training sizes (the Image Editor)</b></summary>

<br>

A separate window for getting images ready to train, which **writes to its own output folder and never touches your originals**.

- **Batch mode:** crop many images to training-bucket sizes at once, with a running report of what each image needs (already a bucket size, crop to shape, too large, unreadable).
- **One-by-one mode:** confirm each crop yourself — **right-click and drag to move the image** within the frame, scroll to resize. The input and output folder pickers each remember their own last location.
- **Kohya-style aspect-ratio bucketing** (2048 / 1536 / 1024 / 768 / 512, default 1024).
- **Colour jitter:** apply controlled, re-rollable colour variation.

</details>

<details>
<summary><b>Browse Danbooru for reference (the Post Browser)</b></summary>

<br>

Search Danbooru posts for reference while you tag (only if you have turned network lookups on), with rating filters and a bookmark manager to save posts you want to revisit. It respects your tag blacklist before any search.

</details>

---

## Sessions, safety, and settings

<details>
<summary><b>How your work is saved, and what you can configure</b> (click to expand)</summary>

<br>

**Sessions**
- Save and reload everything: every decision, every tag-status change, your filters and sort order, your front-locked tokens, and exactly where you left off.
- **Autosave** runs on a timer and after a set number of actions, so closing mid-review loses nothing.
- If the dataset changed on disk between sessions, TagWalker **reconciles automatically** — dropping references to deleted images and vanished tags, and telling you what changed, rather than breaking.
- Saves are atomic: a failed write never corrupts your previous session.

**Safety**
- **Your original images are never modified.** Caption `.txt` files are the only writes, and every one is atomic and undoable.
- **Undo covers every destructive action**, batch operations included, plus an "undo everything" option and an action log.
- **The network is off by default.** Nothing leaves your machine unless you enable Danbooru lookups — and even then, only a tag name or post id is ever sent, never a caption, filename, or path.
- **Load several folders at once** and walk them as one dataset; the window title shows what is loaded.

**Settings**
- Seven themes, fully rebindable keyboard shortcuts, and defaults for sort, filter, orphan visibility, auto-confirm, co-occurrence hints, and grid size.
- **Co-occurrence hints** while you walk (*images with `maid` usually also have `maid_headdress`*) — a gentle nudge toward tags you might be missing, toggleable and never automatic.
- Robust caption reading (UTF-8, then CP932, with UTF-16 detected automatically) — a file it cannot decode is never overwritten.

</details>

---

## Two things it does not do

**It does not tag images for you.** There is no model inside. TagWalker is for *reviewing and fixing* the tags an auto-tagger already produced — it makes that fast, but it does not replace the tagger.

**It does not predict your training results.** Everything it reports is a count of something real. Final quality depends on your learning rate, optimiser, rank, and luck — and a confident wrong number would be worse than none.

---

## Download & run

Grab `TagWalker.exe` from [Releases](../../releases). No installer, no Python, no dependencies — one file, double-click, done. Windows 10 or 11, 64-bit.

Settings, bookmarks, and the image cache live in `%APPDATA%\TagWalker\`. The error log lives in `%USERPROFILE%\.tagwalker\` — or open it from **Help → Diagnostic Log** without hunting for it.

<details>
<summary><b>Dataset format, antivirus note, and limitations</b></summary>

<br>

**Dataset format** — the convention every trainer uses:

```
my_dataset/
  image_001.png
  image_001.txt      <- comma-separated tags
  image_002.jpg
  image_002.txt
```

Images without a caption file are shown and can be given one. Several folders can be loaded and walked as one dataset.

**A note on antivirus** — Windows Defender or SmartScreen may warn about `TagWalker.exe`. This is normal for unsigned PyInstaller executables; a code-signing certificate costs hundreds a year and this is a free project. The source is here — build it yourself with `build.bat` if you prefer.

**Honest limitations**
- **Windows only** in practice. The code is cross-platform, but only the Windows build is tested and shipped.
- **Danbooru conventions.** Everything reference-related assumes booru-style tags; natural-language captioning is out of scope.
- **The bundled data is dated.** The "current" snapshot is fixed at build time; tags newer than that are correctly reported as too new.
- **Danbooru lookups are optional and off by default.** With them off, the program makes no network requests at all.
- **Video posts are not played.** Animated GIF and WebP are shown; MP4 and WebM show a still frame with a download option.

</details>

---

## Built by an AI — the honest version

This program was written, in full, by **Claude Opus** (Anthropic). Not "AI-assisted" — the state engine, the file-writing layer, the save format, the interface, and the 92-suite regression harness were all authored by the AI across a long series of design sessions. The v1 prototype was vibe-coded with Qwen and Claude Sonnet; v2 is a complete rebuild.

The publisher writes no code. What they contributed is the thing that mattered: **knowing what was wrong.** Nearly every bug that made this program usable was found by a human using it and reporting what felt off — thumbnails that would not open, a window that hid behind the main one, a program that stuttered on every page of images. The AI's own tests passed through all of them; several of those tests asserted the bug was correct behaviour.

The engineering is the AI's. The judgement about what a caption tool should *be* is the publisher's.

---

## License — read before you fork

TagWalker is **source-available, non-commercial, and share-alike** (full terms in [LICENSE](LICENSE)). In plain words: use it free, study it, modify it, and share your modified versions — with credit, under these same terms. Selling it or folding it into a commercial or proprietary product requires the publisher's written permission.

The goal is simple: keep TagWalker free and improvable by individuals and small creators, and keep it from being quietly swallowed into something closed. Tinkerers welcome; companies, talk to the publisher first.

> This is intentionally **not** an OSI "open source" license — it restricts commercial use on purpose. Please don't describe it as open source. (The v1 prototype remains under its original MIT license; v2 is a new work under the terms above.)

---

## If it saved you time

TagWalker is free and stays free — no ads, no telemetry, no "Pro" tier holding features hostage. If it spared you an evening of squinting at captions and you would like to say thanks, there is a tip jar. Entirely optional, quietly appreciated, and it keeps a strange little one-human-zero-programmers project alive.

- Ko-fi: *(your link)*

---

## Bug reports

A good bug report is a gift: what you did, what you expected, what happened — plus the log from **Help → Diagnostic Log** if anything misbehaved. The most valuable things to stress-test: sessions across dataset changes, large batch operations, and real Windows file-locking (cloud sync, antivirus).

*Published and directed by Elliezrah · engineered by Claude Opus (Anthropic).*
