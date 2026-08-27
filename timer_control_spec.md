# アクチュエータタイマー制御 仕様書（PcrIoT） - Rev 1.2

## 1. 概要
本仕様は、Web UI（ブラウザ）からBLE通信を介してMicroPythonデバイス（デバイスタイプ: `PcrIoT`）に接続し、アクチュエータ（ポンプおよびLEDライト）の「手動操作」および「タイマー制御」を行うための仕様を定める。

---

## 2. 画面・UI仕様

### 2.1 画面表示条件
* ブラウザUIは、接続されたデバイスの `DEVICE_TYPE` を識別して動的に切り替わる。
* `DEVICE_TYPE == "PcrIoT"` かつ **BLE接続が確立されている場合**、アクチュエータ制御画面（タイマー設定・手動操作）を表示する。

### 2.2 画面構成要素
1. **手動操作エリア**
   * ポンプ：ON / OFF 切替ボタン
   * LEDライト：ON / OFF 切替ボタン
   * *※BLE接続中は手動操作が最優先され、実行中のタイマー動作を一時中断/上書きできる。*
2. **タイマー設定エリア**
   * **ポンプタイマー**
     * 稼働開始時刻（HH:MM）
     * 稼働時間（秒。0を指定した場合は自動稼働しない）
   * **LEDライトタイマー**
     * 稼働開始時刻（HH:MM）
     * 稼働時間（hh:mm形式。内部的には分単位で保持し、0:00を指定した場合は自動点灯しない）
   * 設定保存・送信ボタン

---

## 3. 制御・優先度仕様

### 3.1 制御優先度（接続状態に応じた優先度の切り替え）

| 状態 | 優先度 | 動作挙動 |
| :--- | :--- | :--- |
| **BLE接続中** | **手動操作 優先** | 手動でON/OFFボタンが押された場合、タイマーの稼働状態にかかわらず即座に出力を書き換える（手動オーバーライド状態）。 |
| **BLE切断時** | **タイマー制御 復帰** | **BLEが切断された瞬間、手動オーバーライド状態を解除する。** その時点の現在時刻がタイマー稼働時間内であればタイマー動作を再開し、時間外であれば通常のタイマー待機状態（OFF）に戻る。 |

### 3.2 データ同期の基本原則
設定値のマスターデータは「ユーザーのブラウザ操作」とし、**ローカルストレージ（ブラウザ）** と **デバイス（MicroPython）** の両方に同期・保持させる。

### 3.3 動作フロー

| イベント | ブラウザUIの挙動 | デバイス（MicroPython）の挙動 |
| :--- | :--- | :--- |
| **BLE接続確立時** | ローカルストレージの設定値を読み出し、BLE経由で送信（自動再設定）。 | 受信した設定値を反映。BLE接続状態フラグをONにする。 |
| **手動操作実行時** | 手動制御コマンド（ON/OFF）をBLEで送信。 | 即座にGPIO出力を切り替え、手動フラグを有効化（タイマー判定を一時スルー）。 |
| **BLE切断発生時** | 切断状態をUIに反映。 | **手動フラグをクリア。現在時刻とタイマー設定を照合し、本来あるべき出力状態（ON/OFF）へ自動復帰。** |

---

## 4. データ構造・プロトコル仕様

### 4.1 ローカルストレージ保存形式
* **Key:** `PcrIoT_Config_{ble_device_name}`
* **Value (JSON):**

```json
{
  "pump": {
    "time": "08:00",
    "duration_sec": 30
  },
  "led": {
    "time": "06:00",
    "duration_min": 720
  }
}

```

※初期状態（未設定時）の既定値は `duration_sec: 0` / `duration_min: 0` とし、ポンプ・LEDとも自動稼働しないようにする。

### 4.2 BLE通信コマンドフォーマット

#### ① タイマー設定コマンド (`SET_TIMER`)

```text
SET_TIMER:{"p_time":"08:00","p_sec":30,"l_time":"06:00","l_min":720}

```

#### ② 手動操作コマンド (`CMD`)

