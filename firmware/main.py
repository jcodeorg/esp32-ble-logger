import time
import struct
import bluetooth
import machine
import gc
from machine import Pin, SoftI2C, RTC
import ssd1306  # OLEDディスプレイ用（I2C用）

# ==========================================
# 1. 設定・初期化
# ==========================================

# 使用するデバイス構成。書き込み対象のハードウェアに合わせて "soil_cds" / "pump_led" を選択する

# ACTIVE_PROFILE = "soil_cds"
ACTIVE_PROFILE = "pump_led"

if ACTIVE_PROFILE == "pump_led":
    from sensors_actuators import (
        DEVICE_TYPE, CSV_FIELDS, init_sensors, read_sensor, init_actuators, tick_actuators,
        handle_actuator_command,
    )
else:
    from sensors import (
        DEVICE_TYPE, CSV_FIELDS, init_sensors, read_sensor, init_actuators, tick_actuators,
        handle_actuator_command,
    )

# 測定間隔（秒）: 1時間 = 3600秒 (テスト時は短くしてください)
MEASURE_INTERVAL = 3 # 3600 
last_measure_tick = 0

# RAM上に保持するログの上限件数（超過分は古い方から破棄してメモリ枯渇を防ぐ）
MAX_LOG_ENTRIES = 5000

# ウォッチドッグタイマー: この5分以内にfeed()されないとデバイスを自動リセットする
# ESP32のWDTは一度起動すると停止できないため、このフラグファイルがある間はThonnyでの
# メンテナンス作業がリセットで妨げられないよう、起動をスキップする
WDT_TIMEOUT_MS = 300000
MAINTENANCE_FLAG_FILE = "maintenance.flag"

def _is_maintenance_mode():
    try:
        import os
        os.stat(MAINTENANCE_FLAG_FILE)
        return True
    except OSError:
        return False

wdt = None
if _is_maintenance_mode():
    print("[INFO] {} を検知したためメンテナンスモードで起動します（WDT無効）".format(MAINTENANCE_FLAG_FILE))
else:
    try:
        wdt = machine.WDT(timeout=WDT_TIMEOUT_MS)
    except Exception as e:
        print("[WARN] WDT初期化失敗（このボードでは未対応の可能性）:", e)
        wdt = None

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

# BLE経由でRTCが同期されるまではログを蓄積しない
rtc_synced = False

# BLE中央側（ブラウザ）が現在接続中か
ble_connected = False

# OLEDに一時的に表示するメッセージ（BLE割り込み内ではI2Cセンサーを右接阀しないため、メインループで反映する）
oled_flash_msg = None
oled_flash_until = 0

# BLEデバイス名（BLEUARTServer初期化後にセットされる）
ble_device_name = ""

# アクティブなプロファイルのセンサーを初期化する
init_sensors(i2c)

def get_formatted_time():
    t = rtc.datetime()
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(t[0], t[1], t[2], t[4], t[5], t[6])

def epoch_to_str(epoch):
    """time.time()と同じ基準のepoch秒を文字列に変換する（RTC未同期時はダミー日時になる）"""
    t = time.localtime(epoch)
    return "{:04d}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(t[0], t[1], t[2], t[3], t[4], t[5])

def update_oled(sensor_data, status_msg=None):
    if not display:
        return
    if status_msg is None:
        status_msg = "Running" if rtc_synced else "No RTC!"
    display.fill(0)
    
    # 画面表示のレイアウト
    dy = 9
    # 接続中はデバイス名を反転表示、切断中は通常表示
    if ble_connected:
        display.fill_rect(0, dy*0, 128, dy, 1)
        display.text(f"{ble_device_name}", 0, dy*0, 0)
    else:
        display.text(f"{ble_device_name}", 0, dy*0, 1)
    display.text(f"Time : {get_formatted_time().split()[1]}", 0, dy*1, 1)

    # CSV_FIELDSの並び順でセンサー値を表示する（画面の行数上限に収まる分だけ）
    max_data_lines = 4
    line = 2
    for key in CSV_FIELDS[:max_data_lines]:
        display.text(f"{key}: {sensor_data.get(key)}", 0, dy*line, 1)
        line += 1

    display.text(f"Log:{len(log_buffer)} {status_msg}", 0, dy*6, 1)
    
    display.show()

