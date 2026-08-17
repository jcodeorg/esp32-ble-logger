from neopixel import NeoPixel
from machine import Pin

npled = NeoPixel(Pin(16, Pin.OUT), 2)
npled[0] = (0, 0, 0)  # 0番の NeoPixel を消灯
npled[1] = (0, 0, 0)  # 1番の NeoPixel を消灯
npled.write()