| 操作 | 送信文字列 | デバイス動作 |
| --- | --- | --- |
| **ポンプ ON** | `PUMP:1` | ポンプ出力ON（手動フラグ有効） |
| **ポンプ OFF** | `PUMP:0` | ポンプ出力OFF（手動フラグ有効） |
| **LED ON** | `LED:1` | LED出力ON（手動フラグ有効） |
| **LED OFF** | `LED:0` | LED出力OFF（手動フラグ有効） |

---

## 5. MicroPython（デバイス側）実装ロジック

### 5.0 GPIOピン割り当て（`sensors_actuators.py`）

| アクチュエータ | ピン | 備考 |
| --- | --- | --- |
| ポンプ | `PUMP_PIN_NO = 0` | PWM対応ピンだが現状はON/OFF制御のみ |
| LED | `LED_PIN_NO = 2` | PWM対応ピンだが現状はON/OFF制御のみ |

### 5.1 公開インターフェース

初学者にも分かりやすいよう、`sensors_actuators.py` がmain.pyに公開する関数は次の3つのみとする（`SET_TIMER`/`PUMP`/`LED`/`RESET_OVERRIDE`は `handle_actuator_command()` に集約）。

| 関数 | 役割 |
| --- | --- |
| `init_actuators()` | 起動時に1回呼ばれ、ポンプ/LEDのGPIOを初期化する |
| `tick_actuators(now_epoch)` | メインループから毎秒呼ばれ、タイマー判定でON/OFFを更新する |
| `handle_actuator_command(command, payload)` | `"SET_TIMER"`(payload=dict) / `"PUMP"`(payload=bool) / `"LED"`(payload=bool) / `"RESET_OVERRIDE"` を引数で分岐して処理する |

### 5.2 切断検知とフラグ管理（`main.py` の BLE割り込み）

```python
# _irq 内の切断イベント処理
elif event == 2: # 切断
    self._conn_handle = None
    ble_connected = False
    print("[BLE] 切断されました - タイマー自動制御に復帰します")
    # アクチュエータの手動オーバーライド状態を解除する
    handle_actuator_command("RESET_OVERRIDE")

```

### 5.3 制御判定ロジック（`sensors_actuators.py` 等）

実装では `ble_connected` の参照は行わず、BLE切断イベントで `handle_actuator_command("RESET_OVERRIDE")` が呼ばれてオーバーライドが解除される前提で判定している。

```python
_pump_manual_override = False
_led_manual_override = False

def handle_actuator_command(command, payload=None):
    global _pump_manual_override, _led_manual_override

    if command == "SET_TIMER":
        # payload(dict) で _timer_config を更新する
        pass
    elif command == "PUMP":
        _pump_manual_override = True  # BLE接続中の手動優先フラグを立てる
        _pump_pin.value(1 if payload else 0)
    elif command == "LED":
        _led_manual_override = True
        _led_pin.value(1 if payload else 0)
    elif command == "RESET_OVERRIDE":
        # BLE切断時に手動優先フラグを解除し、タイマー判定に委ねる
        _pump_manual_override = False
        _led_manual_override = False

def _is_in_timer_window(start_seconds, duration_sec, now_epoch):
    """現在時刻がタイマー稼働時間内かどうかを判定する関数（日またぎにも対応）"""
    # RTCの現在時刻から本日の経過秒数を計算し、開始秒 <= 現在 < 開始秒+稼働秒 で True を返す
    pass

def tick_actuators(now_epoch):
    global _pump_state

    # 1. 手動操作された場合は、タイマー自動判定をスキップする（BLE切断時は自動的に解除済み）
    if _pump_manual_override:
        return

    # 2. 手動操作されていない場合はタイマーの本来の状態に同期する
    should_be_on = _is_in_timer_window(_hhmm_to_seconds(_timer_config["p_time"]), _timer_config["p_sec"], now_epoch)

    # 状態が変わっていれば出力を更新
    if _pump_pin.value() != should_be_on:
        _pump_pin.value(1 if should_be_on else 0)


```
