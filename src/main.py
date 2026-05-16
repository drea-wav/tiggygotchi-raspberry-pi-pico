"""
Tiggygotchi — a kawaii Tamagotchi-style cat-feeding sim for the Raspberry Pi Pico.

Hardware: Pico (RP2040) + Waveshare Pico-LCD-1.3 hat
  - 240×240 ST7789 LCD driven over SPI
  - 5-way joystick (up/down/left/right/center) and 4 face buttons (A/B/X/Y)

What this file does, top to bottom:
  1. Imports MicroPython built-in modules and a vendored LCD driver.
  2. Defines color constants, ASCII-art sprite data, and timing constants.
  3. Configures GPIO pins for every button.
  4. Pre-renders every sprite once into a scaled mono framebuffer at startup.
  5. Shows a boot title screen, then enters the main game loop.
  6. Each loop iteration: reads buttons → updates cat state → draws the frame.

Big-picture flow of each frame:
  Read input → update state machine → clear LCD buffer → draw sprites → push to screen.

MicroPython note: this file is named main.py, which means the Pico runs it
automatically on every power-up. There is no operating system — the RP2040
chip boots directly into the MicroPython interpreter and executes this file.
"""

# MicroPython provides a subset of Python 3's standard library as built-in
# modules compiled into the firmware. You import them the same way you would
# in normal Python, but they live in the chip's flash, not on disk.
import framebuf       # pixel-buffer drawing primitives (fill, text, rect, blit, …)
from machine import Pin  # GPIO pin control
import utime          # time functions: sleep_ms, ticks_ms, ticks_diff
import urandom        # hardware random-number generator
from Pico_LCD_1_3 import LCD_1inch3  # vendored Waveshare driver (see Pico_LCD_1_3.py)

SCREEN_W, SCREEN_H = 240, 240  # ST7789 display resolution in pixels
SCALE = 1.5
CELL = int(8 * SCALE)  # pixel size of one source char at the chosen scale

# ── RGB565 color constants ────────────────────────────────────────────────────
# The ST7789 LCD stores each pixel as a 16-bit RGB565 value:
#   bits 15-11 → 5 bits of red   (max = 0b11111 = 31)
#   bits 10-5  → 6 bits of green (max = 0b111111 = 63)
#   bits 4-0   → 5 bits of blue  (max = 0b11111 = 31)
# Green gets an extra bit because the human eye is most sensitive to it.
# Examples:
#   Pure red:   0xF800  (binary: 1111_1000_0000_0000)
#   Pure green: 0x07E0  (binary: 0000_0111_1110_0000)
#   Pure blue:  0x001F  (binary: 0000_0000_0001_1111)
#   White:      0xFFFF  (all bits set)
#   Black:      0x0000  (no bits set)
BLACK  = 0x0000
WHITE  = 0xFFFF
PINK   = 0xF81F  # max red + max blue, no green → magenta/pink
YELLOW = 0xFFE0  # max red + max green, no blue
CYAN   = 0x07FF  # max green + max blue, no red

# ── Sprite definitions ────────────────────────────────────────────────────────
# Each sprite is a tuple of strings representing ASCII art. When passed to
# render_scaled_mono(), each character is rendered using MicroPython's built-in
# 8×8 pixel font, producing a monochrome bitmap that can be blitted to the LCD.
#
# Raw strings (r"...") are used here so that backslashes — common in cat-ear
# art like /\ — don't need to be escaped as \\. Without the r prefix,
# r"   ^ ^   " would need to be written as "   ^ ^   " (same here, no slashes),
# but lines like r"   / \   " would need "   / \   " — fine, but r"" makes it
# consistent and safe for future edits involving backslashes.
#
# Lines within a sprite don't have to be the same length; render_scaled_mono
# uses the longest line to determine the output buffer width.

# Cat sprites — 5 rows x 9 cols each (subagent output normalized to consistent width).
CAT_IDLE  = (
    r"   ^ ^   ",
    r"  (o.o)  ",
    r"   / \    ",
    r"  |   |  / ",
    r"  | 0 | / ",
    r"   \_/_/   "
)

