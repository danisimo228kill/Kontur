import csv
import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault('QT_QUICK_BACKEND', 'software')
os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu --no-sandbox')

from PyQt5.QtCore import Qt, QEvent, QTimer, QPointF, QRectF, pyqtSignal, QPropertyAnimation, QEasingCurve
from PyQt5.QtGui import QColor, QPainter, QPen, QPolygonF, QPainterPath, QFont, QFontDatabase, QWheelEvent, QPixmap
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QDoubleSpinBox, QComboBox, QFormLayout,
    QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView, QFileDialog,
    QMessageBox, QDialog, QAbstractItemView, QScrollArea, QCheckBox, QStackedWidget, QSizePolicy, QTabWidget,
    QToolButton, QMenu, QAction, QSlider)

from model import TrafficSource, Tracks, world_pixel, latlon_from_pixel, destination, RADAR_RATE
from labels import FIELDS, metric_lines, target_name, target_table_name, MARK_NAMES, gps_label
from yandex_map import api_key, create_view, snapshot
from offline_map import OfflineMap, DEFAULT_MAP, contains
from markers import icon_kind, draw_icon, display_type
from coordinates import WGS84, SK42, NAMES, convert, coordinate_text, bearing_sector, scale_bar
from region_maps import find_region, RegionDownload, DETAIL_ZOOM
from gnss_reader import GNSSReader
from mog_panel import MogPanel
from esp_panel import ESPControlDialog
import hardware_config



def radar_bounds(lat, lon, radius_m):
    lat_rad = math.radians(lat)
    meters_per_degree_lat = 111320.0
    meters_per_degree_lon = 111320.0 * math.cos(lat_rad)
    dlat = radius_m / meters_per_degree_lat
    dlon = radius_m / meters_per_degree_lon
    return ((lat - dlat, lon - dlon), (lat + dlat, lon + dlon))


def region_bounds(lat, lon, size_m=10000):
    lat_rad = math.radians(lat)
    meters_per_degree_lat = 111320.0
    meters_per_degree_lon = 111320.0 * math.cos(lat_rad)
    half_size = size_m / 2.0
    dlat = half_size / meters_per_degree_lat
    dlon = half_size / meters_per_degree_lon
    return ((lat - dlat, lon - dlon), (lat + dlat, lon + dlon))


def _range_m(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    dlat, dlon = b - a, math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
    return 2 * 6371008.8 * math.asin(min(1, math.sqrt(h)))



MARK_COLORS = {'green': '#36c96c', 'red': '#ff5656', 'blue': '#59dbdf'}
MARK_NAMES_OLD = {'friend': 'Свой', 'foe': 'Чужой', 'unknown': 'Неопр.'}
MARK_TO_COLOR = {'friend': 'green', 'foe': 'red', 'unknown': 'blue'}
COLOR_TO_MARK = {v: k for k, v in MARK_TO_COLOR.items()}
CYAN = QColor('#59dbdf')
GOLD = QColor('#ffca72')
OUT_OF_RANGE_COLOR = QColor('#78909c')

MOG_IDS = ['МОГ1', 'МОГ2', 'МОГ3']

RADAR_MODES = {
    'Ближняя (10 км)': 10000,
    'Дальняя (70 км)': 70000,
    'Сверхдальняя (300 км)': 300000,
}

TIME_OF_DAY_MODES = {'День': 'day', 'Сумерки': 'dusk', 'Ночь': 'night'}

class MockSample:
    __slots__ = ('track_id', 'object_type', 'timestamp', 'lat', 'lon',
                 'altitude_m', 'ground_range_m', 'speed_mps', 'course_deg', 'appearance')

    def __init__(self, track_id, lat, lon, timestamp, ground_range_m=0.0):
        self.track_id = track_id
        self.object_type = 'Наземный'
        self.appearance = 'Наземный'
        self.timestamp = timestamp
        self.lat = lat
        self.lon = lon
        self.altitude_m = 0.0
        self.ground_range_m = ground_range_m
        self.speed_mps = 0.0
        self.course_deg = 0.0


class CollapsibleSection(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)

        self.button = QToolButton(self)
        self.button.setText(title)
        self.button.setCheckable(True)
        self.button.setChecked(False)
        self.button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.button.setStyleSheet('''
            QToolButton { background: #1b2e3d; color: #91a7b7; border: 1px solid #304553; border-radius: 4px; padding: 8px; text-align: left; font-weight: 600; }
            QToolButton:hover { background: #253e50; }
            QToolButton:checked { background: #294758; color: #edf6fb; }
        ''')
        self.button.clicked.connect(self.toggle)
        self.layout.addWidget(self.button)

        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(10, 10, 10, 10)
        self.content_layout.setSpacing(8)
        self.content.setVisible(False)
        self.layout.addWidget(self.content)

        self.animation = QPropertyAnimation(self.content, b"maximumHeight")
        self.animation.setDuration(200)
        self.animation.setEasingCurve(QEasingCurve.InOutQuad)

    def toggle(self):
        try:
            if self.button.isChecked():
                self.content.setVisible(True)
                self.animation.setStartValue(0)
                self.animation.setEndValue(self.content.sizeHint().height())
                self.animation.start()
            else:
                self.animation.setStartValue(self.content.height())
                self.animation.setEndValue(0)
                try:
                    self.animation.finished.disconnect()
                except TypeError:
                    pass
                self.animation.finished.connect(self._on_animation_finished)
                self.animation.start()
        except RuntimeError:
            pass

    def _on_animation_finished(self):
        try:
            if self.content:
                self.content.setVisible(False)
        except RuntimeError:
            pass


class PanelWheelMixin:
    def wheelEvent(self, event):
        parent = self.parentWidget()
        while parent is not None:
            if isinstance(parent, QScrollArea):
                viewport = parent.viewport()
                forwarded = QWheelEvent(QPointF(viewport.mapFromGlobal(event.globalPos())), event.globalPosF(),
                    event.pixelDelta(), event.angleDelta(), event.buttons(), event.modifiers(),
                    event.phase(), event.inverted())
                QApplication.sendEvent(viewport, forwarded)
                event.accept()
                return
            parent = parent.parentWidget()
        event.ignore()


class NoWheelSpinBox(PanelWheelMixin, QDoubleSpinBox):
    pass


class NoWheelComboBox(PanelWheelMixin, QComboBox):
    def __init__(self, *args):
        super().__init__(*args)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(12)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)


