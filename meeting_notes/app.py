"""PySide6 desktop shell: tray icon + meeting library window (Pillar 1a).

Thin by design — data, rendering, and clipboard payload live in tested pure
modules. Per the epic's provider rule, this file knows nothing about claude -p.
"""

import os
import subprocess
import sys

from PySide6.QtCore import QMimeData
from PySide6.QtGui import QAction, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QSplitter,
    QSystemTrayIcon,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from meeting_notes.clipboard_payload import build_payload
from meeting_notes.library import list_meetings, load_notes
from meeting_notes.markdown_render import md_to_html


# Modern-minimal chrome. One accent (matches the tray icon), soft neutrals,
# flat controls with intentional spacing — no default 3D Qt look.
_STYLESHEET = """
QMainWindow { background: #ffffff; }
QWidget#content { background: #ffffff; }

QListWidget {
    background: #fafbfc;
    border: none;
    outline: 0;
    padding: 8px;
    font-size: 13px;
}
QListWidget::item {
    padding: 10px 12px;
    margin-bottom: 2px;
    border-radius: 8px;
    color: #3a3b42;
}
QListWidget::item:hover { background: #f0f1f4; }
QListWidget::item:selected { background: #eef1fd; color: #2b46c4; }

QTextBrowser {
    background: #ffffff;
    border: none;
    font-size: 14px;
}

QPushButton {
    background: transparent;
    color: #3a3b42;
    border: 1px solid #dcdde3;
    border-radius: 8px;
    padding: 8px 18px;
    font-size: 13px;
}
QPushButton:hover { background: #f2f3f6; }
QPushButton:disabled { color: #bcbfc7; border-color: #ececf0; }

QPushButton#primary {
    background: #4a6cf7;
    color: #ffffff;
    border: none;
}
QPushButton#primary:hover { background: #3f5fe0; }
QPushButton#primary:pressed { background: #3552c9; }
QPushButton#primary:disabled { background: #c7cef7; }

QStatusBar { background: #ffffff; color: #6b7280; border-top: 1px solid #ececf0; }
QStatusBar::item { border: none; }

QSplitter::handle:horizontal { background: #ececf0; width: 1px; }

QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical {
    background: #d3d5db; border-radius: 5px; min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #bfc2ca; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }

QMenu { background: #ffffff; border: 1px solid #e2e3e8; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 8px 20px; border-radius: 6px; color: #3a3b42; }
QMenu::item:selected { background: #eef1fd; color: #2b46c4; }
QMenu::separator { height: 1px; background: #ececf0; margin: 6px 8px; }
"""

# Typography for the rendered notes (Qt rich-text CSS subset).
_NOTE_CSS = """
p { margin-top: 6px; margin-bottom: 6px; color: #3a3b42; }
h1 { font-size: 20px; margin-top: 14px; margin-bottom: 6px; color: #1a1b22; }
h2 { font-size: 17px; margin-top: 12px; margin-bottom: 6px; color: #1a1b22; }
h3 { font-size: 15px; margin-top: 10px; margin-bottom: 4px; color: #1a1b22; }
li { margin-top: 3px; margin-bottom: 3px; color: #3a3b42; }
strong { color: #14151b; }
"""


def _tray_icon():
    """Placeholder icon: solid square. Replaced by real branding later."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor("#4a6cf7"))
    return QIcon(pixmap)


def open_folder(path):
    """Reveal a meeting folder in the OS file manager (per-OS shim, see PRD)."""
    if sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    elif sys.platform == "win32":
        os.startfile(path)


class LibraryWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Meeting Notes")
        self.resize(900, 600)
        self._meetings = []
        self._current = None

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._on_select)

        self.viewer = QTextBrowser()
        self.viewer.document().setDefaultStyleSheet(_NOTE_CSS)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("primary")
        self.copy_btn.clicked.connect(self._copy)
        self.copy_btn.setEnabled(False)

        self.folder_btn = QPushButton("Open folder")
        self.folder_btn.clicked.connect(self._open_folder)
        self.folder_btn.setEnabled(False)

        # Right-aligned actions, primary (Copy) furthest right.
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch()
        actions.addWidget(self.folder_btn)
        actions.addWidget(self.copy_btn)

        right = QWidget()
        right.setObjectName("content")
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(16, 12, 16, 16)
        right_layout.setSpacing(12)
        right_layout.addWidget(self.viewer)
        right_layout.addLayout(actions)

        splitter = QSplitter()
        splitter.setHandleWidth(1)
        splitter.addWidget(self.list)
        splitter.addWidget(right)
        splitter.setSizes([280, 620])
        self.setCentralWidget(splitter)
        self.statusBar()  # create it so showMessage works

        self.refresh()

    def refresh(self):
        self._meetings = list_meetings()
        # clear() first: it emits intermediate currentRowChanged signals that
        # re-enter _on_select and would repopulate the state we reset below.
        self.list.clear()
        self._current = None
        self.viewer.clear()
        self.copy_btn.setEnabled(False)
        self.folder_btn.setEnabled(False)
        for m in self._meetings:
            status = "notes ready" if m.has_notes else "no notes"
            QListWidgetItem(f"{m.date}   {m.title}   ({status})", self.list)

    def _read_notes(self):
        """Load current notes; on failure surface the error instead of crashing."""
        try:
            return load_notes(self._current)
        except OSError as e:
            self.viewer.setPlainText(f"Could not read notes: {e}")
            self.copy_btn.setEnabled(False)
            return None

    def _on_select(self, row):
        if row < 0 or row >= len(self._meetings):
            return
        self._current = self._meetings[row]
        self.folder_btn.setEnabled(True)
        if self._current.has_notes:
            notes = self._read_notes()
            if notes is not None:
                self.viewer.setHtml(md_to_html(notes))
                self.copy_btn.setEnabled(True)
        else:
            self.viewer.setPlainText("No notes for this meeting yet.")
            self.copy_btn.setEnabled(False)

    def _copy(self):
        if self._current is None:
            return
        notes = self._read_notes()
        if notes is None:
            return
        plain, html = build_payload(notes)
        mime = QMimeData()
        mime.setText(plain)
        mime.setHtml(html)
        QApplication.clipboard().setMimeData(mime)
        self.statusBar().showMessage("Copied to clipboard", 4000)

    def _open_folder(self):
        if self._current is not None:
            open_folder(self._current.path)

    def closeEvent(self, event):
        """Closing the window hides to tray; Quit lives in the tray menu."""
        event.ignore()
        self.hide()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(_STYLESHEET)
    app.setQuitOnLastWindowClosed(False)

    window = LibraryWindow()

    tray = QSystemTrayIcon(_tray_icon())
    menu = QMenu()
    open_action = QAction("Open Library", menu)

    def _open_library():
        window.refresh()
        window.show()
        window.raise_()
        window.activateWindow()

    open_action.triggered.connect(_open_library)
    quit_action = QAction("Quit", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(open_action)
    menu.addSeparator()
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.setToolTip("Meeting Notes")
    tray.show()

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
