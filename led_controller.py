import socket
import struct
import time
import machine
import network
import ssd1306

from machine import Pin, I2C, SPI, bitstream

# ============================================================
# Configuration
# ============================================================

WIDTH = 32
HEIGHT = 8
TOTAL_LEDS = WIDTH * HEIGHT

LED_PIN_NAME = "PC13"

# WS2812/NeoPixel bitstream configuration. The old firmware exposes
# machine.bitstream() but does not provide a reliable neopixel module.
LED_BITSTREAM_ENCODING = 0
LED_BITSTREAM_TIMING = (400, 850, 800, 450)
LED_FRAME_REPEAT_DELAY_MS = 10

ENCODER_A_PIN_NAME = "PA2"
ENCODER_B_PIN_NAME = "PA1"

MOSFET_PIN_NAME = "PD4"
MOSFET_ACTIVE_LEVEL = 1

# Button is wired between PB0 and ground. The internal pull-up makes
# the input active-low: released=1, pressed=0.
BUTTON_PIN_NAME = "PA8"
BUTTON_ACTIVE_LEVEL = 0
BUTTON_DEBOUNCE_MS = 40
MATRIX_POWER_SETTLE_MS = 100

lights_enabled = True
matrix_power_ready_at = 0

ETHERNET_SPI_ID = 2
ETHERNET_SPI_BAUDRATE = 8_000_000
ETHERNET_CS_PIN_NAME = "PB12"
ETHERNET_RESET_PIN_NAME = "PD9"

UDP_RX_PORT = 6454
UDP_TX_PORT = 6455

OLED_WIDTH = 128
OLED_HEIGHT = 64
OLED_I2C_ID = 1
OLED_I2C_ADDRESS = 0x3D

FRAME_HEADER_MAGIC = b"\xAA\x55"
FRAME_HEADER_SIZE = 4
BYTES_PER_PIXEL = 3
PAYLOAD_SIZE = TOTAL_LEDS * BYTES_PER_PIXEL
EXPECTED_FRAME_SIZE = FRAME_HEADER_SIZE + PAYLOAD_SIZE

MAX_PACKETS_PER_LOOP = 1
DHCP_TIMEOUT_MS = 10_000
OLED_UPDATE_INTERVAL_MS = 250

# ============================================================
# Global application state
# ============================================================

encoder_value = 50

# Updated by the encoder interrupt.
encoder_delta = 0
encoder_step_counter = 0
encoder_last_state = 0

# Set by the main loop after applying encoder_delta.
encoder_changed = False

pc_ip = None

frame_count = 0
dropped_frames = 0
last_frame_id = None

last_stats_time = time.ticks_ms()

# Button debounce state.
button_pin = None
button_last_raw_state = 1
button_stable_state = 1
button_state_changed_at = 0


def initialize_mosfet():
    """Configure and return the GPIO pin that controls the LED power MOSFET."""
    mosfet_pin = Pin(
        MOSFET_PIN_NAME,
        Pin.OUT,
        value=0,
    )
    return mosfet_pin

def set_lights_enabled(mosfet_pin, enabled):
    """
    Switch power to the LED matrix on or off via the MOSFET.
    Sets a brief settling delay on power-up before pixels can be sent.
    """
    global lights_enabled
    global matrix_power_ready_at

    lights_enabled = bool(enabled)
    mosfet_pin.value(
        MOSFET_ACTIVE_LEVEL if lights_enabled else 1 - MOSFET_ACTIVE_LEVEL
    )

    if lights_enabled:
        matrix_power_ready_at = time.ticks_add(
            time.ticks_ms(),
            MATRIX_POWER_SETTLE_MS,
        )
    else:
        matrix_power_ready_at = 0

def clear_matrix(matrix):
    """Set every pixel to black (0,0,0) and flush to the LED strip."""
    for physical_index in range(TOTAL_LEDS):
        matrix[physical_index] = (0, 0, 0)

    matrix.write()

def toggle_lights(mosfet_pin, matrix):
    """Toggle the matrix power state; clears pixels before powering down."""
    if lights_enabled:
        clear_matrix(matrix)

    set_lights_enabled(mosfet_pin, not lights_enabled)

    print(
        "[LIGHTS]",
        "ON" if lights_enabled else "OFF",
    )


def initialize_button():
    """Set up the toggle button GPIO with an internal pull-up resistor."""
    global button_pin
    global button_last_raw_state
    global button_stable_state
    global button_state_changed_at

    button_pin = Pin(BUTTON_PIN_NAME, Pin.IN, Pin.PULL_UP)
    button_last_raw_state = button_pin.value()
    button_stable_state = button_last_raw_state
    button_state_changed_at = time.ticks_ms()
    return button_pin

