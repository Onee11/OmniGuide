from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from PySide6.QtCore import QThread, QTimer, QRect, Qt, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
MODEL_NAME = "meta-models/muse-spark-open-1026"
BASE_URL = "https://inference.baseten.co/v1"
SERVER_NAME = "omniguide-palette-onee11"

STYLE = """
QWidget#LauncherRoot { background: transparent; color: #edf4ff; font-family: Inter, 'Noto Sans', sans-serif; }
QFrame#Shell { background: rgba(17, 24, 39, 145); border: 1px solid rgba(148, 163, 184, 105); border-radius: 20px; }
QLabel#Mark { color: #65e4ff; font-size: 22px; font-weight: 800; padding-left: 2px; }
QLabel#Brand { color: #eaf4ff; font-size: 12px; font-weight: 700; }
QLabel#Hint { color: #8ba2bf; font-size: 11px; }
QLineEdit#Prompt { background: transparent; color: #f8fafc; border: 0; padding: 8px 4px; font-size: 15px; selection-background-color: #126a8a; }
QLineEdit#Prompt:focus { border: 0; }
QPushButton { background: rgba(30, 41, 59, 115); color: #dbeafe; border: 1px solid rgba(148, 163, 184, 95); border-radius: 10px; padding: 7px 10px; font-weight: 600; }
QPushButton:hover { background: #26364e; border-color: #55cfee; }
QPushButton:disabled { color: #64748b; background: rgba(24, 34, 50, 135); border-color: rgba(38, 52, 73, 145); }
QPushButton#Send { color: #04131e; background: rgba(101, 228, 255, 225); border: 0; min-width: 38px; max-width: 38px; font-size: 16px; }
QPushButton#Send:hover { background: #94edff; }
QPushButton#Dismiss { background: transparent; border: 0; color: #8ba2bf; min-width: 24px; max-width: 24px; padding: 4px; }
QPushButton#Dismiss:hover { color: #ffffff; background: #293548; }
QLabel#Status { color: #65e4ff; font-size: 11px; font-weight: 650; }
QFrame#ResultCard { background: rgba(11, 18, 32, 112); border: 1px solid rgba(71, 85, 105, 110); border-radius: 14px; }
QTextBrowser#Result { background: transparent; color: #dce7f7; border: 0; padding: 4px; font-size: 13px; }
QProgressBar { background: rgba(31, 44, 64, 135); border: 0; border-radius: 2px; min-height: 3px; max-height: 3px; }
QProgressBar::chunk { background: #65e4ff; border-radius: 2px; }
QPushButton#Action { background: rgba(18, 53, 73, 175); border: 1px solid rgba(23, 97, 123, 190); color: #bcefff; }
QPushButton#Action:hover { background: #16465f; }
"""


def parse_json(content: str) -> dict[str, Any]:
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise ValueError("Muse Spark Open did not return valid JSON for this task.")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("Muse Spark Open returned an unexpected response format.")

    bbox = data.get("bbox_1000")
    if bbox is None and isinstance(data.get("bbox"), list):
        candidate = data["bbox"]
        if len(candidate) == 4 and all(isinstance(v, (int, float)) for v in candidate):
            bbox = candidate
        elif candidate and isinstance(candidate[0], dict):
            item = candidate[0]
            if all(key in item for key in ("x_min", "y_min", "x_max", "y_max")):
                bbox = [item["x_min"], item["y_min"], item["x_max"], item["y_max"]]
    if data.get("found") and bbox is not None:
        if not isinstance(bbox, list) or len(bbox) != 4 or not all(isinstance(v, (int, float)) for v in bbox):
            raise ValueError("The model returned an invalid bounding box.")
        bbox = [max(0, min(1000, int(round(v)))) for v in bbox]
        x1, y1, x2, y2 = bbox
        if x2 <= x1 or y2 <= y1:
            raise ValueError("The model returned an empty or reversed bounding box.")
        data["bbox_1000"] = bbox
    else:
        data["found"] = False
        data["bbox_1000"] = None
    return data


def classify_request(text: str) -> str:
    value = text.casefold().strip()
    guide_phrases = (
        "how do i", "how do you", "how to ", "show me how", "walk me through",
        "guide me", "teach me to", "help me do", "steps to ", "step by step",
        "walk me through", "help me export", "help me save", "how can i",
    )
    locate_phrases = (
        "find ", "locate ", "highlight ", "circle ", "point to ", "outline ",
        "where is ", "where's ", "where are ", "show me where", "the button",
        "the menu item", "identify the ", "select the ",
    )
    if any(p in value for p in guide_phrases):
        return "guide"
    if any(p in value for p in locate_phrases):
        return "locate"
    return "ask"


