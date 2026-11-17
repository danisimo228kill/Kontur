import math
import socket
import struct
import time
from dataclasses import dataclass

import hardware_config as config


PACKET = struct.Struct('<BBddd')


def encode_command(mode, direction, speed, angle, motor_step):
    if mode not in (0, 1) or direction not in (0, 1):
        raise ValueError('Недопустимый режим или направление ЭСП.')
    values = (speed, angle, motor_step)
    if any(not isinstance(v, (float, int)) or not math.isfinite(v) for v in values):
        raise ValueError('Не заданы конечные числовые параметры ЭСП.')
    if not 0 <= speed <= 120 or not 0 <= angle <= 360 or motor_step <= 0:
        raise ValueError('Параметры ЭСП вне допустимого диапазона.')
    return PACKET.pack(mode, direction, speed, angle, motor_step)


@dataclass(frozen=True)
class Motion:
    mode: int
    direction: int
    speed: float
    angle: float
    start_angle: float
    started: float
    endpoint: tuple
    motor_step: float


class ESPController:
    def __init__(self, clock=time.monotonic):
        self.simulation = bool(config.SIMULATION_MODE)
        self.clock = clock
        self.motion = None
        self.sender = None
        self.stop_failed = False

    @property
    def active(self):
        return self.motion is not None

    def transmit(self, packet, endpoint):
        if self.simulation:
            return
        if self.sender is None:
            self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sender.setblocking(False)
        if self.sender.sendto(packet, endpoint) != len(packet):
            raise OSError('Пакет ЭСП отправлен не полностью.')

    def start(self, mode, direction, speed, angle, start_angle):
        if self.active:
            raise ValueError('Сначала отправьте команду остановки ЭСП.')
        if not self.simulation and not config.ESP_PROTOCOL_CONFIRMED:
            raise ValueError('Пуск недоступен: требуется уточнение протокола ЭСП (см. README).')
        if mode not in (0, 1) or direction not in (0, 1):
            raise ValueError('Недопустимый режим или направление ЭСП.')
        if any(not math.isfinite(v) for v in (speed, angle, start_angle)):
            raise ValueError('Параметры движения должны быть конечными числами.')
        if not 0 < speed <= 120 or not 0 <= angle <= 360 or (mode == 0 and angle <= 0):
            raise ValueError('Для пуска задайте положительные скорость и угол.')
        endpoint = (config.ESP_IP, config.ESP_PORT)
        if not self.simulation:
            packet = encode_command(mode, direction, speed, angle, config.ESP_MOTOR_STEP_DEG)
            self.transmit(packet, endpoint)
        self.motion = Motion(mode, direction, speed, angle, start_angle,
                             self.clock(), endpoint, config.ESP_MOTOR_STEP_DEG)
        self.stop_failed = False

    def position(self):
        motion = self.motion
        if motion is None:
            return None
        distance = max(0.0, self.clock() - motion.started) * motion.speed
        if motion.mode == 0:
            distance = min(distance, motion.angle)
        return (motion.start_angle + (1 if motion.direction else -1) * distance) % 360

    def due(self):
        motion = self.motion
        return bool(motion and motion.mode == 0 and not self.stop_failed and
                    self.clock() - motion.started >= motion.angle / motion.speed)

    def stop(self, mode=1, direction=1):
        motion = self.motion
        if not self.simulation and not motion and not config.ESP_PROTOCOL_CONFIRMED:
            raise ValueError('Команда стопа ЭСП не подтверждена (см. README).')
        endpoint = motion.endpoint if motion else (config.ESP_IP, config.ESP_PORT)
        step = motion.motor_step if motion else config.ESP_MOTOR_STEP_DEG
        position = self.position()
        try:
            if not self.simulation:
                packet = encode_command(motion.mode if motion else mode,
                                        motion.direction if motion else direction, 0.0, 0.0, step)
                self.transmit(packet, endpoint)
        except OSError:
            self.stop_failed = True
            raise
        self.motion = None
        self.stop_failed = False
        if self.sender:
            self.sender.close()
            self.sender = None
        return position