def update_button(mosfet_pin, matrix):
    """
    Sample and debounce the power button.
    Toggles the lights whenever a valid, debounced press edge occurs.
    """
    global button_last_raw_state
    global button_stable_state
    global button_state_changed_at

    raw_state = button_pin.value()
    now = time.ticks_ms()

    # Track input bounce
    if raw_state != button_last_raw_state:
        button_last_raw_state = raw_state
        button_state_changed_at = now
        return

    # No state change detected
    if raw_state == button_stable_state:
        return

    # Ignore transitions faster than debounce threshold
    if time.ticks_diff(now, button_state_changed_at) < BUTTON_DEBOUNCE_MS:
        return

    button_stable_state = raw_state

    # Trigger action only on active press edge
    if button_stable_state == BUTTON_ACTIVE_LEVEL:
        toggle_lights(mosfet_pin, matrix)

class BitstreamMatrix:
    """256-pixel GRB buffer transmitted with machine.bitstream()."""

    def __init__(self, pin, pixel_count):
        self.pin = pin
        self.pixel_count = pixel_count
        self.buffer = bytearray(pixel_count * BYTES_PER_PIXEL)

    def __setitem__(self, index, color):
        if index < 0 or index >= self.pixel_count:
            raise IndexError("LED index out of range")

        red, green, blue = color
        offset = index * BYTES_PER_PIXEL

        # WS2812 LEDs expect GRB byte order.
        self.buffer[offset] = green
        self.buffer[offset + 1] = red
        self.buffer[offset + 2] = blue

    def write(self):
        # Repeat the complete frame because the older firmware can miss the
        # first transmission after a previous frame or power transition.
        bitstream(
            self.pin,
            LED_BITSTREAM_ENCODING,
            LED_BITSTREAM_TIMING,
            self.buffer,
        )
        time.sleep_ms(LED_FRAME_REPEAT_DELAY_MS)
        bitstream(
            self.pin,
            LED_BITSTREAM_ENCODING,
            LED_BITSTREAM_TIMING,
            self.buffer,
        )


def initialize_led_matrix():
    """Initialize the matrix using the low-level WS2812 bitstream API."""
    led_pin = Pin(LED_PIN_NAME, Pin.OUT, value=0)
    return BitstreamMatrix(led_pin, TOTAL_LEDS)

def initialize_oled():
    """Initialize the SSD1306 OLED if available."""
    try:

        i2c = I2C(OLED_I2C_ID)

        return ssd1306.SSD1306_I2C(
            OLED_WIDTH,
            OLED_HEIGHT,
            i2c,
            addr=OLED_I2C_ADDRESS,
        )

    except ImportError:
        print("[OLED] ssd1306 module not found")
        return None

    except OSError as exc:
        print("[OLED] Initialization failed:", exc)
        return None

    except Exception as exc:
        print("[OLED] Unexpected initialization error:", exc)
        return None

def initialize_encoder():
    """
    Initialize the two-channel rotary encoder.

    The encoder module does not use a button pin. Only PA0 and PA1
    are configured.
    """
    global encoder_last_state

    pin_a = Pin(ENCODER_A_PIN_NAME, Pin.IN, Pin.PULL_UP)
    pin_b = Pin(ENCODER_B_PIN_NAME, Pin.IN, Pin.PULL_UP)

    encoder_last_state = (pin_a.value() << 1) | pin_b.value()

    pin_a.irq(
        trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING,
        handler=encoder_isr,
    )

    pin_b.irq(
        trigger=Pin.IRQ_FALLING | Pin.IRQ_RISING,
        handler=encoder_isr,
    )

    return pin_a, pin_b

def initialize_ethernet():
    """Initialize the W5500 Ethernet controller."""
    spi = SPI(
        ETHERNET_SPI_ID,
        baudrate=ETHERNET_SPI_BAUDRATE,
        polarity=0,
        phase=0,
    )

    cs_pin = Pin(ETHERNET_CS_PIN_NAME, Pin.OUT, value=1)
    reset_pin = Pin(ETHERNET_RESET_PIN_NAME, Pin.OUT)

    print("[NET] Resetting W5500...")
    reset_pin.value(0)
    time.sleep_ms(10)
    reset_pin.value(1)
    time.sleep_ms(200)

    try:
        nic = network.WIZNET5K(spi, cs_pin, reset_pin)
        nic.active(True)
        nic.ifconfig("dhcp")
        return nic

    except Exception as exc:
        print("[NET] W5500 initialization failed:", exc)
        return None