CAT_BLINK = (
    r"   ^ ^   ",
    r"  (u.u)  ",  # 'u' eyes → closed/blinking
    r"   / \    ",
    r"  |   |   ",
    r"  | 0 | /\ ",
    r"   \_/_/   "
)

CAT_HAPPY = (
    r"   ^ ^   ",
    r"  (^w^)  ",  # 'w' mouth → happy cat face
    r"   / \    ",
    r"  |   |  / ",
    r"  | 0 | / ",
    r"   \_/_/   "
)

CAT_SLEEP = (
    r" ^ ^           ",
    r"(-.-)       ",   # '-' eyes → sleepy
    r" (      )    ",
    r" uu (  u)_/ ",
)


# Food sprites — one per food type. Each key matches an entry in FOOD_COLOR.
FOOD_SPRITE = {
    "fish":  (r"  /   ",
              r" /\/ ",
              r" \/\ ",
              r"  \  "),

    "treat": (
        r"/\ /\ ",
        r"\ v / ",
        r" \ /  ",
        r"  V   ",
    ),

    "bird":  (r"   (o)>   ",
              r"/\(__)/\  ",
              r"   ||   ",
              r"  _||_  "),

    "ramen": (r" ~^~^ ",
              r"(====)",
              r" \__/ "),
}
# Each food type gets its own tint color (RGB565) when drawn to the LCD.
FOOD_COLOR = {"fish": CYAN, "treat": PINK, "bird": YELLOW, "ramen": WHITE}

# ── GPIO / button setup ───────────────────────────────────────────────────────
# Pin(n, Pin.IN, Pin.PULL_UP) configures GPIO number n as a digital input and
# enables an internal pull-up resistor, which keeps the line at 3.3 V (logic 1)
# when nothing is connected. The buttons on this hat are wired "active-low":
# pressing a button connects the GPIO pin to GND, pulling the voltage to 0 V.
# So:  pin.value() == 1  →  button NOT pressed  (floating high via pull-up)
#      pin.value() == 0  →  button IS pressed    (pulled to ground)
def btn(n):
    return Pin(n, Pin.IN, Pin.PULL_UP)

# 5-way joystick — each direction is a separate GPIO.
joy_up     = btn(2)
joy_center = btn(3)
joy_left   = btn(16)
joy_down   = btn(18)
joy_right  = btn(20)
# Four face buttons labeled A, B, X, Y on the hat's silkscreen.
key_a, key_b, key_x, key_y = btn(15), btn(17), btn(19), btn(21)

# Map each face button to the food it spawns when pressed.
BUTTON_FOOD = ((key_a, "fish"), (key_b, "treat"), (key_x, "bird"), (key_y, "ramen"))

# ── LCD initialisation ────────────────────────────────────────────────────────
# LCD_1inch3 (from Pico_LCD_1_3.py) subclasses framebuf.FrameBuffer.
# That means lcd IS a FrameBuffer — all drawing calls (fill, text, rect, blit,
# pixel, …) go into an in-memory byte array called the framebuffer.
# The pixels are NOT visible yet; you must call lcd.show() to push the entire
# buffer over SPI to the ST7789 controller on the physical display.
lcd = LCD_1inch3()
lcd.fill(BLACK)   # paint the in-memory buffer black
lcd.show()        # push it to the screen so we start with a blank display