class CaptureWorker(QThread):
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, output_path: str):
        super().__init__()
        self.output_path = output_path

    def run(self) -> None:
        try:
            executable = shutil.which("spectacle")
            if not executable:
                raise RuntimeError("Spectacle was not found. Install it with: sudo dnf install spectacle")
            result = subprocess.run(
                [executable, "--background", "--nonotify", "--output", self.output_path],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError((result.stderr or result.stdout or "Spectacle capture failed.").strip())
            path = Path(self.output_path)
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError("Spectacle did not create the screenshot file.")
            self.done.emit(str(path))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class VisionWorker(QThread):
    done = Signal(object, str)
    failed = Signal(str)

    def __init__(self, image_path: str, request: str, mode: str, step: dict[str, str] | None = None):
        super().__init__()
        self.image_path = image_path
        self.request = request
        self.mode = mode
        self.step = step

    def run(self) -> None:
        try:
            key = os.getenv("BASETEN_API_KEY")
            if not key:
                raise RuntimeError("BASETEN_API_KEY is missing. Check the .env file in the project directory.")
            import mimetypes
            import base64
            path = Path(self.image_path)
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            image_url = f"data:{mime};base64,{encoded}"

            if self.mode == "ask":
                prompt = f"""You are OmniGuide, a screen-aware desktop AI companion. The user's request is: {self.request!r}
Look only at the attached screenshot and answer helpfully and concisely. You may describe visible content, explain what is on screen, or explain visible errors. Do not claim to have searched the web or seen anything outside the screenshot. If text is too small or unclear, say so."""
            elif self.mode == "guide":
                prompt = f"""You are OmniGuide, a visual desktop guide. User goal: {self.request!r}
Inspect the attached screenshot and plan a short, safe sequence of 2 to 5 manual steps to achieve the goal in the visible application. The user will perform each click themselves. Never instruct the user to click destructive or irreversible controls without explicit warning. Each step must have an 'instruction' (short imperative text) and a 'target' (the exact visible UI element to highlight).
For the FIRST step only, locate its target in the current screenshot and return a tight bounding box on a normalized 0-1000 grid.
Return only JSON with fields: mode='guide', goal, answer (one concise sentence), steps (array of objects with instruction and target), found (boolean for first target), bbox_1000 ([x_min,y_min,x_max,y_max] or null), reason (short visual evidence). If the first target is not visibly present, found=false and bbox_1000=null. Use only elements visible in the image; do not invent application controls."""
            else:
                if self.mode == "guide_target" and self.step:
                    prompt = f"""You are OmniGuide's visual grounding engine. The user's overall goal is: {self.request!r}
The current manual guide step is: {self.step.get('instruction', '')!r}
Locate this exact target on the CURRENT screenshot: {self.step.get('target', '')!r}
The user has potentially completed the previous step, so inspect the screenshot's actual current state. Return only JSON with fields target, found, bbox_1000 [x_min,y_min,x_max,y_max] on a normalized 0-1000 grid, and reason. Draw a tight box around the target itself. Do not guess. If not visible, found=false and bbox_1000=null."""
                else:
                    prompt = f"""You are OmniGuide's visual grounding engine. Locate the UI element requested by the user: {self.request!r}
Return only JSON with fields target (short name), found (boolean), bbox_1000 [x_min,y_min,x_max,y_max] or null, and reason (short visual evidence).
Coordinates must be integers on a normalized 0-1000 grid relative to the complete supplied screenshot; origin top-left. Draw a tight box around the target itself, not its parent menu or nearby controls. The request describes what to locate; it is not necessarily text to find verbatim. Do not guess. If not clearly visible, found=false and bbox_1000=null."""

            client = OpenAI(api_key=key, base_url=BASE_URL, timeout=150.0, max_retries=0)
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }],
            )
            content = response.choices[0].message.content or ""
            usage = response.usage
            usage_text = ""
            if usage:
                usage_text = f"{usage.prompt_tokens:,} input · {usage.completion_tokens:,} output · {usage.total_tokens:,} total tokens"
            if self.mode == "ask":
                data: dict[str, Any] = {"mode": "ask", "answer": str(content).strip(), "found": False, "bbox_1000": None}
            else:
                data = parse_json(str(content))
                data["mode"] = self.mode
                if self.mode == "guide":
                    steps = data.get("steps")
                    if not isinstance(steps, list):
                        steps = []
                    clean_steps: list[dict[str, str]] = []
                    for item in steps[:5]:
                        if isinstance(item, dict) and item.get("instruction") and item.get("target"):
                            clean_steps.append({"instruction": str(item["instruction"]), "target": str(item["target"])})
                    data["steps"] = clean_steps
            self.done.emit(data, usage_text)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class HighlightOverlay(QWidget):
    def __init__(self, screen, rect: QRect, label: str):
        super().__init__(None)
        self.rect_to_draw = QRect(rect)
        self.label = label
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setGeometry(screen.geometry())
        self.winId()
        handle = self.windowHandle()
        if handle is not None:
            handle.setScreen(screen)
        self.setGeometry(screen.geometry())

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(67, 225, 255, 245), 3))
        painter.setBrush(QColor(67, 225, 255, 32))
        painter.drawRoundedRect(self.rect_to_draw, 8, 8)
        font = QFont("Sans Serif", 10, QFont.Weight.DemiBold)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        width = min(max(150, metrics.horizontalAdvance(self.label) + 24), max(150, self.width() - 12))
        height = metrics.height() + 14
        x = min(max(6, self.rect_to_draw.left()), max(6, self.width() - width - 6))
        y = self.rect_to_draw.top() - height - 7
        if y < 6:
            y = min(self.height() - height - 6, self.rect_to_draw.bottom() + 7)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(13, 25, 40, 240))
        painter.drawRoundedRect(x, y, width, height, 7, 7)
        painter.setPen(QColor(235, 249, 255, 255))
        painter.drawText(x + 11, y + 7 + metrics.ascent(), self.label)
        painter.end()