def wait_for_dhcp(nic, timeout_ms=DHCP_TIMEOUT_MS):
    """Wait for a DHCP lease without blocking indefinitely."""
    start = time.ticks_ms()

    print("[NET] Requesting IP from DHCP server", end="")

    while not nic.isconnected():
        elapsed = time.ticks_diff(time.ticks_ms(), start)

        if elapsed >= timeout_ms:
            print()
            return False

        print(".", end="")
        time.sleep_ms(250)

    print()
    return True

# ============================================================
# Encoder interrupt handling
# ============================================================

def encoder_isr(pin):
    """
    Rotary encoder interrupt handler.

    The ISR only decodes transitions and accumulates movement.
    It does not modify the application value or perform networking.
    """
    global encoder_last_state
    global encoder_step_counter
    global encoder_delta

    # These globals are initialized before interrupts are enabled.
    # The pin values are read directly to avoid doing work elsewhere.
    encoded = (
        (encoder_pin_a.value() << 1)
        | encoder_pin_b.value()
    )

    transition = (encoder_last_state << 2) | encoded

    # Clockwise transitions.
    if transition in (1, 7, 14, 8):
        encoder_step_counter += 1

    # Counter-clockwise transitions.
    elif transition in (2, 11, 13, 4):
        encoder_step_counter -= 1

    encoder_last_state = encoded

    # Four valid transitions represent one physical detent.
    if encoder_step_counter >= 4:
        encoder_delta += 1
        encoder_step_counter = 0

    elif encoder_step_counter <= -4:
        encoder_delta -= 1
        encoder_step_counter = 0

def take_encoder_delta():
    """
    Atomically copy and clear encoder_delta.

    Interrupts are disabled only for the short read-and-clear operation.
    """
    global encoder_delta

    irq_state = machine.disable_irq()

    try:
        delta = encoder_delta
        encoder_delta = 0

    finally:
        machine.enable_irq(irq_state)

    return delta

def update_encoder_value():
    """Apply encoder movement from the main loop."""
    global encoder_value
    global encoder_changed

    delta = take_encoder_delta()

    if delta == 0:
        return

    encoder_value = max(0, min(100, encoder_value + delta*2))
    encoder_changed = True

# ============================================================
# Matrix mapping
# ============================================================

def build_index_map():
    """
    Build a logical-to-physical LED index map.

    Logical input order:
        row 0: x=0, x=1, ..., x=31
        row 1: x=0, x=1, ..., x=31
        ...

    Physical wiring assumption:
        Each column contains HEIGHT LEDs.
        Even columns run from top to bottom.
        Odd columns run from bottom to top.
    """
    index_map = [0] * TOTAL_LEDS

    for y in range(HEIGHT):
        for x in range(WIDTH):
            logical_index = y * WIDTH + x

            if x % 2 == 0:
                physical_index = x * HEIGHT + y
            else:
                physical_index = x * HEIGHT + (HEIGHT - 1 - y)

            index_map[logical_index] = physical_index

    return index_map

# ============================================================
# UDP communication
# ============================================================

def initialize_udp_sockets():
    """Create non-blocking receive and transmit sockets."""
    rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx_sock.bind(("0.0.0.0", UDP_RX_PORT))
    rx_sock.setblocking(False)

    tx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx_sock.setblocking(False)

    return rx_sock, tx_sock

def frame_number_difference(current, previous):
    """
    Return the unsigned 16-bit forward difference between frame IDs.
    """
    return (current - previous) & 0xFFFF

def process_frame(data, addr, matrix, index_map):
    """
    Validate and display one UDP frame.

    Returns:
        True if a frame was displayed.
        False if the packet was invalid or stale.
    """
    global pc_ip
    global frame_count
    global dropped_frames
    global last_frame_id

    if not lights_enabled:
        return False

    if time.ticks_diff(time.ticks_ms(), matrix_power_ready_at) < 0:
        return False

    if len(data) != EXPECTED_FRAME_SIZE:
        return False

    if data[0:2] != FRAME_HEADER_MAGIC:
        return False

    frame_id = (data[2] << 8) | data[3]

    # Ignore duplicate or stale/out-of-order frames.
    if last_frame_id is not None:
        difference = frame_number_difference(frame_id, last_frame_id)

        if difference == 0:
            return False

        # Differences >= 0x8000 indicate an older frame after
        # accounting for 16-bit counter wraparound.
        if difference >= 0x8000:
            return False

        if difference > 1:
            dropped_frames += difference - 1

    last_frame_id = frame_id

    if pc_ip is None:
        pc_ip = addr[0]
        print("[LINK] Connected to PC:", pc_ip)

    payload_offset = FRAME_HEADER_SIZE

    for logical_index in range(TOTAL_LEDS):
        physical_index = index_map[logical_index]

        r = data[payload_offset]
        g = data[payload_offset + 1]
        b = data[payload_offset + 2]

        matrix[physical_index] = (r, g, b)
        payload_offset += BYTES_PER_PIXEL

    matrix.write()
    frame_count += 1

    return True

