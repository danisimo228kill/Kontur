from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QToolButton

from dashboard import RegionPreview
from offline_map import OfflineMap
from region_maps import find_region

MOG_RADIUS_M = 5000
MOG_NAMES = ('МОГ1', 'МОГ2', 'МОГ3')


class MogPreview(RegionPreview):
    def __init__(self, canvas, index):
        super().__init__(canvas, index)
        self.layer = None
        self.layer_path = None
        self.clear_button.hide()

    def map_layer(self):
        if self.layer:
            self.layer.theme = self.canvas.theme
        return self.layer

    def load_region(self):
        region = self.canvas.pinned_regions[self.index]
        path = find_region(region['lat'], region['lon'], radius=MOG_RADIUS_M) if region else None
        if path != self.layer_path:
            self.close_layer()
            if path:
                try:
                    self.layer = OfflineMap(path)
                    self.layer.ready.connect(self.update)
                    self.layer_path = path
                except (OSError, ValueError, KeyError):
                    self.layer = None
        self.update()

    def close_layer(self):
        if self.layer:
            self.layer.close()
        self.layer = None
        self.layer_path = None


class MogPanel(QWidget):
    changed = pyqtSignal()

    def __init__(self, canvas, positions, preferences):
        super().__init__()
        self.canvas = canvas
        self.canvas.mog_previews = True
        self.applied = preferences.get('mog_previews_applied') is True
        self.online = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.toggle = QToolButton()
        self.toggle.setObjectName('mogPanelToggle')
        self.toggle.setCheckable(True)
        self.toggle.setFixedWidth(44)
        layout.addWidget(self.toggle, 0, Qt.AlignTop)
        self.column = QWidget()
        self.column.setFixedWidth(260)
        column = QVBoxLayout(self.column)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        self.previews = [MogPreview(canvas, i) for i in range(3)]
        for preview in self.previews:
            column.addWidget(preview, 1)
            preview.activated.connect(self.activate)
        layout.addWidget(self.column)
        if self.applied:
            self.set_positions(positions)
        self.toggle.setChecked(preferences.get('mog_previews_visible') is True)
        self.toggle.toggled.connect(self.update_visibility)
        self.update_visibility()

    def set_positions(self, positions):
        self.canvas.pinned_regions = [
            dict(lat=positions[name][0], lon=positions[name][1], name=name, radius_m=MOG_RADIUS_M)
            for name in MOG_NAMES
        ]
        for preview in self.previews:
            preview.load_region()

    def apply_positions(self, positions):
        self.applied = True
        self.set_positions(positions)
        self.toggle.setChecked(True)
        self.update_visibility()

    def set_online(self, online):
        self.online = online
        self.update_visibility()

    def update_visibility(self):
        opened = self.toggle.isChecked()
        self.canvas.show_previews = opened
        self.column.setVisible(opened and not self.online)
        self.toggle.setText('МОГ\n◀' if opened else 'МОГ\n▶')
        self.toggle.setToolTip('Свернуть карты МОГ' if opened else 'Открыть карты МОГ · радиус 5 км')
        self.changed.emit()

    def activate(self, index):
        self.canvas.active_region = index
        self.changed.emit()

    def refresh(self):
        if self.column.isVisible():
            for preview in self.previews:
                preview.update()

    def close_layers(self):
        for preview in self.previews:
            preview.close_layer()
