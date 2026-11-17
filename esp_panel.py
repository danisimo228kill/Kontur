from PyQt5.QtCore import QEvent, Qt, QTimer
from PyQt5.QtWidgets import (QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
                             QHBoxLayout, QLabel, QPushButton, QTabWidget,
                             QVBoxLayout, QWidget)

import hardware_config as config
from drive_panel import DrivePanel
from esp_protocol import ESPController


class ESPControlDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent_window = parent
        self.controller = ESPController()
        self.setWindowTitle('Управление ЭСП')
        self.setMinimumSize(540, 410)
        self.resize(560, 440)
        self.setStyleSheet('''
            QDialog { background: #15232f; }
            QTabWidget::pane { border: 1px solid #304553; border-radius: 4px; }
            QTabBar::tab { background: #1b2e3d; color: #91a7b7; padding: 10px 18px; }
            QTabBar::tab:selected { background: #294758; color: #edf6fb; }
            QLabel { color: #e3edf4; }
            QPushButton { background: #253e50; color: #edf6fb; border: 1px solid #39566a;
                          border-radius: 4px; padding: 9px; min-width: 70px; }
            QPushButton:hover { background: #325269; }
            QPushButton:disabled { color: #71838e; background: #1b2e3d; }
            QPushButton#startBtn { background: #2d5a3d; color: #a8e6b8; }
            QPushButton#stopBtn { background: #5a2d2d; color: #e6a8a8; }
            QComboBox, QDoubleSpinBox { background: #10202c; color: #e3edf4;
                border: 1px solid #395061; padding: 6px; border-radius: 3px; }
        ''')
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.inputs = []
        self.statuses = []
        self.starts = []
        self.directions = []
        for mode in (1, 0):
            page = QWidget()
            column = QVBoxLayout(page)
            column.setSpacing(12)
            form = QFormLayout()
            form.setSpacing(12)
            direction = QComboBox()
            direction.addItem('По часовой стрелке', 1)
            direction.addItem('Против часовой стрелки', 0)
            self.directions.append(direction)
            if mode == 1:
                self.mode_label = QLabel()
                form.addRow('Режим радара:', self.mode_label)
                self.prog_speed = QComboBox()
                form.addRow('Скорость:', self.prog_speed)
                self.inputs.append(self.prog_speed)
            else:
                self.manual_angle = QDoubleSpinBox()
                self.manual_angle.setRange(0.1, 360.0)
                self.manual_angle.setDecimals(1)
                self.manual_angle.setValue(10)
                self.manual_angle.setSuffix(' °')
                form.addRow('Шаг поворота антенны:', self.manual_angle)
                self.manual_speed = QDoubleSpinBox()
                self.manual_speed.setRange(0.1, 120.0)
                self.manual_speed.setDecimals(1)
                self.manual_speed.setValue(10)
                self.manual_speed.setSuffix(' °/с')
                form.addRow('Скорость:', self.manual_speed)
                self.inputs.extend((self.manual_angle, self.manual_speed))
            form.addRow('Направление:', direction)
            self.inputs.append(direction)
            column.addLayout(form)
            row = QHBoxLayout()
            start = QPushButton('Старт')
            start.setObjectName('startBtn')
            start.setAutoDefault(False)
            start.clicked.connect(self.on_start)
            self.starts.append(start)
            stop = QPushButton('■ Стоп')
            stop.setObjectName('stopBtn')
            stop.setAutoDefault(False)
            stop.clicked.connect(self.on_stop)
            row.addWidget(start)
            row.addWidget(stop)
            column.addLayout(row)
            status = QLabel('Команды ещё не отправлялись.')
            status.setWordWrap(True)
            status.setMinimumHeight(42)
            self.statuses.append(status)
            column.addWidget(status)
            if not self.controller.simulation:
                note = QLabel('Положение рассчитывается программой. Обратной связи от ЭСП нет.')
                note.setWordWrap(True)
                note.setStyleSheet('color: #91a7b7; font-size: 12px;')
                column.addWidget(note)
            column.addStretch()
            self.tabs.addTab(page, 'Автоматическое' if mode else 'Ручное')
        self.drive_panel = DrivePanel(parent)
        self.tabs.addTab(self.drive_panel, 'Актуатор')
        self.previous_tab = 0
        self.tabs.currentChanged.connect(self.on_tab_changed)
        self.model_timer = QTimer(self)
        self.model_timer.setTimerType(Qt.PreciseTimer)
        self.model_timer.setInterval(20)
        self.model_timer.timeout.connect(self.update_motion)
        self.update_radar_mode()
        if self.controller.simulation:
            self.set_status(f'Готов к пуску · азимут {parent.canvas.azimuth:.1f}°')
        elif not config.ESP_PROTOCOL_CONFIRMED:
            self.set_status('Пуск недоступен: требуется уточнение протокола ЭСП (см. README).')

    def set_status(self, text):
        for label in self.statuses:
            label.setText(text)

    def update_radar_mode(self):
        if self.controller.active and self.controller.motion.mode == 1:
            if not self.stop_active():
                return
        range_m = self.parent_window.radar_range
        self.mode_label.setText(f'{range_m // 1000} км')
        previous = self.prog_speed.currentData()
        self.prog_speed.clear()
        periods = config.ESP_NEAR_PERIODS if range_m == 10000 else (
            (config.ESP_FAR_PERIOD,) if range_m == 70000 else ())
        for period in periods:
            self.prog_speed.addItem(f'1 оборот за {period:g} с · {360 / period:.2f} °/с', period)
        if range_m == 10000:
            index = self.prog_speed.findData(previous if previous in periods else config.ESP_NEAR_DEFAULT_PERIOD)
            self.prog_speed.setCurrentIndex(max(0, index))
        if not periods:
            self.prog_speed.addItem('Скорость для этого режима не задана', None)
        self.update_controls()

    def update_controls(self):
        active = self.controller.active
        for widget in self.inputs:
            widget.setEnabled(not active)
        self.starts[0].setEnabled(not active and self.prog_speed.currentData() is not None)
        self.starts[1].setEnabled(not active)

    def on_start(self):
        index = self.tabs.currentIndex()
        if index not in (0, 1):
            return
        period = self.prog_speed.currentData()
        if index == 0 and period is None:
            self.set_status('Для выбранного режима радара скорость ЭСП не задана.')
            return
        speed = 360 / period if index == 0 else self.manual_speed.value()
        angle = 0.0 if index == 0 else self.manual_angle.value()
        try:
            self.controller.start(1 if index == 0 else 0, self.directions[index].currentData(),
                                  speed, angle, self.parent_window.canvas.azimuth)
        except (OSError, ValueError) as exc:
            self.set_status(str(exc))
            return
        self.parent_window.esp_model_controlled = True
        self.parent_window.canvas.esp_controlled = True
        self.model_timer.start()
        self.update_controls()
        self.update_motion()

    def update_motion(self):
        if not self.controller.active:
            return
        position = self.controller.position()
        self.parent_window.canvas.azimuth = position
        self.parent_window.refresh_map()
        if self.controller.stop_failed:
            return
        if self.controller.due():
            self.stop_active(completed=True)
        else:
            if self.controller.simulation:
                motion = self.controller.motion
                text = f'Вращение · азимут {position:.1f}°'
                if motion.mode == 0:
                    distance = min(motion.angle, max(0, self.controller.clock() - motion.started) * motion.speed)
                    text = f'Поворот {distance:.1f}° из {motion.angle:g}° · азимут {position:.1f}°'
                self.set_status(text)
            else:
                self.set_status(f'Команда движения отправлена · расчётный азимут {position:.1f}°')

    def on_stop(self):
        return self.stop_active(force=True)

    def stop_active(self, force=False, completed=False):
        if not self.controller.active and not force:
            return True
        index = min(self.tabs.currentIndex(), 1)
        try:
            position = self.controller.stop(1 if index == 0 else 0, self.directions[index].currentData())
        except (OSError, ValueError) as exc:
            self.set_status(f'Стоп не отправлен: {exc}')
            self.update_controls()
            return False
        if position is not None:
            self.parent_window.canvas.azimuth = position
            self.parent_window.refresh_map()
        self.model_timer.stop()
        self.update_controls()
        if self.controller.simulation:
            text = 'Поворот завершён' if completed else 'Остановлено'
            self.set_status(f'{text} · азимут {self.parent_window.canvas.azimuth:.1f}°')
        else:
            prefix = 'Расчётный поворот завершён. ' if completed else ''
            self.set_status(prefix + 'Команда стопа отправлена. Остановка ЭСП не подтверждена.')
        return True

    def stop_all(self):
        esp_stopped = self.stop_active()
        self.drive_panel.stop_if_active()
        return esp_stopped and self.drive_panel.active_endpoint is None

    def on_tab_changed(self, index):
        if not self.stop_all():
            self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(self.previous_tab)
            self.tabs.blockSignals(False)
            return
        self.previous_tab = index

    def changeEvent(self, event):
        if event.type() == QEvent.ActivationChange and not self.isActiveWindow() and hasattr(self, 'drive_panel'):
            self.drive_panel.stop_if_active()
        super().changeEvent(event)

    def closeEvent(self, event):
        if self.stop_all():
            event.accept()
        else:
            event.ignore()

    def done(self, result):
        if self.stop_all():
            super().done(result)

    def hideEvent(self, event):
        self.stop_all()
        super().hideEvent(event)
