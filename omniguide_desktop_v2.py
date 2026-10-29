from __future__ import annotations

import base64
import io
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
from PIL import Image, ImageDraw
from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
MODEL_NAME = "meta-models/muse-spark-open-1026"
BASE_URL = "https://inference.baseten.co/v1"


STYLESHEET = """
QWidget {
    background: #0b1020;
    color: #edf2ff;
    font-family: Inter, 'Noto Sans', sans-serif;
    font-size: 13px;
}
QFrame#Sidebar {
    background: #10182a;
    border: 1px solid #202d45;
    border-radius: 16px;
}
QFrame#PreviewFrame {
    background: #0e1627;
    border: 1px solid #202d45;
    border-radius: 16px;
}
QLabel#Brand {
    color: #72e0ff;
    font-size: 22px;
    font-weight: 800;
    letter-spacing: 2px;
}
QLabel#Muted { color: #9aabc6; }
QLabel#Section { color: #d9e7ff; font-size: 14px; font-weight: 700; }
QLineEdit, QTextEdit {
    background: #0b1221;
    border: 1px solid #2a3b59;
    border-radius: 9px;
    padding: 10px;
    selection-background-color: #167bb4;
}
QLineEdit:focus, QTextEdit:focus { border-color: #52c7f4; }
QPushButton {
    background: #1a2941;
    border: 1px solid #324967;
    border-radius: 9px;
    padding: 10px 12px;
    font-weight: 600;
}
QPushButton:hover { background: #243958; border-color: #52c7f4; }
QPushButton:disabled { color: #64748b; background: #182236; border-color: #26344a; }
QPushButton#Primary {
    background: #087fa7;
    border: 1px solid #29b9e7;
    color: white;
    padding: 12px;
}
QPushButton#Primary:hover { background: #0997c2; }
QLabel#Status { color: #72e0ff; font-weight: 600; }
QProgressBar {
    background: #1a2941;
    border: 0;
    border-radius: 3px;
    max-height: 5px;
    min-height: 5px;
}
QProgressBar::chunk { background: #52c7f4; border-radius: 3px; }
QLabel#ImagePreview {
    background: #080d18;
    border: 1px dashed #2b3a52;
    border-radius: 10px;
    color: #8293ad;
}
"""


def parse_model_json(content: str) -> dict[str, Any]:
    """Parse JSON even if the model unexpectedly wraps it in prose/fences."""
    text = content.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise ValueError("The model did not return a JSON object.")
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError("The model response was not a JSON object.")

    bbox = parsed.get("bbox_1000")
    if bbox is None and isinstance(parsed.get("bbox"), list):
        bbox_value = parsed["bbox"]
        if len(bbox_value) == 4 and all(isinstance(v, (int, float)) for v in bbox_value):
            bbox = bbox_value
        elif bbox_value and isinstance(bbox_value[0], dict):
            item = bbox_value[0]
            if all(k in item for k in ("x_min", "y_min", "x_max", "y_max")):
                bbox = [item[k] for k in ("x_min", "y_min", "x_max", "y_max")]

    if parsed.get("found") is True and bbox is not None:
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ValueError("The model returned an invalid bounding box.")
        if not all(isinstance(v, (int, float)) for v in bbox):
            raise ValueError("Bounding-box coordinates must be numbers.")
        # Clamp to the normalized image bounds and validate box ordering.
        bbox = [max(0, min(1000, int(round(v)))) for v in bbox]
        x1, y1, x2, y2 = bbox
        if x2 <= x1 or y2 <= y1:
            raise ValueError("The model returned an empty or reversed bounding box.")
        parsed["bbox_1000"] = bbox
    else:
        parsed["found"] = False
        parsed["bbox_1000"] = None

    return parsed


class CaptureWorker(QThread):
    """Capture all visible desktop screens using KDE Spectacle."""
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, output_path: str):
        super().__init__()
        self.output_path = output_path

    def run(self) -> None:
        try:
            spectacle = shutil.which("spectacle")
            if not spectacle:
                raise RuntimeError(
                    "Spectacle was not found. Install it with: sudo dnf install spectacle"
                )

            result = subprocess.run(
                [spectacle, "--background", "--nonotify", "--output", self.output_path],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "No details returned.").strip()
                raise RuntimeError(f"Spectacle exited with code {result.returncode}: {detail}")

            output = Path(self.output_path)
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError("Spectacle finished but did not create a screenshot.")

            self.succeeded.emit(str(output))
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


