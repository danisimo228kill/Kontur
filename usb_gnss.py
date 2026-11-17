import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = list_ports = None


def discover_ports():

    found = []
    for item in list_ports.comports() if list_ports else []:
        label = ' '.join(str(getattr(item, k, '') or '') for k in ('description', 'manufacturer', 'product')).strip()
        if item.vid == 0x1546 or any(s in label.lower() for s in ('u-blox', 'ublox', 'topgnss', 'gnss', 'gps')):
            found.append((item.device, label, item.serial_number or item.hwid or item.device))
    return sorted(found)


def coordinate(value, direction):
    if direction not in ('N', 'S', 'E', 'W'):
        raise ValueError('Invalid hemisphere')
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError('Invalid coordinate')
    degrees = int(number // 100)
    minutes = number - degrees * 100
    result = degrees + minutes / 60
    if not 0 <= minutes < 60 or result > (90 if direction in 'NS' else 180):
        raise ValueError('Coordinate out of range')
    return -result if direction in 'SW' else result


def finite(value):
    result = float(value) if value else None
    if result is not None and not math.isfinite(result):
        raise ValueError('Non-finite number')
    return result


class GNSSReader:
    FRESH_SECONDS = 5.0

    def __init__(self, port=None, baudrate=9600, clock=time.monotonic):
        self.port = port or os.environ.get('KONTUR_GNSS_PORT') or None
        self.baudrate, self.clock = baudrate, clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = self.serial = None
        self.connected = False
        self.active_port = None
        self.device_name = self.error = ''
        self._identity = self._rmc = self._gga = None
        self._last_position = self._last_fix = self._last_utc = None
        self.bad_messages = 0
        self._jump_until = 0

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        if serial is None:
            self.error = 'Нет python3-serial: USB GPS недоступен'
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, name='USB-GNSS', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._disconnected()

    def _disconnected(self, error=''):
        with self._lock:
            self.connected = False
            self._rmc = self._gga = None
            self.error = error

    def _read_loop(self):
        while not self._stop.is_set():
            connection = None
            try:
                candidates = [(self.port, self.port, self.port)] if self.port else discover_ports()
                candidates.sort(key=lambda row: row[2] != self._identity)
                last_error = ''
                selected = None
                for device, label, identity in candidates:
                    try:
                        connection = serial.Serial(port=None, baudrate=self.baudrate, timeout=.3, write_timeout=.3)
                        connection.dtr = connection.rts = False
                        connection.port = device
                        if os.name != 'nt':
                            connection.exclusive = True
                        connection.open()
                        selected = (device, label, identity)
                        break
                    except (OSError, serial.SerialException) as exc:
                        last_error = f'{device}: порт занят или недоступен ({exc})'
                        if connection:
                            connection.close()
                        connection = None
                if connection is None:
                    self._disconnected(last_error)
                    self._stop.wait(2)
                    continue
                device, label, identity = selected
                with self._lock:
                    if self._identity is not None and self._identity != identity:
                        self._last_position = self._last_fix = self._last_utc = None
                        self._jump_until = 0
                    self._identity = identity
                    self.active_port, self.device_name = device, label
                    self.connected, self.error, self.serial = True, '', connection
                    self._rmc = self._gga = None
                buffer = b''
                last_bytes = self.clock()
                while not self._stop.is_set():
                    data = connection.read(2048)
                    if data:
                        last_bytes = self.clock()
                        buffer += data
                    elif self.clock() - last_bytes > 8:
                        raise OSError('нет данных от приёмника')
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        self._parse_line(line.decode('ascii', errors='replace').strip())
                    if len(buffer) > 16384:
                        buffer = b''
            except (OSError, serial.SerialException) as exc:
                self._disconnected(str(exc))
            finally:
                if connection:
                    connection.close()
                self.serial = None
                if self.connected:
                    self._disconnected()
            self._stop.wait(2)

    @staticmethod
    def _verify_checksum(line):
        try:
            if not line.startswith('$'):
                return False
            body, check = line[1:].split('*')
            actual = 0
            for char in body:
                actual ^= ord(char)
            return len(check) == 2 and actual == int(check, 16)
        except ValueError:
            return False

    def _parse_line(self, line):
        if not self._verify_checksum(line):
            self.bad_messages += 1
            return False
        p = line[1:].split('*')[0].split(',')
        now = self.clock()
        try:
            if p[0][-3:] == 'RMC':
                valid = p[2] == 'A'
                lat = lon = utc = speed = None
                if valid:
                    lat, lon = coordinate(p[3], p[4]), coordinate(p[5], p[6])
                    speed = finite(p[7])
                    if speed is not None and speed < 0:
                        raise ValueError('Negative speed')
                    hour, minute, seconds = int(p[1][:2]), int(p[1][2:4]), float(p[1][4:])
                    if not math.isfinite(seconds) or not 0 <= seconds < 60:
                        raise ValueError('Invalid seconds')
                    day, month, year = int(p[9][:2]), int(p[9][2:4]), 2000 + int(p[9][4:6])
                    utc = datetime(year, month, day, hour, minute, int(seconds), int((seconds % 1)*1e6), tzinfo=timezone.utc)
                with self._lock:
                    self._rmc = dict(valid=valid, stamp=now, epoch=p[1], lat=lat, lon=lon, utc=utc,
                                     speed_kmh=speed*1.852 if speed is not None else None)
            elif p[0][-3:] == 'GGA':
                quality, sats = int(p[6] or '0'), int(p[7] or '0')
                hdop, altitude = finite(p[8]), finite(p[9])
                valid = quality in (1, 2, 4, 5)
                lat, lon = (coordinate(p[2], p[3]), coordinate(p[4], p[5])) if valid else (None, None)
                if sats < 0 or (hdop is not None and hdop < 0):
                    raise ValueError('Invalid quality')
                with self._lock:
                    self._gga = dict(valid=valid, stamp=now, epoch=p[1], lat=lat, lon=lon,
                                     quality=quality, satellites=sats, hdop=hdop, altitude=altitude)
            else:
                return True
            with self._lock:
                if self._fresh_fix(now):
                    r, g = self._rmc, self._gga
                    position = dict(latitude=r['lat'], longitude=r['lon'], altitude=g['altitude'],
                                    speed_kmh=r['speed_kmh'], satellites=g['satellites'], hdop=g['hdop'])
                    if self._last_position and self._last_fix is not None and now > self._last_fix:
                        old = self._last_position
                        h = math.sin(math.radians(r['lat']-old['latitude'])/2)**2 + math.cos(math.radians(old['latitude']))*math.cos(math.radians(r['lat']))*math.sin(math.radians(r['lon']-old['longitude'])/2)**2
                        distance = 12742000*math.asin(min(1, math.sqrt(h)))
                        dt = now-self._last_fix
                        if dt < 10 and distance > 100 and distance/dt > 60:
                            self._jump_until = now+10
                    self._last_position, self._last_fix, self._last_utc = position, now, r['utc']
            return True
        except (ValueError, IndexError, OverflowError):
            self.bad_messages += 1
            return False

    def _fresh_fix(self, now):
        r, g = self._rmc, self._gga
        return bool(self.connected and r and g and r['valid'] and g['valid']
                    and now-r['stamp'] < self.FRESH_SECONDS and now-g['stamp'] < self.FRESH_SECONDS
                    and r['epoch'] == g['epoch']
                    and abs(r['lat']-g['lat']) < .0001 and abs(r['lon']-g['lon']) < .0001)

    def get_status(self):
        with self._lock:
            now = self.clock()
            fresh = self._fresh_fix(now)
            age = now-self._last_fix if self._last_fix is not None else None
            position = dict(self._last_position) if self._last_position else None
            warning = ''
            if fresh and position:
                if self._jump_until > now:
                    warning = 'Скачок координат — проверьте положение'
                elif position['hdop'] is not None and position['hdop'] > 5:
                    warning = 'Неблагоприятная геометрия спутников'
            state = 'fresh' if fresh else 'stale' if position else 'searching' if self.connected else 'disconnected'
            return dict(connected=self.connected, has_fix=fresh, state=state, position=position,
                        utc_time=self._last_utc.strftime('%H:%M:%S') if self._last_utc else None,
                        port=self.active_port, device=self.device_name, warning=warning, error=self.error,
                        satellites=self._gga['satellites'] if self._gga and now-self._gga['stamp'] < 5 else 0,
                        hdop=self._gga['hdop'] if self._gga and fresh else None,
                        fix_quality=self._gga['quality'] if self._gga and fresh else 0,
                        last_fix_age=age, bad_messages=self.bad_messages)

    def get_utc_time(self):
        with self._lock:
            now = self.clock()
            if self._fresh_fix(now) and self._last_utc is not None:
                return self._last_utc+timedelta(seconds=now-self._last_fix), True
        return datetime.now(timezone.utc), False


def describe_status(status):
    headline = {'fresh': 'Координаты по данным приёмника', 'stale': 'Последняя позиция — данные устарели',
                'searching': 'Приёмник подключён, поиск координат', 'disconnected': 'USB GPS не подключён'}[status['state']]
    lines = [headline, f"Порт: {status.get('port') or 'автопоиск u-blox / GNSS'}"]
    if status.get('device'):
        lines.append(status['device'])
    position = status.get('position')
    if position:
        lines += [f"WGS84: {position['latitude']:.7f}°, {position['longitude']:.7f}°",
                  f"Спутников в последнем решении: {position['satellites']}"]
        for key, title, unit in [('altitude', 'Высота', ' м'), ('speed_kmh', 'Скорость', ' км/ч'), ('hdop', 'HDOP', '')]:
            value = position.get(key)
            lines.append(f'{title}: {value:.1f}{unit}' if value is not None else f'{title}: —')
        lines.append(f"Обновлено: {status['last_fix_age']:.1f} с назад")
    if status.get('warning'):
        lines.append(status['warning'])
    if status.get('error'):
        lines.append(status['error'])
    lines.append('Это показания приёмника; достоверность положения независимо не проверена.')
    return '\n'.join(lines)
