import time
import struct
import bluetooth
from machine import Pin, SoftI2C, RTC, ADC
import ssd1306  # OLEDディスプレイ用（I2C用）
from ahtx0 import AHT20

# ==========================================
# 1. 設定・初期化
# ==========================================
# I2Cピンの定義（お使いのボードに合わせてSCL/SDAを変更してください。例: SCL=23, SDA=22）
i2c = SoftI2C(scl=Pin(23), sda=Pin(22))

# OLEDディスプレイの初期化 (128x64解像度を想定)
try:
    display = ssd1306.SSD1306_I2C(128, 64, i2c)
except Exception as e:
    print("OLED初期化エラー:", e)
    display = None

rtc = RTC()

# RAM上のログバッファ（ここに1時間ごとのデータを蓄積）
log_buffer = []

# BLEデバイス名（BLEUARTServer初期化後にセットされる）
ble_device_name = ""

# 測定間隔（秒）: 1時間 = 3600秒 (テスト時は短くしてください)
MEASURE_INTERVAL = 3600 
last_measure_tick = 0


# A1: 土壌水分センサ用 ADC を初期化する
adc_soil = ADC(Pin(1, Pin.IN))
adc_soil.atten(ADC.ATTN_11DB)   # 0〜3.3V の範囲を読む
adc_soil.width(ADC.WIDTH_12BIT) # 分解能 12 ビット（0〜4095）

# A2: CdS 照度センサ用 ADC を初期化する
adc_cds = ADC(Pin(2, Pin.IN))
adc_cds.atten(ADC.ATTN_11DB)
adc_cds.width(ADC.WIDTH_12BIT)

# センサーの初期化（ダミー関数。実際のセンサーに合わせて書き換えてください）
def read_sensor():
    """AHT20センサーから温湿度を取得する"""

    try:
        aht20 = AHT20(i2c)
        temp = round(aht20.temperature, 1)
        humi = round(aht20.relative_humidity, 1)
    except Exception as e:
        print("AHT20 error:", e)
        temp = 0.0
        humi = 0.0
    soil = adc_soil.read()
    ligh = adc_cds.read()
    return temp, humi, soil, ligh

def get_formatted_time():
    t = rtc.datetime()
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(t[0], t[1], t[2], t[4], t[5], t[6])

def update_oled(temp, hum, soil, ligh, status_msg="Running"):
    if not display:
        return
    display.fill(0)
    
    # 画面表示のレイアウト
    display.text(f"BLE: {ble_device_name}", 0, 0, 1)
    display.text(f"Time: {get_formatted_time().split()[1]}", 0, 11, 1)
    display.text(f"Temp: {temp:.1f} C", 0, 22, 1)
    display.text(f"Hum : {hum:.1f} %", 0, 33, 1)
    display.text(f"Soil: {soil}", 0, 44, 1)
    display.text(f"Ligh: {ligh}", 0, 55, 1)
    
    display.show()