# ==========================================
# 2. BLE (Bluetooth Low Energy) 設定
# ==========================================
class BLEUARTServer:
    def __init__(self, name=None):
        self._ble = bluetooth.BLE()
        self._ble.active(True)
        self._ble.irq(self._irq)
        # 既定のMTU(23バイト)だとCSV1行が収まらず分割/失敗するため、大きめのMTUを要求する
        try:
            self._ble.config(mtu=200)
        except Exception as e:
            print("[WARN] MTU設定失敗:", e)

        if name is None:
            mac = self._ble.config('mac')[1]  # 6バイトのMACアドレス
            name = DEVICE_TYPE + "-" + self.get_friendly_name(mac)

        # Nordic UART Service の UUID（RX=書き込み用, TX=通知用。ブラウザ側と合わせる）
        self.UART_UUID = bluetooth.UUID("6e400001-b5a3-f393-e0a9-e50e24dcca9e")
        self.RX_UUID = bluetooth.UUID("6e400002-b5a3-f393-e0a9-e50e24dcca9e")
        self.TX_UUID = bluetooth.UUID("6e400003-b5a3-f393-e0a9-e50e24dcca9e")
        
        TRANSPORT_SERVICE = (
            self.UART_UUID,
            (
                (self.RX_UUID, bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE),
                (self.TX_UUID, bluetooth.FLAG_NOTIFY),
            ),
        )
        
        SERVICES = (TRANSPORT_SERVICE,)
        ((self._rx_handle, self._tx_handle),) = self._ble.gatts_register_services(SERVICES)
        # デフォルトの受信バッファ(約20バイト)だとTIME:コマンドが途中で切れるため拡張する
        self._ble.gatts_set_buffer(self._rx_handle, 200, False)
        # CSV1行分の通知データ（60〜100バイト程度）が既定バッファ(約20バイト)を超えてENOMEMになるため拡張する
        self._ble.gatts_set_buffer(self._tx_handle, 250, False)
        
        self._conn_handle = None
        self._name = name
        # GET_LOGは重い送信処理のためBLE割り込み内では実行せず、フラグを立ててメインループに処理させる
        self.get_log_requested = False
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
        # BLEスタックのコールバック内で例外を漏らすとBLE自体が不安定になるため必ず捕捉する
        global ble_connected
        try:
            if event == 1: # 接続
                self._conn_handle, _, _ = data
                ble_connected = True
                print("[BLE] 接続されました")
            elif event == 2: # 切断
                self._conn_handle = None
                ble_connected = False
                print("[BLE] 切断されました - タイマー自動制御に復帰します")
                # 手動オーバーライドを解除し、アクチュエータをタイマー判定に委ねる
                try:
                    handle_actuator_command("RESET_OVERRIDE")
                except Exception as e:
                    print("[ERROR] アクチュエータオーバーライド解除中に例外発生:", e)
                self._advertise(self._name)
            elif event == 3: # データ受信 (Chromebookからの書き込み)
                conn_handle, value_handle = data
                if value_handle == self._rx_handle:
                    packet = self._ble.gatts_read(self._rx_handle).decode('utf-8')
                    self.handle_command(packet.strip())
        except Exception as e:
            print("[ERROR] BLE割り込み処理中に例外発生:", e)

    def send(self, data):
        # BLEスタックの送信バッファが一時的に枯渇（ENOMEM）しても少し待ってリトライする
        max_retries = 8
        for attempt in range(max_retries):
            try:
                if self._conn_handle is None:
                    return False
                self._ble.gatts_notify(self._conn_handle, self._tx_handle, data)
                return True
            except OSError:
                time.sleep(0.2)
            except Exception as e:
                print("[ERROR] BLE送信失敗:", e)
                return False
        print("[ERROR] BLE送信失敗（バッファ枯渇のためリトライ上限に到達）")
        return False

    def _advertise(self, name):
        adv_data = bytearray(b'\x02\x01\x06') + bytearray((len(name) + 1, 0x09)) + name.encode()
        self._ble.gap_advertise(100, adv_data)

    def handle_command(self, cmd):
        global log_buffer, rtc_synced, oled_flash_msg, oled_flash_until
        print(f"[CMD 受信] {cmd}")
        
        try:
            if cmd.startswith("TIME:"):
                # 時刻同期コマンド: TIME:[2026,8,17,10,30,0]
                try:
                    import json
                    time_arr = json.loads(cmd[5:])
                    if len(time_arr) < 6:
                        raise ValueError("time_arr too short: {}".format(time_arr))
                    # 同期前のRTC(未補正)epochを記録し、正しいepochとの差分を未同期時の蓄積データ補正に使う
                    old_epoch = time.time()
                    new_epoch = time.mktime((time_arr[0], time_arr[1], time_arr[2], time_arr[3], time_arr[4], time_arr[5], 0, 0))
                    delta = new_epoch - old_epoch

                    # (年, 月, 日, 曜日(0-6), 時, 分, 秒, サブ秒)
                    # MicroPythonの曜日計算はざっくりでOKなので0を入れる
                    rtc.datetime((time_arr[0], time_arr[1], time_arr[2], 0, time_arr[3], time_arr[4], time_arr[5], 0))
                    rtc_synced = True

                    # 未同期の間に蓄積したログのタイムスタンプを補正する
                    corrected = 0
                    for row in log_buffer:
                        if not row["synced"]:
                            row["epoch"] += delta
                            row["synced"] = True
                            corrected += 1
                    print("[INFO] 時刻を同期しました:", get_formatted_time(), "（過去ログ{}件のタイムスタンプを補正）".format(corrected))
                except Exception as e:
                    print("[ERROR] 時刻同期失敗:", e, "受信文字列:", cmd)
                    
            elif cmd == "GET_LOG":
                # 重い送信ループはBLE割り込み内で実行しない（BLEスタック自体をブロックしENOMEMが固定化するため）
                # フラグを立てるだけにとどめ、実際の送信はメインループの process_pending() で行う
                self.get_log_requested = True
                print("[INFO] GET_LOGを受け付けました。メインループで送信します")
                
            elif cmd == "CLEAR_LOG":
                # データクリアコマンド（I2Cセンサー読み取りなどの重い処理はBLE割り込み内では行わない）
                log_buffer.clear()
                print("[INFO] RAM上のログをクリアしました")
                self.send("OK_CLEARED\n")
                oled_flash_msg = "Cleared!"
                oled_flash_until = time.time() + 2

            elif cmd.startswith("SET_TIMER:"):
                # タイマー設定コマンド: SET_TIMER:{"p_time":"08:00","p_sec":30,"l_time":"06:00","l_min":720}
                try:
                    import json
                    config = json.loads(cmd[len("SET_TIMER:"):])
                    handle_actuator_command("SET_TIMER", config)
                    self.send("OK_TIMER_SET\n")
                except Exception as e:
                    print("[ERROR] タイマー設定失敗:", e, "受信文字列:", cmd)

            elif cmd.startswith("PUMP:"):
                # 手動操作コマンド: PUMP:1 / PUMP:0
                handle_actuator_command("PUMP", cmd[len("PUMP:"):] == "1")
                self.send("OK_MANUAL\n")

            elif cmd.startswith("LED:"):
                # 手動操作コマンド: LED:1 / LED:0
                handle_actuator_command("LED", cmd[len("LED:"):] == "1")
                self.send("OK_MANUAL\n")
        except Exception as e:
            print("[ERROR] コマンド処理中に例外発生:", e)

    def send_log_data(self):
        """GET_LOGの実際の送信処理。重いループなので必ずメインループから呼ぶこと（BLE割り込み内では呼ばない）"""
        print("[INFO] ログデータを送信します...")
        try:
            # CSVヘッダーはアクティブなプロファイルのCSV_FIELDSに合わせて動的に生成する
            header = "timestamp," + ",".join(CSV_FIELDS) + ",device\n"
            self.send(header)
            time.sleep(0.1)

            for row_index, row in enumerate(log_buffer):
                values = ",".join(str(row[key]) for key in CSV_FIELDS)
                line = "{},{},{}\n".format(epoch_to_str(row["epoch"]), values, ble_device_name)
                self.send(line)
                time.sleep(0.05) # パケットあふれ防止のウェイト
                # 送信中のGC発生によるタイミング不安定を避けるため、定期的にメモリを回収して断片化を抑える
                if row_index % 20 == 0:
                    gc.collect()
                    # 送信件数が多いとこのループだけでWDTタイムアウトを超えるため、ここでもfeedする
                    if wdt:
                        wdt.feed()
            # ブラウザ側が固定タイムアウトではなく完了を検知できるようにマーカーを送る
            self.send("END_LOG\n")
            print("[INFO] 送信完了")
        except Exception as e:
            print("[ERROR] ログ送信中に例外発生:", e)

    def process_pending(self):
        """BLE割り込みで立てられたフラグをメインループから処理する"""
        if self.get_log_requested:
            self.get_log_requested = False
            self.send_log_data()

