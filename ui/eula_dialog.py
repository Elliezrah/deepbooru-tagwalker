"""
ui/eula_dialog.py

First-launch End User License Agreement.

Shown once, before the main window, until the user accepts. Acceptance is
remembered in user config by EULA version (settings key
``eula/accepted_version``); bumping ``EULA_VERSION`` re-prompts everyone.

Accept -> the app proceeds. Decline (or closing the dialog) -> the app
does not start. The text stresses the two things that matter: the user
must back up their data, and the author is not liable for data loss.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

# Bump this when the terms materially change, to re-prompt all users.
EULA_VERSION = "1.1"

# Kept in sync with EULA.md. The document is the authoritative copy; this
# is the in-app presentation, trimmed to what fits a launch dialog.
_EULA_TEXT = """\
TagWalker — End User License Agreement (v1.1)

By installing, launching, or using TagWalker ("the Software"), you agree \
to these terms. If you do not agree, do not use the Software.

1. BACK UP YOUR DATA FIRST
The Software edits caption files and related data in the folders you \
point it at. Before using the Software on any dataset, make a complete, \
separate backup of that dataset, and keep backups current as you work. \
You are solely responsible for maintaining backups. Many operations — \
bulk edits, tag removals, merges, reformatting, and cleanup tools — \
modify or delete data, and some may not be reversible. Any change \
carries the risk of unintended loss.

2. NO WARRANTY
The Software is provided "AS IS" and "AS AVAILABLE", without warranty of \
any kind, express or implied, including implied warranties of \
merchantability, fitness for a particular purpose, and non-infringement. \
The author does not warrant that the Software is free of defects or will \
operate without interruption or error. You use the Software at your own \
risk.

3. LIMITATION OF LIABILITY
To the maximum extent permitted by law, the author shall not be liable \
for any loss of or damage to data, files, captions, datasets, or work \
product, nor for any direct, indirect, incidental, special, \
consequential, or punitive damages arising out of or relating to your \
use of, or inability to use, the Software — even if advised of the \
possibility of such damages. This includes loss caused by software \
defects, unexpected behavior, crashes, failed or partial writes, \
operator error, or any operation that removes, alters, merges, or \
overwrites data. Your sole and exclusive remedy for dissatisfaction \
with the Software is to stop using it.

4. YOUR RESPONSIBILITIES
You are responsible for maintaining current, independent backups before \
and during use; for verifying the results of any operation — especially \
bulk or destructive ones — before relying on them or discarding your \
backups; for ensuring you have the right to use and modify the files you \
process; and for your use of any third-party data or services accessed \
through the Software.

5. THIRD-PARTY DATA
At your request, the Software can retrieve reference data from a \
third-party service (for example, tag reference information) and display \
it to you. That incoming data is provided for convenience, may be \
incomplete, outdated, or inaccurate, and is subject to the rights and \
terms of its respective owners.

6. PRIVACY — THE AUTHOR RECEIVES NO DATA
The Software runs locally on your computer. The author does not collect, \
receive, transmit, or store any of your data, files, captions, or usage \
information. There is no telemetry, no automatic reporting, and no \
automatic update or analytics call. The only outbound network activity \
is the optional reference lookups you choose to perform, which go \
directly to the relevant third-party service — not to the author. If you \
wish to report a problem, you may manually export a log and share it \
yourself; this never happens automatically.

7. ACKNOWLEDGMENT
By accepting, you acknowledge that you have read this Agreement, \
understand it, and agree to be bound by it — and in particular that you \
are responsible for backing up your data and that the author is not \
liable for any loss of data or other damages arising from your use of \
the Software.

This Agreement is not legal advice.
"""


class EulaDialog(QDialog):
    """Modal agreement shown at first launch (and after a version bump)."""

    def __init__(self, parent=None, review_mode: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("TagWalker — License Agreement")
        self.setModal(True)
        self.resize(640, 560)
        self._accepted = False
        # review_mode=True: shown from Help to re-read the agreement, so
        # no Accept/Decline gate — just a Close button. Nothing to accept
        # because acceptance already happened at first launch.
        self._review_mode = review_mode
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        heading = QLabel(
            "License Agreement" if self._review_mode
            else "Please read and accept before continuing")
        f = heading.font()
        f.setBold(True)
        heading.setFont(f)
        layout.addWidget(heading)

        backup = QLabel(
            "\u26a0  Back up your dataset before using TagWalker. "
            "Editing and bulk operations can change or remove data.")
        backup.setWordWrap(True)
        layout.addWidget(backup)

        body = QTextEdit()
        body.setReadOnly(True)
        body.setPlainText(_EULA_TEXT)
        layout.addWidget(body, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        if self._review_mode:
            self.btn_close = QPushButton("Close")
            self.btn_close.setDefault(True)
            self.btn_close.clicked.connect(self.accept)
            row.addWidget(self.btn_close)
        else:
            self.btn_decline = QPushButton("Decline and Exit")
            self.btn_decline.clicked.connect(self.reject)
            row.addWidget(self.btn_decline)
            self.btn_accept = QPushButton("I Agree")
            self.btn_accept.setDefault(True)
            self.btn_accept.clicked.connect(self._on_accept)
            row.addWidget(self.btn_accept)
        layout.addLayout(row)

    def _on_accept(self) -> None:
        self._accepted = True
        self.accept()

    @property
    def was_accepted(self) -> bool:
        # NOT named 'accepted' — QDialog already has an 'accepted' signal,
        # and a property of that name is shadowed by it (the signal object
        # is always truthy, which would make a decline read as an accept).
        return self._accepted


def show_review(parent=None) -> None:
    """Show the agreement read-only (from Help), with just a Close button.
    Does not change acceptance state — it's already been accepted."""
    dlg = EulaDialog(parent, review_mode=True)
    dlg.exec()


def ensure_accepted(settings) -> bool:
    """Show the EULA if the current version has not been accepted.

    Returns True if the user has accepted (now or previously) and the app
    may proceed; False if the user declined (the caller should exit).
    Records acceptance in settings under ``eula/accepted_version``.
    """
    already = settings.eula_accepted_version
    if already == EULA_VERSION:
        return True
    dlg = EulaDialog()
    dlg.exec()
    if dlg.was_accepted:
        settings.eula_accepted_version = EULA_VERSION
        return True
    return False
