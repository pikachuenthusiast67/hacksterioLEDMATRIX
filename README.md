# W55MH32 LED Matrix Streaming and Blynk Controls

This project has two separate communication paths:

1. **Python UDP LED streaming:** `streamer.py` runs on a computer and sends 32x8 LED frames to `UDP.py` running on the W55MH32 board.
2. **Blynk MQTT controls:** `streamer.py` also connects to Blynk over MQTT and applies animation, text, speed, and brightness changes.

The embedded C MQTT example in `MQTT/do_mqtt.c` is a separate Adafruit IO MQTTS application. It does **not** use the Blynk topics used by `streamer.py` and is not required for the Python LED streamer.

## System overview

```text
Blynk dashboard
      │ MQTT
      ▼
Computer running streamer.py
      │ UDP 6454: LED frames
      │ UDP 6455: encoder telemetry back-channel
      ▼
W55MH32 board running UDP.py
      │
      ▼
32 x 8 WS2812 LED matrix
```

The computer and board must be reachable on the same network, or the network must route UDP traffic between them.

## Requirements

### Computer

- Python 3
- A network connection to the W55MH32 board
- The Paho MQTT client:

```powershell
python -m pip install paho-mqtt
```

If Windows has multiple Python installations, use the same Python executable for installation and execution, for example:

```powershell
py -3 -m pip install paho-mqtt
```

### W55MH32 board

- W55MH32 board with MicroPython firmware that provides:
  - `machine`
  - `network.WIZNET5K`
  - `machine.bitstream()`
- W5500 Ethernet hardware
- 32x8 WS2812-compatible LED matrix
- `UDP.py` copied to the board as its startup/main script

The board-side hardware configuration in `UDP.py` is:

| Function | Pin or setting |
| --- | --- |
| WS2812 data | `PC13` |
| W5500 SPI | SPI2 at 8 MHz |
| W5500 chip select | `PB12` |
| W5500 reset | `PD9` |
| Rotary encoder A/B | `PA2` / `PA1` |
| LED power MOSFET | `PD4`, active high |
| Button | `PA8`, active low |
| Matrix | 32 columns x 8 rows, 256 pixels |

The board obtains its IP address through DHCP. After booting, read the board IP from the serial output or the optional OLED display.

## 1. Configure the board IP in `streamer.py`

Open `streamer.py` and either change the default address:

```python
BOARD_IP = os.getenv("LED_BOARD_IP", "10.0.1.214")
```

or set the `LED_BOARD_IP` environment variable before starting the streamer:

```powershell
$env:LED_BOARD_IP = "10.0.1.214"
```

Replace `10.0.1.214` with the actual DHCP address printed by the board. The address in the source is only a default and may not match the board's current lease.

The streamer sends LED frames to UDP port **6454** and listens for board telemetry on UDP port **6455**. Allow both UDP ports through the computer's firewall if necessary.

## 2. Start `UDP.py` on the board

`UDP.py` executes immediately when loaded; it does not have a normal Python `main()` function or command-line interface.

Copy/upload `UDP.py` to the board using the MicroPython tool appropriate for the installed firmware, and run it as the board's main/startup script. The repository does not include a board upload utility or serial-port setting, so the exact upload command depends on the MicroPython loader being used.

A successful startup should show messages similar to:

```text
[BOOT] Starting controller
[NET] Requesting IP from DHCP server...
[NET] DHCP success. Board IP: <board-ip>
[UDP] Listening on port 6454
```

If DHCP fails, check the Ethernet cable, switch, DHCP server, and W5500 wiring. The script waits up to 10 seconds for a DHCP lease.

## 3. Start `streamer.py` on the computer

From the repository root, run:

```powershell
python streamer.py
```

or:

```powershell
py -3 streamer.py
```

The script starts these services:

- UDP LED frame transmission at approximately 60 FPS
- UDP telemetry listener on port 6455
- NTP synchronization for the clock animation
- Blynk MQTT listener, when enabled

The initial animation state is:

| Setting | Initial value |
| --- | --- |
| Animation | `plasma` |
| Text | `HELLO` |
| Speed | `1.0` |
| Brightness | `0.4` |

Stop the streamer with `Ctrl+C`.

### Optional environment settings

These settings can be supplied in PowerShell without editing the script:

```powershell
$env:LED_BOARD_IP = "10.0.1.214"
$env:LED_MQTT_ENABLED = "1"
$env:LED_MQTT_HOST = "sgp1.blynk.cloud"
$env:LED_MQTT_PORT = "1883"
$env:LED_NTP_ENABLED = "1"
python streamer.py
```

Set `LED_MQTT_ENABLED` to `0`, `false`, or `no` to run UDP streaming without Blynk:

```powershell
$env:LED_MQTT_ENABLED = "0"
python streamer.py
```

## 4. Set up Blynk for MQTT controls

`streamer.py` expects a Blynk template/device using these datastream virtual pins:

| Virtual pin | Suggested datastream name | Type | Purpose | Accepted values |
| --- | --- | --- | --- | --- |
| `V0` | Animation | String or enumerable | Select animation | `plasma`, `rainbow`, `waves`, `checker`, `solid`, `clock`, `text` |
| `V1` | Text / LED Text | String | Text animation content | Characters supported by the built-in font; up to 64 characters |
| `V2` | Animation Speed | Double or integer | Animation speed | Clamped to `0.2` through `3.0` |
| `V3` | Brightness | Double or integer | LED brightness | Clamped to `0.0` through `1.0` |

