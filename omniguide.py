"""OmniGuide v1.8.4: normal launcher process + isolated LayerShellQt overlay daemon.

Wayland guidance uses compositor-managed LayerShellQt surfaces; a click-through, non-activating overlay cannot receive
keys. Grabbing Escape globally would steal it from every app. Cancel during
guidance is `python omniguide_palette_v1_2.py --cancel` via QLocalServer.
Bind that in KDE Custom Shortcuts if you want a cancel key; do not bind
Alt+Space. The summon shortcut (default Ctrl+Shift+Space) continues a guide.
Escape still hides the palette when the palette itself has focus.
"""

from __future__ import annotations

import os
import sys

# The main palette MUST use the normal Wayland shell integration.
# Only the isolated --overlay-daemon child process enables LayerShellQt.
if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
    os.environ["QT_QPA_PLATFORM"] = "wayland"
    if "--overlay-daemon" not in sys.argv:
        os.environ.pop("QT_WAYLAND_SHELL_INTEGRATION", None)
    else:
        os.environ["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"

import json
import math
import re
import time
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from PySide6.QtCore import QCoreApplication, QByteArray, QThread, QTimer, QPointF, QRect, Qt, Signal, QUrl
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtGui import QColor, QCursor, QFont, QGuiApplication, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QMenu,
    QSystemTrayIcon,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
load_dotenv(APP_DIR / ".env")
MODEL_NAME = "meta-models/muse-spark-open-1026"
BASE_URL = "https://inference.baseten.co/v1"
APP_VERSION = "1.8.4"
SERVER_NAME = "omniguide-palette-onee11-v184"
OVERLAY_SERVER_NAME = "omniguide-overlay-onee11-v184"

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
            import base64
            from io import BytesIO
            from PIL import Image

            path = Path(self.image_path)
            # Send a compressed copy to reduce upload and vision-processing
            # overhead. The original screenshot remains untouched for precise
            # coordinate conversion in show_target().
            with Image.open(path) as source_image:
                request_image = source_image.convert("RGB")
                request_image.thumbnail((2304, 2304), Image.Resampling.LANCZOS)
                buffer = BytesIO()
                request_image.save(buffer, format="JPEG", quality=90, optimize=True)
                encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            image_url = f"data:image/jpeg;base64,{encoded}"

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
                    next_instruction = self.step.get("next_instruction", "")
                    next_target = self.step.get("next_target", "")
                    prompt = f"""You are OmniGuide's visual grounding and action-verification engine.
The user's overall goal is: {self.request!r}
CURRENT GUIDE STEP:
Instruction: {self.step.get('instruction', '')!r}
Expected target: {self.step.get('target', '')!r}
NEXT GUIDE STEP (if any):
Instruction: {next_instruction!r}
Expected target: {next_target!r}

Inspect the CURRENT screenshot and classify the state of the CURRENT step. Do not assume the user has completed it merely because its target disappeared.
Choose exactly one step_state:
- "target_visible": the current step still needs to be performed and its target is visibly present. Return a tight box around the CURRENT target.
- "already_completed": there is positive visual evidence that the current step has already been completed, but the next step's target is not clearly visible. Return found=false and bbox_1000=null.
- "next_target_visible": there is positive visual evidence the current step has been completed AND the NEXT step's target is visible. Return a tight box around the NEXT target; set target to the next target's name.
- "not_ready": the current target is not visible and there is no reliable evidence that the current step has been completed (for example, a required menu has not been opened yet). Return found=false.
- "uncertain": the screenshot does not provide enough evidence to decide. Return found=false.

Evidence rules:
1. A disappeared menu item alone is NOT proof of completion.
2. A resulting dialog, page, selected item, changed value, or other visible post-action state can be evidence of completion when it clearly follows from the current step.
3. If the next step target is visible after that transition, use "next_target_visible" so OmniGuide can move directly to it.
4. Never claim a step is complete based only on an assumption. If uncertain, choose "uncertain".

Return ONLY JSON with fields: step_state, target, found, bbox_1000, reason. Coordinates must be integers on a normalized 0-1000 grid relative to the complete supplied screenshot, origin top-left. Draw a tight box around the target itself, not its parent or nearby controls. For "already_completed", "not_ready", and "uncertain", found must be false and bbox_1000 must be null."""
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
    """Click-through, non-activating highlight. Must not take keyboard focus.

    WindowTransparentForInput is required so the user can click the real
    control. That also means this widget will never see Escape on Wayland.
    """

    def __init__(self, screen, rect: QRect, label: str):
        super().__init__(None)
        self.rect_to_draw = QRect(rect)
        self.label = label
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
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


def local_help_answer(request: str) -> str | None:
    """Answer a few deterministic KDE questions without screenshot/API use."""
    value = re.sub(r"[^a-z0-9]+", " ", request.casefold()).strip()
    words = set(value.split())

    terminal_context = bool(words.intersection({"terminal", "konsole", "shell"}))
    tab_context = bool(words.intersection({"tab", "tabs"}))
    new_or_open = bool(words.intersection({"new", "open", "create", "another"}))

    if terminal_context and tab_context and new_or_open:
        return (
            "## Open a new tab in KDE Konsole\n\n"
            "Press **Ctrl + Shift + T**. This is Konsole's default **File → New Tab** shortcut.\n\n"
            "You can also use **File → New Tab** from the menu. If the shortcut was "
            "customized on your system, the menu shows the currently assigned shortcut.\n\n"
            "_Answered locally — no screenshot or API request was used._"
        )

    editor_context = bool(words.intersection({"kwrite", "kate", "editor"}))
    save_context = bool(words.intersection({"save", "saving", "saved"}))
    if editor_context and save_context:
        return (
            "## Save a document in KWrite\n\n"
            "1. Press **Ctrl + S** to save the current document.\n"
            "2. If it is a new document, the **Save As** dialog opens. Choose a folder, "
            "enter a filename, then click **Save**.\n"
            "3. To save it under a different name or location later, use **Ctrl + Shift + S**.\n\n"
            "_Answered locally — no screenshot or API request was used._"
        )

    return None


class TriangleCursorOverlay(QWidget):
    """Animated, click-through triangular guide pointer for one monitor.

    Each monitor owns a transparent overlay so the pointer can travel across
    the virtual desktop without changing the existing highlight coordinate
    mapping or taking focus from the application being guided.
    """

    def __init__(self, screen, start_global: QPointF, target_global: QPointF, message: str):
        super().__init__(None)
        self.screen = screen
        self.screen_geometry = QRect(screen.geometry())
        self.start_global = QPointF(start_global)
        self.target_global = QPointF(target_global)
        self.global_position = QPointF(start_global)
        self.message = message.strip() or "Follow the highlighted target"
        self.trail: list[QPointF] = []
        self.started_at = time.monotonic()
        distance = math.hypot(
            self.target_global.x() - self.start_global.x(),
            self.target_global.y() - self.start_global.y(),
        )
        self.duration = max(0.48, min(1.05, distance / 1750.0))
        self.control_global = self._control_point(self.start_global, self.target_global)
        self.angle_degrees = self._heading(
            self.target_global.x() - self.start_global.x(),
            self.target_global.y() - self.start_global.y(),
        )
        self.arrived = distance < 4

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setGeometry(self.screen_geometry)
        self.winId()
        handle = self.windowHandle()
        if handle is not None:
            handle.setScreen(screen)
        self.setGeometry(self.screen_geometry)

        self.animation_timer = QTimer(self)
        self.animation_timer.setInterval(16)
        self.animation_timer.timeout.connect(self._tick)

    def start_animation(self) -> None:
        """Start the flight when the overlay becomes visible, not when built."""
        self.started_at = time.monotonic()
        self.global_position = QPointF(self.start_global)
        self.trail.clear()
        self.arrived = math.hypot(
            self.target_global.x() - self.start_global.x(),
            self.target_global.y() - self.start_global.y(),
        ) < 4
        self.animation_timer.start()
        self.update()

    @staticmethod
    def _heading(dx: float, dy: float) -> float:
        if abs(dx) + abs(dy) < 0.001:
            return 0.0
        # The triangle's unrotated tip faces upward (negative Y).
        return math.degrees(math.atan2(dy, dx)) + 90.0

    @staticmethod
    def _control_point(start: QPointF, end: QPointF) -> QPointF:
        dx = end.x() - start.x()
        dy = end.y() - start.y()
        distance = math.hypot(dx, dy)
        if distance < 1:
            return QPointF(start)
        bend = min(115.0, max(28.0, distance * 0.16))
        # A consistent perpendicular bend creates a deliberate, curved flight.
        return QPointF(
            (start.x() + end.x()) / 2.0 - (dy / distance) * bend,
            (start.y() + end.y()) / 2.0 + (dx / distance) * bend,
        )

    def _tick(self) -> None:
        elapsed = time.monotonic() - self.started_at
        if not self.arrived:
            t = min(1.0, elapsed / self.duration)
            eased = 1.0 - (1.0 - t) ** 3
            inverse = 1.0 - eased
            sx, sy = self.start_global.x(), self.start_global.y()
            cx, cy = self.control_global.x(), self.control_global.y()
            ex, ey = self.target_global.x(), self.target_global.y()
            x = inverse * inverse * sx + 2 * inverse * eased * cx + eased * eased * ex
            y = inverse * inverse * sy + 2 * inverse * eased * cy + eased * eased * ey
            # Use the Bézier tangent to rotate the pointer along its flight.
            tangent_x = 2 * inverse * (cx - sx) + 2 * eased * (ex - cx)
            tangent_y = 2 * inverse * (cy - sy) + 2 * eased * (ey - cy)
            self.angle_degrees = self._heading(tangent_x, tangent_y)
            self.global_position = QPointF(x, y)
            if t >= 1.0:
                self.arrived = True
                self.global_position = QPointF(self.target_global)
        else:
            # A restrained hover makes the pointer feel alive without wobbling
            # away from the element it is indicating.
            self.global_position = QPointF(
                self.target_global.x(),
                self.target_global.y() + math.sin(elapsed * 4.2) * 2.2,
            )

        if not self.trail or math.hypot(
            self.global_position.x() - self.trail[-1].x(),
            self.global_position.y() - self.trail[-1].y(),
        ) >= 3.0:
            self.trail.append(QPointF(self.global_position))
            self.trail = self.trail[-9:]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        geometry = self.screen_geometry
        global_pos = self.global_position
        if not geometry.contains(round(global_pos.x()), round(global_pos.y())):
            return

        x = global_pos.x() - geometry.x()
        y = global_pos.y() - geometry.y()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # Fading motion trail. Each monitor paints only the part of the flight
        # that falls within its own screen geometry.
        visible_trail = [p for p in self.trail if geometry.contains(round(p.x()), round(p.y()))]
        for index, point in enumerate(visible_trail):
            progress = (index + 1) / max(1, len(visible_trail))
            alpha = int(18 + progress * 72)
            radius = 2.0 + progress * 2.0
            local = QPointF(point.x() - geometry.x(), point.y() - geometry.y())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(77, 220, 255, alpha))
            painter.drawEllipse(local, radius, radius)

        pulse = 1.0 + (0.045 * math.sin((time.monotonic() - self.started_at) * 5.0) if self.arrived else 0.0)
        painter.save()
        painter.translate(x, y)
        painter.rotate(self.angle_degrees)
        painter.scale(pulse, pulse)

        # Soft cyan aura behind a crisp, small triangular pointer.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(62, 210, 255, 38))
        painter.drawEllipse(QRect(-18, -18, 36, 36))
        painter.setBrush(QColor(62, 210, 255, 24))
        painter.drawEllipse(QRect(-24, -24, 48, 48))

        triangle = QPolygonF([QPointF(0, 0), QPointF(-10.5, 25), QPointF(10.5, 25)])
        shadow = QPolygonF([QPointF(2, 3), QPointF(-8.5, 28), QPointF(12.5, 28)])
        painter.setBrush(QColor(0, 8, 18, 145))
        painter.drawPolygon(shadow)
        painter.setPen(QPen(QColor(205, 249, 255, 250), 1.5))
        painter.setBrush(QColor(57, 207, 255, 245))
        painter.drawPolygon(triangle)
        painter.setPen(QPen(QColor(255, 255, 255, 215), 1.2))
        painter.drawLine(QPointF(0, 4), QPointF(0, 17))
        painter.restore()

        self._paint_message(painter, x, y)
        painter.end()

    def _paint_message(self, painter: QPainter, x: float, y: float) -> None:
        font = QFont("Inter", 9, QFont.Weight.DemiBold)
        painter.setFont(font)
        metrics = painter.fontMetrics()
        box_width = min(280, max(158, metrics.horizontalAdvance(self.message) + 28))
        box_height = 36
        box_x = x + 22
        if box_x + box_width > self.width() - 8:
            box_x = x - box_width - 22
        box_y = y - box_height - 18
        if box_y < 8:
            box_y = y + 24
        box_x = max(8, min(box_x, self.width() - box_width - 8))
        box_y = max(8, min(box_y, self.height() - box_height - 8))

        painter.setPen(QPen(QColor(75, 207, 239, 175), 1))
        painter.setBrush(QColor(9, 22, 37, 238))
        painter.drawRoundedRect(round(box_x), round(box_y), round(box_width), box_height, 10, 10)
        painter.setPen(QPen(QColor(101, 228, 255, 235), 2))
        painter.drawLine(round(box_x + 12), round(box_y + 9), round(box_x + 12), round(box_y + 27))
        painter.setPen(QColor(239, 249, 255, 255))
        text = metrics.elidedText(self.message, Qt.TextElideMode.ElideRight, round(box_width - 38))
        painter.drawText(round(box_x + 23), round(box_y + 23), text)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.animation_timer.stop()
        event.accept()


