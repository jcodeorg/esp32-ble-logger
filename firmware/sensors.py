"""
デバイス構成: 土壌水分センサー + CdS照度センサー構成（現行の標準構成）

main.py から呼ばれる共通インターフェース:
    DEVICE_TYPE   : BLEデバイス名やログに埋め込むこの構成の識別子
    CSV_FIELDS    : read_sensor() が返す辞書のキーを、CSV出力・OLED表示に使う順序で並べたもの
    init_sensors(i2c)   : センサー用ピン/オブジェクトの初期化（起動時に1回呼ばれる）
    read_sensor(i2c)    : センサー値を dict で返す（毎回の計測・OLED更新時に呼ばれる）
    init_actuators()    : アクチュエータの初期化（起動時に1回呼ばれる。このプロファイルでは未使用）
    tick_actuators(now_epoch) : アクチュエータのタイマー制御（メインループから毎秒呼ばれる。このプロファイルでは未使用）
    update_timer_config(config) / set_pump_manual(state) / set_led_manual(state) / reset_actuator_overrides()
        : タイマー制御仕様(timer_control_spec.md)向けの追加インターフェース。このプロファイルでは未使用
"""
from machine import Pin, ADC
from ahtx0 import AHT20

DEVICE_TYPE = "EnvLog"
CSV_FIELDS = ["temp", "humid", "soil", "light"]

_adc_soil = None
_adc_cds = None


def init_sensors(i2c):
    global _adc_soil, _adc_cds
    # A1: 土壌水分センサ用 ADC
    _adc_soil = ADC(Pin(1, Pin.IN))
    _adc_soil.atten(ADC.ATTN_11DB)   # 0〜3.3V の範囲を読む
    _adc_soil.width(ADC.WIDTH_12BIT) # 分解能 12 ビット（0〜4095）

    # A2: CdS 照度センサ用 ADC
    _adc_cds = ADC(Pin(2, Pin.IN))
    _adc_cds.atten(ADC.ATTN_11DB)
    _adc_cds.width(ADC.WIDTH_12BIT)


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
        light = _adc_cds.read()
    except Exception as e:
        print("ADC error:", e)
        soil = 0
        light = 0

    return {"temp": temp, "humid": humid, "soil": soil, "light": light}


def init_actuators():
    pass  # このプロファイルにはアクチュエータなし


def tick_actuators(now_epoch):
    pass  # このプロファイルにはアクチュエータなし


def update_timer_config(config):
    pass  # このプロファイルにはアクチュエータなし


def set_pump_manual(state):
    pass  # このプロファイルにはアクチュエータなし


def set_led_manual(state):
    pass  # このプロファイルにはアクチュエータなし


def reset_actuator_overrides():
    pass  # このプロファイルにはアクチュエータなし