# ==========================================
# 3. メインループ
# ==========================================
def main():
    global last_measure_tick, ble_device_name
    print("ESP32 環境ロガー起動")

    # BLE初期化に失敗しても諦めずにリトライする（センサー計測自体は継続できるようにする）
    ble_server = None
    while ble_server is None:
        try:
            ble_server = BLEUARTServer()
            ble_device_name = ble_server._name
        except Exception as e:
            print("[ERROR] BLE初期化失敗。5秒後にリトライします:", e)
            time.sleep(5)

    # アクティブなプロファイルのアクチュエータを初期化する（このプロファイルに無ければ何もしない）
    try:
        init_actuators()
    except Exception as e:
        print("[ERROR] アクチュエータ初期化失敗:", e)

    # 起動直後の初期値取得
    sensor_data = {key: 0 for key in CSV_FIELDS}
    try:
        sensor_data = read_sensor(i2c)
    except Exception as e:
        print("[ERROR] 起動時センサー読み取り失敗:", e)
    update_oled(sensor_data, "Started")

    while True:
        if wdt:
            wdt.feed()

        current_tick = time.time()

        # BLE割り込みで受け付けたリクエスト（GET_LOGなど）をここで処理する
        try:
            ble_server.process_pending()
        except Exception as e:
            print("[ERROR] 保留中のBLEリクエスト処理中に例外発生:", e)

        # アクチュエータのタイマー制御（毎秒。失敗してもループ自体は継続する）
        try:
            tick_actuators(current_tick)
        except Exception as e:
            print("[ERROR] アクチュエータ制御中に例外発生:", e)

        # 1時間（MEASURE_INTERVAL）ごとの計測（センサー障害やメモリ不足があっても稼働を継続する）
        if current_tick - last_measure_tick >= MEASURE_INTERVAL or last_measure_tick == 0:
            try:
                sensor_data = read_sensor(i2c)

                # RTC未同期でも蓄積し、epochとsynced状態を記録しておく（同期時に過去分のepochを補正する）
                row = {"epoch": time.time(), "synced": rtc_synced}
                row.update(sensor_data)
                log_buffer.append(row)

                # 上限を超えたら古いデータから破棄してメモリ枯渇を防ぐ
                if len(log_buffer) > MAX_LOG_ENTRIES:
                    del log_buffer[0 : len(log_buffer) - MAX_LOG_ENTRIES]

                status = "" if rtc_synced else "（RTC未同期、後で補正されます）"
                values_str = ", ".join(f"{key}: {sensor_data[key]}" for key in CSV_FIELDS)
                print(f"[計測] {epoch_to_str(row['epoch'])} - {values_str} (累計: {len(log_buffer)}件){status}")

                last_measure_tick = current_tick
            except Exception as e:
                print("[ERROR] 計測・蓄積処理中に例外発生:", e)
                last_measure_tick = current_tick

        # OLEDの画面更新（毎秒。失敗してもループ自体は継続する）
        try:
            sensor_data = read_sensor(i2c)
            flash = oled_flash_msg if oled_flash_msg and time.time() < oled_flash_until else None
            update_oled(sensor_data, flash)
        except Exception as e:
            print("[ERROR] OLED更新中に例外発生:", e)

        time.sleep(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("プログラムを停止しました。")
    except Exception as e:
        # 想定外の致命的な例外はソフトリセットして復旧する（WDTがあればそちらでも救済される）
        print("[FATAL] 想定外のエラーが発生したため再起動します:", e)
        time.sleep(2)
        machine.reset()