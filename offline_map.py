import math
import time
import atexit
import threading
from collections import OrderedDict
from queue import SimpleQueue
from pathlib import Path

from PyQt5.QtCore import QObject, QTimer, QThread, QCoreApplication, pyqtSignal
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap, QImage, QPainter, QColor

from model import MBTiles


DEFAULT_MAP = Path(__file__).parent / 'data' / 'spb-north-osm.mbtiles'
DETAIL_ZOOM = 12


NUM_RENDER_WORKERS = 1
_RENDERER = None


def contains(bounds, lat, lon):
    if not bounds:
        return False
    west, south, east, north = bounds
    if west <= east:
        return south <= lat <= north and west <= lon <= east
    return south <= lat <= north and (lon >= west or lon <= east)


def render_payload(payload, key):
    blob, vector, parent_z = payload
    z, x, y, theme = key
    scale = 2 ** (z - parent_z)
    if vector:
        try:
            from vector_map import render_image
            return render_image(blob, z, theme, (z - parent_z, x % scale, y % scale))
        except ImportError:
            return QImage()
    image = QImage.fromData(blob)
    if image.isNull():
        return QImage()
    if scale > 1:
        src_x = (x % scale) * 256 // scale
        src_y = (y % scale) * 256 // scale
        src_w = max(1, 256 // scale)
        src_h = max(1, 256 // scale)
        image = image.copy(src_x, src_y, src_w, src_h).scaled(
            256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
    if theme == 'night' and not image.isNull():
        image.invertPixels()
        painter = QPainter(image)
        painter.fillRect(image.rect(), QColor(18, 32, 48, 100))
        painter.end()
    elif theme == 'dusk' and not image.isNull():
        painter = QPainter(image)
        painter.fillRect(image.rect(), QColor(255, 140, 60, 45))
        painter.end()
    return image


class TileRenderer(QThread):
    def __init__(self):
        super().__init__()
        self.tasks = SimpleQueue()
        self.setTerminationEnabled(False)
        self.workers = []

    def run(self):
        for _ in range(NUM_RENDER_WORKERS):
            worker = threading.Thread(target=self._worker_loop, daemon=True)
            worker.start()
            self.workers.append(worker)
        while not self.isInterruptionRequested():
            self.msleep(100)

    def _worker_loop(self):
        while not self.isInterruptionRequested():
            try:
                task = self.tasks.get(timeout=0.1)
            except Exception:
                continue
            if task is None:
                return
            owner, payload, key = task
            if owner._closed:
                continue
            try:
                image = render_payload(payload, key)
            except Exception:
                image = QImage()
            if not owner._closed:
                try:
                    owner._completed.emit(key, image)
                except RuntimeError:
                    pass

    def stop(self):
        self.requestInterruption()
        for _ in self.workers:
            try:
                self.tasks.put_nowait(None)
            except Exception:
                pass
        self.wait(1500)


def stop_renderer():
    global _RENDERER
    if _RENDERER is not None:
        _RENDERER.stop()
        _RENDERER = None


atexit.register(stop_renderer)


class OfflineMap(QObject):
    ready = pyqtSignal()
    _completed = pyqtSignal(object, object)

    def __init__(self, path=DEFAULT_MAP, async_render=True):
        super().__init__()
        from vector_map import VectorArchive

        if Path(path).suffix == '.pmtiles':
            self.archive = VectorArchive(path)
        else:
            self.archive = MBTiles(path)

        if getattr(self.archive, 'metadata', {}).get('kontur_download_complete') == 'false':
            self.archive.close()
            raise ValueError('Пакет карты не завершён (скачивание было прервано)')

        overview = Path(__file__).parent / 'data' / 'regions' / 'russia-overview.pmtiles'
        self.fallback = None
        if overview.exists() and Path(path).resolve() != overview.resolve():
            try:
                self.fallback = VectorArchive(overview)
            except Exception:
                pass

        self.bounds = self.fallback.bounds if self.fallback else self.archive.bounds

        self.additional = []
        if overview.exists():
            for candidate in sorted(overview.parent.glob('*.pmtiles')):
                if candidate.resolve() in (Path(path).resolve(), overview.resolve()):
                    continue
                try:
                    archive = VectorArchive(candidate)
                    if archive.max_zoom >= DETAIL_ZOOM:
                        self.additional.append(archive)
                    else:
                        archive.close()
                except (OSError, ValueError, KeyError, IndexError):
                    continue

        all_layers = [a for a in (self.archive, *self.additional, self.fallback) if a]
        self.layers = sorted(all_layers, key=lambda a: a.max_zoom, reverse=True)

        self.cache = OrderedDict()
        self.cache_max = 2048
        self.pending_max = 512

        self._tile_born = {}
        self.FADE_DURATION = 0.35

        self.theme = 'night'
        self.async_render = async_render
        self.revision = 0
        self._closed = False
        self._views = {}
        self._pending = OrderedDict()
        self._active_keys = set()

        self._view_lat = None
        self._view_lon = None
        self._view_zoom = None

        self._completed.connect(self._accept)

        if async_render:
            QCoreApplication.instance().aboutToQuit.connect(self.close)

    def set_viewport(self, lat, lon, zoom):
        self._view_lat = lat
        self._view_lon = lon
        self._view_zoom = zoom

    def begin_view(self, scope):
        self._views[scope] = set()

    def end_view(self, scope):
        needed = set().union(*self._views.values()) if self._views else set()
        for key in list(self._pending):
            if key not in needed:
                self._pending.pop(key, None)

    def _payload(self, key):
        z, x, y, _ = key
        for archive in self.layers:
            parent_z = min(z, archive.max_zoom, DETAIL_ZOOM)
            scale = 2 ** (z - parent_z)
            blob = archive.tile(parent_z, x // scale, y // scale)
            if blob:
                is_vector = getattr(archive, 'metadata', {}).get('format') == 'pbf'
                return blob, is_vector, parent_z
        return None

    def _store(self, key, pixmap):
        self.cache[key] = pixmap
        self.cache.move_to_end(key)
        if len(self.cache) > self.cache_max:
            self.cache.popitem(last=False)
            self._tile_born.pop(key, None)
        self.revision += 1
        if key not in self._tile_born:
            self._tile_born[key] = time.time()

    def tile(self, z, x, y, scope='direct'):
        key = (z, x % 2**z, y, self.theme)

        if self._closed:
            return QPixmap()

        if self.async_render:
            self._views.setdefault(scope, set()).add(key)

        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]

        if not self.async_render:
            payload = self._payload(key)
            if payload:
                image = render_payload(payload, key)
                self._store(key, QPixmap.fromImage(image))
            else:
                self._store(key, QPixmap())
            return self.cache[key]

        if key not in self._pending and key not in self._active_keys:
            self._pending[key] = None
            if len(self._pending) > self.pending_max:
                self._evict_farthest_pending()
            QTimer.singleShot(0, self._pump)

        for parent_z in range(z - 1, max(-1, z - 7), -1):
            scale = 2 ** (z - parent_z)
            parent_key = (parent_z, key[1] // scale, key[2] // scale, self.theme)
            parent = self.cache.get(parent_key)
            if parent is not None and not parent.isNull():
                src_x = (key[1] % scale) * 256 // scale
                src_y = (key[2] % scale) * 256 // scale
                src_w = max(1, 256 // scale)
                src_h = max(1, 256 // scale)
                return parent.copy(src_x, src_y, src_w, src_h).scaled(256, 256)

        return QPixmap()

    def _evict_farthest_pending(self):
        if self._view_lat is None or self._view_zoom is None:
            self._pending.popitem(last=False)
            return
        scored = []
        for key in self._pending:
            z, x, y, _ = key
            score = self._distance_to_viewport(z, x, y)
            scored.append((score, key))
        scored.sort(reverse=True)
        for _, key in scored[self.pending_max:]:
            self._pending.pop(key, None)

    def _distance_to_viewport(self, z, x, y):
        if self._view_lat is None or self._view_zoom is None:
            return 0
        cx, cy = self._latlon_to_tile(self._view_lat, self._view_lon, self._view_zoom)
        scale = 2 ** (z - self._view_zoom)
        dx = abs(x - cx * scale)
        dy = abs(y - cy * scale)
        return dx + dy

    @staticmethod
    def _latlon_to_tile(lat, lon, zoom):
        n = 2.0 ** zoom
        x = (lon + 180.0) / 360.0 * n
        lat_rad = math.radians(lat)
        y = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n
        return x, y

    def _pump(self):
        global _RENDERER
        if self._closed or not self._pending:
            return
        if len(self._active_keys) >= NUM_RENDER_WORKERS:
            return

        best_key = None
        best_score = float('inf')
        for key in self._pending:
            z, x, y, _ = key
            score = self._distance_to_viewport(z, x, y)
            if score < best_score:
                best_score = score
                best_key = key

        if best_key is None:
            return

        self._pending.pop(best_key)
        payload = self._payload(best_key)

        if payload is None:
            self._store(best_key, QPixmap())
            self.ready.emit()
            QTimer.singleShot(0, self._pump)
            return

        self._active_keys.add(best_key)

        if _RENDERER is None:
            _RENDERER = TileRenderer()
            QCoreApplication.instance().aboutToQuit.connect(stop_renderer)
            _RENDERER.start()

        _RENDERER.tasks.put((self, payload, best_key))

    def _accept(self, key, image):

        if self._closed:
            return
        self._active_keys.discard(key)
        self._store(key, QPixmap.fromImage(image))
        self.ready.emit()
        QTimer.singleShot(0, self._pump)

    def has_fading_tiles(self):
        now = time.time()
        for key, born in list(self._tile_born.items()):
            if now - born < self.FADE_DURATION:
                return True
            if key not in self.cache:
                self._tile_born.pop(key, None)
        return False

    def tile_opacity(self, key):
        if key not in self._tile_born:
            return 1.0
        age = time.time() - self._tile_born[key]
        if age >= self.FADE_DURATION:
            return 1.0
        t = age / self.FADE_DURATION
        return 1.0 - (1.0 - t) ** 3

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._pending.clear()
        self._views.clear()
        self._active_keys.clear()
        self._tile_born.clear()
        self.archive.close()
        if self.fallback:
            self.fallback.close()
        for archive in self.additional:
            archive.close()
        self.cache.clear()
