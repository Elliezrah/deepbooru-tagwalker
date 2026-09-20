"""
ui/image_editor_window.py

The image editor — a separate program wearing the same theme.

It works on ITS OWN input folder and writes to ITS OWN output folder.
It never touches the dataset loaded in the main window, never reads or
writes a caption, and never modifies an original file. Cropping comes
before captioning in the workflow, because cropping changes what a
caption should say — so there are no captions here to carry along.

Phase 1: measure a folder and report. Nothing is written yet.

The report panel in the corner is deliberately not a popup. During a
one-by-one crop the user confirms an image every few seconds, and a
dialog for each would be intolerable; a line of coloured text they can
glance at is not. White for clean, amber for something worth knowing,
red for a failure.
"""
from __future__ import annotations

import random
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from config.theme import Colors, Fonts, Spacing
from core import color_jitter as cj
from core import image_prep as prep

# How many lines the corner report keeps. Enough to see a batch's
# worth of trouble, short enough not to become a log nobody reads.
REPORT_LINES = 200

VERDICT_TEXT = {
    prep.VERDICT_TOO_SMALL: "too small to use \u2014 see below",
    prep.VERDICT_EXACT: "already a bucket size",
    prep.VERDICT_DOWNSCALE: "shrink to fit",
    prep.VERDICT_CROP: "crop to shape",
    prep.VERDICT_UPSCALE: "enlarge slightly",
    prep.VERDICT_PAD: "pad with even bars",
    prep.VERDICT_HUGE: "far larger than any bucket",
    prep.VERDICT_UNREADABLE: "unreadable",
}


