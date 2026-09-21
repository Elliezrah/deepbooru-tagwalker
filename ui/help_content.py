"""
ui/help_content.py

The content behind Help ▸ User Guide, organized as categories for the
split-view help browser (ui/help_browser_dialog.py): the left pane lists
these categories, clicking one shows its body in the scrollable right
pane.

This replaces the old linear "Getting Started" paged walkthrough. The
walkthrough was good but it was ONE path (do step 1, then 2, …); a
categorized browser lets someone jump to what they need — "how do I
control it", "what does the Pruning Advisor do", "what order should I
work in" — instead of paging through all of it. The recommended workflow
is preserved intact as its own category ("Recommended workflow"), since
the order-to-do-things-in is genuinely useful and was the old guide's
whole point.

Each entry is (category_title, body_text). Bodies are plain text with
blank lines between paragraphs; the browser renders them read-only and
scrolls when long. {placeholders} in the control sections are replaced
with the user's CURRENT keybinds at display time by the browser, so the
guide stays correct when keys are rebound.
"""
from __future__ import annotations

# (category title, body). Order here is the order shown in the left list.
HELP_SECTIONS: list[tuple[str, str]] = [

    ("Welcome",
     "TagWalker is a fast, image-first way to review and fix the tags "
     "across a whole training dataset \u2014 one image at a time.\n\n"
     "You see an image, you judge one of its tags (Yes / No / Skip), and "
     "the caption file is rewritten instantly. That is the core loop; "
     "everything else is here to support it.\n\n"
     "Pick a topic on the left:\n\n"
     "\u2022  Controls \u2014 how to drive the program.\n"
     "\u2022  The tools \u2014 what each feature does.\n"
     "\u2022  Recommended workflow \u2014 the order to work in with a "
     "fresh dataset.\n"
     "\u2022  Tips and Safety \u2014 getting faster, and protecting your "
     "work.\n\n"
     "New here? Read Controls, then Recommended workflow."),

    # ===== CONTROLS =================================================

    ("Controls \u00b7 The core loop",
     "TagWalker reviews captions one decision at a time.\n\n"
     "Click a tag in the tag list. The queue fills with every image that "
     "has that tag, and the image panel shows the first. Your question: "
     "does this tag belong on this image?\n\n"
     "\u2022  {yes} \u2014 Yes, keep it. Saved to disk, next image.\n"
     "\u2022  {no} \u2014 No, remove it. Saved to disk, next image.\n"
     "\u2022  {skip} \u2014 Skip, decide later.\n"
     "\u2022  {back} \u2014 undo the last decision and step back.\n\n"
     "Every answer writes to the caption file the instant you press it "
     "\u2014 there is no save step. A completion bar tracks how much of "
     "the dataset you have actually decided.\n\n"
     "You can also work image by image instead of tag by tag \u2014 just "
     "step through images and review each full caption.\n\n"
     "Every key here is rebindable in Settings."),

    ("Controls \u00b7 Moving around",
     "\u2022  Up / Down arrows step to the previous / next image while "
     "the image queue is focused.\n"
     "\u2022  Hold {wheel_modifier} and scroll the wheel over the queue "
     "to flip through images quickly.\n"
     "\u2022  Click any image in the queue to jump to it.\n\n"
     "Zoom the image:\n"
     "\u2022  Scroll the wheel over the image to zoom toward the "
     "cursor.\n"
     "\u2022  Drag to pan while zoomed.\n"
     "\u2022  {close_zoom} or double left-clicking closes the zoom.\n\n"
     "Narrow what you see with the queue filter \u2014 for example, show "
     "only the images still undecided for the current tag, turning a "
     "full pass into a quick spotting pass. Sort the queue by name or by "
     "tag count."),

    ("Controls \u00b7 Batch decisions",
     "To apply one decision to many images at once:\n\n"
     "1.  Click a tag to load its images into the queue.\n"
     "2.  In the queue, Shift+click to select a range, or Ctrl+click to "
     "pick individual images \u2014 or select the whole queue.\n"
     "3.  Press {yes} or {no}. The decision applies to every selected "
     "image at once, as a single undo.\n\n"
     "This is the fast way to fix a tag that is wrong (or right) across a "
     "whole group without visiting each image one by one."),

    ("Controls \u00b7 Right-click menus",
     "Right-click an image in the queue for:\n\n"
     "\u2022  Properties\u2026 \u2014 dimensions, file size, format, and "
     "token count.\n\n"
     "\u2022  Meta info\u2026 \u2014 the prompt, negative prompt, and "
     "settings the image was generated with, read from the image's "
     "embedded metadata. Works for Automatic1111 / Forge, ComfyUI, and "
     "EXIF (JPEG/WebP). Useful for checking how an image was made, or "
     "lifting a prompt to reuse.\n\n"
     "In the Image Editor's one-by-one view, right-click and drag to "
     "move the image within the crop frame.\n\n"
     "Right-click an tag category for:\n\n"
     "\u2022  Quick tag referencer access\u2014 For quicker look ups of tag"
     "definition.\n\n"
     "\u2022  Mark/Skip control\n\n"
     "\u2022  Rename/Delete tag category"),

    # ===== THE TOOLS ===============================================

    ("Controls \u00b7 The file-state panel",
     "The file-state panel (lower area) shows the FULL caption of the "
     "current image \u2014 every tag on it, not just the one you are "
     "walking. It is how you fix a single image completely.\n\n"
     "\u2022  Each tag is a clickable chip. Click a tag to Remove or "
     "Rename it on this image only.\n"
     "\u2022  The [+ Add] box adds a new tag to this image.\n"
     "\u2022  Right-click a tag chip to look it up in the Tag "
     "Referencer.\n\n"
     "Use this when you want to perfect ONE image rather than judge one "
     "tag across many. Walk to the image (or click it in the queue), "
     "then read its whole caption here and add, remove, or rename tags "
     "until that image is exactly right. Every change writes to disk at "
     "once and is undoable, the same as a Yes/No decision.\n\n"
     "So there are two ways to work, and you can mix them: judge one tag "
     "across the whole set (the walk), or fix one image top to bottom "
     "here."),

    ("Controls \u00b7 Multi-select tags",
     "Normally clicking a tag starts walking it. Multi-select tags "
     "(the button above the tag list) switches the list into checkboxes "
     "so you can pick SEVERAL tags at once, then act on all of them "
     "together.\n\n"
     "Turn it on, tick the tags you want, and the queue shows the images "
     "those tags cover. A single Yes / No then applies to that whole set "
     "\u2014 useful for stamping or clearing a group of related tags in "
     "one motion.\n\n"
     "This is the tag-side counterpart to selecting several images in "
     "the queue: there you pick images and act on one tag; here you pick "
     "tags and act across their images. Turn it off to return to the "
     "normal one-tag walk."),

    ("Controls \u00b7 Queue filter, sort & buttons",
     "The small bar above the image queue controls what the queue shows "
     "and in what order.\n\n"
     "Sort:\n"
     "\u2022  Name (A\u2013Z / Z\u2013A) \u2014 alphabetical by filename.\n"
     "\u2022  Tag count (most / fewest) \u2014 surfaces the over- or "
     "under-tagged images.\n\n"
     "Filter (which images appear):\n"
     "\u2022  Pending / Done / Skipped \u2014 by decision state, so you "
     "can show only what is still undecided for the current tag and turn "
     "a full review into a quick spotting pass.\n"
     "\u2022  Orphan visibility \u2014 show or hide images that have no "
     "caption file yet.\n\n"
     "Filter and sort are the biggest levers for working quickly on a "
     "large set: narrow to what still needs a decision, order it so the "
     "important cases come first, and you spend your attention only "
     "where it is needed."),

    # ===== SESSIONS, LOADING & PRESETS (added) =====================

    ("The tools \u00b7 Tag Referencer",
     "Look up any tag and see whether your base model actually knows "
     "it.\n\n"
     "\u2022  Post counts across four eras \u2014 2017, the Pony era "
     "(2023), the Illustrious/NoobAI era (2024), and now \u2014 so you "
     "can tell if a tag is new, established, or something your model "
     "never saw.\n"
     "\u2022  A one-line verdict: stable, renamed since, or too new for "
     "your data.\n"
     "\u2022  Aliases (both directions) and the tags that most often "
     "appear alongside this one.\n"
     "\u2022  Compare mode puts two tags side by side to answer \u201care "
     "these interchangeable?\u201d\n\n"
     "All offline. The optional online half adds the Danbooru wiki page "
     "and example images, and is off by default."),

    ("The tools \u00b7 Pruning Advisor",
     "Ranks every tag by how worthwhile it is to cut, in three tiers, "
     "and explains every verdict. It changes nothing itself \u2014 it "
     "hands you a sorted list to act on.\n\n"
     "\u2022  Tier 1 is safe to cut: read the reasons, delete the ones "
     "you agree with.\n"
     "\u2022  Tier 2 needs your decision.\n"
     "\u2022  Tier 3 is the tool showing its working \u2014 if you only "
     "open the \u201cyour goals disagree\u201d group there, it is "
     "behaving as intended.\n\n"
     "Set your training goal first: a character LoRA and a style LoRA "
     "want opposite things from the same tag.\n\n"
     "Note: a rare tag your base model already knows can still be worth "
     "keeping \u2014 it names a feature so that feature attaches to the "
     "tag instead of your trigger word, which works from a single "
     "image."),

    ("The tools \u00b7 Token counter",
     "Counts each caption against your trainer's real limit, using the "
     "tokeniser that trainer actually uses \u2014 because the right "
     "ruler depends on the model:\n\n"
     "\u2022  SDXL / Illustrious \u2014 CLIP (75 / 150 / 225)\n"
     "\u2022  Flux.1 \u2014 T5-XXL\n"
     "\u2022  Flux.2 Klein \u2014 Qwen3\n"
     "\u2022  Flux.2 Dev \u2014 Mistral (Tekken)\n\n"
     "Krea 2 and Anima use the Flux.2 Klein setting \u2014 they share "
     "its Qwen3 tokeniser.\n\n"
     "Pick your target and the limit, thresholds and per-tag costs all "
     "follow it. Over-limit captions get a marker so they sort to the "
     "top of the folder.\n\n"
     "Why it matters: past the limit the caption's tail is cut, and with "
     "caption shuffling on a DIFFERENT tail is cut every step \u2014 "
     "contradictory supervision that quietly degrades training."),

    ("The tools \u00b7 Statistics & Health Check",
     "Dataset Health Check scans the whole folder and flags, each on its "
     "own:\n\n"
     "\u2022  Images with no caption file.\n"
     "\u2022  Caption files that exist but are empty.\n"
     "\u2022  Images at an odd resolution, or that cannot be read.\n"
     "\u2022  Images that are not perfectly square (off by default).\n"
     "\u2022  Stray caption files with no image.\n\n"
     "Statistics shows the dataset at a glance: caption-length "
     "distribution against the token limit, how evenly your images are "
     "described, tag frequency, and which tags appear together. You can "
     "export the tag statistics to a file."),

    ("The tools \u00b7 Bulk edits",
     "Changes that touch the whole dataset, each atomic and a single "
     "undo:\n\n"
     "\u2022  Rename a tag everywhere (merging automatically where the "
     "new name already exists).\n"
     "\u2022  Split one tag into several, or delete a tag everywhere.\n"
     "\u2022  Find and remove duplicate tags within captions.\n"
     "\u2022  Convert tag separators between underscores and spaces, with "
     "a preview and a guard against format-breaking edits.\n\n"
     "Your images are never touched \u2014 only caption files are "
     "written."),

    ("The tools \u00b7 Tag audit & conflict rules",
     "Tag audit checks every tag against a bundled 201,269-tag Danbooru "
     "database and offers fixes for typos, deprecated tags and unknown "
     "inventions \u2014 applied and undone in one step. Keep an "
     "exceptions list for deliberate tags (studio names, OC tags).\n\n"
     "Conflict rules catch captions that say two things at once. Two "
     "kinds:\n\n"
     "\u2022  EXCLUSION \u2014 tags that must never appear together "
     "(\u201cindoors\u201d with \u201coutdoors\u201d).\n"
     "\u2022  REQUIREMENT \u2014 one tag that implies another "
     "(\u201ccat_ears\u201d needs \u201canimal_ears\u201d).\n\n"
     "Rules are alias-aware and unconditional \u2014 there is no \u201conly "
     "when solo\u201d, so on a mixed dataset a rule like long_hair vs "
     "short_hair will also flag legitimate two-character images. That is "
     "a reason to REVIEW, not to avoid: every violation has a View-image "
     "button, and nothing changes until you choose. An over-reporting "
     "rule costs time, not accuracy \u2014 just never fix in bulk "
     "without looking."),

    ("The tools \u00b7 Image Editor",
     "A separate window for getting images ready to train. It writes to "
     "its own output folder and never touches your originals.\n\n"
     "\u2022  Batch mode: crop many images to training-bucket sizes at "
     "once, with a report of what each needs.\n"
     "\u2022  One-by-one mode: confirm each crop yourself \u2014 "
     "right-click and drag to move the image in the frame, scroll to "
     "resize.\n"
     "\u2022  Kohya-style aspect-ratio bucketing (2048 / 1536 / 1024 / "
     "768 / 512).\n"
     "\u2022  Colour jitter: controlled, re-rollable colour variation.\n\n"
     "The input and output folder pickers each remember their own last "
     "location."),

    # ===== RECOMMENDED WORKFLOW ====================================

    ("Tools \u00b7 Exporting for an AI to analyse",
     "The Pruning Advisor and the tag statistics can both be exported to "
     "a file and handed to a language model (ChatGPT, Claude, etc.) for a "
     "second opinion \u2014 useful when you want reasoning about your "
     "whole tag set, not just the program's ranking.\n\n"
     "In the Pruning Advisor, Export to File writes the ranked verdicts, "
     "and you can attach per-image caption data:\n\n"
     "\u2022  Caption export mode chooses what per-image data goes in \u2014 "
     "none, a full per-image dump, or an inverted view.\n"
     "\u2022  \u201cInclude AI instructions\u201d adds a block at the top "
     "telling the model how to read the data safely \u2014 to match "
     "WHOLE tags (not substrings), resolve aliases, and intersect "
     "correctly \u2014 so it does not, say, treat \u201chair\u201d as "
     "part of \u201cshort_hair\u201d.\n\n"
     "From Statistics you can export the tag frequency table \u2014 every "
     "tag with its counts \u2014 as a plain file to paste in as well.\n\n"
     "How to use it: export with AI instructions on, paste the file into "
     "the model, and ask your question \u2014 \u201cwhich of these tags "
     "are redundant for a style LoRA?\u201d, \u201cwhat is "
     "over-represented?\u201d, \u201cwhat am I missing?\u201d. The "
     "instructions block keeps the model from making the whole-tag "
     "mistakes that make tag analysis go wrong. Treat its answer as "
     "advice to weigh, not an order \u2014 the same as the Pruning "
     "Advisor's own verdicts."),

    # ===== SETTINGS (added) ========================================

    ("Workflow \u00b7 The order to work in",
     "TagWalker has a dozen tools and no single forced path. This is the "
     "order that works with a fresh dataset. The steps that follow "
     "(Look, Fix, Trigger, Prune, Walk, Check) each have their own topic "
     "below.\n\n"
     "1.  Look \u2014 run the checks, change nothing yet.\n"
     "2.  Fix the mechanical errors \u2014 in bulk.\n"
     "3.  Add your trigger word.\n"
     "4.  Prune.\n"
     "5.  Walk the tags \u2014 the actual review.\n"
     "6.  Check before you train.\n\n"
     "Steps 1\u20134 take about twenty minutes whether you have sixty "
     "images or two thousand \u2014 that part does not scale. Step 5 is "
     "the one that scales with your dataset, and how far you take it is "
     "your call (see \u201cMaking the walk faster\u201d)."),

    ("Workflow \u00b7 1. Look before you change",
     "Open your folder, run two things, and change nothing yet.\n\n"
     "Tools \u2192 Dataset Health Check finds captions that are missing, "
     "empty or unreadable, and images whose resolution will land oddly "
     "in training.\n\n"
     "Statistics \u2192 Dataset health shows the shape: how long your "
     "captions run against the token limit, how evenly your images are "
     "described, and how much of your vocabulary the base model has "
     "actually seen.\n\n"
     "Five minutes here tells you which of the next steps you actually "
     "need."),

    ("Workflow \u00b7 2. Fix the mechanical errors",
     "These changes have no judgement in them, so do them first and in "
     "bulk.\n\n"
     "Tools \u2192 Tag Audit checks every tag against the Danbooru list "
     "and offers fixes for typos, renamed tags and aliases \u2014 "
     "applied and undone in one step.\n\n"
     "Tools \u2192 Find Duplicate Tags removes tags repeated within a "
     "caption.\n\n"
     "Then delete anything the tagger emitted that you know is junk. "
     "Every batch operation is a single undo.\n\n"
     "Next, catch contradictions with Tools \u2192 Conflict Rules "
     "(see the Conflict rules topic above) \u2014 scan and read the "
     "violations before fixing anything."),

    ("Workflow \u00b7 3. Add your trigger word",
     "If your tagger did not insert it, Tools \u2192 Trigger Token "
     "will.\n\n"
     "A fast route once it exists on one image: the tag appears in the "
     "tag tree \u2014 select it, highlight every image in the queue, and "
     "one Yes applies it to all of them.\n\n"
     "The trigger is the one tag the Pruning Advisor will never suggest "
     "cutting. Being on every image is what makes an ordinary tag "
     "droppable; for the trigger, that ubiquity is the entire "
     "mechanism.\n\n"
     "You can also front-lock the trigger (in the tag list) so it is "
     "pinned to the front of every caption."),

    ("Workflow \u00b7 4. Prune",
     "Tools \u2192 Tag Pruning Advisor ranks every tag by how much it is "
     "worth cutting, and explains each verdict. It writes nothing.\n\n"
     "Read the Pruning Advisor topic above for how the tiers work. Set "
     "your training goal first \u2014 a character LoRA and a style LoRA "
     "want opposite things from the same tag."),

    ("Workflow \u00b7 5. Walk the tags",
     "This is the work, and how far you take it is your call.\n\n"
     "The program is built for a COMPLETE audit: a tag counts as done "
     "only when every image carrying it has a Yes or a No, and the "
     "completion percentage tracks exactly that. At 100% you know the "
     "captions are right rather than hoping they are \u2014 which is the "
     "whole reason to use a tool like this instead of trusting the "
     "tagger.\n\n"
     "A complete audit of sixty-four images is roughly a full day's "
     "work. It is achievable, and for a dataset you train repeatedly it "
     "is usually worth it. But a partial audit is a legitimate choice "
     "\u2014 see \u201cMaking the walk faster\u201d."),

    ("Workflow \u00b7 6. Check before you train",
     "Statistics \u2192 Dataset health once more, and look at caption "
     "length.\n\n"
     "Anything past the token limit gets its tail cut during training. "
     "With caption shuffling on, a DIFFERENT tail is cut every step, so "
     "the model receives contradictory supervision that never settles "
     "\u2014 which is how training quality degrades for no visible "
     "reason.\n\n"
     "Tools \u2192 Token Counter lists the offenders and can rename them "
     "so they sort to the top of the folder. Count against YOUR "
     "trainer's tokeniser (see the Token counter topic)."),

    # ===== TIPS & SAFETY ==========================================

    ("Projects \u00b7 Saving your work",
     "File \u2192 Save Session stores everything about your progress: "
     "every Yes / No / Skip decision, tag-status changes, your filter "
     "and sort settings, front-locked trigger tokens, and exactly where "
     "you left off in the walk.\n\n"
     "Save Session As\u2026 writes a new session file (keep several if "
     "you like). Autosave also runs on its own \u2014 on a timer and "
     "after a set number of actions \u2014 so closing mid-review loses "
     "nothing.\n\n"
     "A session file is small: it records your DECISIONS, not your "
     "images or captions (those live in the dataset folder). Saving is "
     "for your review progress; the captions themselves are already on "
     "disk the moment you answer.\n\n"
     "Tip: keep the session file next to its dataset \u2014 the load "
     "dialog opens there, so it is one click to resume."),

    ("Projects \u00b7 Loading a dataset",
     "Load a dataset from the File menu.\n\n"
     "\u2022  Single folder \u2014 point it at one folder of images and "
     "captions.\n"
     "\u2022  Multiple folders \u2014 pick several folders and they load "
     "and walk as ONE merged dataset. The window title shows how many "
     "folders are loaded.\n\n"
     "Subfolders are IGNORED. Only images sitting directly in the "
     "folder(s) you load are read \u2014 anything inside a nested "
     "subfolder is quietly skipped. If your images are spread across "
     "subfolders, load each of those folders explicitly with the "
     "multiple-folders option.\n\n"
     "The audit is global: once loaded, all the folders are one tag "
     "inventory, and a tag that appears in several folders is one tag in "
     "the list."),

    ("Projects \u00b7 Presets for quick loading",
     "A folder selection can be saved as a PRESET so you never have to "
     "hunt for it again.\n\n"
     "Set up which folder (or folders) make up a dataset once, save it "
     "as a named preset, and next time you load it in a click instead of "
     "navigating the file dialog. For a multi-folder dataset this is the "
     "easy way to keep the exact set together.\n\n"
     "Even for a SINGLE folder, saving it as a preset is worth it: "
     "browsing to a folder takes navigation every time; a preset is one "
     "click. If you reopen the same dataset regularly, make it a preset "
     "the first time and load it instantly after that."),

    ("Projects \u00b7 What happens when the dataset changed",
     "Sessions store your decisions by filename, so TagWalker RECONCILES "
     "the session against the folder each time you load \u2014 and tells "
     "you what changed.\n\n"
     "\u2022  Images added since you saved \u2014 picked up and shown as "
     "new, undecided.\n"
     "\u2022  Images deleted since you saved \u2014 dropped from the "
     "session; any decisions that referred to them are discarded (they "
     "have nowhere to apply).\n"
     "\u2022  A caption edited outside the program \u2014 the current "
     "caption on disk always wins; the file on disk is the truth, and "
     "the session tracks your review of it, not a copy of its text.\n\n"
     "After a load, a short summary reports exactly what was reconciled "
     "\u2014 how many images were new and how many went missing \u2014 "
     "so a dataset that changed on disk never silently breaks your "
     "session. You just see what moved and carry on."),

    ("Projects \u00b7 Progress in Statistics",
     "The completion percentage \u2014 how much of the dataset you have "
     "actually decided \u2014 is part of the session state. It reflects "
     "the decisions you have made, so it is saved and restored with the "
     "session and moves in step with your work.\n\n"
     "Because it is tied to your decisions, reconciliation keeps it "
     "honest: if images were deleted since you saved, the decisions that "
     "referred to them are dropped, and the percentage reflects what is "
     "actually left to do rather than counting work on images that no "
     "longer exist.\n\n"
     "So the number always answers \u201chow much of THIS dataset, as it "
     "is now, have I decided\u201d \u2014 not a stale figure from a "
     "previous state of the folder."),

    # ===== EXPORTING FOR AI (added) ================================

    ("Settings \u00b7 Preferences, briefly",
     "Settings (Preferences) holds the defaults and toggles. The main "
     "ones:\n\n"
     "Appearance \u2014 theme (seven of them), and the little animated "
     "indicator on/off.\n\n"
     "Defaults \u2014 how a fresh load behaves: sort order, filter mode, "
     "whether orphan (caption-less) images show, Auto-confirm on/off, "
     "co-occurrence hints on/off, and the grid size for group view.\n\n"
     "Autosave \u2014 on/off, and how often it fires (by seconds and by "
     "number of actions).\n\n"
     "Keyboard shortcuts \u2014 every key (Yes, No, Skip, Back, undo, "
     "save, navigation) is rebindable here. The User Guide always shows "
     "your CURRENT keys, so rebinding is safe.\n\n"
     "Danbooru / network \u2014 all off by default. If you enable "
     "lookups, this is where the online options live: whether images "
     "load, the image-cache size, blacklist handling, and the Discover "
     "settings. Nothing here reaches the network unless you turn lookups "
     "on.\n\n"
     "Tag audit \u2014 which Danbooru tag-list snapshot the audit checks "
     "against.\n\n"
     "Change what does not suit you and leave the rest; the defaults are "
     "chosen to be sensible."),

    # ===== DIAGNOSTIC LOG (added) ==================================

    ("Training \u00b7 How captions teach the model",
     "A quick mental model, because it explains why the tools here "
     "matter.\n\n"
     "During training the model learns by association: a tag that "
     "consistently appears with a visual feature becomes a handle for "
     "that feature. So the goal of captioning is accurate, consistent "
     "tags \u2014 the right tags on the right images \u2014 which is "
     "exactly what the walk is for.\n\n"
     "Two forces to keep in mind:\n\n"
     "\u2022  A tag on EVERY image (like your trigger word) attaches "
     "whatever is constant across the set to that tag. That is why the "
     "trigger is never pruned, and why a redundant everywhere-tag is "
     "safe to cut.\n"
     "\u2022  A tag the base model has never seen teaches slowly and "
     "wastes caption space \u2014 which is what the Tag Referencer's era "
     "counts are for.\n\n"
     "The rest of this section covers the differences between trainers, "
     "and one concrete way to save tokens on SDXL."),

    ("Training \u00b7 SDXL, Flux, Flux.2 & Krea 2",
     "Different trainers read your caption with different tokenisers and "
     "different limits \u2014 which is why the Token Counter lets you "
     "pick a target. What that means in practice:\n\n"
     "\u2022  SDXL / Illustrious \u2014 CLIP tokeniser, and the practical "
     "budget is 75 tokens per chunk (225 across three chunks). This is "
     "the TIGHTEST budget, so token efficiency matters most here. Booru "
     "tags are the native language.\n\n"
     "\u2022  Flux.1 \u2014 T5 tokeniser, a much larger 512-token budget, "
     "so length is rarely the constraint. It also understands natural "
     "language, not only tags.\n\n"
     "\u2022  Flux.2 (Klein and Dev) \u2014 Qwen3 and Mistral tokenisers "
     "respectively, also 512, also language-capable.\n\n"
     "\u2022  Krea 2 and Anima \u2014 share Flux.2 Klein's Qwen3 "
     "tokeniser, so choose the Flux.2 Klein target to count for them.\n\n"
     "The takeaway: on SDXL you are working against a tight token budget "
     "and should trim; on the Flux family and Krea 2 you have far more "
     "room, so prune for CLARITY rather than to fit. Always count against "
     "the trainer you actually use \u2014 counting an SDXL caption with a "
     "Flux ruler (or the reverse) gives the wrong answer."),

    ("Training \u00b7 Saving tokens on SDXL",
     "On SDXL the 75/225 budget is tight, so shaving tokens lets you fit "
     "more real content. One easy lever is here in the program.\n\n"
     "Underscores vs spaces. A multi-word tag can be written "
     "\u201clong_hair\u201d (underscore) or \u201clong hair\u201d "
     "(spaces). Under CLIP, the underscore form is often a SINGLE token, "
     "while the spaced form is two \u2014 so converting spaced tags to "
     "underscores can meaningfully cut your token count on a "
     "tag-heavy caption, with no change to meaning.\n\n"
     "Tools \u2192 the underscore/space conversion does this across the "
     "whole dataset at once, with a preview and a guard against "
     "format-breaking edits. On SDXL, converting toward underscores is a "
     "quick way to reclaim tokens.\n\n"
     "(On the Flux family and Krea 2 the budget is large, so this is "
     "optional there \u2014 use whichever format your base model was "
     "trained on. The saving is an SDXL concern.)\n\n"
     "Check the result with the Token Counter set to SDXL: convert, "
     "recount, and watch the over-limit captions drop. Combined with "
     "pruning genuinely redundant tags, it is usually enough to bring a "
     "long caption back under the limit without losing anything you "
     "wanted to keep."),

    ("Tips \u00b7 Making the walk faster",
     "Step 5 (the walk) is the only part that scales with your dataset. "
     "Two settings decide how long a complete pass takes.\n\n"
     "\u201cAuto-confirm images that already have the tag\u201d answers "
     "Yes for images the tagger already tagged, leaving you only the "
     "ones where it might have missed something. This is the single "
     "biggest lever, and the decisions stay undoable like any other.\n\n"
     "The filter mode narrows the queue further \u2014 walking only "
     "images that LACK a tag turns a verification pass into a spotting "
     "pass.\n\n"
     "If you stop short of 100%, the percentage tells you where you left "
     "off, and the tag tree sorts by frequency so the tags that shape "
     "your concept come first. A partial audit is fine \u2014 just make "
     "it a deliberate choice rather than running out of patience."),

    ("Tips \u00b7 Small things worth knowing",
     "\u2022  {undo} undoes anything, batch edits included. The Edit "
     "menu also has \u201cundo everything\u201d to roll a session all "
     "the way back.\n\n"
     "\u2022  Co-occurrence hints can nudge you toward tags you might be "
     "missing (\u201cimages with maid usually also have "
     "maid_headdress\u201d). Toggle in Settings.\n\n"
     "\u2022  Multiple folders load and walk as one dataset \u2014 the "
     "window title shows what is loaded.\n\n"
     "\u2022  {save_session} saves the session; autosave also runs on "
     "its own. Reopen later and you land where you left off.\n\n"
     "\u2022  Seven themes and every keybind are in Settings."),

    ("Help \u00b7 The diagnostic log",
     "Help \u2192 Diagnostic Log opens a record of what the program did "
     "and any errors it hit. You do not need it in normal use \u2014 it "
     "is there for when something goes wrong.\n\n"
     "Use it when:\n\n"
     "\u2022  Something misbehaves or a feature does not work as "
     "expected \u2014 the log often names the actual error.\n"
     "\u2022  You are reporting a bug \u2014 include the log. It is the "
     "single most useful thing for working out what happened, far more "
     "than a description alone.\n\n"
     "The log lives on disk too (under your user folder), but the menu "
     "item opens it without hunting. It records events and errors, not "
     "your captions or images \u2014 there is nothing sensitive about "
     "your dataset in it."),

    # ===== TRAINING KNOWLEDGE (added) ==============================

    ("Safety \u00b7 Back up your captions",
     "Copy your caption files somewhere safe before a big pass.\n\n"
     "TagWalker writes to disk the moment you answer, and undo covers "
     "the current session only \u2014 close the program and the previous "
     "state is gone. There is no built-in backup because copying a "
     "folder is something Windows already does well, and a tool that "
     "silently keeps its own copies is one you stop trusting.\n\n"
     "Your images are never modified \u2014 only caption .txt files are "
     "written, and every write is atomic, so a crash cannot leave a "
     "half-written file. But before a bulk rename, delete, or reformat "
     "across the whole dataset, one copy of the folder costs ten "
     "seconds and is the only thing that protects against a mistake you "
     "notice later."),

    # ===== FILE STATE & SELECTION (added) ==========================
]