LAYER_SHELL_QML = r"""
import QtQuick
import QtQuick.Window
import org.kde.layershell 1.0 as LayerShell

Window {
    id: root
    visible: false
    width: 640
    height: 420
    color: "transparent"
    // Match the proven standalone test: the compositor must pass pointer input through.
    flags: Qt.FramelessWindowHint | Qt.WindowTransparentForInput
    title: "OmniGuide Guidance Overlay"

    LayerShell.Window.layer: LayerShell.Window.LayerOverlay
    LayerShell.Window.anchors: LayerShell.Window.AnchorTop | LayerShell.Window.AnchorLeft
    LayerShell.Window.margins: Qt.margins(Math.round(root.marginX), Math.round(root.marginY), 0, 0)
    LayerShell.Window.keyboardInteractivity: LayerShell.Window.KeyboardInteractivityNone
    LayerShell.Window.exclusionZone: 0

    property real marginX: 0
    property real marginY: 0
    // Global origin of this bounded surface, used to translate desktop coordinates.
    property real screenX: 0
    property real screenY: 0
    property bool hasHighlight: false
    property real highlightX: 0
    property real highlightY: 0
    property real highlightWidth: 0
    property real highlightHeight: 0
    property string targetLabel: "OmniGuide target"

    property real startX: 0
    property real startY: 0
    property real targetX: 0
    property real targetY: 0
    property real currentX: 0
    property real currentY: 0
    property real angleDegrees: 0
    property real durationMs: 650
    property real startedAt: 0
    property bool arrived: false
    property var trail: []
    property string message: "Follow the highlighted target"

    function heading(dx, dy) {
        if (Math.abs(dx) + Math.abs(dy) < 0.001)
            return 0
        return Math.atan2(dy, dx) * 180 / Math.PI + 90
    }

    function beginAnimation() {
        startedAt = Date.now()
        currentX = startX
        currentY = startY
        angleDegrees = heading(targetX - startX, targetY - startY)
        arrived = Math.hypot(targetX - startX, targetY - startY) < 4
        trail = []
        canvas.requestPaint()
    }

    function advanceAnimation() {
        if (!visible || startedAt <= 0)
            return
        var elapsed = Date.now() - startedAt
        if (!arrived) {
            var t = Math.min(1, elapsed / durationMs)
            var eased = 1 - Math.pow(1 - t, 3)
            var inverse = 1 - eased
            var dx = targetX - startX
            var dy = targetY - startY
            var distance = Math.hypot(dx, dy)
            var bend = Math.min(115, Math.max(28, distance * 0.16))
            var cx = (startX + targetX) / 2 - (distance > 0 ? dy / distance : 0) * bend
            var cy = (startY + targetY) / 2 + (distance > 0 ? dx / distance : 0) * bend
            currentX = inverse * inverse * startX + 2 * inverse * eased * cx + eased * eased * targetX
            currentY = inverse * inverse * startY + 2 * inverse * eased * cy + eased * eased * targetY
            var tangentX = 2 * inverse * (cx - startX) + 2 * eased * (targetX - cx)
            var tangentY = 2 * inverse * (cy - startY) + 2 * eased * (targetY - cy)
            angleDegrees = heading(tangentX, tangentY)
            if (t >= 1) {
                arrived = true
                currentX = targetX
                currentY = targetY
            }
        } else {
            currentX = targetX
            currentY = targetY + Math.sin(elapsed / 1000 * 4.2) * 2.2
        }
        var nextTrail = trail.slice(0)
        if (nextTrail.length === 0 || Math.hypot(currentX - nextTrail[nextTrail.length - 1].x,
                                                 currentY - nextTrail[nextTrail.length - 1].y) >= 3) {
            nextTrail.push({x: currentX, y: currentY})
            if (nextTrail.length > 9)
                nextTrail.shift()
            trail = nextTrail
        }
        canvas.requestPaint()
    }

    function roundedPath(ctx, x, y, w, h, r) {
        var radius = Math.min(r, w / 2, h / 2)
        ctx.beginPath()
        ctx.moveTo(x + radius, y)
        ctx.lineTo(x + w - radius, y)
        ctx.quadraticCurveTo(x + w, y, x + w, y + radius)
        ctx.lineTo(x + w, y + h - radius)
        ctx.quadraticCurveTo(x + w, y + h, x + w - radius, y + h)
        ctx.lineTo(x + radius, y + h)
        ctx.quadraticCurveTo(x, y + h, x, y + h - radius)
        ctx.lineTo(x, y + radius)
        ctx.quadraticCurveTo(x, y, x + radius, y)
        ctx.closePath()
    }

    onVisibleChanged: {
        if (visible)
            beginAnimation()
    }

    Canvas {
        id: canvas
        anchors.fill: parent
        renderStrategy: Canvas.Immediate
        onPaint: {
            var ctx = getContext("2d")
            ctx.clearRect(0, 0, width, height)

            if (root.hasHighlight) {
                ctx.lineWidth = 3
                ctx.strokeStyle = "rgba(67,225,255,0.96)"
                ctx.fillStyle = "rgba(67,225,255,0.13)"
                root.roundedPath(ctx, root.highlightX, root.highlightY,
                                 root.highlightWidth, root.highlightHeight, 8)
                ctx.fill()
                ctx.stroke()

                ctx.font = "600 13px sans-serif"
                var labelWidth = Math.min(Math.max(150, ctx.measureText(root.targetLabel).width + 24), Math.max(150, width - 12))
                var labelHeight = 32
                var lx = Math.min(Math.max(6, root.highlightX), Math.max(6, width - labelWidth - 6))
                var ly = root.highlightY - labelHeight - 7
                if (ly < 6)
                    ly = Math.min(height - labelHeight - 6, root.highlightY + root.highlightHeight + 7)
                ctx.fillStyle = "rgba(13,25,40,0.96)"
                root.roundedPath(ctx, lx, ly, labelWidth, labelHeight, 7)
                ctx.fill()
                ctx.fillStyle = "rgba(235,249,255,1)"
                ctx.fillText(root.targetLabel, lx + 11, ly + 21, labelWidth - 20)
            }

            var localX = root.currentX - root.screenX
            var localY = root.currentY - root.screenY
            var onSurface = localX >= 0 && localY >= 0 && localX <= width && localY <= height
            if (!onSurface)
                return

            for (var i = 0; i < root.trail.length; ++i) {
                var point = root.trail[i]
                var px = point.x - root.screenX
                var py = point.y - root.screenY
                if (px < 0 || py < 0 || px > width || py > height)
                    continue
                var progress = (i + 1) / Math.max(1, root.trail.length)
                ctx.beginPath()
                ctx.arc(px, py, 2 + progress * 2, 0, Math.PI * 2)
                ctx.fillStyle = "rgba(77,220,255," + (0.07 + progress * 0.28) + ")"
                ctx.fill()
            }

            var pulse = 1 + (root.arrived ? 0.045 * Math.sin((Date.now() - root.startedAt) / 1000 * 5) : 0)
            ctx.save()
            ctx.translate(localX, localY)
            ctx.rotate(root.angleDegrees * Math.PI / 180)
            ctx.scale(pulse, pulse)
            ctx.beginPath()
            ctx.arc(0, 0, 18, 0, Math.PI * 2)
            ctx.fillStyle = "rgba(62,210,255,0.15)"
            ctx.fill()
            ctx.beginPath()
            ctx.arc(0, 0, 24, 0, Math.PI * 2)
            ctx.fillStyle = "rgba(62,210,255,0.09)"
            ctx.fill()
            ctx.shadowColor = "rgba(54,143,255,0.8)"
            ctx.shadowBlur = 10
            ctx.beginPath()
            ctx.moveTo(2, 3)
            ctx.lineTo(-8.5, 28)
            ctx.lineTo(12.5, 28)
            ctx.closePath()
            ctx.fillStyle = "rgba(0,8,18,0.57)"
            ctx.fill()
            ctx.shadowBlur = 0
            ctx.beginPath()
            ctx.moveTo(0, 0)
            ctx.lineTo(-10.5, 25)
            ctx.lineTo(10.5, 25)
            ctx.closePath()
            ctx.fillStyle = "rgba(57,207,255,0.96)"
            ctx.strokeStyle = "rgba(205,249,255,0.98)"
            ctx.lineWidth = 1.5
            ctx.fill()
            ctx.stroke()
            ctx.beginPath()
            ctx.moveTo(0, 4)
            ctx.lineTo(0, 17)
            ctx.strokeStyle = "rgba(255,255,255,0.84)"
            ctx.lineWidth = 1.2
            ctx.stroke()
            ctx.restore()

            ctx.font = "600 12px sans-serif"
            var bubbleWidth = Math.min(280, Math.max(158, ctx.measureText(root.message).width + 28))
            var bubbleHeight = 36
            var bx = localX + 22
            if (bx + bubbleWidth > width - 8)
                bx = localX - bubbleWidth - 22
            var by = localY - bubbleHeight - 18
            if (by < 8)
                by = localY + 24
            bx = Math.max(8, Math.min(bx, width - bubbleWidth - 8))
            by = Math.max(8, Math.min(by, height - bubbleHeight - 8))
            ctx.lineWidth = 1
            ctx.strokeStyle = "rgba(75,207,239,0.69)"
            root.roundedPath(ctx, bx, by, bubbleWidth, bubbleHeight, 10)
            ctx.fillStyle = "rgba(9,22,37,0.96)"
            ctx.fill()
            ctx.stroke()
            ctx.beginPath()
            ctx.moveTo(bx + 12, by + 9)
            ctx.lineTo(bx + 12, by + 27)
            ctx.strokeStyle = "rgba(101,228,255,0.92)"
            ctx.lineWidth = 2
            ctx.stroke()
            ctx.fillStyle = "rgba(239,249,255,1)"
            ctx.fillText(root.message, bx + 23, by + 23, bubbleWidth - 38)
        }
    }

    Timer {
        interval: 30
        repeat: true
        running: root.visible
        onTriggered: root.advanceAnimation()
    }
}
"""