# ==========================================
# 2. BLE (Bluetooth Low Energy) 設定
# ==========================================
class BLEUARTServer:
    def __init__(self, name=None):
        self._ble = bluetooth.BLE()
        self._ble.active(True)
        self._ble.irq(self._irq)

        if name is None:
            mac = self._ble.config('mac')[1]  # 6バイトのMACアドレス
            name = "EnvLog-" + self.get_friendly_name(mac)

        # Nordic UART Service の UUID
        self.UART_UUID = bluetooth.UUID("6e400001-b5a3-f393-e0a9-e50e24dcca9e")
        self.TX_UUID = bluetooth.UUID("6e400002-b5a3-f393-e0a9-e50e24dcca9e")
        self.RX_UUID = bluetooth.UUID("6e400003-b5a3-f393-e0a9-e50e24dcca9e")
        
        TRANSPORT_SERVICE = (
            self.UART_UUID,
            (
                (self.RX_UUID, bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE),
                (self.TX_UUID, bluetooth.FLAG_NOTIFY),
            ),
        )
        
        SERVICES = (TRANSPORT_SERVICE,)
        ((self._tx_handle, self._rx_handle),) = self._ble.gatts_register_services(SERVICES)
        
        self._conn_handle = None
        self._name = name
        self._advertise(name)

    def get_friendly_name(self, unique_id):
        """ユニークIDからフレンドリー名を生成"""
        length = 5
        letters = 5
        codebook = [
            ['z', 'v', 'g', 'p', 't'],
            ['u', 'o', 'i', 'e', 'a'],
            ['z', 'v', 'g', 'p', 't'],
            ['u', 'o', 'i', 'e', 'a'],
            ['z', 'v', 'g', 'p', 't']
        ]
        name = []
        mac_padded = b'\x00\x00' + unique_id
        _, n = struct.unpack('>II', mac_padded)
        ld = 1
        d = letters

        for i in range(0, length):
            h = (n % d) // ld
            n -= h
            d *= letters
            ld *= letters
            name.insert(0, codebook[i][h])

        return "".join(name)

    def _irq(self, event, data):
        if event == 1: # 接続
            self._conn_handle, _, _ = data
            print("[BLE] 接続されました")
        elif event == 2: # 切断
            self._conn_handle = None
            print("[BLE] 切断されました")
            self._advertise(self._name)
        elif event == 3: # データ受信 (Chromebookからの書き込み)
            conn_handle, value_handle = data
            if value_handle == self._rx_handle:
                packet = self._ble.gatts_read(self._rx_handle).decode('utf-8')
                self.handle_command(packet.strip())

    def send(self, data):
        if self._conn_handle is not None:
            self._ble.gatts_notify(self._conn_handle, self._tx_handle, data)

    def _advertise(self, name):
        adv_data = bytearray(b'\x02\x01\x06') + bytearray((len(name) + 1, 0x09)) + name.encode()
        self._ble.gap_advertise(100, adv_data)

    def handle_command(self, cmd):
        global log_buffer
        print(f"[CMD 受信] {cmd}")
        
        if cmd.startswith("TIME:"):
            # 時刻同期コマンド: TIME:[2026,8,17,10,30,0]
            try:
                import json
                time_arr = json.loads(cmd[5:])
                # (年, 月, 日, 曜日(0-6), 時, 分, 秒, サブ秒)
                # MicroPythonの曜日計算はざっくりでOKなので0を入れる
                rtc.datetime((time_arr[0], time_arr[1], time_arr[2], 0, time_arr[3], time_arr[4], time_arr[5], 0))
                print("[INFO] 時刻を同期しました:", get_formatted_time())
            except Exception as e:
                print("[ERROR] 時刻同期失敗:", e)
                
        elif cmd == "GET_LOG":
            # ログデータ一括送信
            print("[INFO] ログデータを送信します...")
            # CSVヘッダーとデータをまとめて送信
            header = "timestamp,temperature,humidity\n"
            self.send(header)
            time.sleep(0.1)
            
            for row in log_buffer:
                self.send(row)
                time.sleep(0.05) # パケットあふれ防止のウェイト
            print("[INFO] 送信完了")
            
        elif cmd == "CLEAR_LOG":
            # データクリアコマンド
            log_buffer.clear()
            print("[INFO] RAM上のログをクリアしました")
            self.send("OK_CLEARED\n")

# ==========================================
# 3. メインループ
# ==========================================
def main():
    global last_measure_tick, ble_device_name
    print("ESP32 環境ロガー起動")
    
    ble_server = BLEUARTServer()
    ble_device_name = ble_server._name
    
    # 起動直後の初期値取得
    temp, hum, soil, ligh = read_sensor()
    update_oled(temp, hum, soil, ligh, "Started")
    
    while True:
        current_tick = time.time()
        
        # 1時間（MEASURE_INTERVAL）ごとの計測
        if current_tick - last_measure_tick >= MEASURE_INTERVAL or last_measure_tick == 0:
            temp, hum, soil, ligh = read_sensor()
            timestamp = get_formatted_time()
            
            # RAMに蓄積
            row = f"{timestamp},{temp},{hum}\n"
            log_buffer.append(row)
            
            last_measure_tick = current_tick
            print(f"[計測] {timestamp} - Temp: {temp}C, Hum: {hum}% (累計: {len(log_buffer)}件)")
            
        # OLEDの画面更新（毎秒）
        update_oled(temp, hum, soil, ligh)
        time.sleep(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("プログラムを停止しました。")