class OmniGuidePalette(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("LauncherRoot")
        self.setWindowTitle("OmniGuide")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(STYLE)
        self.resize(700, 80)

        self.capture_path = str(Path(tempfile.gettempdir()) / f"omniguide_palette_{os.getpid()}.png")
        self.image_path: str | None = None
        self.pending_request = ""
        self.pending_mode = "ask"
        self.worker: VisionWorker | None = None
        self.capture_worker: CaptureWorker | None = None
        self.latest_bbox: list[int] | None = None
        self.latest_target = ""
        self.latest_usage = ""
        self.steps: list[dict[str, str]] = []
        self.current_step = 0
        self.guide_goal = ""
        self.overlays: list[HighlightOverlay] = []
        self.restore_timer = QTimer(self)
        self.restore_timer.setSingleShot(True)
        self.restore_timer.timeout.connect(self._remove_highlight_restore_palette)
        self.loading_frames = ["Thinking", "Thinking ·", "Thinking ··", "Thinking ···"]
        self.loading_index = 0
        self.loading_timer = QTimer(self)
        self.loading_timer.setInterval(300)
        self.loading_timer.timeout.connect(self._animate_status)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.shell = QFrame()
        self.shell.setObjectName("Shell")
        outer.addWidget(self.shell)
        shell_layout = QVBoxLayout(self.shell)
        shell_layout.setContentsMargins(17, 13, 17, 13)
        shell_layout.setSpacing(9)

        bar = QHBoxLayout()
        bar.setSpacing(10)
        self.mark = QLabel("✦")
        self.mark.setObjectName("Mark")
        bar.addWidget(self.mark)
        self.brand = QLabel("OMNIGUIDE")
        self.brand.setObjectName("Brand")
        bar.addWidget(self.brand)
        self.prompt = QLineEdit()
        self.prompt.setObjectName("Prompt")
        self.prompt.setPlaceholderText("Ask anything about your screen…")
        self.prompt.returnPressed.connect(self.submit_request)
        bar.addWidget(self.prompt, 1)
        self.send_button = QPushButton("↵")
        self.send_button.setObjectName("Send")
        self.send_button.setToolTip("Capture screen and ask OmniGuide (Enter)")
        self.send_button.clicked.connect(self.submit_request)
        bar.addWidget(self.send_button)
        self.dismiss_button = QPushButton("×")
        self.dismiss_button.setObjectName("Dismiss")
        self.dismiss_button.setToolTip("Quit OmniGuide")
        self.dismiss_button.clicked.connect(self.close)
        bar.addWidget(self.dismiss_button)
        shell_layout.addLayout(bar)

        self.status_label = QLabel("Enter to capture · Esc to hide · × to quit")
        self.status_label.setObjectName("Hint")
        shell_layout.addWidget(self.status_label)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.progress.hide()
        shell_layout.addWidget(self.progress)

        self.result_card = QFrame()
        self.result_card.setObjectName("ResultCard")
        result_layout = QVBoxLayout(self.result_card)
        result_layout.setContentsMargins(13, 10, 13, 10)
        result_layout.setSpacing(7)
        self.result = QTextBrowser()
        self.result.setObjectName("Result")
        self.result.setOpenExternalLinks(True)
        self.result.setMinimumHeight(70)
        self.result.setMaximumHeight(170)
        result_layout.addWidget(self.result)
        action_row = QHBoxLayout()
        self.target_button = QPushButton("Show target on desktop")
        self.target_button.setObjectName("Action")
        self.target_button.setEnabled(False)
        self.target_button.clicked.connect(self.show_target)
        action_row.addWidget(self.target_button)
        self.next_button = QPushButton("Next step →")
        self.next_button.setObjectName("Action")
        self.next_button.hide()
        self.next_button.clicked.connect(self.next_step)
        action_row.addWidget(self.next_button)
        self.done_button = QPushButton("Done")
        self.done_button.clicked.connect(self.finish_guide)
        self.done_button.hide()
        action_row.addWidget(self.done_button)
        action_row.addStretch(1)
        result_layout.addLayout(action_row)
        self.result_card.hide()
        shell_layout.addWidget(self.result_card)

        self._drag_offset = None
        self._shutdown_requested = False

    def place_near_cursor(self) -> None:
        cursor = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        x = cursor.x() - self.width() // 2
        y = cursor.y() + 22
        if y + self.height() > available.bottom():
            y = cursor.y() - self.height() - 20
        x = max(available.left() + 8, min(x, available.right() - self.width() + 1))
        y = max(available.top() + 8, min(y, available.bottom() - self.height() + 1))
        self.move(x, y)

    def popup(self) -> None:
        self.show()
        self.adjustSize()
        self.place_near_cursor()
        self.raise_()
        self.activateWindow()
        self.prompt.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.prompt.selectAll()

    def toggle(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.popup()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.loading_timer.stop()
        self.restore_timer.stop()
        for overlay in self.overlays:
            overlay.close()
            overlay.deleteLater()
        self.overlays.clear()

        capture_running = self.capture_worker is not None and self.capture_worker.isRunning()
        vision_running = self.worker is not None and self.worker.isRunning()
        if capture_running or vision_running:
            # Do not destroy a running QThread. Keep the palette visible so the
            # user knows the app is finishing the active request before quitting.
            self._shutdown_requested = True
            self.status_label.setText("Finishing current request, then quitting…")
            self.prompt.setEnabled(False)
            self.send_button.setEnabled(False)
            self.dismiss_button.setEnabled(False)
            self.target_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.done_button.setEnabled(False)
            event.ignore()
            return
        event.accept()

    def _close_after_worker(self) -> None:
        if self._shutdown_requested:
            QTimer.singleShot(0, self.close)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() < 42:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def submit_request(self) -> None:
        if (self.worker and self.worker.isRunning()) or (self.capture_worker and self.capture_worker.isRunning()):
            return
        request = self.prompt.text().strip()
        if not request:
            self.status_label.setText("Type a question or task first.")
            return
        self.pending_request = request
        self.pending_mode = classify_request(request)
        if self.pending_mode == "guide":
            self.steps = []
            self.current_step = 0
            self.guide_goal = request
        self.latest_bbox = None
        self.latest_target = ""
        self.target_button.setEnabled(False)
        self.next_button.hide()
        self.done_button.hide()
        self.result.clear()
        self.result_card.show()
        self.adjustSize()
        self.place_near_cursor()
        self.set_busy("Capturing your desktop")
        self.hide()
        QTimer.singleShot(350, self._start_capture)

    def _start_capture(self) -> None:
        self.capture_worker = CaptureWorker(self.capture_path)
        self.capture_worker.done.connect(self._capture_done)
        self.capture_worker.failed.connect(self._capture_failed)
        self.capture_worker.finished.connect(self._capture_finished)
        self.capture_worker.start()

    def _capture_done(self, path: str) -> None:
        if self._shutdown_requested:
            return
        self.image_path = path
        self.popup()
        if self.pending_mode == "guide_target" and self.steps:
            step = self.steps[self.current_step]
            self._start_vision(self.guide_goal, "guide_target", step)
        else:
            self._start_vision(self.pending_request, self.pending_mode)

    def _capture_failed(self, message: str) -> None:
        if self._shutdown_requested:
            return
        self.popup()
        self.set_idle(f"Capture failed: {message}")
        self.result_card.show()
        self.result.setPlainText("Could not capture the desktop. Check Spectacle and Wayland permissions, then try again.")

    def _capture_finished(self) -> None:
        self.capture_worker = None
        self._close_after_worker()

    def _start_vision(self, request: str, mode: str, step: dict[str, str] | None = None) -> None:
        if not self.image_path:
            self.set_idle("No screenshot available.")
            return
        self.set_busy("Understanding your screen")
        self.result_card.show()
        if mode == "guide_target":
            self.result.setPlainText(f"Step {self.current_step + 1} of {len(self.steps)}: {step.get('instruction', '') if step else ''}\n\nFinding the next target on the updated screen…")
        elif mode == "guide":
            self.result.setPlainText("Planning a short, visual walkthrough…")
        elif mode == "locate":
            self.result.setPlainText("Locating the requested element…")
        else:
            self.result.setPlainText("Reading the screen and preparing an answer…")
        self.worker = VisionWorker(self.image_path, request, mode, step)
        self.worker.done.connect(self._vision_done)
        self.worker.failed.connect(self._vision_failed)
        self.worker.finished.connect(self._vision_finished)
        self.worker.start()

    def set_busy(self, message: str) -> None:
        self.status_label.setText(message + " ·")
        self.loading_index = 0
        self.loading_base = message
        self.loading_timer.start()
        self.progress.show()
        self.send_button.setEnabled(False)
        self.prompt.setEnabled(False)
        self.target_button.setEnabled(False)

    def set_idle(self, message: str) -> None:
        self.loading_timer.stop()
        self.progress.hide()
        self.status_label.setText(message)
        self.send_button.setEnabled(True)
        self.prompt.setEnabled(True)

    def _animate_status(self) -> None:
        self.loading_index = (self.loading_index + 1) % 4
        self.status_label.setText(self.loading_base + (" ·" * (self.loading_index + 1)))

    def _vision_done(self, data: dict[str, Any], usage_text: str) -> None:
        if self._shutdown_requested:
            return
        mode = data.get("mode", self.pending_mode)
        self.latest_usage = usage_text
        self.latest_bbox = data.get("bbox_1000") if data.get("found") else None
        self.latest_target = str(data.get("target", "Target")) if self.latest_bbox else ""
        self.target_button.setEnabled(self.latest_bbox is not None)
        self.next_button.hide()
        self.done_button.hide()
        lines: list[str] = []

        if mode == "ask":
            lines.append(str(data.get("answer", "No answer was returned.")))
        elif mode == "guide":
            self.steps = data.get("steps", [])
            self.current_step = 0
            if not self.steps:
                lines.append("I couldn't create a reliable sequence from this screenshot. Try a more specific task or ask what to click first.")
            else:
                lines.append(f"**Goal:** {data.get('goal', self.guide_goal)}")
                lines.append(f"**Step 1 of {len(self.steps)} — {self.steps[0]['instruction']}**")
                lines.append(f"Target: {self.steps[0]['target']}")
                if data.get("answer"):
                    lines.append(str(data["answer"]))
                if self.latest_bbox:
                    self.latest_target = self.steps[0]["target"]
                    self.target_button.setEnabled(True)
                else:
                    lines.append("The first step's target isn't clearly visible yet. You can adjust the screen and retry.")
                if len(self.steps) > 1:
                    self.next_button.setText("Next step →")
                    self.next_button.show()
                self.done_button.show()
        else:
            lines.extend([
                f"**Target:** {data.get('target', 'Requested element')}",
                f"**Found:** {'Yes' if data.get('found') else 'Not confidently found'}",
                str(data.get('reason', '')),
            ])
            if mode == "guide_target" and self.steps:
                step = self.steps[self.current_step]
                lines.insert(0, f"**Step {self.current_step + 1} of {len(self.steps)} — {step['instruction']}**")
                self.next_button.setText("Next step →" if self.current_step < len(self.steps) - 1 else "Finish guide")
                if self.current_step < len(self.steps) - 1:
                    self.next_button.show()
                else:
                    self.done_button.show()
                if self.latest_bbox:
                    self.latest_target = step["target"]
            elif mode == "locate":
                if self.latest_bbox:
                    lines.append(f"Normalized box: {self.latest_bbox}")

        if usage_text:
            lines.extend(["", f"_{usage_text}_"])
        self.result.setMarkdown("\n\n".join(lines))
        self.adjustSize()
        self.place_near_cursor()
        self.set_idle("Ready · Enter to ask · Esc to hide · × to quit")

    def _vision_failed(self, message: str) -> None:
        if self._shutdown_requested:
            return
        self.result.setPlainText(f"The request failed: {message}\n\nYou can retry. A timeout does not by itself tell us whether the model recognized the image or what was billed.")
        self.set_idle("Request failed · ready to retry")

    def _vision_finished(self) -> None:
        self.worker = None
        self._close_after_worker()

    def show_target(self) -> None:
        if not self.image_path or not self.latest_bbox:
            return
        try:
            from PIL import Image
            with Image.open(self.image_path) as image:
                image_width, image_height = image.size
            x1n, y1n, x2n, y2n = self.latest_bbox
            x1, y1, x2, y2 = (
                round(x1n * image_width / 1000),
                round(y1n * image_height / 1000),
                round(x2n * image_width / 1000),
                round(y2n * image_height / 1000),
            )
            screens = QGuiApplication.screens()
            if not screens:
                raise RuntimeError("No displays detected by Qt.")
            virtual = QRect(screens[0].geometry())
            for screen in screens[1:]:
                virtual = virtual.united(screen.geometry())
            gx1 = round(virtual.x() + x1 * virtual.width() / image_width)
            gy1 = round(virtual.y() + y1 * virtual.height() / image_height)
            gx2 = round(virtual.x() + x2 * virtual.width() / image_width)
            gy2 = round(virtual.y() + y2 * virtual.height() / image_height)
            global_rect = QRect(gx1, gy1, max(1, gx2 - gx1), max(1, gy2 - gy1))

            overlays: list[HighlightOverlay] = []
            for screen in screens:
                sg = screen.geometry()
                part = global_rect.intersected(sg)
                if part.isEmpty():
                    continue
                local = part.translated(-sg.x(), -sg.y())
                overlays.append(HighlightOverlay(screen, local, self.latest_target or "OmniGuide target"))
            if not overlays:
                raise RuntimeError("The target does not intersect the current desktop geometry.")
            self.hide()
            QTimer.singleShot(350, lambda: self._show_overlays(overlays))
        except Exception as exc:
            self.status_label.setText(f"Could not show highlight: {exc}")

    def _show_overlays(self, overlays: list[HighlightOverlay]) -> None:
        self.overlays = overlays
        for overlay in self.overlays:
            overlay.showFullScreen()
        self.restore_timer.start(4500)

    def _remove_highlight_restore_palette(self) -> None:
        for overlay in self.overlays:
            overlay.close()
            overlay.deleteLater()
        self.overlays.clear()
        self.popup()
        self.status_label.setText("Highlight closed · click the target, then choose Next step")

    def next_step(self) -> None:
        if not self.steps:
            return
        if self.current_step >= len(self.steps) - 1:
            self.finish_guide()
            return
        self.current_step += 1
        self.pending_mode = "guide_target"
        self.pending_request = self.guide_goal
        self.latest_bbox = None
        self.target_button.setEnabled(False)
        self.set_busy("Capturing the updated screen")
        self.hide()
        QTimer.singleShot(350, self._start_capture)

    def finish_guide(self) -> None:
        self.steps = []
        self.current_step = 0
        self.guide_goal = ""
        self.latest_bbox = None
        self.target_button.setEnabled(False)
        self.next_button.hide()
        self.done_button.hide()
        self.result.setPlainText("Guide finished. Ask OmniGuide for another task whenever you're ready.")
        self.adjustSize()
        self.popup()
        self.set_idle("Ready for another task")


def toggle_existing_instance() -> bool:
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    if not socket.waitForConnected(250):
        return False
    socket.write(b"toggle\n")
    socket.waitForBytesWritten(250)
    socket.disconnectFromServer()
    return True


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("OmniGuide")
    app.setQuitOnLastWindowClosed(True)
    if toggle_existing_instance():
        return 0

    window = OmniGuidePalette()
    server = QLocalServer(app)
    QLocalServer.removeServer(SERVER_NAME)
    server.listen(SERVER_NAME)

    def handle_connection() -> None:
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            socket.readyRead.connect(lambda s=socket: (s.readAll(), window.toggle(), s.deleteLater()))
            socket.disconnected.connect(socket.deleteLater)

    server.newConnection.connect(handle_connection)
    window.popup()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
