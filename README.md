# Deepbooru TagWalker

A simple tool for anyone who takes dataset quality seriously.

---

✅## What It Does

Most tagging tools are image-centric — you open an image, then edit its tags. TagWalker flips that around.

This tool assumes your dataset is already loosely tagged — whether by an auto-tagger or manually. **Its purpose is not to generate tags from scratch, but to increase the accuracy of what's already there, one tag at a time.**

Tag list is generated exclusively from tags already present in your dataset — nothing external, nothing added. You pick a tag. The program walks you through every image in your dataset, one by one, asking: does this image have this tag correctly applied? Yes or No. Then it moves to the next image automatically.

By the time you finish a tag, you've seen it against every single image in your dataset — consistently, in sequence, without losing your place. No clicking around. No forgetting which images you already checked.

It's a small idea, but nothing else really does it this way.

---

🎁 ## Who It's For

Anyone training LoRA or fine-tuning models on Deepbooru-style tagged datasets who wants to be confident their tags are actually correct — not just present.

Especially useful when:
- Your dataset is large (hundreds of images)
- You're auditing tags added by an auto-tagger
- **You want consistency across a specific tag before training**

---

📜 ## Features

- Tag-first sequential workflow
- Image queue sidebar with full color coding — green (yes), red (no), orange (skipped), blue (current)
- Undo / Back button that correctly restores queue order
- Skip images and return to them later
- Click any image in the queue to jump directly to it
- Zoom popup for inspecting image detail
- Handles large datasets without lag

---

🔷 ## Download

Go to the [Releases](../../releases) page and download the `.exe`. No install, no Python required — just run it.

> **Note:** Windows may show a SmartScreen warning on first launch. This is normal for unsigned indie software. Click **More info → Run anyway**.

---

🔄 ## Usage

1. Launch the program
2. Open your dataset folder (folder containing image + `.txt` tag file pairs) [Both must be present in order for the program to load the lists correctly.]
3. Select a tag from the left sidebar to begin
4. Press **Yes** or **No** for each image — tag files update instantly and written in text file
5. Use **Back** to undo (undo effect reflects in text file instantly), **Skip** to defer uncertain images
6. When a tag is complete, the next one loads automatically

---

🔣 ## Dataset Format

Standard Stable Diffusion / Deepbooru format:

```
dataset/
  image001.jpg
  image001.txt    ← "1girl, solo, outdoors, ..."
  image002.png
  image002.txt
```

---

🔶 ## Known Issues & Limitations

**Missing features**

- No tag search or filter — all tags load into the sidebar as-is
- No save/resume feature — progress is not remembered between sessions, so large projects require manual tracking of where you left off
- No confirmation when accidentally clicking a different tag mid-session — the program will switch context and your current progress tracking is lost

**Quirks**

- When all tags are fully processed, the program automatically jumps back to the top of the tag list rather than showing a completion message — manually verify all tags are marked done before closing
- The current image highlight in the queue sidebar can occasionally behave unexpectedly after manual jumps

These are planned for future versions. For now the core workflow — sequential per-tag reviewing across an entire dataset — works reliably.

---

🌄 ## Origin

This is my first ever vibe coding project. I have zero programming knowledge.

The initial version was built entirely through prompting — using Qwen 3.6 Q4 for the first draft, then Claude Sonnet 4.6 for refinement, debugging, and performance work. Every line of code was AI-generated. I had been tagging datasets for years using other tools, but always wanted something tag-centric that could genuinely improve the accuracy of the process.

Releasing it in case someone else finds it useful — that would bring me joy. If you improve it, share your version freely.

---

## License

MIT — credit appreciated, freedom guaranteed.

---

Readme.md written by Claude Sonnet 4.6. (Supervised by human.)