class LayerShellOverlaySet:
    """One small, click-through LayerShellQt surface around the current target.

    Earlier builds sized the transparent layer surface around the entire path
    from the mouse to the target. On a large desktop that could become almost
    the size of the whole monitor. This version deliberately caps the surface
    and starts the animated triangle at the closest point inside that surface.
    """

    SYSTEM_QT_PLUGIN_PATH = "/usr/lib64/qt6/plugins"
    SYSTEM_QML_PATH = "/usr/lib64/qt6/qml"
    MAX_SURFACE_WIDTH = 620
    MAX_SURFACE_HEIGHT = 440
    MIN_SURFACE_WIDTH = 420
    MIN_SURFACE_HEIGHT = 280

    def __init__(self, screens, global_rect: QRect, start_global: QPointF,
                 target_global: QPointF, target_label: str, message: str):
        if os.environ.get("XDG_SESSION_TYPE", "").lower() != "wayland":
            raise RuntimeError("LayerShellQt overlays are enabled only in a Wayland session.")
        if os.environ.get("QT_WAYLAND_SHELL_INTEGRATION", "") != "layer-shell":
            raise RuntimeError("Qt was not started with QT_WAYLAND_SHELL_INTEGRATION=layer-shell.")

        if Path(self.SYSTEM_QT_PLUGIN_PATH).exists():
            QCoreApplication.addLibraryPath(self.SYSTEM_QT_PLUGIN_PATH)

        self.engine = QQmlEngine()
        if Path(self.SYSTEM_QML_PATH).exists():
            self.engine.addImportPath(self.SYSTEM_QML_PATH)

        self.component = QQmlComponent(self.engine)
        self.component.setData(QByteArray(LAYER_SHELL_QML.encode("utf-8")), QUrl())
        if self.component.isError():
            errors = "\n".join(error.toString() for error in self.component.errors())
            self.engine.deleteLater()
            raise RuntimeError(f"Could not compile the LayerShellQt overlay QML:\n{errors}")

        self.windows: list[QQuickWindow] = []
        self._closed = False
        try:
            # Attach the surface to the monitor that contains the requested UI
            # target. This avoids creating a desktop-sized window on every monitor.
            target_point = target_global.toPoint()
            target_screen = next(
                (screen for screen in screens if screen.geometry().contains(target_point)),
                None,
            )
            if target_screen is None:
                intersecting = [
                    (screen, global_rect.intersected(screen.geometry()).width()
                     * global_rect.intersected(screen.geometry()).height())
                    for screen in screens
                ]
                intersecting = [(screen, area) for screen, area in intersecting if area > 0]
                if intersecting:
                    target_screen = max(intersecting, key=lambda item: item[1])[0]
            if target_screen is None:
                raise RuntimeError("The detected target does not intersect any current display.")

            screen_geometry = target_screen.geometry()
            desired_width = max(self.MIN_SURFACE_WIDTH, min(self.MAX_SURFACE_WIDTH, global_rect.width() + 340))
            desired_height = max(self.MIN_SURFACE_HEIGHT, min(self.MAX_SURFACE_HEIGHT, global_rect.height() + 260))
            surface_width = min(screen_geometry.width(), desired_width)
            surface_height = min(screen_geometry.height(), desired_height)

            # Position the target roughly 60% across/down, leaving room for the
            # triangle to fly in and for its instruction bubble.
            target_centre = global_rect.center()
            surface_x = round(target_centre.x() - surface_width * 0.60)
            surface_y = round(target_centre.y() - surface_height * 0.60)
            surface_x = max(screen_geometry.left(), min(surface_x, screen_geometry.right() - surface_width + 1))
            surface_y = max(screen_geometry.top(), min(surface_y, screen_geometry.bottom() - surface_height + 1))
            surface_geometry = QRect(surface_x, surface_y, surface_width, surface_height)

            # Keep the starting point inside the small surface. When the mouse
            # is on another part of the desktop, the pointer enters from the
            # nearest edge instead of requiring a monitor-sized overlay.
            inset = 30
            min_x = surface_geometry.left() + inset
            max_x = surface_geometry.right() - inset
            min_y = surface_geometry.top() + inset
            max_y = surface_geometry.bottom() - inset
            bounded_start = QPointF(
                max(min_x, min(float(start_global.x()), max_x)),
                max(min_y, min(float(start_global.y()), max_y)),
            )

            target_part = global_rect.intersected(surface_geometry)
            window = self.component.create()
            if window is None or not isinstance(window, QQuickWindow):
                errors = "\n".join(error.toString() for error in self.component.errors())
                raise RuntimeError(f"Could not create a LayerShellQt window. {errors}")
            self.windows.append(window)

            window.setScreen(target_screen)
            window.setWidth(surface_geometry.width())
            window.setHeight(surface_geometry.height())

            values = {
                "marginX": surface_geometry.x() - screen_geometry.x(),
                "marginY": surface_geometry.y() - screen_geometry.y(),
                "screenX": surface_geometry.x(),
                "screenY": surface_geometry.y(),
                "hasHighlight": not target_part.isEmpty(),
                "highlightX": target_part.x() - surface_geometry.x() if not target_part.isEmpty() else 0,
                "highlightY": target_part.y() - surface_geometry.y() if not target_part.isEmpty() else 0,
                "highlightWidth": target_part.width() if not target_part.isEmpty() else 0,
                "highlightHeight": target_part.height() if not target_part.isEmpty() else 0,
                "targetLabel": (target_label or "OmniGuide target")[:100],
                "startX": bounded_start.x(),
                "startY": bounded_start.y(),
                "targetX": target_global.x(),
                "targetY": target_global.y(),
                "durationMs": max(480.0, min(1050.0, math.hypot(
                    target_global.x() - bounded_start.x(), target_global.y() - bounded_start.y()
                ) / 1.75)),
                "message": (message or "Follow the highlighted target")[:220],
            }
            for name, value in values.items():
                if not window.setProperty(name, value):
                    raise RuntimeError(f"Could not set LayerShellQt overlay property: {name}")

            self.surface_geometry = QRect(surface_geometry)
            print(
                f"OmniGuide LayerShell surface: screen={target_screen.name()} "
                f"geometry={surface_geometry.x()},{surface_geometry.y()} "
                f"{surface_geometry.width()}x{surface_geometry.height()} "
                f"(monitor={screen_geometry.width()}x{screen_geometry.height()})",
                file=sys.stderr, flush=True,
            )
        except Exception:
            self.close()
            raise

        if not self.windows:
            self.close()
            raise RuntimeError("No bounded LayerShellQt overlay was created.")

    def show(self) -> None:
        if self._closed:
            return
        for window in self.windows:
            window.show()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for window in self.windows:
            try:
                window.hide()
                window.close()
                window.deleteLater()
            except RuntimeError:
                pass
        self.windows.clear()
        try:
            self.component.deleteLater()
            self.engine.deleteLater()
        except RuntimeError:
            pass


