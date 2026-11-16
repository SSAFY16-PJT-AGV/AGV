# -*- coding: utf-8 -*-
"""배터리 전압 읽기(선택). 확장 보드의 INA219 전류·전압 센서를 I2C로 읽어 0~1로 환산한다.

JETANK 보드에 INA219가 있는지, 주소가 무엇인지는 현장에서 확인해야 한다(Waveshare JetBot 보드는
0x41로 알려져 있음 - 확인 전). config link.battery = {"ina219_addr": 65, "bus": 1, "v_empty": 9.0,
"v_full": 12.6}처럼 주소를 넣었을 때만 쓴다. 못 읽으면 None(상태 메시지 battery가 null).
"""


class Ina219Battery(object):
    def __init__(self, addr, bus=1, v_empty=9.0, v_full=12.6):
        import smbus      # 젯슨 기본 이미지에 있음(python3-smbus)
        self.bus = smbus.SMBus(int(bus))
        self.addr = int(addr)
        self.v_empty, self.v_full = float(v_empty), float(v_full)
        self.volts = None

    def read_volts(self):
        raw = self.bus.read_word_data(self.addr, 0x02)      # bus voltage 레지스터(빅엔디언)
        raw = ((raw & 0xFF) << 8) | (raw >> 8)
        return (raw >> 3) * 0.004

    def __call__(self):
        try:
            v = self.read_volts()
        except Exception:
            return None
        if not 1.0 < v < 30.0:
            return None
        self.volts = v
        f = (v - self.v_empty) / (self.v_full - self.v_empty)
        return round(max(0.0, min(1.0, f)), 3)


def make_battery(cfg):
    """config link.battery에서 읽기 함수. 설정이 없거나 센서를 못 열면 None."""
    b = (cfg or {}).get("battery") or {}
    if not b.get("ina219_addr"):
        return None
    try:
        return Ina219Battery(b["ina219_addr"], b.get("bus", 1), b.get("v_empty", 9.0), b.get("v_full", 12.6))
    except Exception:
        return None
