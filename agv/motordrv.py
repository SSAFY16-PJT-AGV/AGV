# -*- coding: utf-8 -*-
"""jetbot 라이브러리 없이 모터 보드(PCA9685, I2C)를 바로 움직이는 드라이버.

JETANK·Waveshare JetBot 보드는 NVIDIA jetbot 라이브러리(Robot)로 움직이는데, JetPack 4.5 JetBot 이미지는
jetbot 라이브러리가 도커 컨테이너 안에만 있어서 바깥(SSH 터미널)의 python3에서는 import가 안 된다.
그때 이 드라이버를 쓴다. Waveshare jetbot 포크(motor.py)와 같은 레지스터를 쓴다.

    - PCA9685 주소 0x60(Adafruit MotorHAT 기본), I2C 버스 1, PWM 1600Hz
    - 모터 1(왼쪽): Adafruit 핀 PWM 8 / IN2 9 / IN1 10, Waveshare 핀 1·0
    - 모터 2(오른쪽): Adafruit 핀 PWM 13 / IN2 12 / IN1 11, Waveshare 핀 2·3
    - 값 v(-1~1) -> 속도 = |int(255 v)|, v < 0이면 FORWARD + Waveshare 핀 (0, 속도x16),
      아니면 BACKWARD + Waveshare 핀 (속도x16, 0). jetbot과 같은 방향 규칙이라 설정(invert_*)을 그대로 씀

I2C는 smbus2 또는 smbus(apt python3-smbus)로 연다. Python 3.6 호환.
"""
from __future__ import division

import math
import time

MODE1, MODE2, PRESCALE, LED0_ON_L, ALL_LED_ON_L = 0x00, 0x01, 0xFE, 0x06, 0xFA
CHANNELS = {1: {"pwm": 8, "in2": 9, "in1": 10, "ina": 1, "inb": 0},
            2: {"pwm": 13, "in2": 12, "in1": 11, "ina": 2, "inb": 3}}


def open_bus(bus=1):
    try:
        from smbus2 import SMBus
    except ImportError:
        try:
            from smbus import SMBus
        except ImportError:
            raise RuntimeError(u"I2C 라이브러리가 없어요: sudo apt install python3-smbus 또는 pip3 install --user smbus2")
    try:
        return SMBus(bus)
    except (IOError, OSError) as e:
        raise RuntimeError(u"I2C 버스 %d를 못 열었어요(%s). 사용자가 i2c 그룹인지 확인: sudo usermod -aG i2c $USER 후 "
                           u"다시 로그인" % (bus, e))


def find_board(bus, candidates=(0x60, 0x40)):
    """응답하는 PCA9685 주소."""
    for addr in candidates:
        try:
            bus.read_byte_data(addr, MODE1)
            return addr
        except (IOError, OSError):
            continue
    return None


class PCA9685Motors(object):
    """jetbot Robot과 같은 set_motors(left, right) / stop()."""

    def __init__(self, bus=1, addr=None, freq=1600, smbus=None):
        self.bus = smbus if smbus is not None else open_bus(bus)
        self.addr = addr if addr is not None else find_board(self.bus)
        if self.addr is None:
            raise RuntimeError(u"I2C 버스 %d에서 모터 보드(PCA9685, 0x60/0x40)를 못 찾았어요. i2cdetect -y -r %d로 확인하세요"
                               % (bus, bus))
        self._init(freq)

    def _w(self, reg, val):
        self.bus.write_byte_data(self.addr, reg, val & 0xFF)

    def _init(self, freq):
        for i, v in enumerate((0, 0, 0, 0)):                 # 모든 출력 끔
            self._w(ALL_LED_ON_L + i, v)
        self._w(MODE2, 0x04)                                  # OUTDRV
        self._w(MODE1, 0x01)                                  # ALLCALL
        time.sleep(0.005)
        mode1 = self.bus.read_byte_data(self.addr, MODE1) & ~0x10   # 깨우기
        self._w(MODE1, mode1)
        time.sleep(0.005)
        prescale = int(math.floor(25000000.0 / 4096.0 / float(freq) - 1.0 + 0.5))
        old = self.bus.read_byte_data(self.addr, MODE1)
        self._w(MODE1, (old & 0x7F) | 0x10)                  # 잠깐 재우고 주파수 설정
        self._w(PRESCALE, prescale)
        self._w(MODE1, old)
        time.sleep(0.005)
        self._w(MODE1, old | 0x80)

    def _pwm(self, ch, on, off):
        base = LED0_ON_L + 4 * ch
        self._w(base, on & 0xFF)
        self._w(base + 1, on >> 8)
        self._w(base + 2, off & 0xFF)
        self._w(base + 3, off >> 8)

    def _pin(self, ch, value):
        if value:
            self._pwm(ch, 4096, 0)
        else:
            self._pwm(ch, 0, 4096)

    def _motor(self, num, value):
        c = CHANNELS[num]
        mapped = int(255.0 * max(-1.0, min(1.0, value)))
        speed = min(abs(mapped), 255)
        self._pwm(c["pwm"], 0, speed * 16)
        if mapped < 0:
            self._pin(c["in2"], 0)
            self._pin(c["in1"], 1)
            self._pwm(c["ina"], 0, 0)
            self._pwm(c["inb"], 0, speed * 16)
        else:
            self._pin(c["in1"], 0)
            self._pin(c["in2"], 1)
            self._pwm(c["ina"], 0, speed * 16)
            self._pwm(c["inb"], 0, 0)

    def set_motors(self, left, right):
        self._motor(1, left)
        self._motor(2, right)

    def stop(self):
        for num in (1, 2):
            c = CHANNELS[num]
            self._pin(c["in1"], 0)
            self._pin(c["in2"], 0)
            self._pwm(c["ina"], 0, 0)
            self._pwm(c["inb"], 0, 0)
