import math
import tkinter as tk
from PIL import Image, ImageDraw, ImageFont, ImageTk

class VoiceOverlay:
    # State Definitions
    STATE_LISTENING = 0
    STATE_RUNNING = 1
    STATE_RESPONDING = 2

    # Configuration mappings: (Emoji character, Base Font Size)
    STATE_CONFIG = {
        STATE_LISTENING:  {"emoji": "🎙️", "base_size": 80},
        STATE_RUNNING:    {"emoji": "⚙️", "base_size": 80},
        STATE_RESPONDING: {"emoji": "🔊", "base_size": 80},
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
        emoji_char = config["emoji"]
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
        self._render_frame(emoji_char, current_size, current_angle)

        # Advance frame step & schedule next tick (~16ms = 60fps)
        self.anim_frame += 1.0
        self.root.after(16, self._animate)

    def _render_frame(self, char: str, size: int, angle: float):
        """Renders raster image buffer and updates canvas."""
        img_dim = self.size
        img = Image.new("RGBA", (img_dim, img_dim), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        try:
            # Font fallbacks for major OS color emoji fonts
            font = ImageFont.truetype("seguiemj.ttf", size)
        except OSError:
            font = ImageFont.load_default()

        # Render emoji centered
        draw.text(
            (img_dim / 2, img_dim / 2),
            char,
            font=font,
            anchor="mm",
            embedded_color=True
        )

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