def infer_request_mode(request: str, selected_mode: str) -> str:
    """Choose between general screen Q&A and locating/highlighting a target."""
    if selected_mode == "Ask about screen":
        return "ask"
    if selected_mode == "Find & highlight":
        return "locate"

    text = request.casefold().strip()
    locate_phrases = (
        "find ", "locate ", "highlight ", "circle ", "point to ",
        "outline ", "where is ", "where's ", "where are ",
        "show me where", "identify the button", "identify the menu",
        "select the ", "the button for", "the menu item",
    )
    if any(phrase in text for phrase in locate_phrases):
        return "locate"

    # Default to answering the user's question about the screenshot rather than
    # treating the complete sentence as the literal name of a UI element.
    return "ask"


class VisionWorker(QThread):
    succeeded = Signal(object, object, str)
    failed = Signal(str)

    def __init__(self, image_path: str, request: str, mode: str):
        super().__init__()
        self.image_path = image_path
        self.request = request
        self.mode = mode

    def run(self) -> None:
        try:
            api_key = os.getenv("BASETEN_API_KEY")
            if not api_key:
                raise RuntimeError(
                    "BASETEN_API_KEY is missing. Check the .env file in the project folder."
                )

            image_path = Path(self.image_path)
            suffix = image_path.suffix.lower()
            mime_type = {".png": "image/png", ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(suffix)
            if not mime_type:
                mime_type = "image/png"
            raw = image_path.read_bytes()
            encoded = base64.b64encode(raw).decode("ascii")
            image_url = f"data:{mime_type};base64,{encoded}"

            if self.mode == "locate":
                prompt = f"""
You are OmniGuide, a screen-aware desktop assistant. The user wants you to locate a target in the screenshot.

Target requested by the user: {self.request!r}

Return only one JSON object with these fields:
- mode: "locate"
- target: a short name for the target
- found: true or false
- bbox_1000: [x_min, y_min, x_max, y_max], or null
- reason: concise visual evidence for the result

Coordinates must be integers on a normalized 0–1000 grid relative to the full image.
The origin is the top-left; the bottom-right is (1000, 1000).
Draw a tight box around the target itself, not its parent menu or nearby controls.
The request describes what to locate; it is NOT necessarily text that must appear verbatim on screen.
Do not assume the target's position. Do not guess. If it is not clearly visible, use found=false and bbox_1000=null.
"""
            else:
                prompt = f"""
You are OmniGuide, a helpful screen-aware AI companion living alongside the user's desktop.

The user asks: {self.request!r}

Look at the supplied screenshot and answer the user's request based on visible evidence.
You may describe the apps and visible content, explain what is happening, read visible error messages,
or provide useful context about the current screen. Treat the user's message as a QUESTION or REQUEST
about the screenshot, not as the literal name of a UI element to find.
Be concise but genuinely helpful. If some content is too small, cropped, or unclear, say so rather than guessing.
Do not claim to have searched the internet or inspected anything outside this screenshot.
Do not return bounding-box coordinates unless the user explicitly asks for a location; this mode is for answering about the screen.
"""

            client = OpenAI(
                api_key=api_key,
                base_url=BASE_URL,
                timeout=120.0,
                max_retries=0,
            )
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    }
                ],
            )

            content = response.choices[0].message.content or ""
            if not isinstance(content, str):
                content = str(content)

            usage = response.usage
            usage_text = ""
            if usage:
                usage_text = (
                    f"Tokens: {usage.prompt_tokens:,} input · "
                    f"{usage.completion_tokens:,} output · {usage.total_tokens:,} total"
                )

            if self.mode == "locate":
                parsed = parse_model_json(content)
                parsed["mode"] = "locate"
            else:
                parsed = {
                    "mode": "ask",
                    "answer": content.strip(),
                    "found": False,
                    "bbox_1000": None,
                    "reason": "",
                }
            self.succeeded.emit(parsed, usage_text, image_path.name)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ImagePreview(QLabel):
    def __init__(self) -> None:
        super().__init__("Open a screenshot to begin")
        self.setObjectName("ImagePreview")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(520, 380)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._source_pixmap: QPixmap | None = None

    def set_source(self, pixmap: QPixmap) -> None:
        self._source_pixmap = pixmap
        self._refresh_scaled()

    def _refresh_scaled(self) -> None:
        if self._source_pixmap and not self._source_pixmap.isNull():
            self.setPixmap(
                self._source_pixmap.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API name)
        super().resizeEvent(event)
        self._refresh_scaled()


class OmniGuideWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OmniGuide — Desktop Companion")
        self.resize(1220, 800)
        self.setMinimumSize(960, 640)

        self.image_path: str | None = None
        self.annotated_bytes: bytes | None = None
        self.worker: VisionWorker | None = None
        self.capture_worker: CaptureWorker | None = None
        self.capture_path = str(Path(tempfile.gettempdir()) / f"omniguide_capture_{os.getpid()}.png")
        self.loading_frames = ["·", "··", "···", "····"]
        self.loading_index = 0
        self.loading_timer = QTimer(self)
        self.loading_timer.setInterval(260)
        self.loading_timer.timeout.connect(self._animate_loading)

        root = QWidget()
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(18, 18, 18, 18)
        root_layout.setSpacing(16)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(330)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(20, 22, 20, 20)
        side.setSpacing(12)

        brand = QLabel("OMNIGUIDE")
        brand.setObjectName("Brand")
        side.addWidget(brand)

        tagline = QLabel("A screen-aware AI companion")
        tagline.setObjectName("Muted")
        side.addWidget(tagline)

        intro = QLabel(
            "Ask a question about your screen, or ask OmniGuide to find and highlight a specific thing."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        side.addWidget(intro)
        side.addSpacing(10)

        screenshot_heading = QLabel("SCREENSHOT")
        screenshot_heading.setObjectName("Section")
        side.addWidget(screenshot_heading)

        self.capture_button = QPushButton("Capture desktop & ask  →")
        self.capture_button.setObjectName("Primary")
        self.capture_button.setToolTip(
            "Hide OmniGuide, capture the desktop with Spectacle, then ask about the screen or locate a target with Muse Spark Open."
        )
        self.capture_button.clicked.connect(self.capture_desktop_and_analyze)
        side.addWidget(self.capture_button)

        self.open_button = QPushButton("Open screenshot manually…")
        self.open_button.clicked.connect(self.open_screenshot)
        side.addWidget(self.open_button)

        self.file_label = QLabel("No screenshot selected")
        self.file_label.setWordWrap(True)
        self.file_label.setObjectName("Muted")
        side.addWidget(self.file_label)
        side.addSpacing(6)

        target_heading = QLabel("ASK OR FIND SOMETHING")
        target_heading.setObjectName("Section")
        side.addWidget(target_heading)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Auto (recommended)", "Ask about screen", "Find & highlight"])
        self.mode_combo.setToolTip(
            "Auto answers general screen questions and highlights targets when you ask to find or point to something."
        )
        side.addWidget(self.mode_combo)

        self.target_input = QLineEdit("Describe what you see on my screen")
        self.target_input.setPlaceholderText(
            "e.g. describe the screen, explain an error, or find the Save button"
        )
        self.target_input.returnPressed.connect(self.analyze)
        side.addWidget(self.target_input)

        self.analyze_button = QPushButton("Analyze screen  →")
        self.analyze_button.setObjectName("Primary")
        self.analyze_button.clicked.connect(self.analyze)
        side.addWidget(self.analyze_button)

        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.progress.hide()
        side.addWidget(self.progress)

        self.status_label = QLabel("Ready when you are.")
        self.status_label.setObjectName("Status")
        self.status_label.setWordWrap(True)
        side.addWidget(self.status_label)

        result_heading = QLabel("RESULT")
        result_heading.setObjectName("Section")
        side.addWidget(result_heading)

        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setPlaceholderText("Your screen answer or target coordinates will appear here.")
        self.result_text.setMinimumHeight(150)
        side.addWidget(self.result_text, 1)

        self.save_button = QPushButton("Save annotated screenshot…")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_annotated)
        side.addWidget(self.save_button)

        footer = QLabel("Prototype · Muse Spark Open via Baseten")
        footer.setObjectName("Muted")
        side.addWidget(footer)

        preview_frame = QFrame()
        preview_frame.setObjectName("PreviewFrame")
        preview_layout = QVBoxLayout(preview_frame)
        preview_layout.setContentsMargins(16, 16, 16, 16)
        preview_layout.setSpacing(10)

        preview_header = QHBoxLayout()
        preview_title = QLabel("VISUAL CONTEXT")
        preview_title.setObjectName("Section")
        preview_header.addWidget(preview_title)
        preview_header.addStretch(1)
        self.dimensions_label = QLabel("Original image coordinates preserved")
        self.dimensions_label.setObjectName("Muted")
        preview_header.addWidget(self.dimensions_label)
        preview_layout.addLayout(preview_header)

        self.preview = ImagePreview()
        preview_layout.addWidget(self.preview, 1)

        hint = QLabel(
            "Ask about the screen or find a target. Highlights are shown on this preview; a floating live-desktop overlay comes next."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        preview_layout.addWidget(hint)

        root_layout.addWidget(sidebar)
        root_layout.addWidget(preview_frame, 1)
        self.setCentralWidget(root)
        self.setStyleSheet(STYLESHEET)

    def capture_desktop_and_analyze(self) -> None:
        """Hide this window, capture the desktop, then analyze the captured image."""
        if (self.worker and self.worker.isRunning()) or (self.capture_worker and self.capture_worker.isRunning()):
            return

        target = self.target_input.text().strip()
        if not target:
            QMessageBox.information(self, "Enter a target", "Enter a question about the screen or describe what you want to find.")
            return
        if not shutil.which("spectacle"):
            QMessageBox.warning(
                self,
                "Spectacle is required",
                "KDE Spectacle was not found. Install it with:\n\nsudo dnf install spectacle",
            )
            return

        self.capture_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.analyze_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.status_label.setText("Preparing to capture your desktop…")
        self.result_text.setPlainText("OmniGuide is preparing a fresh desktop screenshot.\n\nThe assistant window will briefly hide so it does not cover the applications behind it.")

        # Let Qt repaint the status first, then hide before Spectacle captures.
        self.hide()
        QTimer.singleShot(500, self._start_capture_worker)

    def _start_capture_worker(self) -> None:
        self.capture_worker = CaptureWorker(self.capture_path)
        self.capture_worker.succeeded.connect(self._on_capture_succeeded)
        self.capture_worker.failed.connect(self._on_capture_failed)
        self.capture_worker.finished.connect(self._on_capture_finished)
        self.capture_worker.start()

    def _on_capture_succeeded(self, path: str) -> None:
        self._load_screenshot(path)
        self.show()
        self.raise_()
        self.activateWindow()
        self.status_label.setText("Desktop captured. Sending the screenshot to Muse Spark Open…")
        # The screenshot is saved locally; the next step sends it for the requested visual analysis.
        self.analyze()

    def _on_capture_failed(self, message: str) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.capture_button.setEnabled(True)
        self.open_button.setEnabled(True)
        self.analyze_button.setEnabled(True)
        self.status_label.setText("Desktop capture failed.")
        QMessageBox.warning(self, "Could not capture desktop", message)

    def _on_capture_finished(self) -> None:
        self.capture_worker = None

    def _load_screenshot(self, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            raise RuntimeError("The captured screenshot could not be opened.")
        self.image_path = path
        self.annotated_bytes = None
        self.preview.set_source(pixmap)
        self.file_label.setText(Path(path).name)
        self.dimensions_label.setText(f"{pixmap.width()} × {pixmap.height()} px · source resolution")
        self.result_text.clear()
        self.save_button.setEnabled(False)
        self.status_label.setText("Screenshot loaded. Choose a target to find.")

    def open_screenshot(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open desktop screenshot",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.webp)",
        )
        if not path:
            return
        try:
            self._load_screenshot(path)
        except Exception as exc:
            QMessageBox.warning(self, "Could not open image", str(exc))
            return

    def analyze(self) -> None:
        if self.worker and self.worker.isRunning():
            return
        if not self.image_path:
            QMessageBox.information(self, "Choose a screenshot", "Open a screenshot before asking OmniGuide to analyze it.")
            return
        target = self.target_input.text().strip()
        if not target:
            QMessageBox.information(self, "Enter a target", "Enter a question about the screen or describe a target to locate.")
            return

        self.analyze_button.setEnabled(False)
        self.capture_button.setEnabled(False)
        self.open_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.progress.show()
        self.loading_index = 0
        mode = infer_request_mode(target, self.mode_combo.currentText())
        self.active_mode = mode
        if mode == "locate":
            self.status_label.setText("Muse Spark is locating the target ·")
            self.result_text.setPlainText("Looking for the requested target and preparing its highlight…")
        else:
            self.status_label.setText("Muse Spark is understanding your screen ·")
            self.result_text.setPlainText("Reading the screenshot and preparing an answer…")
        self.loading_timer.start()

        mode = infer_request_mode(target, self.mode_combo.currentText())
        self.active_mode = mode
        self.worker = VisionWorker(self.image_path, target, mode)
        self.worker.succeeded.connect(self.on_success)
        self.worker.failed.connect(self.on_failure)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    def _animate_loading(self) -> None:
        self.loading_index = (self.loading_index + 1) % len(self.loading_frames)
        dots = self.loading_frames[self.loading_index]
        message = "locating the target" if getattr(self, "active_mode", "ask") == "locate" else "understanding your screen"
        self.status_label.setText(f"Muse Spark is {message} {dots}")

    def on_success(self, data: dict[str, Any], usage_text: str, image_name: str) -> None:
        mode = data.get("mode", getattr(self, "active_mode", "locate"))
        self.annotated_bytes = None
        self.save_button.setEnabled(False)

        if mode == "ask":
            details = [data.get("answer", "The model returned an empty answer.")]
            # Restore the unannotated source image if a prior request drew a box.
            if self.image_path:
                original_pixmap = QPixmap(self.image_path)
                if not original_pixmap.isNull():
                    self.preview.set_source(original_pixmap)
            if usage_text:
                details.extend(["", usage_text])
            self.result_text.setPlainText("\n".join(details))
            self.status_label.setText("Screen understood." if data.get("answer") else "No answer returned.")
            return

        details = [
            f"Target: {data.get('target', 'requested element')}",
            f"Found: {'Yes' if data.get('found') else 'No'}",
            f"Reason: {data.get('reason', 'No explanation returned.')}",
        ]

        if data.get("found") and data.get("bbox_1000"):
            try:
                with Image.open(self.image_path) as src:
                    image = src.convert("RGB")
                    width, height = image.size
                    x1n, y1n, x2n, y2n = data["bbox_1000"]
                    x1 = round(x1n * width / 1000)
                    y1 = round(y1n * height / 1000)
                    x2 = round(x2n * width / 1000)
                    y2 = round(y2n * height / 1000)
                    stroke = max(3, min(width, height) // 250)
                    draw = ImageDraw.Draw(image)
                    draw.rectangle((x1, y1, x2, y2), outline=(255, 62, 83), width=stroke)
                    label = f"{data.get('target', 'Target')}  ({x1}, {y1}, {x2}, {y2})"
                    text_y = max(0, y1 - 24)
                    label_right = min(width - 1, x1 + 8 * len(label) + 8)
                    label_bottom = min(height - 1, text_y + 22)
                    draw.rectangle((x1, text_y, label_right, label_bottom), fill=(255, 62, 83))
                    draw.text((x1 + 4, text_y + 4), label, fill=(255, 255, 255))
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    self.annotated_bytes = buffer.getvalue()
                    annotated_pixmap = QPixmap()
                    if not annotated_pixmap.loadFromData(self.annotated_bytes, "PNG"):
                        raise ValueError("Could not load the annotated image into the preview.")
                    self.preview.set_source(annotated_pixmap)
                    details.append(f"Normalized box: {data['bbox_1000']}")
                    details.append(f"Pixel box: [{x1}, {y1}, {x2}, {y2}]")
            except Exception as exc:
                details.append(f"Could not render highlight: {exc}")
        else:
            # Ensure a previous successful highlight is not displayed for a miss.
            if self.image_path:
                original_pixmap = QPixmap(self.image_path)
                if not original_pixmap.isNull():
                    self.preview.set_source(original_pixmap)

        if usage_text:
            details.extend(["", usage_text])
        self.result_text.setPlainText("\n".join(details))
        self.status_label.setText("Target located." if data.get("found") else "Target not confidently located.")
        self.save_button.setEnabled(self.annotated_bytes is not None)

    def on_failure(self, message: str) -> None:
        self.result_text.setPlainText(message)
        self.status_label.setText("The request failed. Check the message above.")

    def on_worker_finished(self) -> None:
        self.loading_timer.stop()
        self.progress.hide()
        self.analyze_button.setEnabled(True)
        self.capture_button.setEnabled(True)
        self.open_button.setEnabled(True)
        self.worker = None

    def save_annotated(self) -> None:
        if not self.annotated_bytes:
            return
        default_name = f"annotated_{Path(self.image_path or 'screenshot.png').stem}.png"
        path, _ = QFileDialog.getSaveFileName(
            self, "Save annotated screenshot", str(Path(self.image_path or APP_DIR).parent / default_name), "PNG image (*.png)"
        )
        if path:
            if not path.lower().endswith(".png"):
                path += ".png"
            Path(path).write_bytes(self.annotated_bytes)
            self.status_label.setText(f"Saved annotation: {Path(path).name}")


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("OmniGuide")
    window = OmniGuideWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
