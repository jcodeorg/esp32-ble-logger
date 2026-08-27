"""BH1750 デジタル照度センサー用の最小限のI2Cドライバ。"""
import time


class BH1750:
    ADDR = 0x23
    CONTINUOUS_HIGH_RES_MODE = 0x10

    def __init__(self, i2c, addr=ADDR):
        self.i2c = i2c
        self.addr = addr
        self.i2c.writeto(self.addr, bytes([self.CONTINUOUS_HIGH_RES_MODE]))
        time.sleep_ms(180)  # 高分解能モードの最大変換時間を待つ

    def luminance(self):
        """照度をlux単位で返す"""
        data = self.i2c.readfrom(self.addr, 2)
        raw = (data[0] << 8) | data[1]
        return round(raw / 1.2, 1)
