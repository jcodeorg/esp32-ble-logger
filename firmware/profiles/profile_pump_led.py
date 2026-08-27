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

    以下はタイマー制御仕様（timer_control_spec.md）に基づく追加インターフェース:
    update_timer_config(config) : ブラウザから受信したタイマー設定(dict)を反映する
    set_pump_manual(state)      : ポンプの手動ON/OFF。手動オーバーライドを有効化する
    set_led_manual(state)       : LEDの手動ON/OFF。手動オーバーライドを有効化する
    reset_actuator_overrides()  : BLE切断時に手動オーバーライドを解除し、タイマー判定に戻す
"""
import time
from machine import Pin, ADC
from ahtx0 import AHT20
from bh1750 import BH1750

DEVICE_TYPE = "PcrIoT"
CSV_FIELDS = ["temp", "humid", "soil", "light"]

# ポンプ・LEDの制御ピン（お使いの配線に合わせて変更してください。両ピンともPWM対応だが現状はON/OFF制御のみ）
PUMP_PIN_NO = 0
LED_PIN_NO = 2

# タイマー設定の既定値（ブラウザから SET_TIMER で上書きされる。マスターデータはブラウザ側）
# 稼働時間を0にすることで、初期状態ではポンプ・LEDとも自動稼働しないようにしている
_timer_config = {
    "p_time": "08:00",
    "p_sec": 0,
    "l_time": "06:00",
    "l_min": 0,
}

_light_sensor = None
_adc_soil = None
_pump_pin = None
_led_pin = None
_pump_state = 0
_led_state = 0
_pump_manual_override = False
_led_manual_override = False


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


def update_timer_config(config):
    """SET_TIMERコマンドで受信した設定(dict)を反映する。想定外のキーは無視する"""
    for key in ("p_time", "p_sec", "l_time", "l_min"):
        if key in config:
            _timer_config[key] = config[key]
    print("[INFO] タイマー設定を更新しました:", _timer_config)


def set_pump_manual(state):
    """手動操作でポンプをON/OFFし、以後のタイマー判定を一時的にスルーする"""
    global _pump_state, _pump_manual_override
    _pump_manual_override = True
    _pump_state = 1 if state else 0
    if _pump_pin:
        _pump_pin.value(_pump_state)


def set_led_manual(state):
    """手動操作でLEDをON/OFFし、以後のタイマー判定を一時的にスルーする"""
    global _led_state, _led_manual_override
    _led_manual_override = True
    _led_state = 1 if state else 0
    if _led_pin:
        _led_pin.value(_led_state)


def reset_actuator_overrides():
    """BLE切断時に手動オーバーライドを解除し、タイマー制御へ復帰させる"""
    global _pump_manual_override, _led_manual_override
    _pump_manual_override = False
    _led_manual_override = False


def _hhmm_to_seconds(hhmm):
    """"HH:MM" を当日0時からの経過秒数に変換する"""
    hour_str, minute_str = hhmm.split(":")
    return int(hour_str) * 3600 + int(minute_str) * 60


def _is_in_timer_window(start_seconds, duration_sec, now_epoch):
    """現在時刻が「開始時刻〜開始時刻+稼働時間」の範囲内かどうかを判定する（日またぎにも対応）"""
    t = time.localtime(now_epoch)
    seconds_today = t[3] * 3600 + t[4] * 60 + t[5]
    end_seconds = start_seconds + duration_sec

    if end_seconds <= 86400:
        # 日をまたがない通常のケース
        return start_seconds <= seconds_today < end_seconds
    # 日をまたぐケース（例: 23:00開始で3時間稼働 -> 翌日2:00まで）
    return seconds_today >= start_seconds or seconds_today < (end_seconds - 86400)


def tick_actuators(now_epoch):
    """RTCの現在時刻とタイマー設定を見てポンプ・LEDのON/OFFを切り替える（メインループから毎秒呼ばれる）"""
    global _pump_state, _led_state

    if _pump_pin is None or _led_pin is None:
        return

    # ポンプ: 手動オーバーライド中はタイマー判定をスキップする
    if not _pump_manual_override:
        pump_should_be_on = _is_in_timer_window(
            _hhmm_to_seconds(_timer_config["p_time"]), _timer_config["p_sec"], now_epoch
        )
        if pump_should_be_on != bool(_pump_state):
            _pump_state = 1 if pump_should_be_on else 0
            _pump_pin.value(_pump_state)

    # LED: 手動オーバーライド中はタイマー判定をスキップする
    if not _led_manual_override:
        led_should_be_on = _is_in_timer_window(
            _hhmm_to_seconds(_timer_config["l_time"]), _timer_config["l_min"] * 60, now_epoch
        )
        if led_should_be_on != bool(_led_state):
            _led_state = 1 if led_should_be_on else 0
            _led_pin.value(_led_state)