class MapCanvas(QWidget):
    selected = pyqtSignal(str)
    changed = pyqtSignal()
    map_ready = pyqtSignal()
    region_clicked = pyqtSignal(float, float)
    gps_clicked = pyqtSignal()

    def __init__(self, tracks):
        super().__init__()
        self.tracks = tracks
        self.lat, self.lon, self.zoom = 60.022612, 30.293248, 11
        self.antenna = (60.022612, 30.293248)
        self.gps_status = None
        self.gps_coordinates_visible = False
        self.gps_hit_rect = None
        self.azimuth = 0
        self.sector_width = 60
        self.radius_m = 70000
        self.ring_step_m = 10000
        self.auto_fit = True
        self.selected_id = None
        self.paused = False
        self.tail_mode = 'short'
        self.label_fields = set()
        self.selected_card = True
        self.target_marks = {}
        self.theme = 'night'
        self.display_crs = WGS84
        self.pinned_regions = [None, None, None]
        self.active_region = 0
        self.live_only = False
        self.show_previews = False
        self.offline = None

        self.hit_positions = {}
        self.label_rects = {}
        self.drag_pos = None
        self.press_pos = None
        self.dragged = False
        self.setMinimumSize(240, 240)
        self.setMouseTracking(True)


        self._tile_born = {}
        self._known_tiles = set()
        self.FADE_DURATION = 0.35
        self._fade_timer = QTimer(self)
        self._fade_timer.setInterval(16)
        self._fade_timer.timeout.connect(self._on_fade_tick)

        self.zoom_buttons = {}
        for index, (title, delta) in enumerate((('+', 1), ('−', -1))):
            button = QPushButton(title, self)
            button.setObjectName('mapZoomButton')
            button.setFixedSize(38, 38)
            button.move(16, 48 + index * 42)
            button.setToolTip('Приблизить карту' if delta > 0 else 'Отдалить карту')
            button.setAccessibleName(button.toolTip())
            button.clicked.connect(lambda checked=False, d=delta: self.set_zoom(self.zoom + d))
            self.zoom_buttons[delta] = button

    def point(self, lat, lon):
        cx, cy = world_pixel(self.lat, self.lon, self.zoom)
        px, py = world_pixel(lat, lon, self.zoom)
        size = 256 * 2 ** self.zoom
        dx = (px - cx + size / 2) % size - size / 2
        return QPointF(self.width() / 2 + dx, self.height() / 2 + py - cy)

    def range_m(self, lat, lon):
        a, b = math.radians(self.antenna[0]), math.radians(lat)
        dlat, dlon = b - a, math.radians(lon - self.antenna[1])
        h = math.sin(dlat / 2) ** 2 + math.cos(a) * math.cos(b) * math.sin(dlon / 2) ** 2
        return 2 * 6371008.8 * math.asin(min(1, math.sqrt(h)))

    def focus_antenna(self):
        self.lat, self.lon = self.antenna
        self.auto_fit = True
        self.fit_radar()
        self.update()

    def fit_radar(self):
        bounds = radar_bounds(*self.antenna, self.radius_m)
        cx, cy = world_pixel(*self.antenna, 0)
        x0, y0 = world_pixel(bounds[0][0], bounds[0][1], 0)
        x1, y1 = world_pixel(bounds[1][0], bounds[1][1], 0)
        span_x = 2 * max(abs(x0-cx), abs(x1-cx))
        span_y = 2 * max(abs(y0-cy), abs(y1-cy))
        ratio_x = max(1, self.width() - 64) / max(1, span_x)
        ratio_y = max(1, self.height() - 64) / max(1, span_y)
        calculated_zoom = math.log2(min(ratio_x, ratio_y))
        self.zoom = max(1, min(18, calculated_zoom))

    def set_zoom(self, zoom):
        self.auto_fit = False
        self.zoom = max(2, min(19, zoom))
        self.update()
        self.changed.emit()

    def load_offline(self, path=DEFAULT_MAP):
        layer = OfflineMap(path, async_render=True)
        layer.ready.connect(self.map_ready)
        if self.offline:
            self.offline.close()
        self.offline = layer
        self.offline.theme = self.theme
        self.offline_path = Path(path)
        if self.auto_fit:
            self.fit_radar()
        self._known_tiles.clear()
        self._tile_born.clear()
        self._fade_timer.start()

    def coverage_rect(self):
        west, south, east, north = self.offline.bounds
        if east - west >= 359:
            return QRectF(0, self.point(north, self.lon).y(), self.width(),
                self.point(south, self.lon).y() - self.point(north, self.lon).y())
        top_left = self.point(north, west)
        bottom_right = self.point(south, east)
        if bottom_right.x() < top_left.x():
            bottom_right.setX(bottom_right.x() + 256 * 2 ** self.zoom)
        return QRectF(top_left, bottom_right)

    def resizeEvent(self, event):
        if self.auto_fit:
            self.fit_radar()
        super().resizeEvent(event)

    def _on_fade_tick(self):
        now = time.time()
        has_fading = False
        for key in list(self._tile_born.keys()):
            if key not in self._known_tiles:
                self._tile_born.pop(key, None)
                continue
            age = now - self._tile_born[key]
            if age < self.FADE_DURATION:
                has_fading = True
            else:
                self._tile_born.pop(key, None)
        if has_fading:
            self._basemap_key = None
            self.update()
        else:
            self._fade_timer.stop()

    def paint_basemap(self, p):
        if self.offline:
            self.offline.set_viewport(self.lat, self.lon, self.zoom)
            self.offline.begin_view("main")
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor('#edf0df' if self.theme == 'day' else '#182733'))
        tile_zoom = max(0, min(19, math.ceil(self.zoom)))
        factor = 2 ** (self.zoom - tile_zoom)
        cx, cy = world_pixel(self.lat, self.lon, tile_zoom)
        left, top = cx - self.width() / (2 * factor), cy - self.height() / (2 * factor)
        right, bottom = left + self.width() / factor, top + self.height() / factor
        p.setPen(QPen(QColor('#243846'), 1))
        p.save()
        if self.offline and self.offline.bounds:
            west, south, east, north = self.offline.bounds
            p.setClipRect(self.coverage_rect())
            bx0, by0 = world_pixel(north, west, tile_zoom)
            bx1, by1 = world_pixel(south, east, tile_zoom)
            if east - west >= 359:
                bx0, bx1 = left, right
            else:
                bx0, by0, bx1, by1 = left, top, right, bottom
            for x in range(math.floor(max(left, bx0) / 256), math.floor(min(right, bx1) / 256) + 1):
                for y in range(math.floor(max(top, by0) / 256), math.floor(min(bottom, by1) / 256) + 1):
                    rect = QRectF((x * 256 - left) * factor, (y * 256 - top) * factor, 256 * factor, 256 * factor)
                    tile = self.offline.tile(tile_zoom, x, y, scope="main") if self.offline else None
                    if tile is not None and not tile.isNull():
                        key = (tile_zoom, x, y, self.offline.theme)
                        if key not in self._known_tiles:
                            self._known_tiles.add(key)
                            self._tile_born[key] = time.time()
                            if not self._fade_timer.isActive():
                                self._fade_timer.start()
                        opacity = 1.0
                        if key in self._tile_born:
                            age = time.time() - self._tile_born[key]
                            if age < self.FADE_DURATION:
                                t = age / self.FADE_DURATION
                                opacity = 1.0 - (1.0 - t) ** 3
                        if opacity < 1.0:
                            p.setOpacity(opacity)
                        p.drawPixmap(rect, tile, QRectF(tile.rect()))
                        if opacity < 1.0:
                            p.setOpacity(1.0)
                    else:
                        p.drawRect(rect)
        p.restore()
        if self.offline:
            self.offline.end_view("main")

    def paintEvent(self, event):
        p = QPainter(self)
        key = (self.width(), self.height(), self.lat, self.lon, self.zoom, self.theme,
            id(self.offline), self.offline.revision if self.offline else 0)
        if getattr(self, '_basemap_key', None) != key:
            self._basemap = QPixmap(self.size())
            painter = QPainter(self._basemap)
            self.paint_basemap(painter)
            painter.end()
            self._basemap_key = key
        p.drawPixmap(0, 0, self._basemap)

        if self.offline and self.offline.bounds:
            p.setPen(QPen(QColor('#6f8b9b'), 1, Qt.DashLine))
            p.drawRect(self.coverage_rect())

        p.setRenderHint(QPainter.Antialiasing)
        antenna = self.point(*self.antenna)

        p.save()
        polygon = QPolygonF([antenna])
        for angle in range(-5, 6, 2):
            polygon.append(self.point(*destination(*self.antenna, self.azimuth + angle, self.radius_m)))
        polygon.append(antenna)
        p.setBrush(QColor(89, 219, 223, 24))
        p.setPen(QPen(QColor(89, 219, 223, 100), 1))
        p.drawPolygon(polygon)

        p.setBrush(Qt.NoBrush)
        for angle in range(0, 360, 30):
            edge = self.point(*destination(*self.antenna, angle, self.radius_m))
            p.setPen(QPen(QColor(89, 219, 223, 125), 1, Qt.DashLine))
            p.drawLine(antenna, edge)
            label = self.point(*destination(*self.antenna, angle, self.radius_m * .92))
            p.setPen(QColor('#174b60' if self.theme == 'day' else '#edf5fa'))
            p.drawText(label, f'{angle:03d}°')

        for radius in range(self.ring_step_m, int(self.radius_m) + 1, self.ring_step_m):
            ring = QPainterPath()
            for angle in range(0, 361, 4):
                point = self.point(*destination(*self.antenna, angle, radius))
                if angle == 0:
                    ring.moveTo(point)
                else:
                    ring.lineTo(point)
            p.setPen(QPen(QColor(89, 219, 223, 65), 1, Qt.DashLine))
            p.drawPath(ring)
            label = self.point(*destination(*self.antenna, 45, radius))
            p.setPen(QColor('#174b60' if self.theme == 'day' else '#edf5fa'))
            p.drawText(label, f'{radius / 1000:g} км')
        p.restore()

        p.save()
        self.hit_positions = {}
        for track_id, history in self.tracks.active_history().items():
            sample = history[-1]
            stale = not self.paused and time.time() - sample.timestamp > 10
            is_out_of_range = (track_id not in MOG_IDS and self.range_m(sample.lat, sample.lon) > self.radius_m)

            if track_id in self.target_marks:
                color = QColor(MARK_COLORS[self.target_marks[track_id]])
            elif is_out_of_range:
                color = OUT_OF_RANGE_COLOR
            elif stale:
                color = QColor('#78909c')
            elif track_id == self.selected_id:
                color = GOLD
            else:
                color = QColor('#007487') if self.theme == 'day' else CYAN

            alpha = 128 if is_out_of_range else 255
            color.setAlpha(alpha)

            points = self.tracks.tail(track_id, self.tail_mode)
            if points:
                p.setBrush(Qt.NoBrush)
                path = QPainterPath(self.point(points[0].lat, points[0].lon))
                for item in points[1:]:
                    path.lineTo(self.point(item.lat, item.lon))
                p.setPen(QPen(color, 2 if track_id == self.selected_id else 1.3))
                p.drawPath(path)

            for item in (points[:-1] if self.tail_mode == 'short' else []):
                pos = self.point(item.lat, item.lon)
                p.setBrush(color)
                p.drawEllipse(pos, 2, 2)

            pos = self.point(sample.lat, sample.lon)
            self.hit_positions[track_id] = pos
            p.setPen(QPen(color, 2))
            p.setBrush(QColor('#101f2b'))
            if track_id == self.selected_id:
                p.drawEllipse(pos, 14, 14)
            p.setBrush(color)
            p.save()
            p.translate(pos)
            kind = icon_kind(sample)
            p.rotate(0 if kind == 'tower' else sample.course_deg or 0)
            draw_icon(p, kind)
            p.restore()

        self.label_rects = {}
        ordered = sorted(self.tracks.active_ids, key=lambda key: key != self.selected_id)
        occupied = [QRectF(pos.x() - 13, pos.y() - 13, 26, 26) for pos in self.hit_positions.values()]

        for track_id in ordered:
            s = self.tracks.history[track_id][-1]
            selected = track_id == self.selected_id
            is_out_of_range = (track_id not in MOG_IDS and self.range_m(s.lat, s.lon) > self.radius_m)

            color = QColor(MARK_COLORS[self.target_marks[track_id]]) if track_id in self.target_marks else OUT_OF_RANGE_COLOR if is_out_of_range else GOLD if selected else CYAN
            pos = self.hit_positions[track_id]
            fields = FIELDS if selected and self.selected_card else self.label_fields
            metrics = metric_lines(s, fields)
            mark = self.target_marks.get(track_id)

            lines = [target_name(track_id)] + ([' · '.join(metrics)] if metrics else [])
            fm = p.fontMetrics()
            width = max(fm.horizontalAdvance(line) for line in lines) + 16
            height = len(lines) * fm.height() + 10

            if not self.rect().adjusted(-10, -10, 10, 10).contains(pos.toPoint()):
                continue

            candidates = []
            label_area = QRectF(self.rect()).adjusted(3, 35, -3, -36)
            for dx, dy in [(-width / 2, -height - 18), (18, -height / 2), (-width - 18, -height / 2),
                (-width / 2, 18), (-width / 2, -height - 60)]:
                x = max(label_area.left(), min(label_area.right() - width, pos.x() + dx))
                y = max(label_area.top(), min(label_area.bottom() - height, pos.y() + dy))
                candidates.append(QRectF(x, y, width, height))

            rect = min(candidates, key=lambda r: sum(r.intersected(old).width() * r.intersected(old).height()
                for old in occupied if r.intersects(old)))
            occupied.append(rect.adjusted(-4, -4, 4, 4))
            self.label_rects[track_id] = rect

            p.setPen(QPen(QColor(140, 170, 185, 110), 1))
            p.drawLine(pos, QPointF(rect.center().x(), rect.bottom()))
            p.setPen(QPen(color if selected else QColor('#466073'), 1))
            p.setBrush(QColor(16, 31, 43, 235))
            p.drawRoundedRect(rect, 4, 4)

            for index, line in enumerate(lines):
                p.setPen(color if index == 0 else QColor('#edf5fa'))
                p.drawText(rect.adjusted(8, 5 + index * fm.height(), -8, -5), Qt.AlignTop | Qt.AlignHCenter, line)
        p.restore()

        for i, region in enumerate(self.pinned_regions):
            if not region:
                continue
            bounds = region_bounds(region['lat'], region['lon'])
            rect = QRectF(self.point(bounds[1][0], bounds[0][1]), self.point(bounds[0][0], bounds[1][1]))
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor('#00c9ed'), 1, Qt.DashLine))
            p.drawRect(rect)
            p.drawText(rect.topLeft() + QPointF(5, 15), region.get('name', f'Участок {i + 1}'))

        p.setBrush(GOLD)
        p.setPen(QPen(GOLD, 2))
        p.drawLine(antenna + QPointF(0, -12), antenna + QPointF(0, 10))
        p.drawLine(antenna + QPointF(-7, 10), antenna + QPointF(7, 10))
        p.drawEllipse(antenna + QPointF(0, -12), 3, 3)
        p.setBrush(Qt.NoBrush)
        p.drawArc(QRectF(antenna.x() - 10, antenna.y() - 23, 20, 20), 20 * 16, 140 * 16)
        p.drawText(antenna + QPointF(14, 18), 'АНТЕННА')


        self.gps_hit_rect = None
        gps = self.gps_status or {}
        position = gps.get('position')
        if position and abs(position['latitude']) <= 85.05112878:
            point = self.point(position['latitude'], position['longitude'])
            fresh = gps.get('has_fix', False)
            color = QColor('#ffb653' if gps.get('warning') else '#329cff' if fresh else '#8794a4')
            label = gps_label(gps)
            width = p.fontMetrics().horizontalAdvance(label) + 16
            rect = QRectF(point.x()+16, point.y()-15, width, 28)
            p.save()
            p.setPen(QPen(color, 2, Qt.SolidLine if fresh else Qt.DashLine))
            p.setBrush(QColor(color.red(), color.green(), color.blue(), 45))
            p.drawEllipse(point, 17, 17)
            p.setBrush(color)
            p.drawEllipse(point, 6, 6)
            p.fillRect(rect, QColor('#142536' if self.theme != 'day' else '#f4f7fb'))
            p.setPen(color)
            p.drawText(rect, Qt.AlignCenter, label)
            if self.gps_coordinates_visible:
                coordinates = f"{position['latitude']:.7f}°, {position['longitude']:.7f}°"
                coordinate_width = p.fontMetrics().horizontalAdvance(coordinates) + 16
                coordinate_rect = QRectF(point.x()-coordinate_width/2, point.y()-53, coordinate_width, 28)
                p.fillRect(coordinate_rect, QColor('#142536' if self.theme != 'day' else '#f4f7fb'))
                p.drawText(coordinate_rect, Qt.AlignCenter, coordinates)
            p.restore()
            self.gps_hit_rect = rect.united(QRectF(point.x()-18, point.y()-18, 36, 36))

        _, _, mpp = scale_bar(self.lat, self.zoom)
        p.fillRect(QRectF(10, self.height() - 99, 310, 62), QColor(12, 22, 32, 230))
        p.setPen(QPen(QColor('#edf5fa'), 2))
        width_m = self.width() * mpp
        width_text = f'{width_m / 1000:.1f} км' if width_m >= 1000 else f'{width_m:.0f} м'
        p.drawText(QPointF(22, self.height() - 76), f'Радиус: {self.radius_m / 1000:g} км · кольца: {self.ring_step_m / 1000:g} км')
        p.drawText(QPointF(22, self.height() - 54), f'Масштаб: ≈ {width_text} по ширине')
        p.setPen(QColor('#174b60' if self.theme == 'day' else '#dbe8ee'))
        p.drawText(QRectF(18, 14, self.width() - 36, 28), Qt.AlignLeft, 'N ↑     СЕВЕР')
        p.fillRect(QRectF(0, self.height() - 31, self.width(), 31), QColor(12, 22, 32, 230))

        if self.offline and self.offline.bounds:
            if not contains(self.offline.bounds, self.lat, self.lon):
                p.drawText(QRectF(18, 43, self.width() - 36, 25), Qt.AlignLeft, 'За пределами сохранённого района')

    def wheelEvent(self, event):
        if not event.angleDelta().y():
            return
        position = event.pos()
        cx, cy = world_pixel(self.lat, self.lon, self.zoom)
        anchor = latlon_from_pixel(cx + position.x() - self.width() / 2, cy + position.y() - self.height() / 2, self.zoom)
        self.set_zoom(self.zoom + (1 if event.angleDelta().y() > 0 else -1))
        px, py = world_pixel(*anchor, self.zoom)
        self.lat, self.lon = latlon_from_pixel(px - position.x() + self.width() / 2, py - position.y() + self.height() / 2, self.zoom)
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        self.press_pos = event.pos()
        self.drag_pos = event.pos() if event.button() == Qt.LeftButton else None
        self.dragged = False

    def mouseMoveEvent(self, event):
        if self.drag_pos is None:
            return
        if not self.dragged and (event.pos() - self.press_pos).manhattanLength() < 5:
            return
        self.dragged = True
        delta = event.pos() - self.drag_pos
        cx, cy = world_pixel(self.lat, self.lon, self.zoom)
        self.lat, self.lon = latlon_from_pixel(cx - delta.x(), cy - delta.y(), self.zoom)
        self.drag_pos = event.pos()
        self.auto_fit = False
        self.update()

    def mouseReleaseEvent(self, event):
        self.drag_pos = None
        if self.press_pos is None or self.dragged or (event.pos() - self.press_pos).manhattanLength() >= 5:
            self.press_pos = None
            return
        self.press_pos = None

        if event.button() == Qt.RightButton:
            cx, cy = world_pixel(self.lat, self.lon, self.zoom)
            lat, lon = latlon_from_pixel(cx + event.pos().x() - self.width() / 2,
                cy + event.pos().y() - self.height() / 2, self.zoom)
            self.region_clicked.emit(lat, lon)
        elif event.button() == Qt.LeftButton:
            if self.gps_hit_rect and self.gps_hit_rect.contains(QPointF(event.pos())):
                self.gps_clicked.emit()
                return
            for track_id, rect in self.label_rects.items():
                if rect.contains(QPointF(event.pos())):
                    self.selected.emit(track_id)
                    return
            nearest = sorted(self.hit_positions,
                key=lambda key: (self.hit_positions[key] - QPointF(event.pos())).manhattanLength())
            if nearest and (self.hit_positions[nearest[0]] - QPointF(event.pos())).manhattanLength() < 16:
                self.selected.emit(nearest[0])