def receive_frames(rx_sock, matrix, index_map):
    """
    Drain several queued UDP packets.

    Processing multiple packets prevents the socket receive buffer
    from filling. Stale packets are rejected by process_frame().
    """
    for _ in range(MAX_PACKETS_PER_LOOP):
        try:
            data, addr = rx_sock.recvfrom(EXPECTED_FRAME_SIZE)

        except OSError:
            # No more packets available.
            break

        except MemoryError:
            print("[UDP] MemoryError while receiving frame")
            break

        process_frame(data, addr, matrix, index_map)

def send_encoder_state(tx_sock):
    """
    Send encoder state to the PC.

    The third field is a reserved switch byte. It is always zero because
    this encoder module has no connected push-button.
    """
    global encoder_changed

    if not encoder_changed or pc_ip is None:
        return

    # Take a stable snapshot of the state before constructing the packet.
    irq_state = machine.disable_irq()

    try:
        value = encoder_value
        encoder_changed = False

    finally:
        machine.enable_irq(irq_state)

    reserved_switch_state = 0

    packet = struct.pack(
        "!2sBBH",
        b"CT",
        value,
        reserved_switch_state,
        min(dropped_frames, 65535),
    )

    try:
        tx_sock.sendto(packet, (pc_ip, UDP_TX_PORT))

    except OSError as exc:
        print("[UDP] Encoder state send failed:", exc)

# ============================================================
# OLED and diagnostics
# ============================================================

def update_oled(oled, nic, fps):
    """Update the OLED display."""
    if oled is None:
        return

    try:
        ip_address = nic.ifconfig()[0] if nic is not None else "NO NET"

        oled.fill(0)
        oled.text("W55MH32L STAGE", 0, 0)
        oled.text("IP: " + ip_address, 0, 12)
        oled.text("FPS: {:.1f}".format(fps), 0, 24)
        oled.text("DROPS: {}".format(dropped_frames), 0, 36)
        oled.text("KNOB: {}%".format(encoder_value), 0, 48)
        oled.show()

    except Exception as exc:
        print("[OLED] Update failed:", exc)

def update_telemetry(oled, nic):
    """Calculate FPS and refresh diagnostics periodically."""
    global frame_count
    global last_stats_time

    now = time.ticks_ms()
    elapsed_ms = time.ticks_diff(now, last_stats_time)

    if elapsed_ms < OLED_UPDATE_INTERVAL_MS:
        return

    elapsed_seconds = elapsed_ms / 1000.0
    fps = frame_count / elapsed_seconds if elapsed_seconds > 0 else 0.0

    frame_count = 0
    last_stats_time = now

    print(
        "[STATS] FPS: {:.1f}, drops: {}, encoder: {}".format(
            fps,
            dropped_frames,
            encoder_value,
        )
    )

    update_oled(oled, nic, fps)

# ============================================================
# Startup
# ============================================================

print("[BOOT] Starting controller")

matrix = initialize_led_matrix()
oled = initialize_oled()

# These globals are used by encoder_isr().
encoder_pin_a, encoder_pin_b = initialize_encoder()

mosfet_pin = initialize_mosfet()
button_pin = initialize_button()
set_lights_enabled(mosfet_pin, lights_enabled)

index_map = build_index_map()

nic = initialize_ethernet()

if nic is None:
    print("[NET] Ethernet unavailable")

elif wait_for_dhcp(nic):
    board_ip = nic.ifconfig()[0]
    print("[NET] DHCP success. Board IP:", board_ip)

else:
    print("[NET] DHCP failed")
    print("[NET] Check the Ethernet cable and DHCP server")

try:
    rx_socket, tx_socket = initialize_udp_sockets()
    print("[UDP] Listening on port", UDP_RX_PORT)

except OSError as exc:
    print("[UDP] Socket initialization failed:", exc)
    rx_socket = None
    tx_socket = None

# ============================================================
# Main loop
# ============================================================

while True:
    update_button(mosfet_pin, matrix)
    update_encoder_value()

    if rx_socket is not None:
        receive_frames(rx_socket, matrix, index_map)

    # Sample again after network/matrix work so a press is not missed.
    update_button(mosfet_pin, matrix)

    if tx_socket is not None:
        send_encoder_state(tx_socket)

    update_telemetry(oled, nic)

    # Avoid monopolizing the CPU when no UDP packets are available.
    time.sleep_ms(1)