### Create the Blynk template/device

1. Sign in to Blynk Console.
2. Create or open the template used by the device.
3. Add the four datastreams above using virtual pins `V0`, `V1`, `V2`, and `V3`.
4. Add widgets to the web or mobile dashboard:
   - A selector/dropdown for `V0`.
   - A text input for `V1`.
   - A slider for `V2`, with a range such as `0.2` to `3.0`.
   - A slider for `V3`, with a range of `0` to `1`.
5. Create a Blynk device from the template.
6. Copy the device/template ID and authentication token from Blynk Device Info.

### Configure credentials in `streamer.py`

The script currently contains these settings:

```python
MQTT_HOST = os.getenv("LED_MQTT_HOST", "sgp1.blynk.cloud")
MQTT_PORT = int(os.getenv("LED_MQTT_PORT", "1883"))
MQTT_CLIENT_ID = "TMPL6VEugAJ7i"
MQTT_USERNAME = "device"
MQTT_PASSWORD = "<BLYNK_AUTH_TOKEN>"
```

Replace `MQTT_CLIENT_ID` and `MQTT_PASSWORD` with the values for your Blynk device. The username must remain `device` for this Blynk MQTT connection.

The repository currently contains credentials in source files. Treat those credentials as exposed: revoke or rotate them in Blynk before using this project publicly, and avoid committing replacement tokens to source control.

The Python implementation uses unencrypted MQTT on TCP port `1883`. If your Blynk setup requires a different broker or port, set `LED_MQTT_HOST` and `LED_MQTT_PORT` accordingly and confirm that the broker supports the connection settings used by the script.

### Blynk MQTT topics used by the script

On connection, `streamer.py` subscribes to:

```text
downlink/ds/+
downlink/ds/#
```

Blynk updates are recognized by the following topic suffixes:

```text
V0       Animation
V1       Text, LED Text, or text
V2       Animation Speed or speed
V3       Brightness or brightness
```

The script publishes initial values to:

```text
ds/V0
ds/V2
ds/V3
```

When the connection succeeds, the terminal should report a Blynk connection and a heartbeat sent to these datastreams.

## Optional Blynk connection test

`streamer2.py` is only a basic MQTT connectivity test. It does not stream LEDs, subscribe to control topics, or publish datastream values.

Run it with:

```powershell
python streamer2.py
```

It uses `blynk.cloud:1883`, while `streamer.py` defaults to `sgp1.blynk.cloud:1883`. Use `streamer.py` for the complete UDP and Blynk control workflow.

## UDP protocol details

Each LED frame sent by `streamer.py` is 772 bytes:

- Bytes 0-1: `AA 55` magic header
- Bytes 2-3: unsigned 16-bit frame ID, big-endian
- Remaining 768 bytes: 256 RGB pixels in row-major order, 32 pixels wide by 8 pixels high

The board rejects malformed, duplicate, stale, and out-of-order frames. It counts forward frame gaps as dropped frames.

The board sends encoder telemetry to the first computer that sends a valid frame. The five-byte packet is:

```text
!2sBBH
```

This contains `CT`, the encoder value from 0 to 100, a reserved byte, and the dropped-frame count. The board learns the computer IP only from the first valid frame; reboot the board if the sender computer changes.

## Troubleshooting

### No LEDs or no UDP connection

- Confirm `UDP.py` is running on the board.
- Confirm the board's current DHCP address and set `LED_BOARD_IP` to that address.
- Confirm the computer and board are on the same routed network.
- Check that UDP `6454` is allowed through the computer firewall.
- Check the board serial output for W5500, DHCP, or socket errors.
- Confirm the LED matrix power MOSFET and WS2812 data wiring match the pin definitions in `UDP.py`.

### Blynk connects but controls do not work

- Confirm the Blynk device uses virtual datastreams `V0` through `V3`.
- Send the exact animation names listed in the datastream table.
- Confirm the Blynk token and client/template ID in `streamer.py`.
- Confirm that `LED_MQTT_ENABLED` is not set to `0`.
- Look for `[RAW MQTT]` messages in the `streamer.py` terminal to verify the topic and payload being received.

### MQTT package errors

Install Paho MQTT into the Python environment used to run the script:

```powershell
python -m pip install paho-mqtt
```

If Paho cannot be imported, UDP streaming can still start, but Blynk controls will be unavailable.

### Frame drops

The board reports frame statistics periodically. Frame drops can be caused by network congestion, an overloaded MicroPython loop, slow WS2812 output, or a computer firewall/network path problem. Keep the computer and board on a low-latency wired network where possible.

## Separate embedded C MQTT example

`MQTT/do_mqtt.c` and `main.c` implement a different application:

- Broker: Adafruit IO (`io.adafruit.com`)
- Default mode: MQTTS on port `8883`
- Topic: `Mr_huang1220/groups/w55mh32-evb`
- Payloads: JSON containing `led`, `beep`, `humidity`, and `temperature`

This C application is not part of the `UDP.py` + `streamer.py` Blynk workflow. It requires the separate embedded build and flash process, which is not included in this repository.
