import datetime
import json
import math
import os
import socket
import struct
import threading
import time

# Optional dependency: install with `python -m pip install paho-mqtt`.
try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

# Must match the IP address shown by UDP.py after DHCP/static setup.
BOARD_IP = os.getenv("LED_BOARD_IP", "10.0.1.214")
STREAM_PORT = 6454
BACKCHANNEL_PORT = 6455

WIDTH = 32
HEIGHT = 8

# NTP keeps the host clock used by the clock animation accurate. Between NTP
# syncs, the operating-system clock (normally backed by the host RTC) is used.
NTP_ENABLED = os.getenv("LED_NTP_ENABLED", "1").lower() not in {"0", "false", "no"}
NTP_SERVER = os.getenv("LED_NTP_SERVER", "pool.ntp.org")
NTP_PORT = 123
NTP_TIMEOUT_SECONDS = float(os.getenv("LED_NTP_TIMEOUT", "3"))
NTP_SYNC_INTERVAL_SECONDS = float(os.getenv("LED_NTP_INTERVAL", "21600"))
ntp_offset_lock = threading.Lock()
ntp_offset_seconds = 0.0

# MQTT settings. Keep credentials in environment variables rather than source code.
MQTT_ENABLED = os.getenv("LED_MQTT_ENABLED", "1").lower() not in {"0", "false", "no"}
MQTT_HOST = os.getenv("LED_MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("LED_MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("LED_MQTT_TOPIC", "led-matrix/control")
MQTT_ANIMATION_TOPIC = os.getenv("LED_MQTT_ANIMATION_TOPIC", "")
MQTT_TEXT_TOPIC = os.getenv("LED_MQTT_TEXT_TOPIC", "")
MQTT_USERNAME = os.getenv("LED_MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("LED_MQTT_PASSWORD")

ANIMATIONS = ("plasma", "rainbow", "waves", "checker", "solid", "clock", "text")

# 5x7 font. Each character is represented by seven rows of five pixels.
FONT = {
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    ":": ("00000", "00100", "00100", "00000", "00100", "00100", "00000"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
}

# Add the alphabet using compact 5x7 bitmap strings.
for _letter, _rows in {
    "A":"01110 10001 10001 11111 10001 10001 10001", "B":"11110 10001 10001 11110 10001 10001 11110",
    "C":"01111 10000 10000 10000 10000 10000 01111", "D":"11110 10001 10001 10001 10001 10001 11110",
    "E":"11111 10000 10000 11110 10000 10000 11111", "F":"11111 10000 10000 11110 10000 10000 10000",
    "G":"01111 10000 10000 10111 10001 10001 01111", "H":"10001 10001 10001 11111 10001 10001 10001",
    "I":"11111 00100 00100 00100 00100 00100 11111", "J":"00111 00010 00010 00010 00010 10010 01100",
    "K":"10001 10010 10100 11000 10100 10010 10001", "L":"10000 10000 10000 10000 10000 10000 11111",
    "M":"10001 11011 10101 10101 10001 10001 10001", "N":"10001 11001 10101 10011 10001 10001 10001",
    "O":"01110 10001 10001 10001 10001 10001 01110", "P":"11110 10001 10001 11110 10000 10000 10000",
    "Q":"01110 10001 10001 10001 10101 10010 01101", "R":"11110 10001 10001 11110 10100 10010 10001",
    "S":"01111 10000 10000 01110 00001 00001 11110", "T":"11111 00100 00100 00100 00100 00100 00100",
    "U":"10001 10001 10001 10001 10001 10001 01110", "V":"10001 10001 10001 10001 10001 01010 00100",
    "W":"10001 10001 10001 10101 10101 11011 10001", "X":"10001 10001 01010 00100 01010 10001 10001",
    "Y":"10001 10001 01010 00100 00100 00100 00100", "Z":"11111 00001 00010 00100 01000 10000 11111",
}.items():
    FONT[_letter] = tuple(_rows.split())

state_lock = threading.Lock()
# Global control states modified by board knob and MQTT.
state = {
    "speed": 1.0,
    "brightness": 0.4,
    "animation": "plasma",
    "text": "HELLO",
    "dropped_frames": 0,
}


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def synchronize_clock_with_ntp():
    """Update the offset between the host clock and an NTP server."""
    global ntp_offset_seconds

    packet = bytearray(48)
    packet[0] = 0x1B  # NTP client request, version 3.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(NTP_TIMEOUT_SECONDS)
    try:
        request_time = time.time()
        sock.sendto(packet, (NTP_SERVER, NTP_PORT))
        response, _ = sock.recvfrom(512)
        receive_time = time.time()

        if len(response) < 48:
            raise ValueError("NTP response is too short")

        # Transmit timestamp starts at byte 40 and is seconds since 1900.
        seconds, fraction = struct.unpack("!II", response[40:48])
        ntp_epoch = seconds - 2208988800 + fraction / 2**32
        measured_offset = ntp_epoch - ((request_time + receive_time) / 2)

        with ntp_offset_lock:
            ntp_offset_seconds = measured_offset
        print(f"[NTP] Synchronized with {NTP_SERVER}; offset {measured_offset:+.3f}s")
        return True
    except (OSError, ValueError, struct.error) as exc:
        print(f"[NTP] Synchronization failed: {exc}; using host RTC/system clock")
        return False
    finally:
        sock.close()


def clock_datetime():
    """Return local time corrected by the most recent NTP synchronization."""
    with ntp_offset_lock:
        offset = ntp_offset_seconds
    return datetime.datetime.fromtimestamp(time.time() + offset)


def ntp_worker():
    """Synchronize periodically without interrupting the video stream."""
    if not NTP_ENABLED:
        print("[NTP] Disabled by LED_NTP_ENABLED")
        return

    while True:
        synchronize_clock_with_ntp()
        time.sleep(max(60.0, NTP_SYNC_INTERVAL_SECONDS))


def apply_animation_command(payload):
    """Select one of the built-in animations from a feed payload."""
    animation = payload.decode("utf-8").strip().lower()
    if animation in ANIMATIONS:
        with state_lock:
            state["animation"] = animation
        print(f"[MQTT] Animation selected: {animation}")
    else:
        print(
            f"[MQTT] Ignoring unknown animation '{animation}'. "
            f"Choose: {', '.join(ANIMATIONS)}"
        )


def apply_text_command(payload):
    """Update the scrolling message from an MQTT feed."""
    text = payload.decode("utf-8", errors="ignore").upper().strip()
    text = "".join(character for character in text if character in FONT)
    if text:
        with state_lock:
            state["text"] = text[:64]
        print(f"[MQTT] Text selected: {text[:64]}")


def apply_mqtt_command(payload):
    """Apply a JSON control command without allowing invalid frame values."""
    try:
        command = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"[MQTT] Ignoring invalid JSON: {exc}")
        return

    if not isinstance(command, dict):
        print("[MQTT] Ignoring command: JSON object expected")
        return

    changed = []
    with state_lock:
        if "speed" in command:
            try:
                state["speed"] = clamp(float(command["speed"]), 0.2, 3.0)
                changed.append(f"speed={state['speed']:.2f}")
            except (TypeError, ValueError):
                print("[MQTT] Ignoring invalid speed")

        if "brightness" in command:
            try:
                state["brightness"] = clamp(float(command["brightness"]), 0.0, 1.0)
                changed.append(f"brightness={state['brightness']:.2f}")
            except (TypeError, ValueError):
                print("[MQTT] Ignoring invalid brightness")

    if changed:
        print("[MQTT] Applied " + ", ".join(changed))
    else:
        print("[MQTT] Command contained no supported fields (speed, brightness)")


def text_pixel(text, x, y, offset):
    """Return whether a pixel is lit in a horizontally scrolling 5x7 message."""
    message_width = len(text) * 6
    column = (x + offset) % (message_width + WIDTH) - WIDTH
    if column < 0 or column >= message_width or y >= 7:
        return False
    character_index, character_column = divmod(column, 6)
    if character_column == 5:
        return False
    rows = FONT.get(text[character_index], FONT[" "])
    return rows[y][character_column] == "1"


def animation_pixel(animation, x, y, t, brightness, text):
    """Return an RGB pixel for the selected animation."""
    if animation == "clock":
        now = clock_datetime()
        clock_text = now.strftime("%H:%M")
        # Blink the colon once per second while keeping the rest of the clock steady.
        if now.second % 2 == 1:
            clock_text = clock_text.replace(":", " ")
        total_width = len(clock_text) * 6 - 1
        start_x = max(0, (WIDTH - total_width) // 2)
        clock_column = x - start_x
        lit = False
        if 0 <= clock_column < total_width and y < 7:
            character_index, character_column = divmod(clock_column, 6)
            if character_column < 5:
                lit = FONT[clock_text[character_index]][y][character_column] == "1"
        return (int(255 * brightness), int(180 * brightness), int(40 * brightness)) if lit else (0, 0, 0)
    if animation == "text":
        lit = text_pixel(text, x, y, int(t * 8))
        return (int(80 * brightness), int(220 * brightness), int(255 * brightness)) if lit else (0, 0, 0)
    if animation == "rainbow":
        phase = (x / WIDTH + t * 0.08) % 1.0
        red = int((math.sin(phase * math.tau) + 1.0) * 127.5)
        green = int((math.sin(phase * math.tau + 2.094) + 1.0) * 127.5)
        blue = int((math.sin(phase * math.tau + 4.188) + 1.0) * 127.5)
    elif animation == "waves":
        wave = (math.sin(x * 0.35 + y * 0.7 - t * 2.0) + 1.0) * 0.5
        red = int(40 + 80 * wave)
        green = int(80 + 150 * wave)
        blue = int(140 + 115 * wave)
    elif animation == "checker":
        phase = int(t * 2.0) % 2
        lit = ((x // 4 + y // 2 + phase) % 2) == 0
        red, green, blue = ((255, 35, 15) if lit else (10, 10, 45))
    elif animation == "solid":
        red, green, blue = (40, 120, 255)
    else:  # plasma
        red = int((math.sin(x * 0.25 + t) + 1.0) * 127.0)
        green = int((math.cos(y * 0.6 + t) + 1.0) * 127.0)
        blue = int((math.sin((x + y) * 0.2 + t) + 1.0) * 127.0)

    return (
        int(clamp(red * brightness, 0, 255)),
        int(clamp(green * brightness, 0, 255)),
        int(clamp(blue * brightness, 0, 255)),
    )


def mqtt_worker():
    """Subscribe to remote controls while UDP streaming runs independently."""
    if not MQTT_ENABLED:
        print("[MQTT] Disabled by LED_MQTT_ENABLED")
        return
    if mqtt is None:
        print("[MQTT] Disabled: install paho-mqtt first")
        return

    def on_connect(client, userdata, flags, rc, properties=None):
        if rc == 0:
            client.subscribe(MQTT_TOPIC, qos=0)
            if MQTT_ANIMATION_TOPIC:
                client.subscribe(MQTT_ANIMATION_TOPIC, qos=0)
            if MQTT_TEXT_TOPIC:
                client.subscribe(MQTT_TEXT_TOPIC, qos=0)
            topics = MQTT_TOPIC
            if MQTT_ANIMATION_TOPIC:
                topics += f", {MQTT_ANIMATION_TOPIC}"
            if MQTT_TEXT_TOPIC:
                topics += f", {MQTT_TEXT_TOPIC}"
            print(f"[MQTT] Connected to {MQTT_HOST}:{MQTT_PORT}; subscribed to {topics}")
        else:
            print(f"[MQTT] Connection failed, code {rc}")

    def on_message(client, userdata, message):
        if MQTT_ANIMATION_TOPIC and message.topic == MQTT_ANIMATION_TOPIC:
            apply_animation_command(message.payload)
        elif MQTT_TEXT_TOPIC and message.topic == MQTT_TEXT_TOPIC:
            apply_text_command(message.payload)
        else:
            apply_mqtt_command(message.payload)

    # Support both paho-mqtt 1.x and 2.x callback APIs.
    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    else:
        client = mqtt.Client()
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD or "")
    if MQTT_PORT == 8883:
        client.tls_set()
    client.on_connect = on_connect
    client.on_message = on_message

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_forever()
    except Exception as exc:
        print(f"[MQTT] Worker stopped: {exc}")


def backchannel_worker():
    """Listens for encoder telemetry sent from the W55MH32L board."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", BACKCHANNEL_PORT))
    print(f"[Telemetry] Listening on port {BACKCHANNEL_PORT}...")

    while True:
        data, _ = sock.recvfrom(32)
        if len(data) >= 5 and data[0:2] == b"CT":
            knob_val = data[2]
            dropped = (data[3] << 8) | data[4]
            # Preserve the existing physical knob behavior.
            with state_lock:
                state["speed"] = 0.2 + (knob_val / 100.0) * 2.8
                state["brightness"] = 0.1 + (knob_val / 100.0) * 0.7
                state["dropped_frames"] = dropped
            print(
                f"\r[Board Sync] Knob: {knob_val}% | "
                f"Speed: {state['speed']:.2f}x | Hardware Drops: {dropped}",
                end="",
            )


def run_streamer():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    frame_id = 0
    t = 0.0
    target_frame_time = 1.0 / 60.0

    print(f"[Streamer] Streaming 60 FPS video to {BOARD_IP}:{STREAM_PORT}...")

    try:
        while True:
            start_time = time.perf_counter()
            with state_lock:
                speed = state["speed"]
                brightness = state["brightness"]
                animation = state["animation"]
                text = state["text"]

            t += 0.05 * speed
            # Construct 4-byte header: 0xAA 0x55 + uint16 frame_id
            frame = bytearray([0xAA, 0x55, (frame_id >> 8) & 0xFF, frame_id & 0xFF])

            # Generate the selected 8x32 animation.
            for y in range(HEIGHT):
                for x in range(WIDTH):
                    r, g, b = animation_pixel(animation, x, y, t, brightness, text)
                    frame.extend([r, g, b])

            sock.sendto(frame, (BOARD_IP, STREAM_PORT))
            frame_id = (frame_id + 1) & 0xFFFF

            # Precision 60 FPS sleep timer.
            elapsed = time.perf_counter() - start_time
            sleep_time = target_frame_time - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()


if __name__ == "__main__":
    t_listen = threading.Thread(target=backchannel_worker, daemon=True)
    t_listen.start()

    t_mqtt = threading.Thread(target=mqtt_worker, daemon=True)
    t_mqtt.start()

    t_ntp = threading.Thread(target=ntp_worker, daemon=True)
    t_ntp.start()

    run_streamer()
