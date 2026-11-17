import socket
import time

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (QComboBox, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout, QWidget)


import hardware_config as config


class DrivePanel(QWidget):
    def __init__(self, window):
        super().__init__()
        self.simulation = bool(config.SIMULATION_MODE)
        self.clock = time.monotonic
        self.active_endpoint = None
        self.active_command = None
        self.sender = None
        self.model_angle = 0
        self.motion_timer = QTimer(self)
        self.motion_timer.setInterval(20)
        self.motion_timer.setTimerType(Qt.PreciseTimer)
        self.motion_timer.timeout.connect(self.update_motion)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        self.position = QLabel()
        self.position.setStyleSheet(
            'color: #ffca72; font-weight: 600; font-size: 13px; padding: 8px 12px;'
            'background: #10202c; border: 1px solid #304553; border-radius: 4px;'
        )
        self.update_position()
        layout.addWidget(self.position, 0, Qt.AlignRight)

        middle = QHBoxLayout()
        frame = QWidget()
        frame.setObjectName('actuatorControls')
        frame.setFixedWidth(120)
        frame.setStyleSheet("""
            QWidget#actuatorControls { border: 1px solid #304553; border-radius: 4px; }
            QPushButton { background: #253e50; color: #59dbdf; border: 1px solid #39566a;
                          border-radius: 4px; font-size: 22px; padding: 0; min-width: 0; }
            QPushButton:hover { background: #325269; }
            QPushButton:pressed { background: #1a2a38; }
        """)
        arrows = QVBoxLayout(frame)
        arrows.setContentsMargins(12, 8, 20, 8)
        arrows.setSpacing(8)
        self.step = QComboBox()
        for degrees in (1, 5, 10, 15, 30, 45, 90):
            self.step.addItem(f'{degrees}°', degrees)
        self.step.setCurrentIndex(1)
        self.step.setFixedHeight(28)
        self.step.setToolTip('Угол поворота при удержании стрелки.' if self.simulation else
                            'Шаг модели на экране. На привод отправляется только направление.')
        arrows.addWidget(self.step)
        self.up = QPushButton('▲')
        self.down = QPushButton('▼')
        for button, name in ((self.up, 'Вверх'), (self.down, 'Вниз')):
            button.setAccessibleName(name)
            button.setToolTip(f'{name} — удерживайте для движения, отпустите для остановки')
            button.setAutoDefault(False)
            button.setFixedSize(70, 50)
            arrows.addWidget(button)
        middle.addWidget(frame)
        middle.addSpacing(8)

        controls = QVBoxLayout()
        hint = QLabel('Удерживайте стрелку для поворота на выбранный шаг.\n'
                      'Отпустите для досрочной остановки.' if self.simulation else
                      'Удерживайте стрелку для движения.\nОтпустите для остановки.')
        hint.setWordWrap(True)
        controls.addWidget(hint)
        self.stop = QPushButton('■ Стоп')
        self.stop.setObjectName('stopBtn')
        self.stop.setAutoDefault(False)
        controls.addWidget(self.stop)
        middle.addLayout(controls, 1)
        layout.addLayout(middle)
        self.status = QLabel('Готов к движению.' if self.simulation else
                             'Команды ещё не отправлялись.')
        self.status.setAlignment(Qt.AlignCenter)
        self.status.setStyleSheet('padding: 6px; color: #91a7b7; font-size: 12px;')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()

        self.up.pressed.connect(lambda: self.start(1))
        self.down.pressed.connect(lambda: self.start(2))
        self.up.released.connect(lambda: self.release(1))
        self.down.released.connect(lambda: self.release(2))
        self.stop.pressed.connect(self.send_stop)

    def update_position(self):
        sign = '−' if self.model_angle < 0 else ''
        self.position.setText(f'Положение актуатора: {sign}{abs(self.model_angle):.1f}°')

    def endpoint(self):
        return config.ACTUATOR_IP, config.ACTUATOR_PORT

    def transmit(self, command, endpoint):
        if self.simulation:
            return True
        try:
            if self.sender is None:
                self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.sender.setblocking(False)
            self.sender.sendto(bytes([command]), endpoint)
        except OSError as exc:
            self.status.setText(f'Ошибка отправки {command:02b}: {exc}')
            return False
        action = {0: 'стоп', 1: 'вверх', 2: 'вниз'}[command]
        self.status.setText(f'Команда «{action}» отправлена. Подтверждения от привода нет.')
        return True

    def start(self, command):
        if self.active_endpoint:
            return
        if self.simulation:
            self.active_endpoint = ('simulation', 0)
            self.active_command = command
            self.motion_start = self.model_angle
            self.motion_started = self.clock()
            delta = self.step.currentData() * (1 if command == 1 else -1)
            self.motion_target = max(-90, min(90, self.model_angle + delta))
            self.step.setEnabled(False)
            self.motion_timer.start()
            self.status.setText('Движение вверх.' if command == 1 else 'Движение вниз.')
            self.update_motion()
            return
        endpoint = self.endpoint()
        if endpoint and self.transmit(command, endpoint):
            self.active_endpoint = endpoint
            self.active_command = command
            step = self.step.currentData()
            self.model_angle = max(-90, min(90, self.model_angle + (step if command == 1 else -step)))
            self.update_position()

    def send_stop(self):
        if self.simulation:
            self.update_motion(finish=False)
            self.motion_timer.stop()
            self.active_endpoint = self.active_command = None
            self.step.setEnabled(True)
            self.status.setText('Остановлено.')
            return
        endpoint = self.active_endpoint or self.endpoint()
        if endpoint and self.transmit(0, endpoint):
            self.active_endpoint = self.active_command = None
            self.sender.close()
            self.sender = None

    def update_motion(self, finish=True):
        if not self.simulation or self.active_command is None:
            return
        travel = max(0, self.clock() - self.motion_started) * config.ACTUATOR_SIMULATION_SPEED
        distance = abs(self.motion_target - self.motion_start)
        self.model_angle = self.motion_start + min(travel, distance) * (1 if self.active_command == 1 else -1)
        self.update_position()
        if finish and travel >= distance:
            self.model_angle = self.motion_target
            self.update_position()
            self.motion_timer.stop()
            self.active_endpoint = self.active_command = None
            self.step.setEnabled(True)
            self.status.setText('Заданный поворот завершён.')

    def release(self, command):
        if self.active_command == command:
            self.send_stop()

    def stop_if_active(self):
        if self.active_endpoint:
            self.send_stop()

    def hideEvent(self, event):
        self.stop_if_active()
        super().hideEvent(event)
