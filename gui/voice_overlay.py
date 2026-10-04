import math
import tkinter as tk
from functools import lru_cache
from PIL import Image, ImageDraw, ImageTk

# ── Vector icons ─────────────────────────────────────────────────────────────
# Same 24-unit stroke-icon style as the chat UI's icon set, drawn with PIL
# (supersampled for smooth edges) instead of rendering emoji glyphs.
_SS = 4                      # supersampling factor for smooth edges
_INK = (226, 236, 255, 255)  # icon colour

def _pt(p, s):
    return (p[0] * s, p[1] * s)

@lru_cache(maxsize=256)   # the overlay redraws every frame; each (icon, size) is only drawn once
def draw_icon(name: str, px: int, color=_INK) -> Image.Image:
    """Renders a 24-unit stroke icon (same style as the chat UI icons) to an RGBA image of px x px."""
    big = px * _SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = big / 24.0
    w = max(2, int(round(2 * u)))            # 2-unit stroke, like the SVG icons

    def line(pts, closed=False):
        pts = [_pt(p, u) for p in pts]
        if closed:
            pts = pts + [pts[0]]
        d.line(pts, fill=color, width=w, joint="curve")
        for x, y in (pts[0], pts[-1]):       # round caps
            d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=color)

    def arc(cx, cy, r, a0, a1):
        d.arc(((cx - r) * u, (cy - r) * u, (cx + r) * u, (cy + r) * u), a0, a1, fill=color, width=w)
        for a in (a0, a1):                   # round caps
            x = (cx + r * math.cos(math.radians(a))) * u
            y = (cy + r * math.sin(math.radians(a))) * u
            d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=color)

    if name == "mic":
        d.rounded_rectangle((9 * u, 1 * u, 15 * u, 15 * u), radius=3 * u, outline=color, width=w)
        arc(12, 11, 7, 0, 180)               # cup under the capsule
        line([(5, 10), (5, 11)]); line([(19, 10), (19, 11)])
        line([(12, 19), (12, 23)]); line([(8, 23), (16, 23)])
    elif name == "gear":
        pts = []
        teeth = 8
        for i in range(teeth * 4):
            a = math.radians(i * 360 / (teeth * 4) - 90)
            r = 10.2 if (i % 4) in (0, 1) else 7.6
            pts.append((12 + r * math.cos(a), 12 + r * math.sin(a)))
        line(pts, closed=True)
        d.ellipse((9 * u, 9 * u, 15 * u, 15 * u), outline=color, width=w)
    elif name == "speaker":
        line([(11, 5), (6, 9), (2, 9), (2, 15), (6, 15), (11, 19)], closed=True)
        arc(11, 12, 6.2, -42, 42)
        arc(11, 12, 10.6, -46, 46)
    else:
        raise ValueError(name)
    return img.resize((px, px), Image.LANCZOS)

class VoiceOverlay:
    # State Definitions
    STATE_LISTENING = 0
    STATE_RUNNING = 1
    STATE_RESPONDING = 2

    # Configuration mappings: (icon name, base icon size in px)
    STATE_CONFIG = {
        STATE_LISTENING:  {"icon": "mic",     "base_size": 80},
        STATE_RUNNING:    {"icon": "gear",    "base_size": 80},
        STATE_RESPONDING: {"icon": "speaker", "base_size": 80},
    }

    def __init__(self, x=100, y=100, size=200):
        self.size = size
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        self.root.geometry(f"{size}x{size}+{x}+{y}")
        self.root.bind("<Escape>", lambda e: self.root.destroy())

        self.canvas = tk.Canvas(
            self.root, 
            width=size, 
            height=size, 
            highlightthickness=0,
            bg="black"
        )
        self.canvas.pack(fill="both", expand=True)

        # Runtime State
        self.current_state = self.STATE_LISTENING
        self.anim_frame = 0.0
        
        # Buffer handles
        self._tk_image = None
        self._canvas_image_id = None

        # Start animation loop (~60 FPS)
        self._animate()

    def set_state(self, state: int):
        """Switches execution state and resets internal animation counter."""
        if state in self.STATE_CONFIG:
            self.current_state = state
            self.anim_frame = 0.0

    def _animate(self):
        """Internal frame renderer driven by root.after()."""
        config = self.STATE_CONFIG[self.current_state]
        icon_name = config["icon"]
        base_size = config["base_size"]

        current_size = base_size
        current_angle = 0.0

        # State 0: Listening -> Pulsing Mic
        if self.current_state == self.STATE_LISTENING:
            # Pulse amplitude +/- 15px around base size
            current_size = int(base_size + 15 * math.sin(self.anim_frame * 0.15))

        # State 1: Running -> Spinning Gear
        elif self.current_state == self.STATE_RUNNING:
            # Rotates 6 degrees per frame
            current_angle = (self.anim_frame * 6.0) % 360

        # State 2: Responding -> Pulsing Speaker
        elif self.current_state == self.STATE_RESPONDING:
            # Faster, sharper pulse for active output
            current_size = int(base_size + 20 * math.sin(self.anim_frame * 0.25))

        # Draw frame to canvas
        self._render_frame(icon_name, current_size, current_angle)

        # Advance frame step & schedule next tick (~16ms = 60fps)
        self.anim_frame += 1.0
        self.root.after(16, self._animate)

    def _render_frame(self, icon_name: str, size: int, angle: float):
        """Renders raster image buffer and updates canvas."""
        img_dim = self.size
        img = Image.new("RGBA", (img_dim, img_dim), (0, 0, 0, 0))

        # Draw the vector icon centered
        icon = draw_icon(icon_name, max(8, int(size)))
        img.alpha_composite(icon, ((img_dim - icon.width) // 2, (img_dim - icon.height) // 2))

        # Apply rotation if active
        if angle != 0.0:
            img = img.rotate(-angle, resample=Image.BICUBIC, expand=False)

        # Push to canvas
        self._tk_image = ImageTk.PhotoImage(img)
        if self._canvas_image_id is None:
            self._canvas_image_id = self.canvas.create_image(
                img_dim / 2, img_dim / 2, image=self._tk_image
            )
        else:
            self.canvas.itemconfig(self._canvas_image_id, image=self._tk_image)

    def start(self):
        self.root.mainloop()


if __name__ == "__main__":
    overlay = VoiceOverlay(x=200, y=200, size=200)

    # Demo: Cycle through states every 3 seconds
    def cycle_demo(step=0):
        states = [
            VoiceOverlay.STATE_LISTENING,
            VoiceOverlay.STATE_RUNNING,
            VoiceOverlay.STATE_RESPONDING
        ]
        next_state = states[step % len(states)]
        overlay.set_state(next_state)
        overlay.root.after(3000, cycle_demo, step + 1)

    overlay.root.after(100, cycle_demo)
    overlay.start()