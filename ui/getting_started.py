"""
ui/getting_started.py

The order to do things in.

TagWalker has a dozen tools and, until this existed, no opinion about
which to reach for first. Someone opening it with a fresh dataset met
a menu rather than a path — and for a program whose whole claim is
speed, knowing what to do first matters as much as each step being
quick.

The sequence below is the one that works, and page five carries the
single most important thing in it: do NOT walk every tag. A new user
assumes the walk means answering all of them, discovers that 64
images with 200 unique tags is twelve thousand decisions, and
concludes the program does not scale. It does; that is simply not how
it is meant to be used.
"""
from __future__ import annotations

PAGES: list[tuple[str, str]] = [
    ("Before you touch anything",
     "Copy your caption files somewhere safe first.\n\n"
     "TagWalker writes to disk the moment you answer, and undo covers "
     "the current session only — close the program and the previous "
     "state is gone. There is no built-in backup because copying a "
     "folder is something Windows already does well, and a tool that "
     "silently keeps its own copies is a tool you stop trusting.\n\n"
     "For a dataset you care about, one copy costs ten seconds."),

    ("1 · Look before you change",
     "Open your folder, then run two things and change nothing yet.\n\n"
     "Tools \u2192 Dataset Health Check finds captions that are "
     "missing, empty or unreadable, and images whose resolution will "
     "land oddly in training.\n\n"
     "Statistics \u2192 Dataset health shows the shape: how long your "
     "captions run against the token limit, how evenly your images "
     "are described, and how much of your vocabulary the base model "
     "has actually seen.\n\n"
     "Five minutes here tells you which of the next steps you "
     "actually need."),

    ("2 · Fix the mechanical errors",
     "These are the changes with no judgement in them, so do them "
     "first and in bulk.\n\n"
     "Tools \u2192 Tag Audit checks every tag against a 201,269-tag "
     "Danbooru list and offers fixes for typos, renamed tags and "
     "aliases \u2014 applied and undone in one step.\n\n"
     "Tools \u2192 Find Duplicate Tags removes tags repeated within a "
     "caption.\n\n"
     "Then delete anything the tagger emitted that you know is junk. "
     "Every batch operation is a single undo."),

    ("2b · Catch contradictions",
     "Tools \u2192 Conflict Rules finds captions that say two things "
     "at once. Some rules ship with the program; you can add your "
     "own, and for a specific dataset your own are usually the "
     "useful ones.\n\n"
     "Two kinds. An EXCLUSION says a set of tags must never appear "
     "together \u2014 \u201cindoors\u201d with "
     "\u201coutdoors\u201d. A REQUIREMENT says one tag implies "
     "another \u2014 \u201ccat_ears\u201d needs "
     "\u201canimal_ears\u201d.\n\n"
     "Scan, then read the violations before fixing anything. A "
     "contradiction is usually a tagger error, but not always \u2014 "
     "see the next page."),

    ("2c · When a conflict is not a conflict",
     "\u201clong_hair\u201d and \u201cshort_hair\u201d on one "
     "image looks obviously wrong \u2014 and for a solo character it "
     "is. For a two-character scene it is simply correct: one has "
     "long hair, the other short.\n\n"
     "Rules are UNCONDITIONAL. There is no way to write \u201conly "
     "when solo\u201d, so such a rule will also flag every "
     "legitimate multi-character image in a mixed dataset.\n\n"
     "That is a reason to review rather than a reason to abstain. "
     "Every violation in the scan has a \u201cView image\u201d "
     "button, so you look at the picture and decide that case on its "
     "own \u2014 which is the only way this question can be answered "
     "anyway. No fix is applied until you choose one.\n\n"
     "So an over-reporting rule costs you time, not accuracy. On a "
     "mixed dataset the hair rule is still worth running if it also "
     "catches real errors; you simply page through more images. What "
     "you should not do is fix in bulk without looking."),

    ("3 · Add your trigger word",
     "If your tagger did not insert it, Tools \u2192 Trigger Token "
     "will.\n\n"
     "A fast route once it exists on one image: the tag appears in "
     "the tag tree, select it, highlight every image in the queue, "
     "and one Yes applies it to all of them.\n\n"
     "The trigger is the one tag the Pruning Advisor will never "
     "suggest cutting. Being on every image is what makes an "
     "ordinary tag droppable; for the trigger, that ubiquity is the "
     "entire mechanism."),

    ("4 · Prune",
     "Tools \u2192 Tag Pruning Advisor ranks every tag by how much it "
     "is worth cutting, and explains each verdict. It writes "
     "nothing.\n\n"
     "Tier 1 is safe to cut \u2014 read the reasons, then delete the "
     "ones you agree with.\n\n"
     "Tier 2 needs a decision from you. Tier 3 is the tool showing "
     "its working; if you only ever open the \u201cyour goals "
     "disagree\u201d group there, it is behaving as intended.\n\n"
     "Set your training goal first. A character LoRA and a style "
     "LoRA want opposite things from the same tag."),

    ("5 \u00b7 Walk the tags",
     "This is the work, and how far you take it is your call.\n\n"
     "The program is built for a COMPLETE audit: a tag counts as "
     "done only when every image carrying it has a Yes or a No, and "
     "the completion percentage tracks exactly that. At 100% you "
     "know the captions are right rather than hoping they are \u2014 "
     "which is the whole reason to use a tool like this instead of "
     "trusting the tagger.\n\n"
     "For sixty-four images that is a full day's work. It is "
     "achievable, and for a dataset you intend to train repeatedly "
     "it is usually worth it."),

    ("5b \u00b7 Making the walk faster",
     "Two settings decide how long a complete pass takes.\n\n"
     "\u201cAuto-confirm images that already have the tag\u201d "
     "answers Yes for images the tagger already tagged, leaving you "
     "only the ones where it might have missed something. This is "
     "the single biggest lever, and the decisions stay undoable like "
     "any other.\n\n"
     "The filter mode narrows the queue further \u2014 walking only "
     "images that lack a tag turns a verification pass into a "
     "spotting pass.\n\n"
     "If you stop short of 100%, the percentage tells you where you "
     "left off, and the tag tree sorts by frequency so the tags that "
     "shape your concept come first. A partial audit is a legitimate "
     "choice; it is just one you should make deliberately rather "
     "than by running out of patience."),

    ("6 · Check before you train",
     "Statistics \u2192 Dataset health once more, and look at caption "
     "length.\n\n"
     "Anything past 225 tokens gets its tail cut during training. "
     "With caption shuffling on, a DIFFERENT tail is cut every step, "
     "so the model receives contradictory supervision that never "
     "settles \u2014 which is how training quality degrades for no "
     "visible reason.\n\n"
     "Tools \u2192 Token Counter lists the offenders and can rename "
     "them so they sort to the top of the folder."),

    ("How long this should take",
     "Steps 1 to 4 \u2014 the checks, the mechanical fixes, the "
     "trigger, the pruning \u2014 run about twenty minutes, and cost "
     "roughly the same whether you have sixty-four images or two "
     "thousand. That part does not scale with your dataset.\n\n"
     "Step 5 is the one that does. A complete audit of sixty-four "
     "images is a full day. With auto-confirm on and the filter "
     "narrowed, considerably less. Walking only the tags that decide "
     "your concept, under an hour.\n\n"
     "All three are valid. The completion percentage is there so the "
     "choice stays visible rather than accidental.\n\n"
     "If steps 1 to 4 are taking you an hour, something is fighting "
     "you \u2014 that is worth reporting."),
]
