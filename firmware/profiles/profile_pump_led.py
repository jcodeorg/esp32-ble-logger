"""
デバイスプロファイル: 温湿度(AHT20) + 土壌水分 + 照度センサー(BH1750) + 水中ポンプ + LED タイマー制御構成
（ポンプ・LEDの状態はCSV出力の対象外。read_sensor()が返すのはセンサー値のみ）

main.py から呼ばれる共通インターフェース:
    DEVICE_TYPE   : BLEデバイス名やログに埋め込むこの構成の識別子
    CSV_FIELDS    : read_sensor() が返す辞書のキーを、CSV出力・OLED表示に使う順序で並べたもの
    init_sensors(i2c)   : センサー用ピン/オブジェクトの初期化（起動時に1回呼ばれる）
    read_sensor(i2c)    : センサー値を dict で返す（毎回の計測・OLED更新時に呼ばれる）
    init_actuators()    : アクチュエータの初期化（起動時に1回呼ばれる）
    tick_actuators(now_epoch) : アクチュエータのタイマー制御（メインループから毎秒呼ばれる）
"""
import time
from machine import Pin, ADC
from ahtx0 import AHT20
from bh1750 import BH1750

DEVICE_TYPE = "PcrIoT"
CSV_FIELDS = ["temp", "humid", "soil", "light"]

# ポンプ・LEDの制御ピン（お使いの配線に合わせて変更してください）
PUMP_PIN_NO = 4
LED_PIN_NO = 5

# タイマー設定（24時間表記の時刻。日をまたぐ場合は開始>終了でも判定できるようにしている）
PUMP_ON_HOUR = 8
PUMP_ON_MINUTE = 0
PUMP_DURATION_SEC = 300  # ポンプを動かす秒数

LED_ON_HOUR = 18   # この時刻からLEDを点灯
LED_OFF_HOUR = 22  # この時刻でLEDを消灯

_light_sensor = None
_adc_soil = None
_pump_pin = None
_led_pin = None
_pump_state = 0
_led_state = 0
_pump_started_epoch = None


def init_sensors(i2c):
    global _light_sensor, _adc_soil
    try:
        _light_sensor = BH1750(i2c)
    except Exception as e:
        print("BH1750 init error:", e)
        _light_sensor = None

    # A1: 土壌水分センサ用 ADC
    _adc_soil = ADC(Pin(1, Pin.IN))
    _adc_soil.atten(ADC.ATTN_11DB)   # 0〜3.3V の範囲を読む
    _adc_soil.width(ADC.WIDTH_12BIT) # 分解能 12 ビット（0〜4095）


def read_sensor(i2c):
    try:
        aht20 = AHT20(i2c)
        temp = round(aht20.temperature, 1)
        humid = round(aht20.relative_humidity, 1)
    except Exception as e:
        print("AHT20 error:", e)
        temp = 0.0
        humid = 0.0

    try:
        soil = _adc_soil.read()
    except Exception as e:
        print("ADC error:", e)
        soil = 0

    try:
        light = _light_sensor.luminance() if _light_sensor else 0.0
    except Exception as e:
        print("BH1750 error:", e)
        light = 0.0

    return {"temp": temp, "humid": humid, "soil": soil, "light": light}


def init_actuators():
    global _pump_pin, _led_pin
    _pump_pin = Pin(PUMP_PIN_NO, Pin.OUT)
    _pump_pin.value(0)
    _led_pin = Pin(LED_PIN_NO, Pin.OUT)
    _led_pin.value(0)


def _in_led_window(hour):
    if LED_ON_HOUR <= LED_OFF_HOUR:
        return LED_ON_HOUR <= hour < LED_OFF_HOUR
    return hour >= LED_ON_HOUR or hour < LED_OFF_HOUR  # 日をまたぐ設定にも対応


def tick_actuators(now_epoch):
    """RTCの現在時刻を見てポンプ・LEDのON/OFFを切り替える（メインループから毎秒呼ばれる）"""
    global _pump_state, _led_state, _pump_started_epoch

    if _pump_pin is None or _led_pin is None:
        return

    t = time.localtime(now_epoch)
    hour, minute = t[3], t[4]

    # ポンプ: 設定時刻ちょうどに開始し、PUMP_DURATION_SEC秒後に停止する
    if _pump_started_epoch is None:
        if hour == PUMP_ON_HOUR and minute == PUMP_ON_MINUTE:
            _pump_started_epoch = now_epoch
            _pump_state = 1
            _pump_pin.value(1)
    else:
        if now_epoch - _pump_started_epoch >= PUMP_DURATION_SEC:
            _pump_state = 0
            _pump_pin.value(0)
            _pump_started_epoch = None

    # LED: 時間帯で単純にON/OFF
    led_should_be_on = _in_led_window(hour)
    if led_should_be_on != bool(_led_state):
        _led_state = 1 if led_should_be_on else 0
        _led_pin.value(_led_state)