class BusyIndicator(QWidget):
    """Small animated, click-through status bubble beside the mouse cursor."""

    def __init__(self):
        super().__init__(None)
        self.message = "Working"
        self.angle = 0
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(250, 48)

        self.animation_timer = QTimer(self)
        self.animation_timer.setInterval(40)
        self.animation_timer.timeout.connect(self._animate)

        self.position_timer = QTimer(self)
        self.position_timer.setInterval(80)
        self.position_timer.timeout.connect(self._move_near_cursor)

    def show_message(self, message: str) -> None:
        self.message = message
        self.setFixedWidth(max(230, min(390, 64 + len(message) * 7)))
        self._move_near_cursor()
        self.show()
        self.raise_()
        self.animation_timer.start()
        self.position_timer.start()
        self.update()

    def hide_indicator(self) -> None:
        self.animation_timer.stop()
        self.position_timer.stop()
        self.hide()

    def _animate(self) -> None:
        self.angle = (self.angle + 24) % 360
        self.update()

    def _move_near_cursor(self) -> None:
        cursor = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = cursor.x() + 18
        y = cursor.y() + 22
        if x + self.width() > area.right() + 1:
            x = cursor.x() - self.width() - 18
        if y + self.height() > area.bottom() + 1:
            y = cursor.y() - self.height() - 18
        x = max(area.left() + 6, min(x, area.right() - self.width() + 1))
        y = max(area.top() + 6, min(y, area.bottom() - self.height() + 1))
        self.move(x, y)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bounds = self.rect().adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor(92, 180, 205, 155), 1))
        painter.setBrush(QColor(13, 23, 38, 238))
        painter.drawRoundedRect(bounds, 13, 13)

        spinner = QRect(15, 14, 20, 20)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(55, 83, 103, 220), 2.5))
        painter.drawEllipse(spinner)
        pen = QPen(QColor(101, 228, 255, 255), 2.7)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(spinner, self.angle * 16, 270 * 16)

        painter.setPen(QColor(232, 245, 255, 255))
        painter.setFont(QFont("Inter", 9, QFont.Weight.DemiBold))
        metrics = painter.fontMetrics()
        label = metrics.elidedText(self.message, Qt.TextElideMode.ElideRight, self.width() - 55)
        painter.drawText(46, 29, label)
        painter.end()


def make_omniguide_icon() -> QIcon:
    """Create a small, dependency-free blue triangle tray icon."""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor(86, 218, 255, 230), 1.5))
    painter.setBrush(QColor(9, 22, 37, 245))
    painter.drawRoundedRect(3, 3, 58, 58, 17, 17)
    triangle = QPolygonF([QPointF(33, 12), QPointF(17, 49), QPointF(47, 49)])
    painter.setPen(QPen(QColor(218, 250, 255, 255), 2))
    painter.setBrush(QColor(57, 207, 255, 250))
    painter.drawPolygon(triangle)
    painter.end()
    return QIcon(pixmap)


def autostart_desktop_file() -> Path:
    return Path.home() / ".config" / "autostart" / "omniguide.desktop"