class ReportPanel(QWidget):
    """The corner report. Three colours, no dialogs."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(Spacing.TIGHT)
        heading = QLabel("<b>Report</b>")
        layout.addWidget(heading)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(REPORT_LINES)
        self.view.setStyleSheet(
            f"QPlainTextEdit {{ background: {Colors.BG_INPUT}; "
            f"color: {Colors.TEXT_SECONDARY}; "
            f"font-family: {Fonts.FAMILY_MONO}; "
            f"font-size: {Fonts.SIZE_SMALL}pt; }}")
        layout.addWidget(self.view, 1)

    def _write(self, text: str, colour: str) -> None:
        self.view.appendHtml(
            f"<span style='color:{colour};'>{text}</span>")
        bar = self.view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def ok(self, text: str) -> None:
        self._write(text, Colors.TEXT_PRIMARY)

    def notice(self, text: str) -> None:
        """Something the user should know but that is not a failure —
        a renamed file, a padded image."""
        self._write(text, Colors.WARNING_AMBER)

    def error(self, text: str) -> None:
        self._write(text, Colors.DANGER_RED)

    def clear(self) -> None:
        self.view.clear()


class ImageEditorWindow(QMainWindow):
    """Prepare raw images for training. Separate dataset, separate
    output, originals never touched."""

    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        # Created parentless by the main window (see _action_image_editor),
        # so it is a fully independent top-level window like Post Browser
        # and Tag Reference — minimising the main window does not touch
        # it. This flag is set defensively; a parentless top-level
        # window already behaves this way, but it costs nothing and
        # protects against a future caller passing a parent.
        self.setWindowFlag(Qt.WindowType.Window, True)
        self._settings = settings
        self._input: Path | None = None
        self._output: Path | None = None
        # Each folder-picker remembers its OWN last location, independently.
        # Qt's QFileDialog otherwise defaults to a single shared last-used
        # directory across every dialog in the app, which made the output
        # picker open at the input folder (and vice versa) — annoying when
        # input and output live in different places and change separately.
        # These two fields let each picker reopen where IT was last used,
        # decoupled from the other. Empty string = Qt's default (first use).
        self._last_input_dir: str = ""
        self._last_output_dir: str = ""
        self._report: prep.FolderReport | None = None

        self.setWindowTitle("Image Editor")
        self.resize(940, 620)
        # Whether to open maximised. Applied in showEvent, not here:
        # calling showMaximized() before the native window exists is
        # unreliable on Windows — the pending resize() above can win
        # the race and the window opens at 940x620 regardless. Setting
        # the state the first time the window is actually shown is the
        # pattern Windows honours. Read once so an in-session toggle in
        # Preferences takes effect on the NEXT open.
        # Persistent intent: should this window be maximised? Set from
        # the setting at construction, re-applied on every show, and
        # cleared only when the user deliberately un-maximises (see
        # changeEvent). This is what keeps it maximised across a
        # whole-program minimise/restore, which on Windows can
        # otherwise drop it back to the default size.
        self._want_maximised = bool(
            settings.image_editor_fullscreen)
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(Spacing.NORMAL, Spacing.NORMAL,
                                 Spacing.NORMAL, Spacing.NORMAL)

        intro_row = QHBoxLayout()
        intro = QLabel(
            "Prepares raw images for training. Works on its own "
            "folders \u2014 never touches the dataset open in the main "
            "window, never modifies an original. Crop first, then "
            "caption.")
        intro.setWordWrap(True)
        intro_row.addWidget(intro, 1)
        btn_guide = QPushButton("Guide")
        btn_guide.setFixedWidth(64)
        btn_guide.setToolTip("How the image editor works, and how to "
                             "prepare good source images")
        btn_guide.clicked.connect(self._show_editor_guide)
        intro_row.addWidget(btn_guide)
        outer.addLayout(intro_row)

        outer.addLayout(self._folder_row())
        outer.addLayout(self._output_row())
        outer.addLayout(self._target_row())

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        overview = QWidget()
        body = QHBoxLayout(overview)
        left = QVBoxLayout()
        self.summary = QLabel("Choose a folder to see what it needs.")
        self.summary.setWordWrap(True)
        self.summary.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        left.addWidget(self.summary, 1)
        # Export controls belong to the batch tab, not the window.
        # Shared, they appeared beside the one-by-one cropper where
        # "Export all" makes no sense at all.
        left.addLayout(self._export_row())
        body.addLayout(left, 2)

        # Two tabs, both work modes. Colour jitter was a third tab
        # and did not belong beside them: it is a setting that both
        # modes read, not a way of working, and sitting in the same
        # row implied it was a third thing you could be doing.
        self.tabs.addTab(overview, "Batch")
        self.tabs.addTab(self._build_single_tab(), "One by one")
        self._jitter_page = self._build_jitter_tab()

        # The report sits BELOW the tabs, not inside one. It lived on
        # the overview tab, so a crop confirmed in the one-by-one tab
        # wrote its line somewhere the user could not see — the export
        # worked and looked like it had done nothing.
        # The report sits in a compact strip along the bottom-left,
        # rather than spanning the full width and crowding the tabs
        # above it. Kept short and to one side so the viewport in the
        # One-by-one tab has the room \u2014 the viewport is where the
        # work happens; the report is a glance, not a focus.
        report_strip = QHBoxLayout()
        self.report = ReportPanel()
        self.report.setMaximumHeight(96)
        self.report.setMaximumWidth(560)
        report_strip.addWidget(self.report)
        report_strip.addStretch(1)
        outer.addLayout(report_strip)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

    # ------------------------------------------------------------------
    def _folder_row(self):
        row = QHBoxLayout()
        self.btn_input = QPushButton("Choose input folder\u2026")
        self.btn_input.clicked.connect(self._pick_input)
        row.addWidget(self.btn_input)
        self.lbl_input = QLabel("no folder chosen")
        self.lbl_input.setProperty("role", "secondary")
        row.addWidget(self.lbl_input, 1)
        return row

    def _output_row(self):
        """Where exports go. Shown always and changeable at any point.

        It used to be asked for once, on the first export, and then
        silently reused — so changing it mid-session meant restarting
        the tool.
        """
        row = QHBoxLayout()
        self.btn_output = QPushButton("Choose output folder\u2026")
        self.btn_output.clicked.connect(self._pick_output)
        row.addWidget(self.btn_output)
        self.lbl_output = QLabel("not set")
        self.lbl_output.setProperty("role", "secondary")
        row.addWidget(self.lbl_output, 1)
        return row

    def _pick_output(self) -> bool:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose an output folder (originals are never "
                  "modified)", self._last_output_dir)
        if not chosen:
            return False
        # Remember where the OUTPUT picker was used, independently of the
        # input picker (see _last_output_dir). We record it even before the
        # differ-from-input check below, so a corrected re-pick reopens at
        # the folder just tried rather than jumping back to the input.
        self._last_output_dir = chosen
        out = Path(chosen)
        if self._input is not None and out.resolve() == \
                self._input.resolve():
            self.report.error(
                "output folder must differ from the input folder")
            return False
        self._output = out
        self.lbl_output.setText(str(out))
        self.report.ok(f"exports will go to {out}")
        return True

    def _ensure_output(self) -> bool:
        return self._output is not None or self._pick_output()

    def _target_row(self):
        row = QHBoxLayout()
        row.addWidget(QLabel("Target resolution:"))
        self.target = QComboBox()
        for base, label in prep.COMMON_TARGETS:
            self.target.addItem(label, base)
        # Selected explicitly rather than left on the first entry: the
        # list runs largest-first for readability, and the largest is
        # not the common case.
        default = self.target.findData(prep.DEFAULT_TARGET)
        if default >= 0:
            self.target.setCurrentIndex(default)
        self.target.setToolTip(
            "The base resolution your trainer is configured for. "
            "Bucket sizes are derived from it, so this must match "
            "your training config or every recommendation below is "
            "measured against the wrong thing.")
        self.target.currentIndexChanged.connect(self._rescan)
        row.addWidget(self.target)
        row.addStretch(1)
        self.btn_jitter_settings = QPushButton("Colour jitter\u2026")
        self.btn_jitter_settings.setToolTip(
            "How much to shift the colour of images you choose to "
            "jitter. A setting both modes read, not a mode of its "
            "own.")
        self.btn_jitter_settings.clicked.connect(
            self._open_jitter_settings)
        row.addWidget(self.btn_jitter_settings)

        self.btn_rescan = QPushButton("Rescan")
        self.btn_rescan.clicked.connect(self._rescan)
        self.btn_rescan.setEnabled(False)
        row.addWidget(self.btn_rescan)
        return row

    def _build_single_tab(self) -> QWidget:
        """The one-by-one cropper.

        Separate from the batch tab rather than a mode switch on one
        screen: the two have almost nothing in common on the surface,
        and a screen that rearranges itself is harder to learn than
        two screens that do not.
        """
        from ui.crop_view import CropView

        page = QWidget()
        layout = QVBoxLayout(page)

        top = QHBoxLayout()
        self.lbl_single = QLabel("Choose an input folder first.")
        top.addWidget(self.lbl_single, 1)
        self.chk_lock = QCheckBox("Lock to bucket size")
        # Unlocked by default. Locked was the default and the wheel
        # is ignored when locked, so the tool's headline control
        # appeared to be broken on arrival.
        self.chk_lock.setChecked(False)
        self.chk_lock.setToolTip(
            "On: the box is the bucket's exact pixel size and can "
            "only be moved \u2014 no resampling beyond the crop "
            "itself.\n\nOff: the wheel resizes the box, keeping the "
            "bucket's shape, and the selection is scaled to fit on "
            "export.")
        self.chk_lock.toggled.connect(self._reload_single)
        top.addWidget(self.chk_lock)
        layout.addLayout(top)

        frame_row = QHBoxLayout()
        frame_row.addWidget(QLabel("Frame:"))
        self.shape = QComboBox()
        self.shape.setToolTip(
            "The shape of the crop box, switchable at any time.\n\n"
            "Square is for training at a single fixed resolution. The "
            "rectangles are for bucketing: the shortest side becomes "
            "the target either way, so a portrait and a landscape "
            "crop of the same picture carry the same detail.")
        self.shape.currentIndexChanged.connect(self._shape_changed)
        frame_row.addWidget(self.shape)

        frame_row.addWidget(QLabel("Format:"))
        self.fmt_single = QComboBox()
        self.fmt_single.addItem("PNG (lossless)", prep.FORMAT_PNG)
        self.fmt_single.addItem("JPEG (smaller)", prep.FORMAT_JPEG)
        frame_row.addWidget(self.fmt_single)

        frame_row.addWidget(QLabel("Bars:"))
        self.pad_single = QComboBox()
        self.pad_single.addItem("Black", prep.PAD_BLACK)
        self.pad_single.addItem("White", prep.PAD_WHITE)
        self.pad_single.setToolTip(
            "Fills any part of the box pulled outside the picture.")
        frame_row.addWidget(self.pad_single)
        frame_row.addStretch(1)
        layout.addLayout(frame_row)

        opts_row = QHBoxLayout()
        self.chk_fit_short = QCheckBox("Fit shortest side (adds bars)")
        self.chk_fit_short.setToolTip(
            "Default fits the crop inside the picture (no bars). Turn "
            "this on and the shortest side is fitted to the frame "
            "instead, so the box overhangs the long edges and those "
            "become even bars \u2014 an instant pillar/letterbox "
            "without dragging the box out by hand.")
        opts_row.addWidget(self.chk_fit_short)

        self.chk_show_small = QCheckBox("Show small images")
        self.chk_show_small.setToolTip(
            "Off by default. When off, images too small for the chosen "
            "target are hidden from the list \u2014 filling the target "
            "would enlarge them past the safe threshold and invent "
            "detail. Turn this on to list and edit them anyway; "
            "exporting one still warns unless \u201cAllow small "
            "images\u201d is also on.")
        self.chk_show_small.toggled.connect(self._on_show_small_toggled)
        opts_row.addWidget(self.chk_show_small)

        self.chk_allow_small = QCheckBox("Allow small images")
        self.chk_allow_small.setToolTip(
            "Off by default. When off, a selection that would be "
            "enlarged past the safe threshold prompts an \u201care "
            "you sure\u201d before exporting. When on, small "
            "selections export without the prompt \u2014 useful when "
            "you have already decided a slightly soft result is "
            "acceptable.")
        opts_row.addWidget(self.chk_allow_small)
        opts_row.addStretch(1)
        layout.addLayout(opts_row)

        split = QHBoxLayout()
        # A list you click, rather than Previous/Skip. Stepping
        # blindly through an unknown number of images is a poor way to
        # find the one you want, and it hides how much is left.
        self.image_list = QListWidget()
        self.image_list.setMaximumWidth(190)
        self.image_list.currentRowChanged.connect(self._pick_from_list)
        split.addWidget(self.image_list)

        self.crop = CropView()
        self.crop.changed.connect(self._refresh_crop_label)
        self.crop.step_requested.connect(self._step_single)
        # Connected here, after the crop view exists: the checkbox is
        # built earlier in the layout than the view it drives.
        self.chk_fit_short.toggled.connect(self.crop.set_fit_short)
        split.addWidget(self.crop, 1)

        # R resets the crop box; Ctrl+R the view. Scoped to the editor
        # window so they do not fire while the main window has focus.
        from PySide6.QtGui import QKeySequence, QShortcut

        sc_box = QShortcut(QKeySequence("R"), self)
        sc_box.activated.connect(self.crop.reset_crop)
        sc_view = QShortcut(QKeySequence("Ctrl+R"), self)
        sc_view.activated.connect(self.crop.reset_view_zoom)
        layout.addLayout(split, 1)

        self.lbl_crop = QLabel("")
        self.lbl_crop.setProperty("role", "secondary")
        layout.addWidget(self.lbl_crop)

        jitter_row = QHBoxLayout()
        self.chk_jitter_one = QCheckBox("Jitter this image")
        # OFF by default, deliberately. In a group of near-identical
        # images the first is the reference and belongs in the set in
        # its original colour; it is the copies that must differ from
        # it. Defaulting this on would shift the reference too.
        self.chk_jitter_one.setChecked(False)
        self.chk_jitter_one.setToolTip(
            "Apply a colour shift to this image only.\n\n"
            "Leave the first of a set of near-identical images alone "
            "and turn this on for the duplicates: the shapes then "
            "repeat while the exact pixel values do not, which is what "
            "stops the model memorising them.")
        self.chk_jitter_one.toggled.connect(self._roll_single_jitter)
        jitter_row.addWidget(self.chk_jitter_one)

        self.btn_reroll = QPushButton("Re-roll")
        self.btn_reroll.setToolTip(
            "Draw a different shift. The values below are what will "
            "actually be applied \u2014 nothing is random at the "
            "moment of export.")
        self.btn_reroll.clicked.connect(self._roll_single_jitter)
        jitter_row.addWidget(self.btn_reroll)

        self.lbl_jitter_one = QLabel("")
        self.lbl_jitter_one.setProperty("role", "secondary")
        jitter_row.addWidget(self.lbl_jitter_one, 1)
        layout.addLayout(jitter_row)

        # Tilt: turn the picture under the fixed crop box, to level a
        # crooked shot or right a mis-oriented one. Two habits, two
        # controls that share one value (set_image_angle, one clamp):
        # type an exact figure in the box, or orbit the picture with
        # Ctrl+Shift+drag for a quick freehand turn. The box takes the
        # whole +/-180 range, so a flip upside down can simply be typed.
        from ui.crop_view import MAX_TILT
        tilt_row = QHBoxLayout()
        tilt_row.addWidget(QLabel("Tilt:"))
        self.tilt_spin = QDoubleSpinBox()
        self.tilt_spin.setRange(-MAX_TILT, MAX_TILT)
        self.tilt_spin.setDecimals(1)
        self.tilt_spin.setSingleStep(0.1)
        self.tilt_spin.setSuffix("\u00b0")
        self.tilt_spin.setFixedWidth(84)
        self.tilt_spin.setToolTip(
            "Exact tilt angle in degrees (\u00b1180, so the picture can "
            "be levelled or flipped fully over). The crop box stays put; "
            "only the picture turns.\n\nQuick alternative: Ctrl+Shift+"
            "drag on the picture to orbit it.")
        tilt_row.addWidget(self.tilt_spin)

        self.btn_level = QPushButton("Level")
        self.btn_level.setFixedWidth(52)
        self.btn_level.setToolTip("Set the tilt back to zero.")
        tilt_row.addWidget(self.btn_level)
        tilt_row.addStretch(1)

        # The spinbox, the crop view and the tilt gesture are three
        # views of one number. Route each through _set_tilt; a guard flag
        # stops the value echoing around the ring. The crop view owns the
        # clamp, so a figure typed past the limit comes back corrected
        # and the box follows.
        self._tilt_syncing = False
        self.tilt_spin.valueChanged.connect(self._on_tilt_spin)
        self.btn_level.clicked.connect(lambda: self._set_tilt(0.0))
        # A tilt from the gesture (or a reset) makes the view emit
        # changed; mirror that back into the number box.
        self.crop.changed.connect(self._sync_tilt_controls)
        layout.addLayout(tilt_row)

        lock_row = QHBoxLayout()
        lock_row.addWidget(QLabel("Drag along:"))
        self.axis = QComboBox()
        self.axis.addItem("Any direction", "free")
        self.axis.addItem("Horizontal only (X)", "x")
        self.axis.addItem("Vertical only (Y)", "y")
        self.axis.setToolTip(
            "Constrains the drag to one axis, so a crop can be moved "
            "without losing the alignment already found on the "
            "other.")
        self.axis.currentIndexChanged.connect(
            lambda: self.crop.set_axis(self.axis.currentData()))
        lock_row.addWidget(self.axis)

        self.btn_help = QPushButton("\u2328")   # keyboard glyph
        self.btn_help.setFixedWidth(34)
        self.btn_help.setToolTip("Mouse and keyboard controls")
        self.btn_help.clicked.connect(self._show_controls_help)
        lock_row.addWidget(self.btn_help)

        self.btn_reset_all = QPushButton("\u21ba")   # reset glyph
        self.btn_reset_all.setFixedWidth(34)
        self.btn_reset_all.setToolTip(
            "Reset everything \u2014 zoom, pan and the crop box "
            "(Ctrl+R for the view, R for the box)")
        self.btn_reset_all.clicked.connect(self.crop.reset_all)
        lock_row.addWidget(self.btn_reset_all)
        lock_row.addStretch(1)
        layout.addLayout(lock_row)

        nav = QHBoxLayout()
        nav.addStretch(1)
        self.btn_crop = QPushButton("Crop && export \u2192")
        self.btn_crop.setToolTip(
            "Write the selection and move to the next image.")
        self.btn_crop.clicked.connect(self._crop_current)
        nav.addWidget(self.btn_crop)
        layout.addLayout(nav)
        return page

    def _single_plans(self) -> list:
        """The croppable plans, cached.

        This is read on every navigation step (shift+wheel, list
        click, export-advance) and by the jitter preview, so rebuilding
        the filtered list each time was needless work on a large
        folder. The set only changes on a rescan, which clears the
        cache."""
        if self._report is None:
            return []
        cached = getattr(self, "_single_plans_cache", None)
        if cached is None:
            show_small = (hasattr(self, "chk_show_small")
                          and self.chk_show_small.isChecked())
            if show_small:
                # List everything croppable, including images too small
                # for the target. They can still be edited and, with
                # "Allow small images" on, exported; the export path
                # warns about the upscale either way.
                cached = list(self._report.plans)
            else:
                cached = [p for p in self._report.plans
                          if p.verdict != prep.VERDICT_TOO_SMALL]
            self._single_plans_cache = cached
        return cached

    def _populate_shapes(self, base: int) -> None:
        """Refill the frame selector for this target, keeping the
        orientation the user had chosen."""
        shapes = prep.shape_buckets(base)
        chosen = self.shape.currentData()
        keep = chosen[0] if isinstance(chosen, tuple) else None
        self.shape.blockSignals(True)
        self.shape.clear()
        for key, label in ((prep.SHAPE_SQUARE, "Square"),
                           (prep.SHAPE_PORTRAIT, "Portrait"),
                           (prep.SHAPE_LANDSCAPE, "Landscape")):
            width, height = shapes[key]
            self.shape.addItem(f"{label}  {width}\u00d7{height}",
                               (key, (width, height)))
        if keep is not None:
            for row in range(self.shape.count()):
                if self.shape.itemData(row)[0] == keep:
                    self.shape.setCurrentIndex(row)
                    break
        self.shape.blockSignals(False)

    def _shape_changed(self) -> None:
        """Switch the crop frame without losing the picture or the
        position the user had found."""
        data = self.shape.currentData()
        if not data:
            return
        self.crop.set_bucket(data[1])
        self._refresh_crop_label()

    def _populate_list(self) -> None:
        """Fill the picker with everything croppable."""
        plans = self._single_plans()
        self.image_list.blockSignals(True)
        self.image_list.clear()
        for plan in plans:
            self.image_list.addItem(
                f"{plan.path.name}  ({plan.width}\u00d7{plan.height})")
        if plans:
            self.image_list.setCurrentRow(
                min(getattr(self, "_single_index", 0), len(plans) - 1))
        self.image_list.blockSignals(False)

    def _pick_from_list(self, row: int) -> None:
        if row < 0:
            return
        self._single_index = row
        self._reload_single()

    def _on_show_small_toggled(self, _checked: bool) -> None:
        """Re-list when small images are shown or hidden.

        The croppable-plans list is cached, so it must be invalidated
        for the change to take. The image the user was on is kept
        selected if it survives the filter (it always does when turning
        the option ON, and does when turning it OFF unless that image
        was itself too small); otherwise the selection falls back to a
        valid row rather than vanishing.
        """
        current = None
        plans = self._single_plans()
        idx = getattr(self, "_single_index", 0)
        if plans and 0 <= idx < len(plans):
            current = plans[idx].path

        self._single_plans_cache = None      # the filter changed
        new_plans = self._single_plans()
        if current is not None:
            for i, p in enumerate(new_plans):
                if p.path == current:
                    self._single_index = i
                    break
            else:
                # The image the user was on is no longer listed (it was
                # too small and small images were just hidden). Clamp to
                # a valid row.
                self._single_index = min(idx, max(0, len(new_plans) - 1))
        self._populate_list()
        self._reload_single()

    def _mark_done(self, row: int, name: str) -> None:
        """Tick an exported image in the list, so a long folder shows
        its own progress without a separate counter."""
        item = self.image_list.item(row)
        if item is not None:
            item.setText(f"\u2713  {name}")

    def _reload_single(self) -> None:
        """Show the current image with a fresh crop box."""
        plans = self._single_plans()
        if not plans:
            self.lbl_single.setText(
                "Nothing to crop \u2014 choose an input folder.")
            return
        self._single_index = max(
            0, min(getattr(self, "_single_index", 0), len(plans) - 1))
        plan = plans[self._single_index]
        from ui.crop_view import MODE_FREE, MODE_LOCKED

        mode = MODE_LOCKED if self.chk_lock.isChecked() else MODE_FREE
        data = self.shape.currentData()
        bucket = data[1] if data else plan.bucket
        ok = self.crop.load(plan.path, bucket, mode)
        self.lbl_single.setText(
            f"<b>{plan.path.name}</b> \u2014 {plan.width}\u00d7"
            f"{plan.height} \u2192 {bucket[0]}\u00d7{bucket[1]}   "
            f"({self._single_index + 1} of {len(plans)})")
        if not ok:
            self.report.error(f"could not open {plan.path.name}")
        self._roll_single_jitter()
        self._refresh_crop_label()

    def showEvent(self, event) -> None:  # noqa: N802
        """Apply the maximised state when the editor is shown.

        Covers the direct case — the editor being opened or shown on
        its own. The harder case, the whole program being minimised and
        restored, does NOT reach here: a parented child gets no
        show/hide/state events when its parent is minimised, so that
        restore is handled by the main window's changeEvent instead
        (it re-maximises this editor). Both paths consult
        _want_maximised, so a deliberate un-maximise is respected by
        each.
        """
        super().showEvent(event)
        if self._want_maximised and not self.isMaximized():
            self.setWindowState(
                self.windowState() | Qt.WindowState.WindowMaximized)

    def changeEvent(self, event) -> None:  # noqa: N802
        """Track a deliberate un-maximise (or re-maximise).

        This fires only for state changes the editor itself receives —
        i.e. the user acting on the editor window while it is focused.
        The platform-driven bit-drop during a parent restore does not
        reach here (the child gets no event then), so any state change
        we see while stably shown is the user's intent, and we record
        it so neither showEvent nor the main window re-imposes
        maximise against a windowed choice.
        """
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.WindowStateChange:
            if self.isVisible() and not self.isMinimized():
                self._want_maximised = self.isMaximized()
        super().changeEvent(event)

    def _show_editor_guide(self) -> None:
        """A booklet on what the editor does and how to feed it well."""
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setWindowTitle("Image editor guide")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            "<b>What this does</b><br>"
            "It resizes and crops your images to the resolutions a "
            "trainer expects, and can pad them to squares or into "
            "buckets. It writes to a separate output folder and never "
            "alters your originals, so you can run it as often as you "
            "like.<br><br>"

            "<b>Feed it the best source you have</b><br>"
            "Every resize and crop is only as good as what goes in, so "
            "start from the original, highest-quality file \u2014 not "
            "a copy that has already been through something.<br><br>"

            "<b>Reading the format and size</b><br>"
            "\u2022 <b>PNG</b> is lossless \u2014 the best case, no "
            "compression damage.<br>"
            "\u2022 <b>JPEG</b> is lossy, but often it is what the "
            "artist actually uploaded; many post their originals as "
            "JPEG without realising it costs a little quality. Usable, "
            "just not pristine.<br>"
            "\u2022 <b>WebP</b> is almost always a re-compressed copy "
            "made by an image host on upload \u2014 treat it as "
            "second-hand.<br><br>"
            "Size is the other tell. A file smaller than roughly HD "
            "(around 1920\u00d71080) is, these days, very likely a "
            "downscaled internet copy rather than an original. It can "
            "still be used, but expect the overview to flag it as too "
            "small for the larger targets, and do not enlarge it far "
            "to compensate \u2014 that only invents detail.<br><br>"

            "<b>The short version</b><br>"
            "Original over copy; PNG over JPEG over WebP; larger over "
            "smaller. When the overview warns an image is too small "
            "for your target, believe it.")
        box.exec()

    def _show_controls_help(self) -> None:
        """A small booklet of the viewer's controls."""
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setWindowTitle("Image editor controls")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(
            "<b>Mouse</b><br>"
            "\u2022 <b>Wheel</b> \u2014 resize the crop box (fine "
            "steps)<br>"
            "\u2022 <b>Drag</b> \u2014 move the crop box<br>"
            "\u2022 <b>Click outside the box</b> \u2014 jump it "
            "there<br>"
            "\u2022 <b>Ctrl + Wheel</b> \u2014 zoom the picture in "
            "the panel<br>"
            "\u2022 <b>Right-click + Drag</b> \u2014 pan the picture "
            "(distinct from moving the box)<br>"
            "\u2022 <b>Ctrl + Shift + Drag</b> \u2014 tilt the picture: "
            "orbit it around the crop centre, like turning a dial<br>"
            "<br>"
            "<b>Keyboard</b><br>"
            "\u2022 <b>R</b> \u2014 reset the crop box<br>"
            "\u2022 <b>Ctrl + R</b> \u2014 reset zoom, pan and tilt<br>"
            "<br>"
            "Zoom and pan exist so you can pull the crop box past the "
            "edge of the picture \u2014 the space outside becomes the "
            "bars of a pillarboxed or letterboxed image.<br><br>"
            "<b>Tilt</b> turns the picture under a crop box that stays "
            "put \u2014 for levelling a crooked shot or righting a "
            "mis-oriented one (the box takes the full \u00b1180\u00b0, so "
            "it can even be flipped over). Type an exact angle in the "
            "Tilt box, or Ctrl+Shift+drag to orbit it. Empty space the "
            "tilt or an over-pulled crop leaves is filled with the Bars "
            "colour, right where the box framed it \u2014 so the export "
            "matches the preview. For balanced pillarbox / letterbox "
            "bars, place the box evenly over the picture.")
        box.exec()

    def _roll_single_jitter(self) -> None:
        """Draw the shift for the current image and show it.

        Rolled here rather than at export so the numbers on screen are
        the numbers that will be written. A shift chosen invisibly at
        the last moment cannot be judged or rejected.
        """
        self._single_roll = None
        if not self.chk_jitter_one.isChecked():
            self.lbl_jitter_one.setText("")
            # Switching jitter off must put the original picture back.
            # This branch returned before the preview was refreshed,
            # so the shifted copy stayed on screen and the panel
            # disagreed with what would be written.
            self.crop.set_preview(None)
            return
        settings = self.jitter_settings()
        if not settings.active():
            self.lbl_jitter_one.setText(
                "no amounts set \u2014 open Colour jitter\u2026 above")
            self.crop.set_preview(None)
            return
        roll = cj.roll(settings)
        self._single_roll = roll
        described = roll.describe()
        self.lbl_jitter_one.setText(
            f"will apply {described}" if described
            else "rolled no change \u2014 re-roll for a shift")
        self._refresh_jitter_preview()

    def _refresh_jitter_preview(self) -> None:
        """Show the shift on the picture itself.

        The numbers alone do not tell you whether a hue rotation has
        turned skin green. Applied to the displayed copy the answer is
        immediate, and re-rolling until it looks right is the whole
        workflow.

        Rendered from a downscaled copy: the preview only has to fill
        a panel, and decoding a full-size image on every re-roll would
        make the button feel broken.
        """
        if not self.chk_jitter_one.isChecked():
            self.crop.set_preview(None)
            return
        roll = getattr(self, "_single_roll", None)
        plans = self._single_plans()
        if roll is None or not plans:
            self.crop.set_preview(None)
            return
        plan = plans[getattr(self, "_single_index", 0)]
        try:
            from PIL import Image as PILImage
            from PySide6.QtGui import QImage, QPixmap

            # Cache the downscaled base copy per image. Re-rolling
            # changes only the shift, not the source, so decoding and
            # thumbnailing from disk on every re-roll was needless work
            # — the decode is the slow part, and it made the Re-roll
            # button feel heavy on large files. The cache is keyed on
            # the path and dropped when the image changes.
            base = getattr(self, "_jitter_base_copy", None)
            if base is None or self._jitter_base_path != plan.path:
                with PILImage.open(plan.path) as raw:
                    raw.draft("RGB", (1400, 1400))
                    base = raw.convert("RGB")
                    base.thumbnail((1400, 1400), PILImage.BILINEAR)
                self._jitter_base_copy = base
                self._jitter_base_path = plan.path

            shifted = cj.apply_result(base, roll)
            data = shifted.tobytes("raw", "RGB")
            image = QImage(data, shifted.width, shifted.height,
                           shifted.width * 3, QImage.Format.Format_RGB888)
            self.crop.set_preview(QPixmap.fromImage(image.copy()))
        except Exception:
            # A preview that cannot be built is not worth an error;
            # the export will report anything genuinely wrong.
            self.crop.set_preview(None)

    def _refresh_crop_label(self) -> None:
        rect = self.crop.crop_rect
        if rect.isEmpty():
            self.lbl_crop.setText("")
            return
        kept = self.crop.coverage()
        hint = ("wheel to resize, drag to position"
                if self.crop.can_resize()
                else "drag to position \u2014 size is locked to the "
                     "bucket")
        pad_note = ("  \u2014 the part outside will be filled with "
                    "bars" if self.crop.overflow() else "")
        self.lbl_crop.setText(
            f"selection {rect.width()}\u00d7{rect.height()} px, "
            f"keeping {kept:.0%} of the image \u2014 {hint}"
            + pad_note)

    # -- Tilt controls -------------------------------------------------
    # The slider (tenths of a degree), the spinbox (degrees) and the
    # crop view's angle are three views of one number. Each setter below
    # routes through _set_tilt, and a guard flag stops the value echoing
    # around the ring. The crop view stays the single source of truth:
    # it owns the clamp, so a figure typed past the limit comes back
    # corrected and the controls follow.
    def _set_tilt(self, degrees: float) -> None:
        """Apply a tilt from any source, then reflect it everywhere."""
        if self._tilt_syncing:
            return
        self.crop.set_image_angle(degrees)
        # set_image_angle clamps; read the value back so the box shows
        # what actually took effect, not what was requested.
        self._sync_tilt_controls()

    def _on_tilt_spin(self, degrees: float) -> None:
        self._set_tilt(degrees)

    def _sync_tilt_controls(self) -> None:
        """Push the crop view's current angle into the number box.

        Called after any change (box, gesture or reset). The guard flag
        means the value we set here does not fire back through the
        handler and recurse.
        """
        if self._tilt_syncing:
            return
        angle = self.crop.image_angle
        self._tilt_syncing = True
        try:
            self.tilt_spin.setValue(angle)
        finally:
            self._tilt_syncing = False

    def _step_single(self, delta: int) -> None:
        plans = self._single_plans()
        if not plans:
            return
        self._single_index = (getattr(self, "_single_index", 0)
                              + delta) % len(plans)
        self.image_list.blockSignals(True)
        self.image_list.setCurrentRow(self._single_index)
        self.image_list.blockSignals(False)
        self._reload_single()

    def _crop_current(self, allow_upscale: bool = False) -> None:
        from ui.crop_view import (BORDER_ERROR, BORDER_NORMAL,
                                   BORDER_WARNING)

        plans = self._single_plans()
        if not plans:
            return
        if not self._ensure_output():
            return
        row = getattr(self, "_single_index", 0)
        plan = plans[row]
        rect = self.crop.crop_rect
        data = self.shape.currentData()
        bucket = data[1] if data else plan.bucket
        box = (rect.left(), rect.top(), rect.width(), rect.height())
        # The tickbox pre-authorises small exports, so no prompt is
        # raised for them at all.
        pre_authorised = allow_upscale or self.chk_allow_small.isChecked()
        result = prep.export_crop(
            plan.path, box, bucket, self._output,
            self.fmt_single.currentData(),
            self.pad_single.currentData(),
            jitter=(getattr(self, "_single_roll", None)
                    if self.chk_jitter_one.isChecked() else None),
            allow_upscale=pre_authorised,
            angle=self.crop.image_angle)
        rect_note = f"{rect.width()}\u00d7{rect.height()}"

        if result.status == prep.RESULT_TOO_SMALL:
            # No longer a hard stop. Colour the border amber, state the
            # exact enlargement, and let the user decide. Yes proceeds
            # and auto-advances; No cancels and the border returns to
            # blue.
            self.crop.set_border(BORDER_WARNING)
            ratio = prep.crop_upscale_ratio(box, bucket)
            from PySide6.QtWidgets import QMessageBox

            answer = QMessageBox.question(
                self, "Selection is small",
                f"This selection is {rect_note} and would be enlarged "
                f"{ratio - 1:.0%} to reach {bucket[0]}\u00d7"
                f"{bucket[1]}.\n\nEnlarging past the safe threshold "
                "invents detail that was never in the source. Export "
                "it anyway?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                self.crop.set_border(BORDER_NORMAL)
                self._crop_current(allow_upscale=True)
            else:
                self.crop.set_border(BORDER_NORMAL)
                self.report.notice(
                    f"{plan.path.name}: export cancelled "
                    "(selection too small)")
            return

        if result.status == prep.RESULT_OK:
            self.crop.set_border(BORDER_NORMAL)
            self.report.ok(
                f"{plan.path.name} \u2192 {result.written.name}  "
                f"({rect_note} \u2192 {bucket[0]}\u00d7{bucket[1]}"
                + (", padded" if result.action == "padded" else "")
                + (", upscaled" if pre_authorised
                   and prep.crop_upscale_ratio(box, bucket) > 1.15
                   else "")
                + ")")
            self._mark_done(row, plan.path.name)
        elif result.status == prep.RESULT_RENAMED:
            self.crop.set_border(BORDER_NORMAL)
            self.report.notice(f"{plan.path.name}: {result.message}")
            self._mark_done(row, plan.path.name)
        else:
            # A genuine error: red border, and stay on the image.
            # Advancing past it would hide the failure and lose the
            # position the user had found.
            self.crop.set_border(BORDER_ERROR)
            self.report.error(f"{plan.path.name}: {result.message}")
            return
        self._step_single(1)

    def _open_jitter_settings(self) -> None:
        """Colour jitter settings, in their own window."""
        existing = getattr(self, "_jitter_dlg", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Colour jitter settings")
        dialog.setMinimumWidth(560)
        layout = QVBoxLayout(dialog)
        layout.addWidget(self._jitter_page)
        close = QPushButton("Close")
        close.clicked.connect(dialog.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)
        self._jitter_dlg = dialog
        dialog.show()

    def _build_jitter_tab(self) -> QWidget:
        """Randomised colour shifts, applied on export by both modes.

        Separate from the export controls because it is a property of
        the whole run rather than of one image, and because leaving it
        beside the buttons made it look like something to set per
        export.
        """
        page = QWidget()
        layout = QVBoxLayout(page)

        head = QHBoxLayout()
        summary = QLabel(
            "Random colour shift for near-identical images, to stop "
            "the model memorising them by pixel.")
        summary.setWordWrap(True)
        head.addWidget(summary, 1)
        btn_help = QPushButton("?")
        btn_help.setFixedWidth(30)
        btn_help.setToolTip("What this is for, and how to use it")
        btn_help.clicked.connect(self._show_jitter_help)
        head.addWidget(btn_help)
        layout.addLayout(head)

        self._jitter_rows = {}
        for key, label, tip in (
                ("hue", "Hue",
                 "Rotates colour around the wheel. Wraps, so a large "
                 "shift comes back round."),
                ("saturation", "Saturation",
                 "Deepens or fades how vivid the colours are."),
                ("brightness", "Brightness",
                 "Lightens or darkens the whole image.")):
            row = QHBoxLayout()
            name = QLabel(label)
            name.setFixedWidth(90)
            name.setToolTip(tip)
            row.addWidget(name)

            row.addWidget(QLabel("up to \u00b1"))
            swing = QSpinBox()
            swing.setRange(0, cj.MAX_HUE_SWING if key == "hue"
                           else cj.MAX_LEVEL_SWING)
            swing.setToolTip(tip + "\n\nZero switches this channel "
                                   "off entirely.")
            row.addWidget(swing)

            row.addWidget(QLabel("but at least \u00b1"))
            floor = QSpinBox()
            floor.setRange(0, cj.MAX_HUE_SWING if key == "hue"
                           else cj.MAX_LEVEL_SWING)
            floor.setToolTip(
                "Excludes the middle. With 3 here, every image moves "
                "by more than 3 in one direction or the other, so "
                "none is left effectively unchanged.")
            row.addWidget(floor)
            row.addStretch(1)
            layout.addLayout(row)
            self._jitter_rows[key] = (swing, floor)

        self.lbl_jitter = QLabel("")
        self.lbl_jitter.setWordWrap(True)
        for swing, floor in self._jitter_rows.values():
            swing.valueChanged.connect(self._refresh_jitter_label)
            floor.valueChanged.connect(self._refresh_jitter_label)
        layout.addWidget(self.lbl_jitter)

        buttons = QHBoxLayout()
        btn_reco = QPushButton("Use recommended (\u00b112, min 7)")
        btn_reco.setToolTip(
            "Up to \u00b112 on each channel, and at least \u00b17, "
            "so every jittered image moves a clearly visible amount "
            "without the shift ever landing near zero. Preview and "
            "re-roll to catch any image the range pushes too far.")
        btn_reco.clicked.connect(self._apply_recommended_jitter)
        buttons.addWidget(btn_reco)
        btn_reset = QPushButton("Reset")
        btn_reset.setToolTip("Set every channel back to zero (off).")
        btn_reset.clicked.connect(self._reset_jitter)
        buttons.addWidget(btn_reset)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        layout.addStretch(1)
        self._refresh_jitter_label()
        return page

    def _apply_recommended_jitter(self) -> None:
        """Preset: up to \u00b112 on each channel, at least \u00b17.

        The floor of 7 guarantees every jittered image actually moves
        a visible amount \u2014 a random draw over \u00b112 alone
        clusters near zero, and a near-zero shift wastes the choice to
        jitter that image. The ceiling of 12 is usually safe for the
        theme; where a particular image drifts too far the preview and
        re-roll catch it before anything is written, which is what
        makes the wider range workable."""
        for key in ("hue", "saturation", "brightness"):
            swing, floor = self._jitter_rows[key]
            swing.setValue(12)
            floor.setValue(7)

    def _reset_jitter(self) -> None:
        for swing, floor in self._jitter_rows.values():
            swing.setValue(0)
            floor.setValue(0)

    def _show_jitter_help(self) -> None:
        """Colour-jitter help, as a paged booklet.

        The explanation is far too long for one message box \u2014 a
        box sized to fit it runs off the screen \u2014 so it uses the
        same PagedHelpDialog the rest of the app uses for long help,
        one idea per page."""
        from ui.paged_help_dialog import PagedHelpDialog

        pages = [
            ("What it is for",
             "Near-identical images \u2014 the same pose shot twice, a "
             "frame sequence, a variant set \u2014 tempt the model to "
             "memorise exact pixels instead of learning the concept.\n\n"
             "Giving the copies slightly different colour keeps their "
             "shapes identical while making the pixel values differ, "
             "which is what breaks the memorisation."),

            ("Why not the trainer's own colour augmentation?",
             "Because you often cannot use it together with latent "
             "caching.\n\n"
             "Caching pre-computes each image once to train faster and "
             "save memory, but a cached image can no longer be altered "
             "on the fly \u2014 so the trainer disables colour "
             "augmentation whenever caching is on.\n\n"
             "Baking the shift into the files here sidesteps that: the "
             "colour is already varied before the trainer ever caches "
             "it, so you keep the speed of caching and still get the "
             "anti-memorisation benefit."),

            ("Most images should NOT be jittered",
             "In a group of near-copies, keep the FIRST one in its "
             "original colour \u2014 it is your clean reference.\n\n"
             "Apply the shift only to the duplicates, so they differ "
             "from that reference and from each other.\n\n"
             "Which images are near-copies is something only you can "
             "see, so the One by one tab lets you pick per image. "
             "Batch is for when an entire folder is variants."),

            ("The two numbers, plainly",
             "Each channel has \u201cup to \u00b1N\u201d and "
             "\u201cbut at least \u00b1N\u201d, on the 0\u2013255 "
             "colour scale.\n\n"
             "\u2022 Up to sets the largest shift allowed.\n\n"
             "\u2022 At least forbids tiny shifts near zero. A random "
             "pick between \u221212 and +12 lands near 0 about as "
             "often as anywhere else, and a near-zero shift barely "
             "changes the image \u2014 which defeats the point if you "
             "chose that image specifically to move it. Setting "
             "\u201cat least 7\u201d guarantees the shift is bigger "
             "than \u00b17, so every image you jitter actually moves."),

            ("Recommended settings",
             "Up to \u00b112 on each of hue, saturation and "
             "brightness, with a minimum of \u00b17 \u2014 the Use "
             "recommended button sets exactly that.\n\n"
             "The minimum of 7 matters: a random draw over \u00b112 "
             "alone clusters near zero, so without a floor many images "
             "would get a shift too small to see. The floor guarantees "
             "every jittered image moves a clearly visible amount.\n\n"
             "This range is usually safe for the theme, but not always "
             "\u2014 which is what the preview is for. Re-roll any "
             "image the shift pushes too far before you export it; "
             "nothing is written until you do."),

            ("Batch vs hand-picking",
             "The recommended figures suit hand-picking specific "
             "duplicates, where a floor makes sense.\n\n"
             "For a whole-folder batch, drop the minimum to 0. Forcing "
             "every file off neutral there just swaps one uniform "
             "colour cast for a uniformly disturbed set \u2014 which "
             "helps nothing.\n\n"
             "In short: a floor when you are choosing images one by "
             "one; no floor when you are treating a whole folder at "
             "once."),
        ]
        self._jitter_help_win = PagedHelpDialog(
            "About colour jitter", pages, self)
        self._jitter_help_win.show()
        self._jitter_help_win.raise_()

    def jitter_settings(self):
        """Current settings, already clamped."""
        hue_swing, hue_min = self._jitter_rows["hue"]
        sat_swing, sat_min = self._jitter_rows["saturation"]
        bright_swing, bright_min = self._jitter_rows["brightness"]
        return cj.JitterSettings(
            hue_swing.value(), hue_min.value(),
            sat_swing.value(), sat_min.value(),
            bright_swing.value(), bright_min.value()).clamped()

    def _refresh_jitter_label(self) -> None:
        settings = self.jitter_settings()
        if not settings.active():
            self.lbl_jitter.setText(
                "<i>Off \u2014 exports keep their original "
                "colour.</i>")
            return
        parts = []
        for key, label in (("hue", "hue"),
                           ("saturation", "saturation"),
                           ("brightness", "brightness")):
            swing, floor = self._jitter_rows[key]
            if swing.value():
                band = (f"\u00b1{floor.value()}\u2013{swing.value()}"
                        if floor.value() else f"\u00b1{swing.value()}")
                parts.append(f"{label} {band}")
        # A minimum above its swing is silently pulled down, so say so
        # rather than letting the number on screen disagree with what
        # will happen.
        warn = ""
        for key in ("hue", "saturation", "brightness"):
            swing, floor = self._jitter_rows[key]
            if floor.value() > swing.value() > 0:
                warn = ("  <span style='color:"
                        f"{Colors.WARNING_AMBER};'>a minimum above its "
                        "range is reduced to match</span>")
                break
        self.lbl_jitter.setText(
            "Each image will be shifted by " + ", ".join(parts) + "."
            + warn)

    def _export_row(self):
        """Batch export controls. Deliberately below the report, so
        the numbers are read before anything is written."""
        row = QHBoxLayout()

        row.addWidget(QLabel("Mode:"))
        self.mode = QComboBox()
        self.mode.addItem("Resize for bucketing (nothing lost)",
                          prep.MODE_BUCKET)
        self.mode.addItem("Pad to square (nothing lost, adds bars)",
                          prep.MODE_SQUARE_PAD)
        # "Crop to square" is deliberately absent. Cropping blind to a
        # square discards the sides of every landscape image in the
        # folder, and no single position is right for all of them.
        # Squaring by hand is what the one-by-one tab is for.
        self.mode.setToolTip(
            "Three genuinely different operations.\n\n"
            "Resize for bucketing: the shortest side is brought to "
            "the target and the picture keeps its shape. Nothing is "
            "cropped and no bars are added \u2014 your trainer's own "
            "bucketing takes it from there. Use this if bucketing is "
            "on.\n\n"
            "Pad to square: the LONGEST side goes to the target and "
            "the short one gains even bars, so every output is "
            "exactly square. Nothing is lost; caption those "
            "pillarboxed or letterboxed.\n\n"
            "To square a picture by cropping, use the one-by-one tab "
            "\u2014 where the position is chosen rather than "
            "assumed.")
        row.addWidget(self.mode)

        row.addWidget(QLabel("Bars:"))
        self.pad_colour = QComboBox()
        self.pad_colour.addItem("Black", prep.PAD_BLACK)
        self.pad_colour.addItem("White", prep.PAD_WHITE)
        row.addWidget(self.pad_colour)

        row.addWidget(QLabel("Format:"))
        self.fmt = QComboBox()
        self.fmt.addItem("PNG (lossless)", prep.FORMAT_PNG)
        self.fmt.addItem("JPEG (smaller)", prep.FORMAT_JPEG)
        row.addWidget(self.fmt)

        row.addStretch(1)
        self.btn_export = QPushButton("Export all\u2026")
        self.btn_export.setEnabled(False)
        self.btn_export.clicked.connect(self._export_all)
        row.addWidget(self.btn_export)
        return row

    def _export_all(self) -> None:
        """Batch-write every scanned image into a chosen folder.

        Driven by a timer rather than a loop or a thread: a loop
        freezes the window for the whole run, and a worker thread
        segfaults Qt in this program (see core/prewarm). Timer chunks
        keep it responsive and interruptible.
        """
        if self._report is None or not self._report.plans:
            return
        if not self._ensure_output():
            return
        out = self._output
        fmt = self.fmt.currentData()
        pad = self.pad_colour.currentData()
        mode = self.mode.currentData()
        target = self.target.currentData() or prep.DEFAULT_TARGET
        jitter = self.jitter_settings()
        rng = random.Random()
        pending = list(self._report.plans)
        total = len(pending)
        # Progress through the list by index rather than pop(0):
        # popping from the front of a Python list is O(n) because it
        # shifts every remaining element, so a large batch would be
        # O(n^2) just in the popping. An index is O(1) per step.
        cursor = {"i": 0}
        counts = {"ok": 0, "renamed": 0, "failed": 0}

        self.report.clear()
        self.report.ok(f"writing {total:,} image(s) to {out}")
        self.progress.setVisible(True)
        self.progress.setMaximum(total)
        self.progress.setValue(0)
        self.btn_export.setEnabled(False)

        def step() -> None:
            if cursor["i"] >= total:
                timer.stop()
                self.progress.setVisible(False)
                self.btn_export.setEnabled(True)
                line = (f"done \u2014 {counts['ok']:,} written")
                if counts["renamed"]:
                    line += f", {counts['renamed']:,} renamed"
                if counts["failed"]:
                    line += f", {counts['failed']:,} failed"
                (self.report.error if counts["failed"]
                 else self.report.notice if counts["renamed"]
                 else self.report.ok)(line)
                return
            plan = pending[cursor["i"]]
            cursor["i"] += 1
            result = prep.export_batch(plan, out, mode, target,
                                       fmt, pad,
                                       jitter=jitter, rng=rng)
            if result.status == prep.RESULT_OK:
                counts["ok"] += 1
            elif result.status == prep.RESULT_RENAMED:
                counts["renamed"] += 1
                self.report.notice(
                    f"{plan.path.name}: {result.message}")
            else:
                counts["failed"] += 1
                self.report.error(
                    f"{plan.path.name}: {result.message}")
            self.progress.setValue(cursor["i"])

        timer = QTimer(self)
        timer.setInterval(0)
        timer.timeout.connect(step)
        timer.start()
        self._export_timer = timer

    # ------------------------------------------------------------------
    def _pick_input(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose the folder of raw images", self._last_input_dir)
        if not chosen:
            return
        # Remember where the INPUT picker was used, independently of the
        # output picker, so next time it reopens here rather than at
        # wherever the output folder was last chosen.
        self._last_input_dir = chosen
        self._input = Path(chosen)
        self.lbl_input.setText(str(self._input))
        self.btn_rescan.setEnabled(True)
        self._rescan()
        self.btn_export.setEnabled(bool(
            self._report and self._report.plans))

    def _rescan(self) -> None:
        if self._input is None:
            return
        base = self.target.currentData() or prep.DEFAULT_TARGET
        self.report.clear()
        self.progress.setVisible(True)
        self.progress.setValue(0)

        def tick(done: int, total: int) -> None:
            if total:
                self.progress.setMaximum(total)
                self.progress.setValue(done)

        report = prep.scan_folder(self._input, base, progress=tick)
        self.progress.setVisible(False)
        self._report = report
        self._single_plans_cache = None    # new scan, rebuild on demand
        self._show(report, base)
        # The crop view belongs to the same scan: changing the target
        # resolution changes every bucket, so the one-by-one tab has
        # to be rebuilt too. Doing this in _pick_input instead meant
        # it only refreshed when the FOLDER changed.
        # Keep the user where they were. Changing the target used to
        # jump back to the first image, which is exactly the moment
        # someone is comparing how one picture looks at two
        # resolutions.
        keep = getattr(self, "_single_index", 0)
        self.btn_export.setEnabled(bool(report.plans))
        self._populate_shapes(base)
        self._populate_list()
        plans = self._single_plans()
        self._single_index = min(keep, max(0, len(plans) - 1))
        self.image_list.blockSignals(True)
        self.image_list.setCurrentRow(self._single_index)
        self.image_list.blockSignals(False)
        self._reload_single()

    # ------------------------------------------------------------------
    def _show(self, report: prep.FolderReport, base: int) -> None:
        if not report.total and not report.unreadable:
            self.summary.setText(
                "No images found in that folder.")
            self.report.notice("nothing to do \u2014 no images found")
            return

        counts = report.by_verdict()
        lines = [f"<b>{report.total:,} image(s)</b> measured against "
                 f"a {base} target.<br>"]

        order = [prep.VERDICT_EXACT, prep.VERDICT_DOWNSCALE,
                 prep.VERDICT_CROP, prep.VERDICT_UPSCALE,
                 prep.VERDICT_PAD, prep.VERDICT_HUGE,
                 prep.VERDICT_TOO_SMALL]
        for verdict in order:
            n = counts.get(verdict, 0)
            if not n:
                continue
            lines.append(
                f"&nbsp;&nbsp;<b>{n:,}</b> \u2014 "
                f"{VERDICT_TEXT[verdict]}")

        spread = report.bucket_spread()
        if spread:
            lines.append("<br><b>Buckets they would fill</b>")
            for (width, height), n in spread[:8]:
                thin = (" &mdash; <i>only a few, may train "
                        "unevenly</i>" if n <= 2 else "")
                lines.append(
                    f"&nbsp;&nbsp;{width}\u00d7{height}: "
                    f"<b>{n:,}</b>{thin}")

        worst = report.heaviest_crops(5)
        if worst:
            lines.append("<br><b>Losing the most to cropping</b>")
            for plan in worst:
                lines.append(
                    f"&nbsp;&nbsp;{plan.path.name} \u2014 "
                    f"{plan.crop_loss:.0%}")
        self.summary.setText("<br>".join(lines))

        # The report panel carries the things worth acting on, in the
        # colour that says how much attention each deserves.
        self.report.ok(f"scanned {report.total:,} image(s)")
        pad = counts.get(prep.VERDICT_PAD, 0)
        if pad:
            self.report.notice(
                f"{pad:,} will need even bars \u2014 caption those "
                "pillarboxed or letterboxed")

        # The one finding that is not a preparation choice but a
        # judgement about the dataset itself.
        stranded = counts.get(prep.VERDICT_TOO_SMALL, 0)
        if stranded:
            self.report.error(
                f"{stranded:,} too small for {base}: neither side "
                "reaches the target, so padding would put bars on all "
                "four sides")
            smaller = report.smaller_target_that_fits()
            if smaller:
                self.report.notice(
                    f"all {stranded:,} would fit a {smaller} target "
                    "\u2014 consider training at that resolution "
                    "instead")
            else:
                self.report.notice(
                    "no smaller target fits them either \u2014 they "
                    "are best removed from this set")
            self.report.notice(
                "enlarging them to fit is possible but NOT "
                "RECOMMENDED: it invents detail the source never had")
        huge = counts.get(prep.VERDICT_HUGE, 0)
        if huge:
            self.report.notice(
                f"{huge:,} far larger than any bucket \u2014 most of "
                "each will be discarded")
        thin = [f"{w}\u00d7{h}" for (w, h), n in spread if n <= 2]
        if thin:
            self.report.notice(
                f"{len(thin)} bucket(s) hold 2 images or fewer: "
                + ", ".join(thin[:4]))
        for path in report.unreadable:
            self.report.error(f"could not read {path.name}")
        if (not pad and not huge and not thin and not stranded
                and not report.unreadable):
            self.report.ok("nothing needs attention")