class Window(QMainWindow):
    def __init__(self, enable_yandex=True):
        super().__init__()
        self.enable_yandex = enable_yandex
        self.preferences_path = Path(__file__).parent / 'data' / 'map-settings.json'
        preferences = {}

        if enable_yandex and '--smoke-test' not in sys.argv:
            try:
                preferences = json.loads(self.preferences_path.read_text(encoding='utf-8'))
                if not isinstance(preferences, dict):
                    preferences = {}
            except (OSError, ValueError):
                pass

        coordinates = preferences.get('antenna', [60.022612, 30.293248, 180])
        if (not isinstance(coordinates, list) or len(coordinates) != 3 or
            not all(isinstance(v, (int, float)) and math.isfinite(v) for v in coordinates) or
            not -85 <= coordinates[0] <= 85 or not -180 <= coordinates[1] <= 180):
            coordinates = [60.022612, 30.293248, 180]

        self.setWindowTitle('Контур · наблюдение')
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1440, screen.width() - 24), min(900, screen.height() - 48))

        self.tracks = Tracks()
        self.elapsed = 0.0
        self.previous_tick = time.monotonic()
        self.running = True

        self.canvas = MapCanvas(self.tracks)
        self.canvas.map_ready.connect(self.canvas.update)
        self.canvas.antenna = tuple(coordinates[:2])
        self.canvas.focus_antenna()
        self.antenna_altitude = coordinates[2]
        self.input_system = WGS84
        self.offline_error = ''
        self.download_job = None
        self.pending_region = False
        self.esp_speed = RADAR_RATE
        self.esp_model_controlled = hardware_config.SIMULATION_MODE
        self.canvas.esp_controlled = self.esp_model_controlled

        self.gnss = GNSSReader(baudrate=9600)
        self.canvas.gps_clicked.connect(self.toggle_gps_coordinates)
        self._gps_first_focus = False

        if enable_yandex and '--smoke-test' not in sys.argv:
            self.gnss.start()

        default_mog = {'МОГ1': [60.030, 30.280], 'МОГ2': [60.015, 30.310], 'МОГ3': [60.025, 30.300]}
        self.mog_positions = preferences.get('mog_positions', default_mog)
        for mog_id in MOG_IDS:
            coords = self.mog_positions.get(mog_id)
            if not isinstance(coords, (list, tuple)) or len(coords) != 2:
                self.mog_positions[mog_id] = default_mog[mog_id]

        self.synthesizer_freq = preferences.get('synthesizer_freq', 8550)
        self.radar_mode = preferences.get('radar_mode', 'Дальняя (70 км)')
        if self.radar_mode not in RADAR_MODES:
            self.radar_mode = 'Дальняя (70 км)'
        self.radar_range = RADAR_MODES.get(self.radar_mode, 70000)
        self.canvas.radius_m = self.radar_range
        self.canvas.ring_step_m = self.radar_range // 7 if self.radar_range >= 70000 else self.radar_range // 5

        self.coord_system_mode = preferences.get('coord_system', 'wgs84')
        self.canvas.display_crs = WGS84 if self.coord_system_mode == 'wgs84' else SK42

        self.time_of_day = preferences.get('time_of_day', 'Ночь')
        self.canvas.theme = TIME_OF_DAY_MODES.get(self.time_of_day, 'night')


        self.map_mode_value = 'offline'

        initial_map = find_region(*coordinates[:2]) or DEFAULT_MAP
        if initial_map.exists():
            try:
                self.canvas.load_offline(initial_map)
            except Exception as exc:
                self.offline_error = f'Ошибка офлайн-карты: {exc}'

        self.source = None
        self.yandex = None
        self.map_stack = QStackedWidget()
        self.map_stack.addWidget(self.canvas)

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)

        self.animation_timer = QTimer(self)
        self.animation_timer.setInterval(33)
        self.animation_timer.setTimerType(Qt.PreciseTimer)
        self.animation_timer.timeout.connect(self.animate_radar)
        self.last_animation = time.monotonic()
        self.last_status = 0.0


        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 10, 16, 12)
        layout.setSpacing(10)

        title = QLabel('КОНТУР  /  наблюдение')
        title.setObjectName('title')

        self.utc_label = QLabel()
        self.utc_label.setObjectName('utcTime')
        self.azimuth_label = QLabel()
        self.azimuth_label.setObjectName('azimuthLabel')
        self.local_label = QLabel()
        self.local_label.setObjectName('localTime')
        self.status = QLabel()


        self.gnss_status_label = QPushButton('GPS: —')
        self.gnss_status_label.setFlat(True)
        self.gnss_status_label.clicked.connect(self.toggle_gps_coordinates)
        self.gnss_status_label.setObjectName('gnssStatus')
        self.gnss_status_label.setStyleSheet('color: #91a7b7; font-size: 11px; padding: 0 8px;')

        header = QHBoxLayout()
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.utc_label)
        header.addWidget(self.azimuth_label)
        header.addWidget(self.local_label)
        header.addWidget(self.gnss_status_label)
        header.addSpacing(20)

        coord_label = QLabel('СК:')
        coord_label.setStyleSheet('color: #91a7b7; font-size: 11px;')
        header.addWidget(coord_label)
        self.coord_system = QComboBox()
        self.coord_system.addItem('WGS84', 'wgs84')
        self.coord_system.addItem('СК-42', 'sk42')
        idx = self.coord_system.findData(self.coord_system_mode)
        if idx != -1:
            self.coord_system.setCurrentIndex(idx)
        self.coord_system.setStyleSheet('''
            QComboBox { background: #10202c; color: #e3edf4; border: 1px solid #395061;
                        padding: 2px 6px; border-radius: 3px; font-size: 11px; }
            QComboBox:hover { background: #172b39; }
        ''')
        self.coord_system.currentIndexChanged.connect(self.change_coordinate_system)
        header.addWidget(self.coord_system)

        header.addSpacing(20)
        header.addWidget(self.status)
        layout.addLayout(header)

        content = QHBoxLayout()
        content.setSpacing(12)
        layout.addLayout(content, 1)

        self.tabs = QTabWidget()
        self.tabs.setFixedWidth(270)

        def page(name):
            widget = QWidget()
            column = QVBoxLayout(widget)
            column.setContentsMargins(10, 10, 10, 10)
            column.setSpacing(14)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(widget)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setFrameShape(QScrollArea.NoFrame)
            self.tabs.addTab(scroll, name)
            return column, scroll

        debug_page, self.debug_scroll = page('Отладка')
        work_page, self.work_scroll = page('Работа')


        synth_box = QGroupBox('Управление синтезатором')
        synth_layout = QVBoxLayout(synth_box)
        synth_layout.setSpacing(8)

        synth_row1 = QHBoxLayout()
        synth_row1.addWidget(QLabel('Частота, МГц:'))
        self.synth_combo = QComboBox()
        for freq in range(8550, 8651, 10):
            self.synth_combo.addItem(str(freq), freq)
        idx = self.synth_combo.findData(self.synthesizer_freq)
        self.synth_combo.setCurrentIndex(idx if idx != -1 else 0)
        self.synth_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        synth_row1.addWidget(self.synth_combo)
        synth_layout.addLayout(synth_row1)

        synth_current = QHBoxLayout()
        synth_current.addWidget(QLabel('Записано:'))
        self.synth_current_label = QLabel(f'{self.synthesizer_freq} МГц')
        self.synth_current_label.setStyleSheet('color: #ffca72; font-weight: 600;')
        synth_current.addWidget(self.synth_current_label)
        synth_current.addStretch()
        synth_layout.addLayout(synth_current)

        synth_buttons = QHBoxLayout()
        self.btn_synth_write = QPushButton('Записать')
        self.btn_synth_write.setStyleSheet('QPushButton { background: #253e50; border: 1px solid #39566a; border-radius: 4px; padding: 8px; } QPushButton:hover { background: #325269; }')
        self.btn_synth_write.clicked.connect(self.write_synthesizer)
        synth_buttons.addWidget(self.btn_synth_write)

        self.btn_synth_check = QPushButton('Проверить')
        self.btn_synth_check.setStyleSheet('QPushButton { background: #253e50; border: 1px solid #39566a; border-radius: 4px; padding: 8px; } QPushButton:hover { background: #325269; }')
        self.btn_synth_check.clicked.connect(self.check_synthesizer)
        synth_buttons.addWidget(self.btn_synth_check)
        synth_layout.addLayout(synth_buttons)
        debug_page.addWidget(synth_box)

        box = QGroupBox('Координаты РЛС')
        form = QFormLayout(box)
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.lat_spin = self.spin(-85, 85, coordinates[0], 6)
        self.lon_spin = self.spin(-180, 180, coordinates[1], 6)
        self.alt_spin = self.spin(-500, 10000, coordinates[2], 1)
        for text, widget in [('Широта, °', self.lat_spin), ('Долгота, °', self.lon_spin), ('Высота, м', self.alt_spin)]:
            form.addRow(text, widget)
        place = QPushButton('Установить антенну')
        place.clicked.connect(self.move_antenna)
        form.addRow(place)
        debug_page.addWidget(box)

        mog_box = QGroupBox('Положение МОГ (наземные цели)')
        mog_layout = QVBoxLayout(mog_box)
        mog_layout.setSpacing(8)
        self.mog_sections = {}
        for mog_name in MOG_IDS:
            section = CollapsibleSection(mog_name, mog_box)
            coords = self.mog_positions.get(mog_name, [60.0, 30.0])
            lat_spin = self.spin(-85, 85, coords[0], 6)
            lon_spin = self.spin(-180, 180, coords[1], 6)
            self.mog_sections[mog_name] = {'section': section, 'lat': lat_spin, 'lon': lon_spin}
            section.content_layout.addWidget(QLabel('Широта, °'))
            section.content_layout.addWidget(lat_spin)
            section.content_layout.addWidget(QLabel('Долгота, °'))
            section.content_layout.addWidget(lon_spin)
            mog_layout.addWidget(section)
        apply_mog = QPushButton('Применить координаты МОГ')
        self.apply_mog_button = apply_mog
        apply_mog.clicked.connect(self.apply_mog_positions)
        mog_layout.addWidget(apply_mog)
        debug_page.addWidget(mog_box)

        debug_page.addWidget(QLabel('Оборудование: не подключено'))
        debug_page.addStretch()


        mode_box = QGroupBox('Режим работы радара')
        mode_form = QFormLayout(mode_box)
        mode_form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.radar_mode_buttons = {}
        for mode_name in RADAR_MODES.keys():
            btn = QPushButton(mode_name)
            btn.setCheckable(True)
            btn.setChecked(mode_name == self.radar_mode)
            btn.setStyleSheet('QPushButton { background: #253e50; border: 1px solid #39566a; border-radius: 4px; padding: 8px; text-align: left; } QPushButton:hover { background: #325269; } QPushButton:checked { background: #3a7a52; color: #a8e6b8; border: 1px solid #4a9a62; font-weight: 600; }')
            btn.clicked.connect(lambda checked=False, name=mode_name: self.select_radar_mode(name))
            mode_form.addRow('Режим:', btn)
            self.radar_mode_buttons[mode_name] = btn
        apply_mode_btn = QPushButton('Применить')
        apply_mode_btn.setStyleSheet('QPushButton { background: #253e50; border: 1px solid #39566a; border-radius: 4px; padding: 8px; } QPushButton:hover { background: #325269; }')
        apply_mode_btn.clicked.connect(self.apply_radar_mode)
        mode_form.addRow(apply_mode_btn)
        work_page.addWidget(mode_box)

        map_box = QGroupBox('Карта')
        map_layout = QVBoxLayout(map_box)
        self.map_mode = NoWheelComboBox()
        self.map_mode.addItem('Офлайн карта', 'offline')
        self.map_mode.addItem('Яндекс · онлайн', 'yandex')
        idx = self.map_mode.findData(self.map_mode_value)
        if idx != -1:
            self.map_mode.setCurrentIndex(idx)
        self.map_mode.currentIndexChanged.connect(self.change_map)
        map_layout.addWidget(self.map_mode)
        self.map_label = QLabel('Офлайн карта активна')
        self.map_label.setObjectName('muted')
        self.map_label.setWordWrap(True)
        map_layout.addWidget(self.map_label)
        work_page.addWidget(map_box)

        radiation_box = QGroupBox('Излучение')
        rad_layout = QVBoxLayout(radiation_box)
        rad_layout.setSpacing(8)
        rad_buttons = QHBoxLayout()
        self.btn_rad_on = QPushButton('ВКЛ')
        self.btn_rad_on.setStyleSheet('QPushButton { background: #2d5a3d; color: #a8e6b8; border: 1px solid #3a7a52; border-radius: 4px; padding: 8px; font-weight: 600; } QPushButton:hover { background: #3a7a52; }')
        self.btn_rad_on.clicked.connect(self.set_radiation_on)
        rad_buttons.addWidget(self.btn_rad_on)
        self.btn_rad_off = QPushButton('ВЫКЛ')
        self.btn_rad_off.setStyleSheet('QPushButton { background: #5a2d2d; color: #e6a8a8; border: 1px solid #7a3a3a; border-radius: 4px; padding: 8px; font-weight: 600; } QPushButton:hover { background: #7a3a3a; }')
        self.btn_rad_off.clicked.connect(self.set_radiation_off)
        rad_buttons.addWidget(self.btn_rad_off)
        rad_layout.addLayout(rad_buttons)
        self.radiation_status = QLabel('Статус: Выключен')
        self.radiation_status.setObjectName('muted')
        self.radiation_status.setAlignment(Qt.AlignCenter)
        self.radiation_status.setStyleSheet('padding: 4px;')
        rad_layout.addWidget(self.radiation_status)
        work_page.addWidget(radiation_box)

        self.esp_button = QPushButton(' Настройка ЭСП')
        self.esp_button.setStyleSheet('QPushButton { background: #3a5a7a; color: #edf6fb; border: 1px solid #4a7aaa; border-radius: 4px; padding: 10px; font-weight: 600; font-size: 13px; } QPushButton:hover { background: #4a7aaa; }')
        self.esp_dialog = None
        self.esp_button.clicked.connect(self.open_esp_control)
        work_page.addWidget(self.esp_button)

        time_box = QGroupBox('Время суток')
        time_layout = QHBoxLayout(time_box)
        time_layout.setSpacing(8)
        self.time_buttons = {}
        for mode_name in TIME_OF_DAY_MODES.keys():
            btn = QPushButton(mode_name)
            btn.setCheckable(True)
            btn.setChecked(mode_name == self.time_of_day)
            btn.setStyleSheet('QPushButton { background: #253e50; border: 1px solid #39566a; border-radius: 4px; padding: 8px; } QPushButton:hover { background: #325269; } QPushButton:checked { background: #3a7a52; color: #a8e6b8; border: 1px solid #4a9a62; font-weight: 600; }')
            btn.clicked.connect(lambda checked=False, name=mode_name: self.set_time_of_day(name))
            time_layout.addWidget(btn)
            self.time_buttons[mode_name] = btn
        work_page.addWidget(time_box)

        self.selected_title = QLabel('Цель не выбрана')
        self.selected_title.setObjectName('sectionTitle')
        work_page.addWidget(self.selected_title)

        work_page.addWidget(QLabel('След за целью'))
        self.tail = NoWheelComboBox()
        for text, mode in [('Последние 5 отметок', 'short'), ('Вся трасса', 'full'), ('Без следа', 'none')]:
            self.tail.addItem(text, mode)
        self.tail.currentIndexChanged.connect(self.change_tail)
        work_page.addWidget(self.tail)

        self.parameters_check = QCheckBox('Параметры всех целей')
        self.parameters_check.setToolTip('Скорость, высота и дальность. У выбранной цели параметры видны всегда.')
        self.parameters_check.toggled.connect(self.change_labels)
        work_page.addWidget(self.parameters_check)

        center = QPushButton('К антенне')
        center.clicked.connect(self.focus_antenna)
        work_page.addWidget(center)

        self.gps_center_button = QPushButton('К GPS')
        self.gps_center_button.setEnabled(False)
        self.gps_center_button.clicked.connect(self.focus_gps)
        work_page.addWidget(self.gps_center_button)

        zooms = QHBoxLayout()
        for text, delta in [('−', -1), ('+', 1)]:
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, d=delta: self.zoom_by(d))
            zooms.addWidget(button)
        work_page.addLayout(zooms)

        self.table_button = QPushButton('Таблица целей')
        self.table_button.clicked.connect(self.show_table)
        work_page.addWidget(self.table_button)
        work_page.addStretch()

        self.mog_panel = MogPanel(self.canvas, self.mog_positions, preferences)
        self.mog_panel.changed.connect(self.refresh_map)
        self.mog_panel.changed.connect(self.save_preferences)
        content.addWidget(self.mog_panel)
        content.addWidget(self.map_stack, 1)
        content.addWidget(self.tabs, 0, Qt.AlignTop)


        self.table_window = QDialog(self, Qt.Window | Qt.WindowTitleHint | Qt.WindowSystemMenuHint | Qt.WindowCloseButtonHint)
        self.table_window.setWindowTitle('Цели')
        self.table_window.setModal(False)
        bl = QVBoxLayout(self.table_window)
        bl.setContentsMargins(8, 8, 8, 8)
        bl.setSpacing(6)

        table_header = QHBoxLayout()
        self.table_count = QLabel()
        table_header.addWidget(self.table_count)
        table_header.addStretch()
        export = QPushButton('Экспорт CSV')
        export.clicked.connect(self.export)
        table_header.addWidget(export)
        bl.addLayout(table_header)

        self.table = QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(['Цель', 'Тип', 'Класс', 'Скорость\nм/с', 'Высота\nм',
            'Дальность\nкм', 'Курс\n°', 'Широта\n°', 'Долгота\n°', 'Время\nUTC'])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setStyleSheet('QHeaderView::section {padding:5px;}')
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.cellClicked.connect(self.table_clicked)
        bl.addWidget(self.table)

        self._table_combos = {}
        self._table_last_ids = set()
        self.table_ids = []

        self.tabs.currentChanged.connect(self.fit_sidebar)
        QTimer.singleShot(0, self.fit_sidebar)
        self.canvas.selected.connect(self.select)
        self.tick()
        self.refresh_selection()
        self.change_tail()
        self.timer.start()
        self.animation_timer.start()
        self.apply_styles()

    @staticmethod
    def spin(lo, hi, value, decimals):
        widget = NoWheelSpinBox()
        widget.setRange(lo, hi)
        widget.setDecimals(decimals)
        widget.setValue(value)
        widget.setKeyboardTracking(False)
        return widget

    def apply_styles(self):
        self.setStyleSheet('''
            QWidget {background:#15232f; color:#e3edf4; font-size:12px;}
            QMainWindow {background:#15232f;}
            QLabel#title {font-size:20px; font-weight:600; color:#ffffff; padding:4px 0;}
            QLabel#sectionTitle {font-size:14px; font-weight:600; color:#edf6fb;}
            QLabel#muted {color:#91a7b7; font-size:11px;}
            QLabel#utcTime {font-size:16px; font-weight:700; color:#ffca72; padding:0 12px;}
            QLabel#azimuthLabel {font-size:16px; font-weight:700; color:#59dbdf; padding:0 12px;}
            QLabel#localTime {font-size:13px; color:#91a7b7; padding:0 12px;}
            QLabel#gnssStatus {color: #91a7b7; font-size: 11px; padding: 0 8px;}
            QTabWidget::pane {border:1px solid #304553; border-radius:4px;}
            QTabBar::tab {background:#1b2e3d; color:#91a7b7; padding:9px 13px;}
            QTabBar::tab:selected {background:#294758; color:#edf6fb;}
            QGroupBox {border:1px solid #344857; border-radius:6px; margin-top:15px; padding-top:15px;}
            QGroupBox::title {subcontrol-origin:margin; left:10px; color:#90adbe;}
            QPushButton {background:#253e50; border:1px solid #39566a; border-radius:4px; padding:8px;}
            QPushButton:hover {background:#325269;}
            QComboBox,QDoubleSpinBox {background:#10202c; border:1px solid #395061; padding:6px; border-radius:3px;}
            QTableWidget {background:#10202c; alternate-background-color:#172b39; gridline-color:#263d4c; border:1px solid #344857;}
            QHeaderView::section {background:#243948; border:0; padding:8px;}
            QTableWidget::item:selected {background:#3a5362; color:#ffca72;}
            QScrollBar:vertical {background:#10202c; width:10px; margin:0; border:0;}
            QScrollBar::handle:vertical {background:#496477; min-height:32px; border-radius:4px;}
            QScrollBar::handle:vertical:hover {background:#678b9f;}
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {height:0;}
            QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {background:none;}
        ''')

    def fit_sidebar(self):
        page = self.tabs.currentWidget().widget()
        page.layout().activate()
        desired = page.sizeHint().height() + self.tabs.tabBar().sizeHint().height() + 4
        available = max(200, self.centralWidget().height() - 70)
        self.tabs.setFixedHeight(min(desired, available))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'tabs'):
            self.fit_sidebar()
        if hasattr(self, 'canvas') and self.canvas.offline:
            try:
                self.canvas.fit_radar()
            except RuntimeError:
                pass

    def open_esp_control(self):
        if self.esp_dialog is None:
            self.esp_dialog = ESPControlDialog(self)
        self.esp_dialog.show()
        self.esp_dialog.raise_()
        self.esp_dialog.activateWindow()

    def set_esp_speed(self, degrees_per_second):
        self.esp_speed = max(0, min(120, degrees_per_second))
        self.statusBar().showMessage(f'Скорость ЭСП: {self.esp_speed}°/с', 2000)

    def select_radar_mode(self, mode_name):
        for name, btn in self.radar_mode_buttons.items():
            btn.setChecked(name == mode_name)
        self.radar_mode = mode_name

    def apply_radar_mode(self):
        if self.esp_dialog and self.esp_dialog.controller.active:
            if self.esp_dialog.controller.motion.mode == 1 and not self.esp_dialog.stop_active():
                return
        self.radar_range = RADAR_MODES[self.radar_mode]
        self.canvas.radius_m = self.radar_range
        self.canvas.ring_step_m = self.radar_range // 7 if self.radar_range >= 70000 else self.radar_range // 5
        self.canvas.auto_fit = True
        self.canvas.focus_antenna()
        self.save_preferences()
        self.refresh_map()
        if self.esp_dialog:
            self.esp_dialog.update_radar_mode()
        self.statusBar().showMessage(f'Режим радара: {self.radar_mode} ({self.radar_range // 1000} км)', 3000)

    def set_radiation_on(self):
        self.radiation_status.setText('Статус: Включен')
        self.radiation_status.setStyleSheet('color: #a8e6b8; font-weight: 600; padding: 4px;')

    def set_radiation_off(self):
        self.radiation_status.setText('Статус: Выключен')
        self.radiation_status.setStyleSheet('color: #91a7b7; font-weight: 400; padding: 4px;')

    def set_time_of_day(self, mode_name):
        if mode_name not in TIME_OF_DAY_MODES:
            return
        self.time_of_day = mode_name
        mode_id = TIME_OF_DAY_MODES[mode_name]
        self.canvas.theme = mode_id
        if self.canvas.offline:
            self.canvas.offline.theme = mode_id
        for name, btn in self.time_buttons.items():
            btn.setChecked(name == mode_name)
        self.canvas.update()
        self.save_preferences()
        self.refresh_map()
        self.statusBar().showMessage(f'Время суток: {mode_name}', 3000)

    def write_synthesizer(self):
        freq = self.synth_combo.currentData()
        self.synthesizer_freq = freq
        self.synth_current_label.setText(f'{freq} МГц')
        self.save_preferences()
        self.statusBar().showMessage(f'Синтезатор: записано {freq} МГц', 3000)

    def check_synthesizer(self):
        QMessageBox.information(self, 'Проверка синтезатора', f'Текущая частота синтезатора:\n{self.synthesizer_freq} МГц')

    def apply_mog_positions(self):
        for mog_name, data in self.mog_sections.items():
            self.mog_positions[mog_name] = [data['lat'].value(), data['lon'].value()]
        self.mog_panel.apply_positions(self.mog_positions)
        self.tick()
        self.save_preferences()

    def change_classification(self, track_id, text):
        class_map = {'Свой': 'friend', 'Чужой': 'foe', 'Неопр.': 'unknown'}
        mark = class_map.get(text, 'unknown')
        color = MARK_TO_COLOR.get(mark, 'blue')
        self.canvas.target_marks[track_id] = color
        self.save_preferences()
        self.refresh_map()

    def change_coordinate_system(self):
        self.coord_system_mode = self.coord_system.currentData()
        self.canvas.display_crs = WGS84 if self.coord_system_mode == 'wgs84' else SK42
        self.save_preferences()
        self.refresh_table()
        self.refresh_status()
        self.statusBar().showMessage(f'Система координат: {self.coord_system.currentText()}', 3000)

    def change_map(self):
        mode = self.map_mode.currentData()
        self.save_preferences()

        if mode == 'offline':
            if self.yandex:
                self.yandex.deactivate()
                self.map_stack.setCurrentWidget(self.canvas)
            self.statusBar().showMessage('Офлайн карта', 3000)
        else:
            if not self.yandex:
                key = api_key()
                if key:
                    try:
                        self.yandex = create_view(self.map_stack, key)
                        self.yandex.bridge.selected.connect(self.select)
                        self.yandex.bridge.gps_clicked.connect(self.toggle_gps_coordinates)
                        self.yandex.bridge.failed.connect(self._on_yandex_failed)
                        self.yandex.bridge.loaded.connect(self._on_yandex_loaded)
                        self.yandex.bridge.region_activated.connect(self.mog_panel.activate)
                        self.map_stack.addWidget(self.yandex)
                    except ImportError as exc:
                        QMessageBox.warning(self, 'Ошибка', f'Qt WebEngine не установлен:\n{exc}')
                        self.map_mode.blockSignals(True)
                        self.map_mode.setCurrentIndex(0)
                        self.map_mode.blockSignals(False)
                        return
                else:
                    QMessageBox.warning(self, 'Ошибка', 'Не задан ключ Яндекс API')
                    self.map_mode.blockSignals(True)
                    self.map_mode.setCurrentIndex(0)
                    self.map_mode.blockSignals(False)
                    return

            self.map_stack.setCurrentWidget(self.yandex)
            self.yandex.activate()
            self.statusBar().showMessage('Яндекс карта', 3000)
        self.mog_panel.set_online(self.map_stack.currentWidget() is self.yandex)
        self.refresh_map()

    def _on_yandex_loaded(self):
        self.statusBar().showMessage('Яндекс карта загружена ✓', 3000)

    def _on_yandex_failed(self, message):
        self.statusBar().showMessage(f'Ошибка Яндекса: {message[:80]}', 10000)