def _desktop_exec_quote(value: str) -> str:
    """Quote one argument according to Desktop Entry Exec key rules."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def install_autostart_entry(script_path: Path | None = None) -> Path:
    script = (script_path or Path(__file__)).resolve()
    python = Path(sys.executable).resolve()
    target = autostart_desktop_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    exec_line = f"{_desktop_exec_quote(str(python))} {_desktop_exec_quote(str(script))} --background"
    content = "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Name=OmniGuide",
        "Comment=Your visual AI desktop companion",
        f"Exec={exec_line}",
        "Icon=applications-accessories",
        "Terminal=false",
        "X-GNOME-Autostart-enabled=true",
        "StartupNotify=false",
        "Categories=Utility;Accessibility;",
        "",
    ])
    target.write_text(content, encoding="utf-8")
    target.chmod(0o644)
    return target


def remove_autostart_entry() -> bool:
    target = autostart_desktop_file()
    if not target.exists():
        return False
    target.unlink()
    return True


class OmniGuideTray:
    """KDE system-tray controller; the assistant stays alive when its palette hides."""

    def __init__(self, app: QApplication, window: "OmniGuidePalette"):
        self.app = app
        self.window = window
        self.icon = QSystemTrayIcon(make_omniguide_icon(), app)
        self.icon.setToolTip("OmniGuide — your desktop companion")

        self.menu = QMenu(window)
        self.open_action = self.menu.addAction("Open OmniGuide")
        self.open_action.triggered.connect(window.popup)
        self.cancel_action = self.menu.addAction("Cancel current guidance")
        self.cancel_action.triggered.connect(window.cancel_guidance)
        self.menu.addSeparator()
        self.autostart_action = self.menu.addAction("")
        self.autostart_action.triggered.connect(self.toggle_autostart)
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction("Quit OmniGuide")
        self.quit_action.triggered.connect(window.quit_application)

        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self._on_activated)
        self.icon.show()
        self._update_autostart_label()

        if not QSystemTrayIcon.isSystemTrayAvailable():
            print("OmniGuide: KDE system tray is not currently available; use the KDE shortcut to open the palette.", file=sys.stderr, flush=True)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            self.window.toggle()

    def _update_autostart_label(self) -> None:
        enabled = autostart_desktop_file().exists()
        self.autostart_action.setText("Disable launch at login" if enabled else "Enable launch at login")

    def toggle_autostart(self) -> None:
        try:
            if autostart_desktop_file().exists():
                remove_autostart_entry()
                message = "OmniGuide will no longer start automatically when you log in."
            else:
                path = install_autostart_entry()
                message = f"OmniGuide will start in the background at login. Entry: {path}"
            self._update_autostart_label()
            self.icon.showMessage("OmniGuide", message, QSystemTrayIcon.MessageIcon.Information, 4500)
        except OSError as exc:
            self.icon.showMessage("OmniGuide", f"Could not update autostart: {exc}", QSystemTrayIcon.MessageIcon.Critical, 6000)


class OverlayDaemon:
    """Separate process that owns LayerShellQt surfaces; the launcher never uses layer-shell."""

    def __init__(self, app: QApplication):
        self.app = app
        self.server = QLocalServer(app)
        self.overlay: LayerShellOverlaySet | None = None
        self.server.newConnection.connect(self._accept_connections)
        if not self.server.listen(OVERLAY_SERVER_NAME):
            probe = QLocalSocket()
            probe.connectToServer(OVERLAY_SERVER_NAME)
            if probe.waitForConnected(250):
                raise RuntimeError("An OmniGuide overlay daemon is already running.")
            probe.abort()
            self.server.close()
            QLocalServer.removeServer(OVERLAY_SERVER_NAME)
            if not self.server.listen(OVERLAY_SERVER_NAME):
                raise RuntimeError(f"Could not listen on overlay IPC socket: {self.server.errorString()}")
        print("OmniGuide overlay daemon ready (LayerShellQt isolated process).", flush=True)

    def _accept_connections(self) -> None:
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            if sock is not None:
                sock.readyRead.connect(lambda s=sock: self._handle_socket(s))
                if sock.bytesAvailable():
                    QTimer.singleShot(0, lambda s=sock: self._handle_socket(s))

    def _handle_socket(self, sock: QLocalSocket) -> None:
        raw = bytes(sock.readAll()).decode("utf-8", errors="replace").strip()
        try:
            data = json.loads(raw)
            command = data.get("command")
            if command == "show":
                self._show(data)
                reply = "OK shown"
            elif command == "hide":
                self._hide()
                reply = "OK hidden"
            elif command == "quit":
                self._hide()
                reply = "OK quitting"
                sock.write((reply + "\n").encode())
                sock.flush()
                sock.disconnectFromServer()
                QTimer.singleShot(80, self.app.quit)
                return
            else:
                reply = "ERROR unknown overlay command"
        except Exception as exc:
            reply = f"ERROR {type(exc).__name__}: {exc}"
            print(reply, file=sys.stderr, flush=True)
        sock.write((reply + "\n").encode())
        sock.flush()
        sock.disconnectFromServer()

    def _hide(self) -> None:
        if self.overlay is not None:
            self.overlay.close()
            self.overlay = None

    def _show(self, data: dict[str, Any]) -> None:
        self._hide()
        screens = QGuiApplication.screens()
        rect_data = data["rect"]
        rect = QRect(int(rect_data[0]), int(rect_data[1]), int(rect_data[2]), int(rect_data[3]))
        start = QPointF(float(data["start"][0]), float(data["start"][1]))
        target = QPointF(float(data["target"][0]), float(data["target"][1]))
        self.overlay = LayerShellOverlaySet(
            screens, rect, start, target,
            str(data.get("label", "OmniGuide target")),
            str(data.get("message", "Follow the highlighted target")),
        )
        self.overlay.show()


def send_overlay_command(data: dict[str, Any], timeout_ms: int = 1800) -> tuple[bool, str]:
    """Send one small JSON command to the independent LayerShellQt process."""
    sock = QLocalSocket()
    sock.connectToServer(OVERLAY_SERVER_NAME)
    if not sock.waitForConnected(180):
        return False, sock.errorString()
    payload = (json.dumps(data, separators=(",", ":")) + "\n").encode("utf-8")
    if sock.write(payload) != len(payload) or not sock.waitForBytesWritten(timeout_ms):
        detail = sock.errorString() or "Could not send overlay command"
        sock.abort()
        return False, detail
    if not sock.waitForReadyRead(timeout_ms):
        detail = sock.errorString() or "Overlay daemon did not acknowledge command"
        sock.abort()
        return False, detail
    reply = bytes(sock.readAll()).decode("utf-8", errors="replace").strip()
    sock.disconnectFromServer()
    return reply.startswith("OK"), reply


class OmniGuidePalette(QWidget):
    def __init__(self):
        super().__init__()
        self.busy_indicator = BusyIndicator()
        self.setObjectName("LauncherRoot")
        self.setWindowTitle("OmniGuide")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(STYLE)
        # The command palette is a compact launcher, never a desktop-sized
        # translucent window. Keep its width bounded and allow only its height
        # to grow when the answer/action card becomes visible.
        self.setFixedWidth(720)
        self.setMinimumHeight(82)
        self.setMaximumHeight(520)
        self.resize(720, 92)

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
        self.cursor_overlays: list[TriangleCursorOverlay] = []
        self.layer_shell_overlay_set: LayerShellOverlaySet | None = None
        self.overlay_daemon_process: subprocess.Popen | None = None
        self.session = "idle"
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
        self.dismiss_button.setToolTip("Hide OmniGuide to the system tray")
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
        self.retry_button = QPushButton("Retry this step")
        self.retry_button.setObjectName("Action")
        self.retry_button.hide()
        self.retry_button.clicked.connect(self.retry_step)
        action_row.addWidget(self.retry_button)
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
        self._cancel_requested = False
        self._pending_ipc_action: str | None = None

    def _fit_palette_to_content(self) -> None:
        """Fit the launcher to its visible content without screen-sized adjustSize()."""
        layout = self.layout()
        if layout is not None:
            layout.activate()
            desired_height = layout.sizeHint().height()
        else:
            desired_height = 92
        height = max(self.minimumHeight(), min(self.maximumHeight(), desired_height))
        self.resize(720, height)
        self.setWindowState(Qt.WindowState.WindowNoState)

    def _log_palette_geometry(self) -> None:
        geometry = self.frameGeometry()
        print(
            f"OmniGuide palette geometry: {geometry.x()},{geometry.y()} "
            f"{geometry.width()}x{geometry.height()} state={self.windowState()!s}",
            file=sys.stderr, flush=True,
        )

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
        self.session = "palette"
        self._fit_palette_to_content()
        self.showNormal()
        self._fit_palette_to_content()
        self.place_near_cursor()
        QTimer.singleShot(0, self._log_palette_geometry)
        self.raise_()
        self.activateWindow()
        self.prompt.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.prompt.selectAll()

    def hide_palette(self) -> None:
        self.hide()
        if self.session in {"palette", "ask_result"}:
            self.session = "idle"

    def toggle(self) -> None:
        if self.isVisible():
            self.hide_palette()
        else:
            self.popup()

    def workers_running(self) -> bool:
        capture_running = self.capture_worker is not None and self.capture_worker.isRunning()
        vision_running = self.worker is not None and self.worker.isRunning()
        return capture_running or vision_running

    def handle_ipc(self, command: str) -> str:
        action = command.strip().casefold() or "summon"
        if action == "cancel":
            self.cancel_guidance()
            return "OK cancel"
        if action == "quit":
            QTimer.singleShot(0, self.quit_application)
            return "OK quit"
        if action == "ping":
            return "OK running"
        if action in {"summon", "continue"}:
            if self.handle_summon():
                return "OK summon"
            return "BUSY"
        return "ERROR unknown command"

    def handle_summon(self) -> bool:
        # Check the user-visible state before the worker flag. QThread can
        # still report isRunning() briefly after emitting its done signal.
        # Queue one Continue action instead of silently dropping the shortcut.
        if self.session == "guiding":
            if self.workers_running():
                self._pending_ipc_action = "continue"
                return True
            self.next_step()
            return True
        if self.session == "located":
            self.cancel_guidance()
            self.popup()
            self.set_idle("Target dismissed · ready for another request")
            return True
        if self.workers_running() or self.session in {"capturing", "inferring"}:
            return False
        self.toggle()
        return True

    def _run_pending_ipc_action(self) -> None:
        if self._shutdown_requested or self.workers_running():
            return
        action = self._pending_ipc_action
        if action is None:
            return
        self._pending_ipc_action = None
        if action == "continue" and self.session == "guiding":
            self.next_step()

    def cancel_guidance(self) -> None:
        """Drop overlay and idle without activating the palette or target app."""
        self._pending_ipc_action = None
        self.busy_indicator.hide_indicator()
        self.set_idle("Cancelled")
        self._clear_overlays()
        self.latest_bbox = None
        self.latest_target = ""
        if self.session in {"guiding", "located", "capturing", "inferring"}:
            self.steps = []
            self.current_step = 0
            self.guide_goal = ""
        if self.workers_running() or self.session in {"capturing", "inferring"}:
            self._cancel_requested = True
        self.hide()
        self.session = "idle"

    def _ensure_overlay_daemon(self) -> None:
        ok, _ = send_overlay_command({"command": "hide"}, timeout_ms=250)
        if ok:
            return
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "wayland"
        env["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"
        env.pop("QT_PLUGIN_PATH", None)
        script = str(Path(__file__).resolve())
        self.overlay_daemon_process = subprocess.Popen(
            [sys.executable, script, "--overlay-daemon"],
            cwd=str(APP_DIR), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=None, start_new_session=True,
        )
        deadline = time.monotonic() + 3.0
        last_error = "Overlay daemon did not start"
        while time.monotonic() < deadline:
            ok, reply = send_overlay_command({"command": "hide"}, timeout_ms=350)
            if ok:
                print("OmniGuide: connected to isolated LayerShellQt overlay daemon.", flush=True)
                return
            last_error = reply
            time.sleep(0.08)
        raise RuntimeError(f"Could not start the LayerShellQt overlay process: {last_error}")

    def _stop_overlay_daemon(self) -> None:
        send_overlay_command({"command": "quit"}, timeout_ms=500)
        process = self.overlay_daemon_process
        self.overlay_daemon_process = None
        if process is not None:
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.terminate()

    def _clear_overlays(self) -> None:
        for overlay in self.overlays:
            overlay.close()
            overlay.deleteLater()
        self.overlays.clear()
        for overlay in self.cursor_overlays:
            overlay.close()
            overlay.deleteLater()
        self.cursor_overlays.clear()
        if self.layer_shell_overlay_set is not None:
            self.layer_shell_overlay_set.close()
            self.layer_shell_overlay_set = None
        # The Wayland overlay belongs to a separate LayerShellQt process.
        send_overlay_command({"command": "hide"}, timeout_ms=250)

    def closeEvent(self, event) -> None:  # noqa: N802
        # Closing the palette only hides it. The resident assistant and active
        # click-through guidance remain alive until explicitly quit from tray.
        event.ignore()
        self.hide_palette()

    def quit_application(self) -> None:
        """Perform a real quit from the tray menu or --quit IPC command."""
        if self._shutdown_requested:
            return
        self._clear_overlays()
        self._stop_overlay_daemon()
        self.busy_indicator.hide_indicator()
        if self.workers_running():
            self._shutdown_requested = True
            self.show()
            self.status_label.setText("Finishing the active request before quitting…")
            self.prompt.setEnabled(False)
            self.send_button.setEnabled(False)
            self.dismiss_button.setEnabled(False)
            self.target_button.setEnabled(False)
            self.retry_button.setEnabled(False)
            self.next_button.setEnabled(False)
            self.done_button.setEnabled(False)
            return
        self._shutdown_requested = True
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)

    def _close_after_worker(self) -> None:
        if self._shutdown_requested and not self.workers_running():
            app = QApplication.instance()
            if app is not None:
                QTimer.singleShot(0, app.quit)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide_palette()
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

        # Deterministic, common KDE shortcut questions do not need vision.
        # Show the answer immediately and do not capture a screenshot or call Baseten.
        quick_answer = local_help_answer(request)
        if quick_answer is not None:
            self._clear_overlays()
            self.latest_bbox = None
            self.latest_target = ""
            self.target_button.setEnabled(False)
            self.retry_button.hide()
            self.next_button.hide()
            self.done_button.hide()
            self.result.setMarkdown(quick_answer)
            self.result_card.show()
            self._fit_palette_to_content()
            self.popup()
            self.set_idle("Quick answer · no screenshot or API request")
            self.session = "ask_result"
            return

        self.pending_request = request
        self.pending_mode = classify_request(request)
        if self.pending_mode == "guide":
            self.steps = []
            self.current_step = 0
            self.guide_goal = request
        self._cancel_requested = False
        self.latest_bbox = None
        self.latest_target = ""
        self.target_button.setEnabled(False)
        self.retry_button.hide()
        self.next_button.hide()
        self.done_button.hide()
        self.result.clear()
        self.result_card.show()
        self._fit_palette_to_content()
        self.place_near_cursor()
        self.set_busy("Capturing your desktop")
        self.hide()
        self.session = "capturing"
        QTimer.singleShot(350, self._start_capture)

    def _start_capture(self) -> None:
        if self._shutdown_requested:
            return
        self.session = "capturing"
        self.capture_worker = CaptureWorker(self.capture_path)
        self.capture_worker.done.connect(self._capture_done)
        self.capture_worker.failed.connect(self._capture_failed)
        self.capture_worker.finished.connect(self._capture_finished)
        self.capture_worker.start()

    def _capture_done(self, path: str) -> None:
        if self._shutdown_requested:
            return
        if self._cancel_requested:
            self._cancel_requested = False
            self.session = "idle"
            return
        self.image_path = path
        # Keep the launcher hidden for every request mode while the model is
        # working. BusyIndicator remains visible beside the cursor instead.
        if self.pending_mode == "guide_target" and self.steps:
            step = dict(self.steps[self.current_step])
            if self.current_step + 1 < len(self.steps):
                step["next_instruction"] = self.steps[self.current_step + 1]["instruction"]
                step["next_target"] = self.steps[self.current_step + 1]["target"]
            self._start_vision(self.guide_goal, "guide_target", step)
        else:
            self._start_vision(self.pending_request, self.pending_mode)

    def _capture_failed(self, message: str) -> None:
        if self._shutdown_requested:
            return
        if self._cancel_requested:
            self._cancel_requested = False
            self.session = "idle"
            return
        self.session = "palette"
        self.popup()
        self.set_idle(f"Capture failed: {message}")
        self.result_card.show()
        self.result.setPlainText("Could not capture the desktop. Check Spectacle and Wayland permissions, then try again.")

    def _capture_finished(self) -> None:
        self.capture_worker = None
        self._close_after_worker()
        self._run_pending_ipc_action()

    def _start_vision(self, request: str, mode: str, step: dict[str, str] | None = None) -> None:
        if not self.image_path:
            self.set_idle("No screenshot available.")
            return
        self.session = "inferring"
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
        self.busy_indicator.show_message(message)
        self.status_label.setText(message + " ·")
        self.loading_index = 0
        self.loading_base = message
        self.loading_timer.start()
        self.progress.show()
        self.send_button.setEnabled(False)
        self.prompt.setEnabled(False)
        self.target_button.setEnabled(False)
        self.retry_button.setEnabled(False)
        self.next_button.setEnabled(False)
        self.done_button.setEnabled(False)

    def set_idle(self, message: str) -> None:
        self.busy_indicator.hide_indicator()
        self.loading_timer.stop()
        self.progress.hide()
        self.status_label.setText(message)
        self.send_button.setEnabled(True)
        self.prompt.setEnabled(True)
        self.retry_button.setEnabled(True)
        self.next_button.setEnabled(True)
        self.done_button.setEnabled(True)

    def _animate_status(self) -> None:
        self.loading_index = (self.loading_index + 1) % 4
        self.status_label.setText(self.loading_base + (" ·" * (self.loading_index + 1)))

    def _vision_done(self, data: dict[str, Any], usage_text: str) -> None:
        if self._shutdown_requested:
            return
        if self._cancel_requested:
            self._cancel_requested = False
            self.session = "idle"
            return
        mode = data.get("mode", self.pending_mode)
        self.latest_usage = usage_text
        self.latest_bbox = data.get("bbox_1000") if data.get("found") else None
        self.latest_target = str(data.get("target", "Target")) if self.latest_bbox else ""
        self.target_button.setEnabled(self.latest_bbox is not None)
        self.retry_button.hide()
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
                    if len(self.steps) > 1:
                        self.next_button.setText("I completed this step →")
                        self.next_button.show()
                    self.done_button.show()
                else:
                    lines.append("The first target is not visible. Open the relevant menu or dialog, then retry this step.")
                    self.retry_button.setText("Retry first target")
                    self.retry_button.show()
        else:
            lines.extend([
                f"**Target:** {data.get('target', 'Requested element')}",
                f"**Found:** {'Yes' if data.get('found') else 'Not confidently found'}",
                str(data.get('reason', '')),
            ])
            if mode == "guide_target" and self.steps:
                step_state = str(data.get("step_state", "")).strip().casefold()
                allowed_states = {"target_visible", "already_completed", "next_target_visible", "not_ready", "uncertain"}
                if step_state not in allowed_states:
                    # Backward-compatible behavior for an older model response.
                    step_state = "target_visible" if self.latest_bbox else "uncertain"

                if step_state == "next_target_visible" and self.latest_bbox and self.current_step < len(self.steps) - 1:
                    self.current_step += 1
                    step = self.steps[self.current_step]
                    self.latest_target = step["target"]
                    lines = [
                        f"**Step {self.current_step} appears complete.**",
                        f"**Step {self.current_step + 1} of {len(self.steps)} — {step['instruction']}**",
                        f"**Next target:** {step['target']}",
                        str(data.get("reason", "The next target is visible on the updated screen.")),
                    ]
                    if self.current_step < len(self.steps) - 1:
                        self.next_button.setText("I completed this step →")
                        self.next_button.show()
                    else:
                        self.done_button.show()
                else:
                    step = self.steps[self.current_step]
                    lines.insert(0, f"**Step {self.current_step + 1} of {len(self.steps)} — {step['instruction']}**")
                    if step_state == "target_visible" and self.latest_bbox:
                        self.latest_target = step["target"]
                        lines.append("The current target is visible. Complete this action, then use your shortcut or the button below.")
                        if self.current_step < len(self.steps) - 1:
                            self.next_button.setText("I completed this step →")
                            self.next_button.show()
                        else:
                            self.done_button.show()
                    elif step_state == "already_completed":
                        lines.append("**This step appears to have already been completed.**")
                        lines.append("You can continue to the next step; OmniGuide did not find a clear next target on this screen.")
                        if self.current_step < len(self.steps) - 1:
                            self.next_button.setText("Continue to next step →")
                            self.next_button.show()
                        else:
                            self.done_button.show()
                    else:
                        if step_state == "not_ready":
                            lines.append("The current step's target is not visible yet, and the screenshot does not show that the action has been completed.")
                        else:
                            lines.append("OmniGuide cannot determine whether this step is still pending or already completed from this screenshot.")
                        lines.append("Keep the relevant screen visible and retry. The guide will not advance without evidence.")
                        self.retry_button.setText("Retry this step")
                        self.retry_button.show()
            elif mode == "locate":
                if self.latest_bbox:
                    lines.append(f"Normalized box: {self.latest_bbox}")

        if usage_text:
            lines.extend(["", f"_{usage_text}_"])
        self.result.setMarkdown("\n\n".join(lines))
        self._fit_palette_to_content()

        visual_success = self.latest_bbox is not None and mode in {"locate", "guide", "guide_target"}
        if visual_success:
            self.set_idle("Guidance active · complete the action, then use the shortcut or button")
            if mode == "locate":
                self.session = "located"
            else:
                self.session = "guiding"
            self.hide()
            self.show_target()
            return

        self.place_near_cursor()
        self.popup()
        if mode == "ask":
            self.session = "ask_result"
        self.set_idle("Ready · Enter to ask · Esc to hide · × to quit")

    def _vision_failed(self, message: str) -> None:
        if self._shutdown_requested:
            return
        if self._cancel_requested:
            self._cancel_requested = False
            self.session = "idle"
            return
        self.result.setPlainText(f"The request failed: {message}\n\nYou can retry. A timeout does not by itself tell us whether the model recognized the image or what was billed.")
        self.popup()
        self.set_idle("Request failed · ready to retry")

    def _vision_finished(self) -> None:
        self.worker = None
        self._close_after_worker()
        self._run_pending_ipc_action()

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
            target_point = QPointF(global_rect.center())

            cursor = QCursor.pos()
            start_point = QPointF(cursor.x() + 20, cursor.y() + 16)
            if self.session == "guiding" and self.steps and self.current_step < len(self.steps):
                cursor_message = self.steps[self.current_step]["instruction"]
            else:
                cursor_message = f"Found: {self.latest_target or 'requested element'}"

            # On every Wayland session, require the tested LayerShellQt path.
            # Never silently fall through to QWidget.showFullScreen(): that was
            # the source of the desktop-sized overlay reported in prior builds.
            platform_name = QGuiApplication.platformName().lower()
            wayland_session = (
                os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
                or platform_name.startswith("wayland")
            )
            if wayland_session:
                try:
                    if not platform_name.startswith("wayland"):
                        raise RuntimeError(
                            f"Qt platform is {platform_name!r}, not Wayland; restart with the Wayland backend."
                        )
                    self._clear_overlays()
                    self._ensure_overlay_daemon()
                    payload = {
                        "command": "show",
                        "rect": [global_rect.x(), global_rect.y(), global_rect.width(), global_rect.height()],
                        "start": [start_point.x(), start_point.y()],
                        "target": [target_point.x(), target_point.y()],
                        "label": self.latest_target or "OmniGuide target",
                        "message": cursor_message,
                    }
                    ok, reply = send_overlay_command(payload, timeout_ms=2500)
                    if not ok:
                        raise RuntimeError(reply)
                    self.hide()
                    return
                except Exception as layer_error:
                    print(f"OmniGuide overlay daemon unavailable: {layer_error}",
                          file=sys.stderr, flush=True)
                    self._clear_overlays()
                    self.session = "palette"
                    self.popup()
                    self.set_idle(f"Wayland overlay failed: {layer_error}")
                    self.result.append(
                        "\n\nThe separate Wayland overlay process could not display guidance. "
                        "The main OmniGuide launcher remained a normal window. Check the Konsole diagnostic."
                    )
                    return

            highlight_overlays: list[HighlightOverlay] = []
            cursor_overlays: list[TriangleCursorOverlay] = []
            for screen in screens:
                sg = screen.geometry()
                part = global_rect.intersected(sg)
                if not part.isEmpty():
                    local = part.translated(-sg.x(), -sg.y())
                    highlight_overlays.append(
                        HighlightOverlay(screen, local, self.latest_target or "OmniGuide target")
                    )
                cursor_overlays.append(
                    TriangleCursorOverlay(screen, start_point, target_point, cursor_message)
                )
            if not highlight_overlays:
                for overlay in cursor_overlays:
                    overlay.close()
                    overlay.deleteLater()
                raise RuntimeError("The target does not intersect the current desktop geometry.")
            self.hide()
            QTimer.singleShot(
                350,
                lambda: self._show_overlays(highlight_overlays, cursor_overlays),
            )
        except Exception as exc:
            self.status_label.setText(f"Could not show target: {exc}")
            self.popup()

    def _show_layer_shell_overlays(self) -> None:
        if self._shutdown_requested:
            self._clear_overlays()
            return
        if self.layer_shell_overlay_set is not None:
            self.layer_shell_overlay_set.show()

    def _show_overlays(
        self,
        overlays: list[HighlightOverlay],
        cursor_overlays: list[TriangleCursorOverlay] | None = None,
    ) -> None:
        if self._shutdown_requested:
            for overlay in [*overlays, *(cursor_overlays or [])]:
                overlay.close()
                overlay.deleteLater()
            return
        self._clear_overlays()
        self.overlays = overlays
        self.cursor_overlays = cursor_overlays or []
        for overlay in self.overlays:
            overlay.showFullScreen()
        for overlay in self.cursor_overlays:
            overlay.showFullScreen()
            overlay.start_animation()
            overlay.raise_()

    def retry_step(self) -> None:
        """Recapture and locate the current guide target without advancing."""
        if not self.steps or self.workers_running() or self._shutdown_requested:
            return
        self._clear_overlays()
        self.pending_mode = "guide_target"
        self.pending_request = self.guide_goal
        self.latest_bbox = None
        self.latest_target = ""
        self.target_button.setEnabled(False)
        self.retry_button.hide()
        self.next_button.hide()
        self.done_button.hide()
        self.set_busy("Retrying the current target")
        self.hide()
        self.session = "capturing"
        QTimer.singleShot(300, self._start_capture)

    def next_step(self) -> None:
        if not self.steps or self.workers_running():
            return
        if self.current_step >= len(self.steps) - 1:
            self.finish_guide(restore_palette=True)
            return
        self._clear_overlays()
        self.current_step += 1
        self.pending_mode = "guide_target"
        self.pending_request = self.guide_goal
        self.latest_bbox = None
        self.latest_target = ""
        self.target_button.setEnabled(False)
        self.set_busy("Capturing the updated screen")
        self.hide()
        self.session = "capturing"
        QTimer.singleShot(350, self._start_capture)

    def finish_guide(self, restore_palette: bool = True) -> None:
        self._clear_overlays()
        self.steps = []
        self.current_step = 0
        self.guide_goal = ""
        self.latest_bbox = None
        self.latest_target = ""
        self.target_button.setEnabled(False)
        self.retry_button.hide()
        self.next_button.hide()
        self.done_button.hide()
        self.result.setPlainText("Guide finished. Ask OmniGuide for another task whenever you're ready.")
        self._fit_palette_to_content()
        if restore_palette:
            self.popup()
            self.set_idle("Ready for another task")
        else:
            self.hide()
            self.session = "idle"


def send_to_existing_instance(payload: bytes) -> tuple[bool, str]:
    socket = QLocalSocket()
    socket.connectToServer(SERVER_NAME)
    if not socket.waitForConnected(1500):
        return False, f"Could not connect to the running instance: {socket.errorString()}"

    written = socket.write(payload)
    if written != len(payload):
        detail = socket.errorString() or "The complete command could not be queued."
        socket.abort()
        return False, detail
    if not socket.waitForBytesWritten(1500):
        detail = socket.errorString() or "The command was not written to the local socket."
        socket.abort()
        return False, detail
    if not socket.waitForReadyRead(2000):
        detail = socket.errorString() or "The running instance did not acknowledge the command."
        socket.abort()
        return False, detail

    reply = bytes(socket.readAll()).decode("utf-8", errors="replace").strip()
    socket.disconnectFromServer()
    if reply.startswith("OK"):
        return True, reply
    return False, reply or "The running instance returned an empty acknowledgement."


def main() -> int:
    args = set(sys.argv[1:])

    if "--install-autostart" in args:
        try:
            print(f"Autostart enabled: {install_autostart_entry()}")
            return 0
        except OSError as exc:
            print(f"Could not install autostart entry: {exc}", file=sys.stderr)
            return 2

    if "--remove-autostart" in args:
        print("Autostart entry removed." if remove_autostart_entry() else "No autostart entry was installed.")
        return 0

    if "--overlay-daemon" in args:
        if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            os.environ["QT_QPA_PLATFORM"] = "wayland"
            os.environ["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"
            system_qt_plugins = "/usr/lib64/qt6/plugins"
            if Path(system_qt_plugins).exists():
                QCoreApplication.addLibraryPath(system_qt_plugins)
        daemon_app = QApplication([sys.argv[0]])
        daemon_app.setApplicationName("OmniGuide Overlay")
        daemon_app.setQuitOnLastWindowClosed(False)
        try:
            daemon = OverlayDaemon(daemon_app)
        except Exception as exc:
            print(f"Overlay daemon startup failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            return 2
        daemon_app._omniguide_overlay_daemon = daemon
        if "--overlay-self-test" in args:
            screens = QGuiApplication.screens()
            primary = QGuiApplication.primaryScreen() or screens[0]
            center = QPointF(primary.geometry().center())
            rect = QRect(round(center.x()) - 55, round(center.y()) - 35, 110, 70)
            daemon._show({"rect": [rect.x(), rect.y(), rect.width(), rect.height()],
                          "start": [QCursor.pos().x(), QCursor.pos().y()],
                          "target": [center.x(), center.y()], "label": "OmniGuide test",
                          "message": "Overlay daemon test · click through"})
            QTimer.singleShot(12000, daemon_app.quit)
        return daemon_app.exec()

    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
        system_qt_plugins = "/usr/lib64/qt6/plugins"
        if Path(system_qt_plugins).exists():
            QCoreApplication.addLibraryPath(system_qt_plugins)
    app = QApplication(sys.argv)
    app.setApplicationName("OmniGuide")
    app.setApplicationDisplayName("OmniGuide")
    app.setWindowIcon(make_omniguide_icon())
    app.setQuitOnLastWindowClosed(False)
    print(
        f"OmniGuide {APP_VERSION} starting: session={os.environ.get('XDG_SESSION_TYPE', 'unknown')} "
        f"qt_platform={QGuiApplication.platformName()} "
        f"shell_integration={os.environ.get('QT_WAYLAND_SHELL_INTEGRATION', 'unset')} "
        f"overlay=separate-process ipc={SERVER_NAME}",
        file=sys.stderr, flush=True,
    )

    if "--diagnostics" in args:
        print("Screens:", file=sys.stderr)
        for screen in QGuiApplication.screens():
            g = screen.geometry()
            print(f"  {screen.name()}: {g.x()},{g.y()} {g.width()}x{g.height()}", file=sys.stderr)
        return 0

    # Test the isolated overlay process; the main launcher process never enables layer-shell.
    if "--layer-shell-test" in args:
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "wayland"
        env["QT_WAYLAND_SHELL_INTEGRATION"] = "layer-shell"
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--overlay-daemon", "--overlay-self-test"],
            cwd=str(APP_DIR), env=env, check=False,
        )
        return result.returncode

    if "--palette-test" in args:
        test_window = OmniGuidePalette()
        app._omniguide_test_window = test_window
        test_window.popup()
        print("Palette test shown for 10 seconds; no screenshot or Muse Spark request is made.", flush=True)
        QTimer.singleShot(10_000, app.quit)
        return app.exec()

    background = "--background" in args
    if "--cancel" in args:
        command = "cancel"
    elif "--quit" in args:
        command = "quit"
    elif background:
        command = "ping"
    else:
        command = "summon"

    payload = f"{command}\n".encode("ascii")
    delivered, detail = send_to_existing_instance(payload)
    if delivered:
        return 0
    if command in {"cancel", "quit"}:
        print(f"OmniGuide command was not delivered: {detail}", file=sys.stderr)
        return 2

    window = OmniGuidePalette()
    tray = OmniGuideTray(app, window)
    # Keep explicit references for the full QApplication lifetime.
    app._omniguide_window = window
    app._omniguide_tray = tray
    app.aboutToQuit.connect(window._stop_overlay_daemon)

    server = QLocalServer(app)
    if not server.listen(SERVER_NAME):
        # First verify once more that no live instance owns the socket. Only
        # then remove a stale filesystem socket and retry listening.
        probe = QLocalSocket()
        probe.connectToServer(SERVER_NAME)
        if probe.waitForConnected(350):
            probe.abort()
            delivered, detail = send_to_existing_instance(payload)
            if delivered:
                return 0
            print(
                "An OmniGuide instance is listening but did not acknowledge the command: "
                f"{detail}", file=sys.stderr,
            )
            return 1

        server.close()
        QLocalServer.removeServer(SERVER_NAME)
        if not server.listen(SERVER_NAME):
            print(
                "Could not start OmniGuide's local command server after stale-socket cleanup. "
                f"Server error: {server.errorString()}", file=sys.stderr,
            )
            return 1

    def handle_connection() -> None:
        while server.hasPendingConnections():
            socket = server.nextPendingConnection()
            buffer = bytearray()
            state = {"handled": False}

            def on_ready(s=socket, data=buffer, status=state) -> None:
                if status["handled"]:
                    return
                data.extend(bytes(s.readAll()))
                if b"\n" not in data:
                    return
                raw, _, _ = data.partition(b"\n")
                status["handled"] = True
                try:
                    reply = window.handle_ipc(raw.decode("utf-8", errors="replace"))
                except Exception as exc:
                    reply = f"ERROR {type(exc).__name__}"
                s.write((reply + "\n").encode("utf-8"))
                s.flush()
                s.disconnectFromServer()

            socket.readyRead.connect(on_ready)
            socket.disconnected.connect(socket.deleteLater)
            if socket.bytesAvailable():
                on_ready()

    server.newConnection.connect(handle_connection)
    app.aboutToQuit.connect(server.close)
    app.aboutToQuit.connect(tray.icon.hide)

    if not background:
        window.popup()
    else:
        print("OmniGuide is running in the background. Use Ctrl+Shift+Space or the system-tray icon.", flush=True)

    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
