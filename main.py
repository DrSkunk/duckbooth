#!/usr/bin/env python3
"""Touch-first Raspberry Pi photobooth for the Camera Module 3 Wide."""

from __future__ import annotations

import json
import logging
import math
import sys
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image
from picamera2 import Picamera2
from PyQt5.QtCore import QElapsedTimer, QEvent, QObject, QRect, QRectF, QTimer, Qt
from PyQt5.QtGui import QColor, QBrush, QConicalGradient, QCursor, QImage, QKeySequence, QLinearGradient, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QShortcut,
    QStackedLayout,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


APP_DIR = Path("/home/booth/duckbooth")
PHOTO_DIR = Path("/home/booth/photobooth/photos")
SETTINGS_PATH = APP_DIR / "settings.json"
LOW_SPACE_BYTES = 1024 * 1024 * 1024
COUNTDOWN_SECONDS = 45
PREVIEW_SECONDS = 5
FINAL_PREVIEW_SECONDS = 5
# Picamera2's RGB888 stream is delivered in BGR byte order on this platform.
# Qt needs to be told that order or reds and blues appear swapped in the live feed.
LIVE_IMAGE_FORMAT = getattr(QImage, "Format_BGR888", QImage.Format_RGB888)


def newest_photos(limit: int = 20) -> list[Path]:
    """Return JPEG originals newest first, without keeping a separate database."""
    return sorted(
        (path for path in PHOTO_DIR.glob("*.jpg") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )[:limit]


def load_camera_settings() -> tuple[float, int, int, int, int]:
    try:
        settings = json.loads(SETTINGS_PATH.read_text())
        zoom = max(1.0, min(3.0, float(settings.get("zoom", 1.0))))
        # Migrate the earlier symmetric crop value to the four independent edges.
        legacy_edge = max(0, min(35, int(settings.get("post_crop", 0)))) // 2
        edges = tuple(
            max(0, min(35, int(settings.get(f"crop_{edge}", legacy_edge))))
            for edge in ("left", "right", "top", "bottom")
        )
        return zoom, *edges
    except (FileNotFoundError, TypeError, ValueError, json.JSONDecodeError):
        return 1.0, 0, 0, 0, 0


def save_camera_settings(zoom: float, left: int, right: int, top: int, bottom: int) -> None:
    SETTINGS_PATH.write_text(
        json.dumps(
            {
                "zoom": zoom,
                "crop_left": left,
                "crop_right": right,
                "crop_top": top,
                "crop_bottom": bottom,
            },
            indent=2,
        )
        + "\n"
    )


def app_screen(app: QApplication, output_name: str, fallback: int | None = None):
    for screen in app.screens():
        if output_name.lower() in screen.name().lower():
            return screen
    screens = app.screens()
    if fallback is not None and len(screens) > fallback:
        return screens[fallback]
    return None


def show_fullscreen_on(window: QMainWindow, screen) -> None:
    """Move an already-created Wayland window after entering fullscreen.

    Labwc chooses the primary output when fullscreen is requested first. Setting
    the target screen afterwards makes Qt recreate that surface on the intended
    output instead of stacking both windows on the touchscreen.
    """
    window.showFullScreen()
    QApplication.processEvents()
    if screen and window.windowHandle():
        window.windowHandle().setScreen(screen)
        window.setGeometry(screen.geometry())
        QApplication.processEvents()


def show_gallery_on(window: QMainWindow, screen) -> None:
    """Map the gallery normally so Labwc's MoveToOutput rule can relocate it."""
    window.show()
    QApplication.processEvents()
    if screen and window.windowHandle():
        window.windowHandle().setScreen(screen)
    window.showMaximized()
    QApplication.processEvents()


class GalleryWindow(QMainWindow):
    """Passive newest-20 gallery, intended for HDMI-A-1."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint)
        self._signature: tuple[tuple[str, int], ...] = ()
        self.setWindowTitle("Duckbooth gallery")
        self.setStyleSheet("background: #111; color: white;")

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(42, 34, 42, 34)
        layout.setSpacing(18)
        title = QLabel("LAATSTE FOTO'S")
        title.setStyleSheet("font-size: 32px; font-weight: 700; letter-spacing: 4px;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(12)
        layout.addWidget(self.grid_host, 1)
        self.setCentralWidget(root)

        self.poll = QTimer(self)
        self.poll.timeout.connect(self.refresh)
        self.poll.start(2000)
        self.refresh(force=True)

    def refresh(self, force: bool = False) -> None:
        photos = newest_photos()
        signature = tuple((photo.name, photo.stat().st_mtime_ns) for photo in photos)
        if not force and signature == self._signature:
            return
        self._signature = signature

        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not photos:
            empty = QLabel("Nieuwe foto's verschijnen hier")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("font-size: 34px; color: #a8a8a8;")
            self.grid.addWidget(empty, 0, 0, 1, 5)
            return

        # Five columns by four rows fills a 16:9 1080p display with useful thumbnails.
        for index, photo in enumerate(photos):
            label = QLabel()
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("background: #262626;")
            pixmap = QPixmap(str(photo))
            if not pixmap.isNull():
                label.setPixmap(pixmap.scaled(338, 174, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.grid.addWidget(label, index // 5, index % 5)


class CountdownVisual(QWidget):
    """A full-screen animated countdown ring, drawn without bitmap assets."""

    def __init__(self) -> None:
        super().__init__()
        self.remaining = float(COUNTDOWN_SECONDS)
        self.total = float(COUNTDOWN_SECONDS)
        self.preview_mode = False
        self.capture_mode = False
        self.error_message: str | None = None
        self.caption: str | None = None
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_state(
        self,
        remaining: float,
        *,
        total: float = COUNTDOWN_SECONDS,
        preview_mode: bool = False,
        capture_mode: bool = False,
        error_message: str | None = None,
        caption: str | None = None,
    ) -> None:
        self.remaining = remaining
        self.total = total
        self.preview_mode = preview_mode
        self.capture_mode = capture_mode
        self.error_message = error_message
        self.caption = caption
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width, height = self.width(), self.height()

        if not self.preview_mode:
            backdrop = QLinearGradient(0, 0, width, height)
            backdrop.setColorAt(0, QColor("#09111b"))
            backdrop.setColorAt(0.55, QColor("#0f1824"))
            backdrop.setColorAt(1, QColor("#110d20"))
            painter.fillRect(self.rect(), backdrop)
        else:
            shade = QLinearGradient(0, 0, 0, height)
            shade.setColorAt(0, QColor(4, 8, 14, 54))
            shade.setColorAt(1, QColor(4, 8, 14, 130))
            painter.fillRect(self.rect(), shade)

        center_x, center_y = width / 2, height / 2
        radius = min(width, height) * (0.23 if self.preview_mode else 0.25)
        ring_rect = QRectF(center_x - radius, center_y - radius, radius * 2, radius * 2)

        if self.error_message:
            painter.setPen(QColor("#ffac9c"))
            font = painter.font()
            font.setPointSize(max(26, int(radius * 0.18)))
            font.setWeight(87)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignCenter, self.error_message)
            return

        pulse = 0.5 + 0.5 * math.sin((COUNTDOWN_SECONDS - self.remaining) * 7.0)
        painter.setPen(Qt.NoPen)
        halo_radius = radius + 46 + pulse * 18
        painter.setBrush(QColor(89, 177, 255, 10 if not self.preview_mode else 30))
        painter.drawEllipse(QRectF(center_x - halo_radius, center_y - halo_radius, halo_radius * 2, halo_radius * 2))

        track_pen = QPen(QColor(255, 255, 255, 35), max(16, int(radius * 0.075)))
        track_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(track_pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(ring_rect)

        completed = max(0.0, min(1.0, (self.total - self.remaining) / self.total))
        ring_gradient = QConicalGradient(center_x, center_y, -90)
        ring_gradient.setColorAt(0.00, QColor("#77f4cc"))
        ring_gradient.setColorAt(0.33, QColor("#68b6ff"))
        ring_gradient.setColorAt(0.66, QColor("#aa7bff"))
        ring_gradient.setColorAt(1.00, QColor("#77f4cc"))
        progress_pen = QPen(QBrush(ring_gradient), max(16, int(radius * 0.075)))
        progress_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(progress_pen)
        painter.drawArc(ring_rect, 90 * 16, -int(completed * 360 * 16))

        if self.capture_mode:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor("#ffffff"))
            painter.drawEllipse(QRectF(center_x - 14, center_y - 14, 28, 28))
            return

        seconds = max(0, int(self.remaining + 0.999))
        painter.setPen(QColor("#f7fbff"))
        font = painter.font()
        font.setPointSize(max(82, int(radius * 0.68)))
        font.setWeight(87)
        painter.setFont(font)
        painter.drawText(ring_rect, Qt.AlignCenter, str(seconds))

        if self.caption:
            painter.setPen(QColor("#f7fbff"))
            caption_font = painter.font()
            caption_font.setPointSize(max(32, int(radius * (0.18 if not self.preview_mode else 0.13))))
            caption_font.setWeight(75)
            painter.setFont(caption_font)
            caption_rect = QRectF(0, center_y + radius + 54, width, height - (center_y + radius + 54))
            painter.drawText(caption_rect, Qt.AlignHCenter | Qt.AlignTop, self.caption)


class CountdownStage(QWidget):
    """Layers the countdown over the live preview during the final five seconds."""

    def __init__(self) -> None:
        super().__init__()
        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setStyleSheet("background: #000;")
        self.visual = CountdownVisual()
        layout = QStackedLayout(self)
        layout.setStackingMode(QStackedLayout.StackAll)
        layout.addWidget(self.preview)
        layout.addWidget(self.visual)
        layout.setCurrentWidget(self.visual)
        self.preview.hide()

    def set_preview_visible(self, visible: bool) -> None:
        self.preview.setVisible(visible)


class CursorHider(QObject):
    """Keep a touch-first booth clean while preserving pointer feedback on movement."""

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self.hidden = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide_cursor)
        app.installEventFilter(self)
        self.note_activity()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if event.type() in {
            QEvent.MouseMove,
            QEvent.MouseButtonPress,
            QEvent.MouseButtonRelease,
            QEvent.TouchBegin,
            QEvent.TouchUpdate,
            QEvent.KeyPress,
        }:
            self.note_activity()
        return False

    def note_activity(self) -> None:
        if self.hidden:
            QApplication.restoreOverrideCursor()
            self.hidden = False
        self.timer.start(1800)

    def hide_cursor(self) -> None:
        if not self.hidden:
            QApplication.setOverrideCursor(QCursor(Qt.BlankCursor))
            self.hidden = True


class BoothWindow(QMainWindow):
    def __init__(self, camera: Picamera2, preview_config) -> None:
        super().__init__()
        self.camera = camera
        self.preview_config = preview_config
        self.still_config = camera.create_still_configuration(main={"size": (4608, 2592)})
        self.scaler_crop: tuple[int, int, int, int] | None = None
        self.preview_visible = False
        self.capturing = False
        (
            self.zoom,
            self.crop_left,
            self.crop_right,
            self.crop_top,
            self.crop_bottom,
        ) = load_camera_settings()
        self.phase = "idle"

        self.setWindowTitle("Duckbooth")
        self.setStyleSheet("background: #101419; color: white;")
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self.idle = self._make_idle()
        self.instructions = self._make_instructions()
        self.countdown = self._make_countdown()
        self.thanks = self._make_thanks()
        self.settings = self._make_settings()
        self.stack.addWidget(self.idle)
        self.stack.addWidget(self.instructions)
        self.stack.addWidget(self.countdown)
        self.stack.addWidget(self.thanks)
        self.stack.addWidget(self.settings)

        self.quit_shortcut = QShortcut(QKeySequence("Ctrl+Alt+Q"), self)
        self.quit_shortcut.activated.connect(QApplication.quit)

        self.elapsed = QElapsedTimer()
        self.tick = QTimer(self)
        self.tick.timeout.connect(self._advance_countdown)
        self.preview_tick = QTimer(self)
        self.preview_tick.timeout.connect(self._render_preview)
        self.settings_preview_tick = QTimer(self)
        self.settings_preview_tick.timeout.connect(self._render_settings_preview)
        self.thanks_timer = QTimer(self)
        self.thanks_timer.setSingleShot(True)
        self.thanks_timer.timeout.connect(self.reset_to_idle)
        # Configure both camera modes with the same crop at startup. Applying a
        # crop through a reconfigure is reliable on the IMX708, including after
        # it has already begun streaming.
        self._apply_zoom(reconfigure=True)

    def _make_idle(self) -> QWidget:
        page = TapPage(self.show_instructions)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(64, 56, 64, 48)
        layout.addStretch(1)
        title = QLabel("Tik hier om te starten")
        title.setObjectName("title")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 62px; font-weight: 800; color: #f7fbff;")
        layout.addWidget(title)
        layout.addStretch(1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        settings_button = QPushButton("⚙")
        settings_button.setToolTip("Configuratie")
        settings_button.setAccessibleName("Configuratie")
        settings_button.setFixedSize(62, 62)
        settings_button.setStyleSheet(
            "QPushButton { color: #b6c7d9; background: rgba(255,255,255,18); "
            "border: 1px solid rgba(255,255,255,40); border-radius: 31px; font-size: 31px; }"
            "QPushButton:pressed { background: rgba(255,255,255,40); }"
        )
        settings_button.clicked.connect(self.open_settings)
        footer.addWidget(settings_button)
        layout.addLayout(footer)
        return page

    def _make_instructions(self) -> QWidget:
        page = TapPage(self.start_countdown)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(100, 100, 100, 100)
        layout.addStretch(1)
        title = QLabel("Open je zakje met LEGO.")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 54px; font-weight: 800; color: #f7fbff;")
        layout.addWidget(title)
        subtitle = QLabel("Als je klaar bent tik je op dit scherm.")
        subtitle.setAlignment(Qt.AlignCenter)
        subtitle.setStyleSheet("font-size: 30px; color: #b5c4d2; margin-top: 30px;")
        layout.addWidget(subtitle)
        layout.addStretch(1)
        return page

    def _make_countdown(self) -> QWidget:
        page = TapPage(self._dismiss_message)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.countdown_stage = CountdownStage()
        self.preview = self.countdown_stage.preview
        self.countdown_visual = self.countdown_stage.visual
        layout.addWidget(self.countdown_stage)
        return page

    def _make_thanks(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(90, 90, 90, 90)
        layout.addStretch(1)
        message = QLabel("Bedankt!\nJe hebt je eendje verdiend, neem hem maar mee.")
        message.setAlignment(Qt.AlignCenter)
        message.setWordWrap(True)
        message.setStyleSheet("font-size: 46px; font-weight: 800; color: #f7fbff;")
        layout.addWidget(message)
        layout.addStretch(1)
        return page

    def _make_settings(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(44, 38, 44, 40)
        title = QLabel("Configuratie camera")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 34px; font-weight: 800; color: #f7fbff;")
        layout.addWidget(title)
        self.settings_preview = QLabel()
        self.settings_preview.setAlignment(Qt.AlignCenter)
        self.settings_preview.setStyleSheet("background: #000; border-radius: 18px;")
        layout.addWidget(self.settings_preview, 1)
        controls = QHBoxLayout()
        controls.setSpacing(12)
        zoom_name = QLabel("Zoom")
        zoom_name.setStyleSheet("font-size: 20px; color: #b6c7d9;")
        controls.addWidget(zoom_name)
        self.zoom_label = QLabel()
        self.zoom_label.setAlignment(Qt.AlignCenter)
        self.zoom_label.setStyleSheet("font-size: 28px; font-weight: 800; min-width: 90px;")
        for label, callback in (("−", lambda: self.change_zoom(-0.1)), ("+", lambda: self.change_zoom(0.1))):
            button = QPushButton(label)
            button.setFixedSize(70, 62)
            button.setStyleSheet("font-size: 34px; font-weight: 700; border-radius: 18px; background: #29384a; color: white;")
            button.clicked.connect(callback)
            controls.addWidget(button)
        controls.insertWidget(2, self.zoom_label)
        reset = QPushButton("Herstel alles")
        reset.setStyleSheet("font-size: 20px; padding: 16px 24px; border-radius: 18px; background: #29384a; color: white;")
        reset.clicked.connect(self.reset_calibration)
        done = QPushButton("Klaar")
        done.setStyleSheet("font-size: 22px; font-weight: 800; padding: 16px 30px; border-radius: 18px; background: #72d7bd; color: #071219;")
        done.clicked.connect(self.close_settings)
        controls.addWidget(reset)
        controls.addStretch(1)
        controls.addWidget(done)
        layout.addLayout(controls)
        crop_controls = QHBoxLayout()
        crop_controls.setSpacing(10)
        crop_title = QLabel("Bijsnijden")
        crop_title.setStyleSheet("font-size: 20px; font-weight: 800; color: #b6c7d9; min-width: 118px;")
        crop_controls.addWidget(crop_title)
        self.crop_labels: dict[str, QLabel] = {}
        for edge, label in (("left", "Links"), ("right", "Rechts"), ("top", "Boven"), ("bottom", "Onder")):
            name = QLabel(label)
            name.setStyleSheet("font-size: 18px; color: #b6c7d9; margin-left: 15px;")
            crop_controls.addWidget(name)
            for symbol, amount in (("−", -5), ("+", 5)):
                button = QPushButton(symbol)
                button.setFixedSize(54, 52)
                button.setStyleSheet("font-size: 27px; font-weight: 700; border-radius: 15px; background: #29384a; color: white;")
                button.clicked.connect(lambda _checked=False, edge=edge, amount=amount: self.change_edge_crop(edge, amount))
                crop_controls.addWidget(button)
                if amount < 0:
                    value = QLabel()
                    value.setAlignment(Qt.AlignCenter)
                    value.setStyleSheet("font-size: 23px; font-weight: 800; min-width: 58px;")
                    self.crop_labels[edge] = value
                    crop_controls.addWidget(value)
        crop_controls.addStretch(1)
        layout.addLayout(crop_controls)
        self._update_calibration_labels()
        return page

    def start_countdown(self) -> None:
        if self.capturing or self.stack.currentWidget() is self.countdown:
            return
        if not self._has_storage_space():
            self._show_storage_error()
            return
        self.stack.setCurrentWidget(self.countdown)
        self.phase = "build"
        self.preview_visible = False
        self.countdown_stage.set_preview_visible(False)
        self.countdown_visual.set_state(
            COUNTDOWN_SECONDS,
            total=COUNTDOWN_SECONDS,
            caption="Bouw nu een eend!",
        )
        self.elapsed.start()
        self.tick.start(50)
        self._advance_countdown()

    def _advance_countdown(self) -> None:
        elapsed_seconds = self.elapsed.elapsed() / 1000.0
        if self.phase == "build":
            remaining = max(0.0, COUNTDOWN_SECONDS - elapsed_seconds)
            caption = "En rap een beke!" if remaining <= 10 else "Bouw nu een eend!"
            self.countdown_visual.set_state(
                remaining,
                total=COUNTDOWN_SECONDS,
                caption=caption,
            )
            if remaining <= 0:
                self.phase = "framing"
                self.preview_visible = True
                self.countdown_stage.set_preview_visible(True)
                self.elapsed.restart()
                self.preview_tick.start(66)
        else:
            remaining = max(0.0, FINAL_PREVIEW_SECONDS - elapsed_seconds)
            self.countdown_visual.set_state(
                remaining,
                total=FINAL_PREVIEW_SECONDS,
                preview_mode=True,
                caption="Zet je eend goed!",
            )
            if remaining <= 0:
                self.tick.stop()
                self.preview_tick.stop()
                self.capture_photo()

    def _render_preview(self) -> None:
        if not self.preview_visible or self.capturing:
            return
        try:
            frame = self.camera.capture_array("main")
            height, width, channels = frame.shape
            image = QImage(frame.data, width, height, channels * width, LIVE_IMAGE_FORMAT).copy()
            self.preview.setPixmap(QPixmap.fromImage(image).scaled(
                self.preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            ))
        except Exception:
            logging.exception("Unable to render camera preview")
            self.preview_tick.stop()
            self.countdown_stage.set_preview_visible(False)
            self.countdown_visual.set_state(
                self.countdown_visual.remaining, error_message="CAMERA PREVIEW\nUNAVAILABLE"
            )

    def capture_photo(self) -> None:
        if self.capturing:
            return
        self.capturing = True
        self.countdown_stage.set_preview_visible(False)
        self.countdown_visual.set_state(0, total=FINAL_PREVIEW_SECONDS, capture_mode=True)
        QApplication.processEvents()
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8] + ".jpg"
        destination = PHOTO_DIR / name
        try:
            # This is Picamera2's high-resolution capture path; it restores preview mode afterwards.
            self.camera.switch_mode_and_capture_file(self.still_config, str(destination))
            self._post_process_crop(destination)
            self._apply_zoom()
            self.show_thanks()
        except Exception:
            logging.exception("Camera capture failed")
            self.countdown_visual.set_state(0, error_message="PHOTO FAILED\nTAP TO TRY AGAIN")
        finally:
            self.capturing = False

    def show_instructions(self) -> None:
        if not self.capturing:
            self.stack.setCurrentWidget(self.instructions)

    def show_thanks(self) -> None:
        self.preview_visible = False
        self.stack.setCurrentWidget(self.thanks)
        self.thanks_timer.start(5000)

    def reset_to_idle(self) -> None:
        if self.capturing:
            return
        self.preview_tick.stop()
        self.settings_preview_tick.stop()
        self.thanks_timer.stop()
        self.countdown_stage.set_preview_visible(False)
        self.phase = "idle"
        self.stack.setCurrentWidget(self.idle)

    def _dismiss_message(self) -> None:
        if not self.tick.isActive() and not self.capturing:
            self.reset_to_idle()

    def open_settings(self) -> None:
        if self.capturing:
            return
        self.stack.setCurrentWidget(self.settings)
        self._render_settings_preview()
        self.settings_preview_tick.start(66)

    def close_settings(self) -> None:
        self.settings_preview_tick.stop()
        self.reset_to_idle()

    def _render_settings_preview(self) -> None:
        try:
            frame = self.camera.capture_array("main")
            height, width, channels = frame.shape
            image = QImage(frame.data, width, height, channels * width, LIVE_IMAGE_FORMAT).copy()
            pixmap = QPixmap.fromImage(image).scaled(
                self.settings_preview.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self.settings_preview.setPixmap(self._with_crop_guide(pixmap))
        except Exception:
            logging.exception("Unable to render configuration preview")

    def change_zoom(self, delta: float) -> None:
        self.set_zoom(self.zoom + delta)

    def set_zoom(self, zoom: float) -> None:
        self.zoom = round(max(1.0, min(3.0, zoom)), 1)
        self._apply_zoom(reconfigure=True)
        self._save_camera_settings()
        self._update_calibration_labels()

    def change_edge_crop(self, edge: str, amount: int) -> None:
        self.set_edge_crop(edge, getattr(self, f"crop_{edge}") + amount)

    def set_edge_crop(self, edge: str, crop: int) -> None:
        setattr(self, f"crop_{edge}", max(0, min(35, int(crop))))
        self._save_camera_settings()
        self._update_calibration_labels()
        self._render_settings_preview()

    def reset_calibration(self) -> None:
        self.set_zoom(1.0)
        self.crop_left = self.crop_right = self.crop_top = self.crop_bottom = 0
        self._save_camera_settings()
        self._update_calibration_labels()
        self._render_settings_preview()

    def _save_camera_settings(self) -> None:
        save_camera_settings(
            self.zoom,
            self.crop_left,
            self.crop_right,
            self.crop_top,
            self.crop_bottom,
        )

    def _update_calibration_labels(self) -> None:
        if hasattr(self, "zoom_label"):
            self.zoom_label.setText(f"{self.zoom:.1f}×")
        if hasattr(self, "crop_labels"):
            for edge, label in self.crop_labels.items():
                label.setText(f"{getattr(self, f'crop_{edge}')}%")

    def _with_crop_guide(self, pixmap: QPixmap) -> QPixmap:
        """Dim discarded edges so calibration shows the saved-photo crop."""
        if not any((self.crop_left, self.crop_right, self.crop_top, self.crop_bottom)) or pixmap.isNull():
            return pixmap
        left = int(pixmap.width() * self.crop_left / 100)
        right = int(pixmap.width() * self.crop_right / 100)
        top = int(pixmap.height() * self.crop_top / 100)
        bottom = int(pixmap.height() * self.crop_bottom / 100)
        kept = QRect(left, top, pixmap.width() - left - right, pixmap.height() - top - bottom)
        guide = QPixmap(pixmap)
        painter = QPainter(guide)
        painter.fillRect(guide.rect(), QColor(0, 0, 0, 125))
        painter.drawPixmap(kept, pixmap, kept)
        painter.setPen(QPen(QColor("#77f4cc"), 4))
        painter.drawRect(kept.adjusted(2, 2, -2, -2))
        painter.end()
        return guide

    def _post_process_crop(self, destination: Path) -> None:
        """Write the configured edge crop atomically after high-resolution capture."""
        if not any((self.crop_left, self.crop_right, self.crop_top, self.crop_bottom)):
            return
        temporary = destination.with_suffix(".cropping.jpg")
        with Image.open(destination) as photo:
            left = int(photo.width * self.crop_left / 100)
            right = int(photo.width * self.crop_right / 100)
            top = int(photo.height * self.crop_top / 100)
            bottom = int(photo.height * self.crop_bottom / 100)
            crop = photo.crop((left, top, photo.width - right, photo.height - bottom))
            options = {"format": "JPEG", "quality": 96}
            if exif := photo.info.get("exif"):
                options["exif"] = exif
            crop.save(temporary, **options)
        temporary.replace(destination)

    def _apply_zoom(self, *, reconfigure: bool = False) -> None:
        try:
            sensor_width, sensor_height = self.camera.camera_properties["PixelArraySize"]
            crop_width = int(sensor_width / self.zoom) // 2 * 2
            crop_height = int(sensor_height / self.zoom) // 2 * 2
            crop_x = (sensor_width - crop_width) // 2
            crop_y = (sensor_height - crop_height) // 2
            self.scaler_crop = (crop_x, crop_y, crop_width, crop_height)
            # Preview and still capture are separate camera configurations. Keep
            # both centered crops in sync so calibration matches the final JPEG.
            self.preview_config["controls"]["ScalerCrop"] = self.scaler_crop
            self.still_config["controls"]["ScalerCrop"] = self.scaler_crop
            if reconfigure:
                self.camera.stop()
                self.camera.configure(self.preview_config)
                self.camera.start()
            else:
                self.camera.set_controls({"ScalerCrop": self.scaler_crop})
        except Exception:
            logging.exception("Unable to apply camera zoom")

    def _has_storage_space(self) -> bool:
        import shutil

        return shutil.disk_usage(PHOTO_DIR).free >= LOW_SPACE_BYTES

    def _show_storage_error(self) -> None:
        self.stack.setCurrentWidget(self.countdown)
        self.countdown_stage.set_preview_visible(False)
        self.countdown_visual.set_state(0, error_message="NOT ENOUGH\nFREE SPACE")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)


class TapPage(QWidget):
    def __init__(self, callback) -> None:
        super().__init__()
        self.callback = callback

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.callback()
        event.accept()


class DuckboothApplication:
    def __init__(self) -> None:
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        self.app = QApplication(sys.argv)
        self.app.setApplicationName("Duckbooth")
        self.cursor_hider = CursorHider(self.app)
        self.camera = Picamera2()
        preview_config = self.camera.create_preview_configuration(
            main={"size": (1536, 864), "format": "RGB888"}
        )
        self.camera.configure(preview_config)
        self.camera.start()

        self.main = BoothWindow(self.camera, preview_config)
        self.main_screen = app_screen(self.app, "HDMI-A-2", fallback=0)
        show_fullscreen_on(self.main, self.main_screen)

        self.gallery: GalleryWindow | None = None
        self.app.screenAdded.connect(self.sync_gallery)
        self.app.screenRemoved.connect(lambda _screen: self.sync_gallery())
        self.screen_poll = QTimer()
        self.screen_poll.timeout.connect(self.sync_gallery)
        self.screen_poll.start(2000)
        self.sync_gallery()

    def sync_gallery(self, *_args) -> None:
        screen = app_screen(self.app, "HDMI-A-1")
        if screen is None:
            if self.gallery:
                self.gallery.close()
                self.gallery.deleteLater()
                self.gallery = None
            return
        if self.gallery is None:
            self.gallery = GalleryWindow()
            show_gallery_on(self.gallery, screen)
        elif self.gallery.windowHandle() and self.gallery.windowHandle().screen() != screen:
            show_gallery_on(self.gallery, screen)

    def run(self) -> int:
        try:
            return self.app.exec_()
        finally:
            self.camera.stop()


def main() -> int:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(APP_DIR / "duckbooth.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return DuckboothApplication().run()
    except Exception:
        logging.exception("Duckbooth failed during startup")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