##
    def tick(self):
        now = time.monotonic()
        if self.running: 
            self.elapsed += now - self.previous_tick
        
        # УБИРАЕМ ВСЕ ЦЕЛИ: список остается строго пустым
        current_samples = []
        
        self.tracks.synchronize(current_samples)
        self.previous_tick = now
        self.canvas.paused = not self.running

        # Очищаем метки, если вдруг что-то осталось в памяти
        inactive_marks = set(self.canvas.target_marks.keys()) - set(self.tracks.active_ids)
        for tid in inactive_marks:
            del self.canvas.target_marks[tid]

        if self.canvas.selected_id not in self.tracks.active_ids: 
            self.canvas.selected_id = None
            
        self.refresh_status()
        self.refresh_table()
        self.refresh_selection()
        self.refresh_map()

    @staticmethod
    def number(value, digits=1):
        return '—' if value is None or not math.isfinite(value) else f'{value:.{digits}f}'

    def _convert_coords(self, lat, lon, alt=0.0):
        if self.coord_system_mode == 'sk42':
            lat_new, lon_new = convert(lat, lon, WGS84, SK42)
            return lat_new, lon_new, alt
        return lat, lon, alt

    def refresh_table(self):
        try:
            self.table_ids = sorted(self.tracks.active_ids, key=lambda x: str(x))
            current_ids = set(self.table_ids)
            if self.table_window.isVisible() and current_ids == self._table_last_ids:
                self._update_table_data_only()
                return
            self._rebuild_table()
            self._table_last_ids = current_ids
        except (RuntimeError, AttributeError) as e:
            print(f"refresh_table error: {e}")

    def _update_table_data_only(self):
        scroll = self.table.verticalScrollBar().value()
        for row, track_id in enumerate(self.table_ids):
            if track_id not in self.tracks.history:
                continue
            history = self.tracks.history[track_id]
            if not history:
                continue
            s = history[-1]
            lat_display, lon_display, _ = self._convert_coords(s.lat, s.lon)
            is_out_of_range = (track_id not in MOG_IDS and self.canvas.range_m(s.lat, s.lon) > self.canvas.radius_m)

            values = {
                0: str(track_id), 1: str(display_type(s)), 3: str(self.number(s.speed_mps)),
                4: str(self.number(s.altitude_m, 0)), 5: str(self.number(s.ground_range_m / 1000 if s.ground_range_m is not None else None, 2)),
                6: str(self.number(s.course_deg)), 7: str(self.number(lat_display, 6)), 8: str(self.number(lon_display, 6)),
                9: datetime.fromtimestamp(s.timestamp, timezone.utc).strftime('%H:%M:%S'),
            }

            for column, value in values.items():
                item = self.table.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.table.setItem(row, column, item)
                if item.text() != str(value):
                    item.setText(str(value))

            if track_id in self._table_combos:
                combo = self._table_combos[track_id]
                color = self.canvas.target_marks.get(track_id, 'blue')
                mark = COLOR_TO_MARK.get(color, 'unknown')
                class_text = MARK_NAMES_OLD[mark]
                if is_out_of_range:
                    class_text += ' ⚠'
                if combo.currentText() != class_text:
                    combo.blockSignals(True)
                    combo.setCurrentText(class_text)
                    combo.blockSignals(False)

        self.table.verticalScrollBar().setValue(scroll)
        self.table_count.setText(f'Цели · {len(self.table_ids)}')

    def _rebuild_table(self):
        scroll = self.table.verticalScrollBar().value()
        self.table.clearContents()
        self.table.setRowCount(0)
        self._table_combos.clear()

        self.table.setRowCount(len(self.table_ids))
        for row, track_id in enumerate(self.table_ids):
            if track_id not in self.tracks.history:
                continue
            history = self.tracks.history[track_id]
            if not history:
                continue
            s = history[-1]
            color = self.canvas.target_marks.get(track_id, 'blue')
            mark = COLOR_TO_MARK.get(color, 'unknown')
            class_text = MARK_NAMES_OLD[mark]
            is_out_of_range = (track_id not in MOG_IDS and self.canvas.range_m(s.lat, s.lon) > self.canvas.radius_m)

            if is_out_of_range:
                class_text += ' ⚠'

            lat_display, lon_display, _ = self._convert_coords(s.lat, s.lon)
            values = [str(track_id), str(display_type(s)), class_text, str(self.number(s.speed_mps)),
                      str(self.number(s.altitude_m, 0)), str(self.number(s.ground_range_m / 1000 if s.ground_range_m is not None else None, 2)),
                      str(self.number(s.course_deg)), str(self.number(lat_display, 6)), str(self.number(lon_display, 6)),
                      datetime.fromtimestamp(s.timestamp, timezone.utc).strftime('%H:%M:%S')]

            for column, value in enumerate(values):
                if column == 2:
                    continue
                item = QTableWidgetItem()
                self.table.setItem(row, column, item)
                item.setText(str(value))
                item.setData(Qt.UserRole, track_id)
                item.setTextAlignment(Qt.AlignVCenter | (Qt.AlignLeft if column < 2 else Qt.AlignRight))

            combo = QComboBox()
            combo.addItem('Неопр.', 'unknown')
            combo.addItem('Свой', 'friend')
            combo.addItem('Чужой', 'foe')
            combo.setCurrentText(class_text)
            combo.setStyleSheet('QComboBox { background: #10202c; color: #e3edf4; border: 1px solid #395061; padding: 2px; }')
            combo.currentTextChanged.connect(lambda text, tid=track_id: self.change_classification(tid, text))
            self.table.setCellWidget(row, 2, combo)
            self._table_combos[track_id] = combo

        self.table.verticalScrollBar().setValue(scroll)
        self.table_count.setText(f'Цели · {len(self.table_ids)}')
        self.fit_table()

    def fit_table(self):
        try:
            self.table_window.ensurePolished()
            self.table.resizeColumnsToContents()
            layout = self.table_window.layout()
            if not layout:
                return
            margins = layout.contentsMargins()
            header_item = layout.itemAt(0)
            if not header_item:
                return
            header_height = header_item.sizeHint().height()
            width = self.table.horizontalHeader().length() + 2 * self.table.frameWidth()
            height = (self.table.horizontalHeader().sizeHint().height() + max(1, self.table.rowCount()) * self.table.verticalHeader().defaultSectionSize() + 2 * self.table.frameWidth())
            screen = self.table_window.screen()
            if not screen:
                return
            screen_geo = screen.availableGeometry()
            max_height = screen_geo.height() - 60 - margins.top() - margins.bottom() - header_height - layout.spacing()
            if height > max_height:
                width += self.table.verticalScrollBar().sizeHint().width()
                height = max_height
            max_width = screen_geo.width() - 40 - margins.left() - margins.right()
            if width > max_width:
                height = min(max_height, height + self.table.horizontalScrollBar().sizeHint().height())
                width = max_width
            self.table_window.setFixedSize(width + margins.left() + margins.right(), height + margins.top() + margins.bottom() + header_height + layout.spacing())
        except (RuntimeError, AttributeError) as e:
            print(f"fit_table error: {e}")

    def table_clicked(self, row, column):
        try:
            item = self.table.item(row, 0)
            if item:
                self.select(item.data(Qt.UserRole))
        except (RuntimeError, AttributeError) as e:
            print(f"table_clicked error: {e}")

    def refresh_map(self):
        self.canvas.update()
        if hasattr(self, 'mog_panel'):
            self.mog_panel.refresh()
        if self.yandex:
            self.yandex.send(snapshot(self.canvas))

    def change_labels(self):
        self.canvas.label_fields = set(FIELDS) if self.parameters_check.isChecked() else set()
        self.refresh_map()

    def refresh_status(self):
        utc_dt, from_gnss = self.gnss.get_utc_time()
        local_dt = utc_dt.astimezone()

        self.utc_label.setText(f'UTC {utc_dt.strftime("%H:%M:%S")}')
        self.utc_label.setToolTip('Источник времени: GNSS' if from_gnss else 'Источник времени: системные часы')
        self.azimuth_label.setText(f'АЗИМУТ {self.canvas.azimuth:05.1f}°')
        self.local_label.setText(f'МСК {local_dt.strftime("%H:%M:%S")}')

        status = self.gnss.get_status()
        self.canvas.gps_status = status
        text = {'fresh': f"GPS: {status['satellites']} спутн.", 'stale': 'GPS: устарело',
                'searching': 'GPS: поиск', 'disconnected': 'GPS: нет связи'}[status['state']]
        color = '#ffca72' if status['warning'] or status['state'] == 'searching' else '#69b8ff' if status['has_fix'] else '#91a7b7'
        self.gnss_status_label.setText(text)
        self.gnss_status_label.setStyleSheet(f'color: {color}; font-size: 11px; padding: 2px 8px; border: 0;')
        position = status.get('position')
        self.gps_center_button.setEnabled(bool(position and abs(position['latitude']) <= 85.05112878))
        if status['has_fix'] and not self._gps_first_focus:
            self._gps_first_focus = True
            if self.canvas.auto_fit:
                self.focus_gps()

        lat_wgs84 = self.canvas.antenna[0]
        lon_wgs84 = self.canvas.antenna[1]
        lat_display, lon_display, _ = self._convert_coords(lat_wgs84, lon_wgs84)
        system_label = 'СК-42' if self.coord_system_mode == 'sk42' else 'WGS84'

        self.status.setText(
            f'АНТ ({system_label})  {lat_display:.6f}°  {lon_display:.6f}°   ·   '
            f'{"ОБЗОР" if self.running else "ПАУЗА"}'
        )

    def animate_radar(self):
        now = time.monotonic()
        dt, self.last_animation = now - self.last_animation, now

        if self.running and not self.esp_model_controlled:
            speed = getattr(self, 'esp_speed', RADAR_RATE)
            self.canvas.azimuth = (self.canvas.azimuth + dt * speed) % 360

        if self.map_stack.currentWidget() is self.canvas:
            self.canvas.update()

        if now - self.last_status >= .1:
            self.refresh_status()
            self.last_status = now

    def show_table(self):
        try:
            self._rebuild_table()
            self._table_last_ids = set(self.table_ids)
            if self.table_window:
                self.table_window.show()
                self.table_window.raise_()
                self.table_window.activateWindow()
        except (RuntimeError, AttributeError) as e:
            print(f"show_table error: {e}")

    def save_preferences(self):
        try:
            self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.preferences_path.with_suffix('.partial')
            temp.write_text(json.dumps({
                'map_mode': self.map_mode.currentData() if hasattr(self, 'map_mode') else 'offline',
                'antenna': [*self.canvas.antenna, self.antenna_altitude],
                'mog_positions': self.mog_positions,
                'mog_previews_applied': self.mog_panel.applied,
                'mog_previews_visible': self.mog_panel.toggle.isChecked(),
                'synthesizer_freq': self.synthesizer_freq,
                'radar_mode': self.radar_mode,
                'coord_system': self.coord_system_mode,
                'time_of_day': self.time_of_day,
                'display_crs': self.canvas.display_crs,
                'theme': self.canvas.theme,
                'pinned_regions': self.canvas.pinned_regions,
            }, ensure_ascii=False), encoding='utf-8')
            temp.replace(self.preferences_path)
        except OSError:
            self.statusBar().showMessage('Не удалось сохранить настройки', 8000)

    def focus_antenna(self):
        self.canvas.focus_antenna()
        if self.yandex and self.map_stack.currentWidget() is self.yandex:
            self.yandex.focus_antenna()

    def focus_gps(self):
        status = self.gnss.get_status()
        position = status.get('position')
        if not position or abs(position['latitude']) > 85.05112878:
            return
        self.canvas.gps_status = status
        if self.yandex and self.map_stack.currentWidget() is self.yandex:
            self.yandex.send(snapshot(self.canvas))
            self.yandex.focus_gps()
        else:
            self.canvas.lat, self.canvas.lon = position['latitude'], position['longitude']
            self.canvas.auto_fit = False
            self.canvas.update()

    def toggle_gps_coordinates(self):
        if self.gnss.get_status().get('position'):
            self.canvas.gps_coordinates_visible = not self.canvas.gps_coordinates_visible
            self.refresh_map()

    def zoom_by(self, delta):
        if self.yandex and self.map_stack.currentWidget() is self.yandex:
            self.yandex.zoom_by(delta)
        else:
            self.canvas.set_zoom(self.canvas.zoom + delta)

    def refresh_selection(self):
        track_id = self.canvas.selected_id
        self.selected_title.setText(f'Выбрана {track_id}' if track_id else 'Цель не выбрана')

        if track_id in self.table_ids:
            scroll = self.table.verticalScrollBar().value()
            self.table.selectRow(self.table_ids.index(track_id))
            self.table.verticalScrollBar().setValue(scroll)
        else:
            self.table.clearSelection()
            self.table.setCurrentItem(None)

    def select(self, track_id):
        if track_id not in self.tracks.active_ids:
            return
        self.canvas.selected_id = None if self.canvas.selected_id == track_id else track_id
        self.refresh_selection()
        self.refresh_map()

    def change_tail(self):
        self.canvas.tail_mode = self.tail.currentData()
        self.refresh_map()

    def move_antenna(self):
        if self.tracks.history and QMessageBox.question(self, 'Новый сеанс', 'Перемещение антенны начнёт новый сеанс. Продолжить?') != QMessageBox.Yes:
            return

        self.source = TrafficSource(self.lat_spin.value(), self.lon_spin.value())
        self.canvas.antenna = self.source.lat, self.source.lon
        self.antenna_altitude = self.alt_spin.value()
        self.canvas.target_marks.clear()
        self.save_preferences()
        self.canvas.focus_antenna()

        self.tracks.history.clear()
        self.tracks.active_ids.clear()
        self.canvas.selected_id = None
        self.elapsed = 0
        self.canvas.azimuth = 0
        self.last_animation = time.monotonic()
        self.previous_tick = time.monotonic()

        self.tracks.synchronize(self.source.samples(0, time.time()))
        self.tick()
        self.focus_antenna()

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, 'История сеанса', 'tracks.csv', 'CSV (*.csv)')
        if not path:
            return

        try:
            with open(path, 'w', newline='', encoding='utf-8-sig') as output:
                writer = csv.writer(output)
                writer.writerow(['source', 'track_id', 'object_type', 'classification', 'timestamp_utc',
                    'lat_wgs84', 'lon_wgs84', 'alt_wgs84', 'lat_sk42', 'lon_sk42', 'alt_sk42',
                    'ground_range_m', 'speed_mps', 'course_deg'])

                for history in self.tracks.history.values():
                    for s in history:
                        color = self.canvas.target_marks.get(s.track_id, 'blue')
                        mark = COLOR_TO_MARK.get(color, 'unknown')
                        lat_sk42, lon_sk42 = convert(s.lat, s.lon, WGS84, SK42)
                        alt_sk42 = s.altitude_m or 0.0
                        writer.writerow(['synthetic', str(s.track_id), s.object_type, mark,
                            datetime.fromtimestamp(s.timestamp, timezone.utc).isoformat(),
                            s.lat, s.lon, s.altitude_m, lat_sk42, lon_sk42, alt_sk42,
                            s.ground_range_m, s.speed_mps, s.course_deg])

            self.statusBar().showMessage(f'История сохранена: {path}', 6000)
        except OSError as exc:
            QMessageBox.warning(self, 'Ошибка экспорта', str(exc))

    def closeEvent(self, event):
        if self.esp_dialog and not self.esp_dialog.stop_all():
            event.ignore()
            return
        if self.tracks.history:
            answer = QMessageBox.question(self, 'Закрыть сеанс?', 'История хранится в памяти. При необходимости экспортируйте CSV перед выходом. Закрыть?', QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                event.ignore()
                return


        self.timer.stop()
        self.animation_timer.stop()


        if hasattr(self.canvas, '_fade_timer'):
            self.canvas._fade_timer.stop()

        self.table_window.close()
        if self.esp_dialog:
            self.esp_dialog.close()

        if self.gnss:
            self.gnss.stop()

        if self.yandex:
            self.yandex.shutdown()
        if self.canvas.offline:
            self.canvas.offline.close()
        self.mog_panel.close_layers()
        event.accept()


def create_application():
    QApplication.setAttribute(Qt.AA_UseDesktopOpenGL, True)
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    if sys.platform.startswith('linux'):
        os.environ.setdefault('QT_QUICK_BACKEND', 'software')
        os.environ.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu')
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    font_family = 'DejaVu Sans'
    if sys.platform == 'win32':
        font_id = QFontDatabase.addApplicationFont('C:/Windows/Fonts/arial.ttf')
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            font_family = families[0]
    app.setFont(QFont(font_family, 10))
    return app


def main():
    app = create_application()
    window = Window()
    window.showMaximized()

    def safe_fit():
        try:
            if window.canvas.offline and window.canvas.isVisible():
                window.canvas.fit_radar()
        except RuntimeError:
            pass

    QTimer.singleShot(500, safe_fit)
    return app.exec_()


if __name__ == '__main__':
    sys.exit(main())