# ── Pre-rendered scaled sprites ───────────────────────────────────────────────
# MicroPython's built-in font renders text at 8×8 pixels per character — fine
# for status messages but tiny on a 240×240 screen. To get bigger sprites we
# scale them up. Doing that scaling every frame in Python would be slow, so we
# do it ONCE here at startup and store the result as a mono FrameBuffer.
# Every frame we then call lcd.blit(pre_rendered_fb, x, y, …), which is a
# single C-level call and much faster than per-pixel Python loops.
#
# render_scaled_mono() supports fractional scale (e.g. 1.5) by computing, for
# each source pixel, the exact rectangle it maps to in the output:
#   dest_x_start = int(src_x * scale)
#   dest_x_end   = int((src_x + 1) * scale)
# Some source pixels map to 1 dest pixel wide, others to 2, giving smooth
# (non-integer) scaling without floating-point per-pixel math at runtime.
def render_scaled_mono(lines, scale):
    # Find the widest line so all lines can share one output buffer.
    n_cols = max(len(l) for l in lines)
    n_rows = len(lines)
    # Source canvas in font pixels (each character = 8×8 px).
    src_w = n_cols * 8
    src_h = n_rows * 8
    # Output buffer dimensions after scaling.
    out_w = int(src_w * scale)
    out_h = int(src_h * scale)
    # MONO_HLSB packs 8 pixels per byte, so stride = ceil(width / 8).
    stride = (out_w + 7) // 8
    out_buf = bytearray(stride * out_h)
    out_fb = framebuf.FrameBuffer(out_buf, out_w, out_h, framebuf.MONO_HLSB)

    for row, line in enumerate(lines):
        L = len(line)
        # Render this line of ASCII art into a temporary 1-bit buffer using the
        # built-in 8×8 font.  We use a fresh scratch buffer per line so shorter
        # lines don't accidentally pick up pixels from the previous iteration.
        scratch_buf = bytearray(L * 8)
        scratch_fb = framebuf.FrameBuffer(scratch_buf, L * 8, 8, framebuf.MONO_HLSB)
        scratch_fb.text(line, 0, 0, 1)  # draw with color 1 (white / set bit)
        # Walk every source pixel in this line and fill_rect its scaled
        # destination region in the output buffer.
        for py in range(8):
            sy = row * 8 + py            # source row in full-sprite coordinates
            dy0 = int(sy * scale)
            dh = int((sy + 1) * scale) - dy0
            if dh <= 0:
                continue
            row_base = py * L            # byte offset into scratch_buf for this row
            for bx in range(L):
                b = scratch_buf[row_base + bx]
                if b == 0:               # all 8 pixels in this byte are off — skip
                    continue
                for bit in range(8):
                    if b & (0x80 >> bit):   # bit is set → this pixel is "on"
                        sx = bx * 8 + bit
                        dx0 = int(sx * scale)
                        dw = int((sx + 1) * scale) - dx0
                        if dw > 0:
                            out_fb.fill_rect(dx0, dy0, dw, dh, 1)
    # Return the buffer, the FrameBuffer object, and the output pixel dimensions.
    # The caller needs all four to blit and to centre-position the sprite.
    return (out_buf, out_fb, out_w, out_h)

# Pre-render every animation frame and food sprite exactly once.
# CAT_FRAMES indices correspond to FRAME_IDLE/BLINK/HAPPY/SLEEP below.
CAT_FRAMES  = [render_scaled_mono(f, SCALE) for f in (CAT_IDLE, CAT_BLINK, CAT_HAPPY, CAT_SLEEP)]
FOOD_FRAMES = {k: render_scaled_mono(s, SCALE) for k, s in FOOD_SPRITE.items()}
# Small decorative sprites blitted as reaction effects.
DECO = {
    "heart":   render_scaled_mono(("<3",),   SCALE),
    "sparkle": render_scaled_mono(("*",),    SCALE),
    "nya":     render_scaled_mono(("nya~",), SCALE),
    "z_lo":    render_scaled_mono(("z",),    SCALE),  # small Z — just above the cat
    "z_mid":   render_scaled_mono(("Z",),    SCALE),  # bigger Z — middle height
    "z_hi":    render_scaled_mono(("z",),    SCALE),  # small Z — highest position
}

# Title screen sprites (scale 2 for chunky text).
TITLE_LINES = ("It's Tiggy", "  Time!   ")           # post-meal celebration
TITLE_SPRITE = render_scaled_mono(TITLE_LINES, 2)
BOOT_LINES = ("Tiggygotchi",)                        # boot / joystick-center
BOOT_SPRITE = render_scaled_mono(BOOT_LINES, 2)

# Cache the cat's pixel dimensions — we use them frequently for positioning.
CAT_PX_W = CAT_FRAMES[0][2]
CAT_PX_H = CAT_FRAMES[0][3]

