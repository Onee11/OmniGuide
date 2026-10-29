#!/usr/bin/env python3
"""No-API smoke test for a transparent, click-through Qt desktop overlay.

Shows a temporary highlight on the primary monitor for eight seconds.
This is a capability probe, not the final overlay or coordinate mapper.
"""
from __future__ import annotations

import sys
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


class OverlayProbe(QWidget):
    def __init__(self, screen):
        super().__init__()
        self.screen = screen
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowTransparentForInput
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(screen.geometry())
        QTimer.singleShot(8000, QApplication.instance().quit)

    def paintEvent(self, event):  # noqa: N802 - Qt API name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # The test target is intentionally illustrative, not a model prediction.
        x = int(self.width() * 0.25)
        y = int(self.height() * 0.30)
        w = int(self.width() * 0.40)
        h = int(self.height() * 0.13)

        painter.setPen(QPen(QColor(255, 55, 90, 245), 4))
        painter.setBrush(QColor(255, 55, 90, 35))
        painter.drawRoundedRect(x, y, w, h, 8, 8)

        label = "OmniGuide overlay test — disappears in 8 seconds"
        painter.setFont(QFont("Sans Serif", 12, QFont.Weight.DemiBold))
        metrics = painter.fontMetrics()
        label_w = metrics.horizontalAdvance(label) + 24
        label_h = metrics.height() + 14
        label_y = max(0, y - label_h - 6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(18, 28, 46, 235))
        painter.drawRoundedRect(x, label_y, label_w, label_h, 7, 7)
        painter.setPen(QColor(255, 255, 255, 255))
        painter.drawText(x + 12, label_y + 7 + metrics.ascent(), label)
        painter.end()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("OmniGuide Overlay Probe")
    screen = app.primaryScreen()
    if screen is None:
        print("ERROR: Qt could not find a primary display.", file=sys.stderr)
        return 1

    geometry = screen.geometry()
    print(f"Desktop session: {__import__('os').environ.get('XDG_SESSION_TYPE', 'unknown')}")
    print(f"Primary monitor geometry: {geometry.width()}x{geometry.height()} at ({geometry.x()}, {geometry.y()})")
    print("Showing a click-through overlay for 8 seconds. No API calls will be made.")
    overlay = OverlayProbe(screen)
    overlay.showFullScreen()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