# ── Monochrome → RGB565 blit helper ──────────────────────────────────────────
# lcd.blit(src, x, y, key, palette) blits a source FrameBuffer onto the LCD.
# When src is mono (1-bit) and dst is RGB565, the 'palette' argument is a
# 2-pixel-wide RGB565 FrameBuffer that maps source bit values to dest colors:
#   palette pixel 0  →  dest color for source pixels that are 0 (background)
#   palette pixel 1  →  dest color for source pixels that are 1 (foreground)
# The 'key' argument is a transparency color: any dest pixel matching 'key'
# after palette lookup is left untouched (i.e. transparent). By setting
# palette[0] = BLACK and key = BLACK, background pixels become transparent.
_pal_buf = bytearray(4)  # 2 pixels × 2 bytes/pixel (RGB565)
_pal_fb = framebuf.FrameBuffer(_pal_buf, 2, 1, framebuf.RGB565)

def draw_mono(sprite, x, y, color):
    """Blit a pre-rendered mono sprite onto the LCD in the given RGB565 color."""
    _pal_fb.pixel(0, 0, BLACK)   # source 0 → black (will be keyed transparent)
    _pal_fb.pixel(1, 0, color)   # source 1 → the requested foreground color
    lcd.blit(sprite[1], x, y, BLACK, _pal_fb)  # BLACK is the transparency key

# ── Frame and state index constants ──────────────────────────────────────────
FRAME_IDLE, FRAME_BLINK, FRAME_HAPPY, FRAME_SLEEP = 0, 1, 2, 3
# State machine states:
#   IDLE  — cat is awake and waiting; moves with joystick, accepts food buttons
#   EAT   — cat just touched food; plays happy frame for EAT_MS milliseconds
#   PURR  — post-eat contentment; shows hearts for PURR_MS milliseconds
#   SLEEP — triggered after SLEEP_AFTER_MS ms with no input; shows Zs
STATE_IDLE, STATE_EAT, STATE_PURR, STATE_SLEEP = 0, 1, 2, 3

STEP = max(1, int(2 * SCALE))  # pixels the cat moves per joystick poll
FRAME_MS = 60          # milliseconds per frame → ~16 fps
# Fixed frame timing keeps the game loop predictable and reduces power draw
# compared to running as fast as possible. At 60 ms/frame the Pico spends
# most of its time in utime.sleep_ms(), drawing very little current.
EAT_MS = 600           # how long the "eating" animation plays
PURR_MS = 1500         # how long the "purring" (heart) animation plays
SLEEP_AFTER_MS = 10000 # idle ms before the cat falls asleep (10 seconds)
NYA_MIN_MS = 6000      # minimum gap between random "nya~" pop-ups
NYA_MAX_MS = 10000     # maximum gap
NYA_DURATION_MS = 1200 # how long "nya~" stays on screen

# ── Timing helpers ────────────────────────────────────────────────────────────
# utime.ticks_ms() returns a millisecond counter that wraps around after about
# 24.8 days. Never subtract ticks directly — after a wrap, (a - b) would give
# a huge negative number. utime.ticks_diff(a, b) handles the wrap correctly by
# working in modular arithmetic, always returning the right signed difference.
def now():
    return utime.ticks_ms()

def since(t):
    """Return elapsed milliseconds since timestamp t, wrap-safe."""
    return utime.ticks_diff(now(), t)

# ── Title / celebration screen ────────────────────────────────────────────────
# play_title_screen() blocks until duration_ms have elapsed, showing a bouncing
# sprite with cycling sparkles. It is called twice: once at boot and once when
# the player has fed Tiggy all four unique foods.
def play_title_screen(sprite, duration_ms):
    tw, th = sprite[2], sprite[3]
    tx = (SCREEN_W - tw) // 2   # horizontally centred
    ty_base = 40
    cat = CAT_FRAMES[FRAME_HAPPY]
    cw = cat[2]
    cx = (SCREEN_W - cw) // 2
    cy_base = 140
    title_colors = (PINK, YELLOW, CYAN, WHITE)
    start = now()
    while since(start) < duration_ms:
        t = now()
        # Framebuffer pattern: clear → draw everything → push to screen.
        lcd.fill(BLACK)
        # Compute a sinusoidal-ish bob offset from the current time.
        bob = ((t // 80) % 12) - 6       # oscillates -6 … +5 px
        title_color = title_colors[(t // 200) % 4]  # cycles through 4 colors
        draw_mono(sprite, tx, ty_base + bob, title_color)
        cat_bob = ((t // 100) % 14) - 7
        draw_mono(cat, cx, cy_base + cat_bob, WHITE)
        # Animate sparkles by cycling which corner is hidden each frame.
        ph = (t // 150) % 4
        sx_l = max(0, tx - 20)
        sx_r = min(SCREEN_W - CELL, tx + tw + 4)
        sy_t = max(0, ty_base - 16)
        sy_b = ty_base + th + 4
        if ph != 0: draw_mono(DECO["sparkle"], sx_l, sy_t, YELLOW)
        if ph != 1: draw_mono(DECO["sparkle"], sx_r, sy_t, PINK)
        if ph != 2: draw_mono(DECO["sparkle"], sx_l, sy_b, CYAN)
        if ph != 3: draw_mono(DECO["sparkle"], sx_r, sy_b, YELLOW)
        lcd.show()
        utime.sleep_ms(FRAME_MS)

# Boot title: "Tiggygotchi" for 5 seconds.
play_title_screen(BOOT_SPRITE, 5000)

# ── Initial game state ────────────────────────────────────────────────────────
cat_x = (SCREEN_W - CAT_PX_W) // 2  # start centred on screen
cat_y = (SCREEN_H - CAT_PX_H) // 2

food = None           # currently active food item, or None if none on screen
state = STATE_IDLE
state_at = now()      # timestamp of the most recent state transition
last_input_at = now() # used to trigger sleep after inactivity
next_nya_at = now() + NYA_MIN_MS  # time at which the next "nya~" appears
nya_until = 0         # time until which "nya~" should be visible
# prev_buttons tracks each button's previous .value() reading.
# This lets us detect EDGE EVENTS — the moment a button transitions from
# released (1) to pressed (0) — rather than the sustained held-down state.
# Without this, holding a button for 60 ms would spawn ~16 food items per
# second instead of exactly one per press.
prev_buttons = [1] * len(BUTTON_FOOD)   # 1 = released (pull-up idle state)
prev_joy_center = 1
eaten = set()  # foods Tiggy has tasted this run; reset after the celebration title screen

# ── Utility functions ─────────────────────────────────────────────────────────
def aabb(ax, ay, aw, ah, bx, by, bw, bh):
    """Axis-aligned bounding-box overlap test. Returns True if the two rectangles intersect."""
    return ax < bx + bw and ax + aw > bx and ay < by + bh and ay + ah > by

def spawn_food(kind):
    """Place a food item at a random position that doesn't overlap the cat."""
    fw, fh = FOOD_FRAMES[kind][2], FOOD_FRAMES[kind][3]
    fx = fy = 0
    for _ in range(20):   # try up to 20 random positions; use last one if all overlap
        fx = urandom.getrandbits(8) % max(1, SCREEN_W - fw)
        fy = urandom.getrandbits(8) % max(1, SCREEN_H - fh)
        if not aabb(cat_x, cat_y, CAT_PX_W, CAT_PX_H, fx, fy, fw, fh):
            return (kind, fx, fy)   # found a non-overlapping spot
    return (kind, fx, fy)           # give up and use whatever position we have

# ── Main game loop ────────────────────────────────────────────────────────────
# The while True loop runs forever (or until the Pico is powered off).
# Each iteration is one frame:
#   1. Read input → detect button/joystick events
#   2. Update state machine (IDLE / EAT / PURR / SLEEP transitions)
#   3. Decide which sprite frame and color to draw this tick
#   4. Clear the LCD buffer, draw everything, push to screen
#   5. Sleep for FRAME_MS ms to maintain a steady ~16 fps
while True:
    t = now()

    # Joystick center press (edge-triggered): replay the Tiggygotchi boot title and reset to idle.
    jc_val = joy_center.value()
    if jc_val == 0 and prev_joy_center == 1:  # 1→0 transition = fresh press
        play_title_screen(BOOT_SPRITE, 5000)
        state = STATE_IDLE
        state_at = now()
        last_input_at = now()
        next_nya_at = now() + NYA_MIN_MS
        food = None
        prev_joy_center = jc_val
        continue   # skip the rest of this frame; start fresh next iteration
    prev_joy_center = jc_val

    inp = False  # will be set True if any meaningful input happened this frame

    # Joystick movement and food button presses are only accepted in IDLE/SLEEP.
    # In EAT and PURR states Tiggy is busy; we still drain the button readings
    # (the else branch below) so prev_buttons stays current.
    if state == STATE_IDLE or state == STATE_SLEEP:
        dx = dy = 0
        if joy_left.value()  == 0: dx -= STEP; inp = True  # active-low: 0 = pressed
        if joy_right.value() == 0: dx += STEP; inp = True
        if joy_up.value()    == 0: dy -= STEP; inp = True  # Y-axis: up = smaller y
        if joy_down.value()  == 0: dy += STEP; inp = True
        if dx or dy:
            # Clamp so the cat can't wander off the edges of the screen.
            cat_x = max(0, min(SCREEN_W - CAT_PX_W, cat_x + dx))
            cat_y = max(0, min(SCREEN_H - CAT_PX_H, cat_y + dy))

        # Edge-triggered food spawn: only spawn on the press event (1→0 edge),
        # not while the button is held down.
        for i, (pin, kind) in enumerate(BUTTON_FOOD):
            v = pin.value()
            if v == 0 and prev_buttons[i] == 1:   # fresh press detected
                food = spawn_food(kind)
                inp = True
            prev_buttons[i] = v   # remember this frame's reading for next frame
    else:
        # Not in a state that accepts input — just keep prev_buttons up to date.
        for i, (pin, _) in enumerate(BUTTON_FOOD):
            prev_buttons[i] = pin.value()

    if inp:
        last_input_at = t
        if state == STATE_SLEEP:   # any input wakes the cat
            state = STATE_IDLE
            state_at = t

    # ── State machine transitions ─────────────────────────────────────────────
    # Check whether the cat should move to a new state:
    #   IDLE → EAT   : cat's bounding box overlaps the food's bounding box
    #   EAT  → PURR  : EAT_MS elapsed (or celebrate if all 4 foods eaten)
    #   EAT  → title : all 4 unique foods eaten — show celebration, reset
    #   PURR → IDLE  : PURR_MS elapsed
    #   IDLE → SLEEP : no input for SLEEP_AFTER_MS ms

    if state == STATE_IDLE and food is not None:
        kind, fx, fy = food
        fw, fh = FOOD_FRAMES[kind][2], FOOD_FRAMES[kind][3]
        if aabb(cat_x, cat_y, CAT_PX_W, CAT_PX_H, fx, fy, fw, fh):
            eaten.add(kind)    # record which food was eaten for the tasting set
            food = None
            state = STATE_EAT
            state_at = t

    if state == STATE_EAT and since(state_at) > EAT_MS:
        if len(eaten) >= len(FOOD_SPRITE):
            # Just finished the 4th unique food — skip purr, celebrate.
            eaten.clear()
            play_title_screen(TITLE_SPRITE, 5000)
            state = STATE_IDLE
            state_at = now()
            last_input_at = now()
            next_nya_at = now() + NYA_MIN_MS
        else:
            state = STATE_PURR; state_at = t
    elif state == STATE_PURR and since(state_at) > PURR_MS:
        state = STATE_IDLE; state_at = t; last_input_at = t
    elif state == STATE_IDLE and utime.ticks_diff(t, last_input_at) > SLEEP_AFTER_MS:
        state = STATE_SLEEP; state_at = t

    # ── Choose sprite frame and color for this tick ───────────────────────────
    # Each state maps to one of the pre-rendered CAT_FRAMES.
    # The PURR state flashes between PINK and WHITE by toggling every 250 ms.
    # The IDLE blink is a three-frame blink at the start of every 60-frame cycle.
    if state == STATE_SLEEP:
        frame_idx = FRAME_SLEEP; cat_color = WHITE
    elif state == STATE_EAT:
        frame_idx = FRAME_HAPPY; cat_color = WHITE
    elif state == STATE_PURR:
        frame_idx = FRAME_HAPPY; cat_color = PINK if (t // 250) % 2 == 0 else WHITE
    else:
        bp = (t // FRAME_MS) % 60              # position within a 60-frame cycle
        frame_idx = FRAME_BLINK if bp < 3 else FRAME_IDLE  # blink for 3 frames
        cat_color = WHITE

    # Schedule the next random "nya~" burst while in IDLE.
    if state == STATE_IDLE and t >= next_nya_at:
        nya_until = t + NYA_DURATION_MS
        # Pick the next trigger time with a random offset so it doesn't feel robotic.
        next_nya_at = t + NYA_MIN_MS + (urandom.getrandbits(12) % (NYA_MAX_MS - NYA_MIN_MS))

    # ── Render ────────────────────────────────────────────────────────────────
    # Framebuffer pattern:
    #   (a) clear the in-memory buffer → (b) draw all sprites → (c) push to screen.
    # Because we always render a complete frame before calling lcd.show(), the
    # physical display is only updated with fully drawn frames — no tearing.
    lcd.fill(BLACK)

    # Draw the food item first so the cat renders on top.
    if food is not None:
        kind, fx, fy = food
        draw_mono(FOOD_FRAMES[kind], fx, fy, FOOD_COLOR[kind])

    draw_mono(CAT_FRAMES[frame_idx], cat_x, cat_y, cat_color)

    # Draw state-specific decorations on top of the cat.
    if state == STATE_PURR:
        hx = min(cat_x + CAT_PX_W + 4, SCREEN_W - 32)
        bob = (t // 100) % 12   # hearts gently float up and down
        draw_mono(DECO["heart"], hx,      max(0, cat_y - 8 - bob), PINK)
        draw_mono(DECO["heart"], hx + 28, min(SCREEN_H - 16, cat_y + 8 + bob), PINK)
    elif state == STATE_EAT:
        # Sparkles appear at three positions around the cat while eating.
        draw_mono(DECO["sparkle"], max(0, cat_x - 16),                  cat_y, YELLOW)
        draw_mono(DECO["sparkle"], min(cat_x + CAT_PX_W, SCREEN_W - 16), cat_y + 16, YELLOW)
        draw_mono(DECO["sparkle"], cat_x + CAT_PX_W // 2,               max(0, cat_y - 16), YELLOW)
    elif state == STATE_SLEEP:
        # Above-and-right of the head, rising diagonally
        zx = min(cat_x + 4 * CELL, SCREEN_W - 3 * CELL)
        bob = (t // 250) % 10   # slow float to suggest lazy breathing
        draw_mono(DECO["z_lo"],  zx,            max(0, cat_y - CELL              - bob), CYAN)
        draw_mono(DECO["z_mid"], zx + CELL,     max(0, cat_y - CELL - CELL // 2  - bob), CYAN)
        draw_mono(DECO["z_hi"],  zx + 2 * CELL, max(0, cat_y - 2 * CELL          - bob), CYAN)
    elif state == STATE_IDLE and t < nya_until:
        # Pop-up "nya~" speech bubble to the right of the cat's head.
        bx = min(cat_x + CAT_PX_W + 4, SCREEN_W - 64)
        draw_mono(DECO["nya"], bx, max(0, cat_y - 8), PINK)

    # Push the completed frame to the physical LCD over SPI.
    lcd.show()
    # Sleep until the next frame. This gives a stable ~16 fps and lets the
    # RP2040 idle, saving power compared to spinning at full speed.
    utime.sleep_ms(FRAME_MS)
