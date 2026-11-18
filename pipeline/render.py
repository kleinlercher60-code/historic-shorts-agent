"""
Stick-figure renderer for Historic Shorts.

Draws one scene of a script (see scripts/*.json) frame by frame with Pillow
and pipes the frames straight into ffmpeg. Everything is drawn in a wobbly
"hand-drawn" ink style on paper, with a gentle line boil and camera push-in.

World coordinates are pixels on a 1080x1920 canvas. Actor/object x values in
the script are 0..1 (fraction of width); y values (where used) are 0..1 of height.
"""
import math, random, os, subprocess, hashlib
from ffbin import FFMPEG
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np

W, H, FPS = 1080, 1920, 30
HERE = os.path.dirname(os.path.abspath(__file__))
FONT_BOLD = os.path.join(HERE, "..", "assets", "fonts", "ComicRelief-Bold.ttf")
FONT_REG = os.path.join(HERE, "..", "assets", "fonts", "ComicRelief-Regular.ttf")

INK = (28, 26, 24)
PAPER = (247, 242, 230)
RED = (206, 58, 46)
GOLD = (236, 182, 38)
GREEN = (74, 150, 72)
BLUE = (58, 112, 190)
GREY = (150, 146, 138)
WHITE = (255, 255, 255)
COLORS = {"ink": INK, "red": RED, "gold": GOLD, "green": GREEN, "blue": BLUE,
          "grey": GREY, "white": WHITE, "paper": PAPER}

GROUND_Y = 1270          # where feet stand
LINE_W = 9               # base ink width
BOIL_EVERY = 3           # redraw jitter every N frames (~10 fps boil)

_font_cache = {}
def font(size, bold=True):
    key = (size, bold)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)
    return _font_cache[key]

def o_col(c, default):
    return col(c, default) if c else default

def col(c, default=INK):
    if c is None:
        return default
    if isinstance(c, str):
        return COLORS.get(c, default)
    return tuple(c)

def lerp(a, b, k):
    return a + (b - a) * k

def ease(k):
    k = max(0.0, min(1.0, k))
    return k * k * (3 - 2 * k)

# ----------------------------------------------------------------------------
# Paper background (computed once)
# ----------------------------------------------------------------------------
_paper = None
def paper():
    global _paper
    if _paper is None:
        rng = np.random.default_rng(7)
        base = np.ones((H, W, 3), np.float32) * np.array(PAPER, np.float32)
        noise = rng.normal(0, 4.0, (H // 4, W // 4, 1)).astype(np.float32)
        noise = np.array(Image.fromarray(np.clip(noise[..., 0] + 128, 0, 255).astype(np.uint8))
                         .resize((W, H), Image.BICUBIC), np.float32)[..., None] - 128
        yy, xx = np.mgrid[0:H, 0:W]
        d = np.sqrt(((xx - W / 2) / (W * 0.75)) ** 2 + ((yy - H / 2) / (H * 0.75)) ** 2)
        vign = (1 - 0.10 * np.clip(d, 0, 1.4) ** 2)[..., None]
        img = np.clip((base + noise) * vign, 0, 255).astype(np.uint8)
        _paper = Image.fromarray(img)
    return _paper

# ----------------------------------------------------------------------------
# Canvas with camera + hand-drawn primitives
# ----------------------------------------------------------------------------
class Canvas:
    def __init__(self, img, zoom=1.0, cx=W / 2, cy=1050, boil_seed=0, shake=(0, 0)):
        self.img = img
        self.d = ImageDraw.Draw(img)
        self.z, self.cx, self.cy = zoom, cx, cy
        self.boil = boil_seed
        self.sx, self.sy = shake

    def T(self, p):
        return ((p[0] - self.cx) * self.z + self.cx + self.sx,
                (p[1] - self.cy) * self.z + self.cy + self.sy)

    def rng(self, key):
        h = int(hashlib.md5(f"{key}|{self.boil}".encode()).hexdigest()[:8], 16)
        return random.Random(h)

    def _wobble(self, pts, key, amp=2.2):
        r = self.rng(key)
        return [(x + r.uniform(-amp, amp), y + r.uniform(-amp, amp)) for x, y in pts]

    def line(self, p1, p2, w=LINE_W, c=INK, key="l", amp=2.0):
        n = max(2, int(math.dist(p1, p2) / 40) + 1)
        pts = [(lerp(p1[0], p2[0], i / n), lerp(p1[1], p2[1], i / n)) for i in range(n + 1)]
        self.polyline(pts, w, c, key, amp)

    def polyline(self, pts, w=LINE_W, c=INK, key="pl", amp=2.0, closed=False):
        pts = [self.T(p) for p in pts]
        pts = self._wobble(pts, key, amp)
        if closed:
            pts = pts + [pts[0]]
        ww = max(1, int(w * self.z))
        self.d.line(pts, fill=c, width=ww, joint="curve")
        r = ww / 2
        for p in (pts[0], pts[-1]):
            self.d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=c)

    def poly(self, pts, fill=None, outline=INK, w=LINE_W, key="pg", amp=2.0):
        tp = self._wobble([self.T(p) for p in pts], key, amp)
        if fill is not None:
            self.d.polygon(tp, fill=fill)
        if outline is not None:
            ww = max(1, int(w * self.z))
            self.d.line(tp + [tp[0]], fill=outline, width=ww, joint="curve")

    def circle(self, c, r, fill=None, outline=INK, w=LINE_W, key="c", amp=1.6):
        n = 28
        rr = self.rng(key)
        ph = rr.uniform(0, 6.28)
        pts = []
        for i in range(n):
            a = ph + i / n * 2 * math.pi
            k = r + rr.uniform(-amp, amp)
            pts.append((c[0] + math.cos(a) * k, c[1] + math.sin(a) * k))
        tp = [self.T(p) for p in pts]
        if fill is not None:
            self.d.polygon(tp, fill=fill)
        if outline is not None:
            ww = max(1, int(w * self.z))
            self.d.line(tp + [tp[0], tp[1]], fill=outline, width=ww, joint="curve")

    def dot(self, c, r, fill=INK):
        x, y = self.T(c)
        r *= self.z
        self.d.ellipse([x - r, y - r, x + r, y + r], fill=fill)

    def text(self, xy, s, size=60, c=INK, anchor="mm", bold=True, stroke=0, stroke_c=PAPER, world=True, angle=0):
        f = font(int(size * (self.z if world else 1)), bold)
        x, y = self.T(xy) if world else xy
        if angle:
            bbox = self.d.textbbox((0, 0), s, font=f, stroke_width=stroke)
            tw, th = bbox[2] - bbox[0] + 20, bbox[3] - bbox[1] + 20
            tile = Image.new("RGBA", (int(tw), int(th)), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((tw / 2, th / 2), s, font=f, fill=c, anchor="mm",
                                      stroke_width=stroke, stroke_fill=stroke_c)
            tile = tile.rotate(angle, expand=True, resample=Image.BICUBIC)
            self.img.paste(tile, (int(x - tile.width / 2), int(y - tile.height / 2)), tile)
        else:
            self.d.text((x, y), s, font=f, fill=c, anchor=anchor, stroke_width=stroke, stroke_fill=stroke_c)


def wrap(s, maxchars):
    words, lines, cur = s.split(), [], ""
    for w_ in words:
        if len(cur) + len(w_) + (1 if cur else 0) > maxchars and cur:
            lines.append(cur); cur = w_
        else:
            cur = f"{cur} {w_}".strip()
    if cur:
        lines.append(cur)
    return lines

# ----------------------------------------------------------------------------
# Stick figure
# ----------------------------------------------------------------------------
# Angles in degrees; 0 = straight down, +90 = pointing forward (facing side),
# 180 = straight up, -90 = pointing backwards. Elbow/knee are relative bends.
POSES = {
    "stand": dict(fa=(18, -8), ba=(-16, 8), fl=(10, 0), bl=(-10, 0), lean=0),
    "point": dict(fa=(92, 0), ba=(-18, 8), fl=(10, 0), bl=(-10, 0), lean=-3),
    "cheer": dict(fa=(150, 10), ba=(-150, -10), fl=(14, 0), bl=(-14, 0), lean=0),
    "sad": dict(fa=(6, 0), ba=(-6, 0), fl=(6, 0), bl=(-6, 0), lean=10),
    "shrug": dict(fa=(55, 95), ba=(-55, -95), fl=(10, 0), bl=(-10, 0), lean=0),
    "think": dict(fa=(35, 125), ba=(-20, 60), fl=(10, 0), bl=(-10, 0), lean=0),
    "fight": dict(fa=(100, -10), ba=(-40, 70), fl=(30, -10), bl=(-30, 0), lean=-8),
    "angry": dict(fa=(40, 100), ba=(-40, -100), fl=(16, 0), bl=(-16, 0), lean=-6),
    "scared": dict(fa=(150, 40), ba=(-150, -40), fl=(18, 0), bl=(-18, 0), lean=14),
    "facepalm": dict(fa=(40, 140), ba=(-12, 6), fl=(8, 0), bl=(-8, 0), lean=8),
    "sit": dict(fa=(40, 40), ba=(-10, 40), fl=(88, -88), bl=(80, -80), lean=0, drop=95),
    "kneel": dict(fa=(20, 0), ba=(-20, 0), fl=(80, -80), bl=(-20, -70), lean=6, drop=70),
    "hold": dict(fa=(60, 40), ba=(-30, 30), fl=(10, 0), bl=(-10, 0), lean=0),
    "crown_self": dict(fa=(165, 20), ba=(-165, -20), fl=(10, 0), bl=(-10, 0), lean=0),
}
ANIMATED = {"walk", "run", "wave", "dance", "laugh"}

def pose_angles(name, t):
    if name == "walk" or name == "run":
        sp, amp = (9, 28) if name == "walk" else (14, 42)
        s = math.sin(t * sp)
        return dict(fa=(amp * 0.8 * -s, 10), ba=(amp * 0.8 * s, 10),
                    fl=(amp * s, max(0, -s) * -30), bl=(-amp * s, max(0, s) * -30),
                    lean=-4 if name == "walk" else -12, bob=abs(math.cos(t * sp)) * (8 if name == "walk" else 16))
    if name == "wave":
        return dict(fa=(150, 25 + 25 * math.sin(t * 12)), ba=(-16, 8), fl=(10, 0), bl=(-10, 0), lean=0)
    if name == "dance":
        s = math.sin(t * 8)
        return dict(fa=(120 + 30 * s, 40), ba=(-120 + 30 * s, -40), fl=(20 * s, 0), bl=(-20 * s, 0),
                    lean=10 * s, bob=abs(s) * 14)
    if name == "laugh":
        s = math.sin(t * 22)
        return dict(fa=(30, 60), ba=(-30, -60), fl=(10, 0), bl=(-10, 0), lean=-10 + 3 * s, bob=abs(s) * 5)
    return POSES.get(name, POSES["stand"])

def blend(a, b, k):
    out = {}
    for key in set(a) | set(b):
        va, vb = a.get(key, 0 if key in ("lean", "drop", "bob") else (0, 0)), b.get(key, 0 if key in ("lean", "drop", "bob") else (0, 0))
        if isinstance(va, tuple):
            out[key] = (lerp(va[0], vb[0], k), lerp(va[1], vb[1], k))
        else:
            out[key] = lerp(va, vb, k)
    return out

def limb(origin, a_deg, bend, l1, l2, facing):
    a1 = math.radians(a_deg)
    p1 = (origin[0] + math.sin(a1) * l1 * facing, origin[1] + math.cos(a1) * l1)
    a2 = math.radians(a_deg + bend)
    p2 = (p1[0] + math.sin(a2) * l2 * facing, p1[1] + math.cos(a2) * l2)
    return p1, p2, a_deg + bend

def rot(p, c, deg):
    a = math.radians(deg)
    x, y = p[0] - c[0], p[1] - c[1]
    return (c[0] + x * math.cos(a) - y * math.sin(a), c[1] + x * math.sin(a) + y * math.cos(a))


def draw_figure(cv, st, key):
    """st: dict with x, y_off, facing, scale, angles, props, face, color, label, fallen(0..1), squash"""
    s = st["scale"]
    facing = st["facing"]
    ang = st["angles"]
    L = dict(leg1=120 * s, leg2=115 * s, body=200 * s, arm1=95 * s, arm2=90 * s, head=58 * s)
    w = LINE_W * max(0.6, s)
    c = st.get("color", INK)
    drop = ang.get("drop", 0) * s
    bob = ang.get("bob", 0) * s
    feet_y = GROUND_Y - st.get("y_off", 0)
    hip = (st["x"], feet_y - L["leg1"] - L["leg2"] + drop - bob)
    lean = math.radians(ang.get("lean", 0) * facing)
    neck = (hip[0] + math.sin(lean) * L["body"], hip[1] - math.cos(lean) * L["body"])
    head_c = (neck[0] + math.sin(lean) * (L["head"] + 4), neck[1] - math.cos(lean) * (L["head"] + 4))
    shoulder = (lerp(hip[0], neck[0], 0.86), lerp(hip[1], neck[1], 0.86))

    k1, f1, _ = limb(hip, ang["fl"][0], ang["fl"][1], L["leg1"], L["leg2"], facing)
    k2, f2, _ = limb(hip, ang["bl"][0], ang["bl"][1], L["leg1"], L["leg2"], facing)
    e1, h1, fa_dir = limb(shoulder, ang["fa"][0], ang["fa"][1], L["arm1"], L["arm2"], facing)
    e2, h2, ba_dir = limb(shoulder, ang["ba"][0], ang["ba"][1], L["arm1"], L["arm2"], facing)

    # fallen: rotate the whole figure around the feet
    fall = st.get("fallen", 0)
    if fall:
        pivot = (st["x"], feet_y)
        deg = -90 * facing * fall
        lift = L["head"] * 0.9 * fall      # keep the head above the ground line
        R = lambda p: (lambda q: (q[0], q[1] - lift))(rot(p, pivot, deg))
        hip, neck, head_c, shoulder = R(hip), R(neck), R(head_c), R(shoulder)
        k1, f1, k2, f2, e1, h1, e2, h2 = map(R, (k1, f1, k2, f2, e1, h1, e2, h2))
    props = st.get("props", [])
    cape = "cape" in props
    if cape:
        cv.poly([shoulder, (shoulder[0] - 70 * s * facing, hip[1] + 60 * s), (shoulder[0] - 10 * s * facing, hip[1] + 40 * s)],
                fill=RED, outline=c, w=w * 0.7, key=key + "cape")
    # back limbs
    cv.polyline([shoulder, e2, h2], w, c, key + "ba")
    cv.polyline([hip, k2, f2], w, c, key + "bl")
    # body
    cv.line(hip, neck, w, c, key + "body")
    if "toga" in props:
        cv.poly([shoulder, (hip[0] + 34 * s * facing, hip[1] + 10 * s), (hip[0] - 30 * s * facing, hip[1] + 14 * s)],
                fill=WHITE, outline=c, w=w * 0.6, key=key + "toga")
    if "armor" in props:
        mid = (lerp(hip[0], neck[0], .5), lerp(hip[1], neck[1], .5))
        cv.circle(mid, 52 * s, fill=GREY, outline=c, w=w * .6, key=key + "arm")
    cv.polyline([hip, k1, f1], w, c, key + "fl")
    # head
    cv.circle(head_c, L["head"], fill=WHITE, outline=c, w=w, key=key + "head")
    draw_face(cv, head_c, L["head"], facing, st.get("face", "neutral"), key, w)
    # head props
    hx, hy, hr = head_c[0], head_c[1], L["head"]
    if not fall:
        if "crown" in props:
            b = hy - hr * 0.78
            pts = [(hx - hr * .75, b), (hx - hr * .75, b - hr * .75), (hx - hr * .38, b - hr * .38), (hx, b - hr * .95),
                   (hx + hr * .38, b - hr * .38), (hx + hr * .75, b - hr * .75), (hx + hr * .75, b)]
            cv.poly(pts, fill=GOLD, outline=c, w=w * .6, key=key + "crown")
        if "laurel" in props:
            for i in range(-4, 5):
                a = math.radians(180 + i * 18)
                p = (hx + math.cos(a) * hr * 1.02, hy + math.sin(a) * hr * 1.0 - hr * .15)
                cv.circle(p, 11 * s, fill=GREEN, outline=None, key=key + f"lf{i}")
        if "helmet" in props:
            pts = [(hx + math.cos(math.radians(a)) * (hr + 8 * s), hy + math.sin(math.radians(a)) * (hr + 8 * s)) for a in range(180, 361, 15)]
            cv.poly(pts, fill=GREY, outline=c, w=w * .6, key=key + "helm")
            crest = [(hx - hr * .9, hy - hr * 1.05), (hx - hr * .3, hy - hr * 1.75), (hx + hr * .5, hy - hr * 1.6), (hx + hr * .7, hy - hr * 1.0)]
            cv.poly(crest, fill=RED, outline=c, w=w * .5, key=key + "crest")
        if "beard" in props:
            pts = [(hx - hr * .6 * facing + hr * .2 * facing, hy + hr * .45), (hx + hr * .55 * facing, hy + hr * .55), (hx + hr * .2 * facing, hy + hr * 1.25)]
            cv.poly(pts, fill=INK, outline=c, w=w * .4, key=key + "beard")
        if "mustache" in props:
            mx, my = hx + hr * .28 * facing, hy + hr * .22
            cv.poly([(mx - hr * .13, my - hr * .07), (mx + hr * .13, my - hr * .07), (mx + hr * .13, my + hr * .08), (mx - hr * .13, my + hr * .08)],
                    fill=INK, outline=INK, w=w * .3, key=key + "mus")
        if "cap" in props:   # peaked officer cap
            b = hy - hr * .62
            cv.poly([(hx - hr * .95, b), (hx - hr * 1.15, b - hr * .55), (hx + hr * 1.15, b - hr * .55), (hx + hr * .95, b)],
                    fill=GREY, outline=c, w=w * .6, key=key + "cap")
            cv.poly([(hx + hr * .2 * facing, b), (hx + hr * 1.35 * facing, b + hr * .12), (hx + hr * .9 * facing, b - hr * .1)],
                    fill=INK, outline=c, w=w * .4, key=key + "visor")
        if "steelhelmet" in props:   # plain WWII helmet, no insignia
            rr = hr + 8 * s
            pts = [(hx + math.cos(math.radians(a)) * rr, hy - hr * .15 + math.sin(math.radians(a)) * rr) for a in range(180, 361, 15)]
            pts = [(hx - rr - 14 * s, hy - hr * .05)] + pts + [(hx + rr + 14 * s, hy - hr * .05)]
            cv.poly(pts, fill=o_col(st.get("helmet_color"), (96, 112, 84)), outline=c, w=w * .6, key=key + "shelm")
        if "bicorne" in props:   # Napoleon-style hat, worn sideways
            b = hy - hr * .55
            pts = [(hx - hr * 1.5, b)] + [(hx + math.cos(math.radians(a)) * hr * 1.5, b + math.sin(math.radians(a)) * hr * .95) for a in range(195, 346, 15)] + [(hx + hr * 1.5, b)]
            cv.poly(pts, fill=INK, outline=c, w=w * .5, key=key + "bic")
            cv.circle((hx, b - hr * .45), hr * .16, fill=RED, outline=None, key=key + "cock")
        if "tyrolhat" in props:  # wide-brimmed black hat with green band + feather
            b = hy - hr * .7
            cv.poly([(hx - hr * 1.45, b), (hx + hr * 1.45, b), (hx + hr * 1.3, b - hr * .18), (hx - hr * 1.3, b - hr * .18)], fill=INK, outline=c, w=w * .4, key=key + "tbrim")
            cv.poly([(hx - hr * .75, b - hr * .15), (hx - hr * .65, b - hr * .85), (hx + hr * .65, b - hr * .85), (hx + hr * .75, b - hr * .15)], fill=INK, outline=c, w=w * .4, key=key + "tcrown")
            cv.line((hx - hr * .72, b - hr * .3), (hx + hr * .72, b - hr * .3), w * .9, GREEN, key + "tband")
            cv.line((hx - hr * .55 * facing, b - hr * .4), (hx - hr * 1.1 * facing, b - hr * 1.5), w * .5, GREEN, key + "feather")
        if "firehelmet" in props:
            rr = hr + 8 * s
            pts = [(hx + math.cos(math.radians(a)) * rr, hy - hr * .2 + math.sin(math.radians(a)) * rr) for a in range(180, 361, 15)]
            pts = [(hx - rr - 10 * s * facing, hy - hr * .1)] + pts + [(hx + rr + 30 * s * facing, hy + hr * .05)]
            cv.poly(pts, fill=RED, outline=c, w=w * .6, key=key + "fhelm")
            cv.circle((hx + hr * .1 * facing, hy - hr * .75), hr * .22, fill=GOLD, outline=c, w=w * .3, key=key + "fbadge")
        if "ushanka" in props:
            cv.poly([(hx - hr * 1.0, hy - hr * .35), (hx - hr * .9, hy - hr * 1.25), (hx + hr * .9, hy - hr * 1.25), (hx + hr * 1.0, hy - hr * .35)],
                    fill=(120, 95, 70), outline=c, w=w * .6, key=key + "ush")
        if "hat" in props:
            cv.poly([(hx - hr * 1.2, hy - hr * .7), (hx + hr * 1.2, hy - hr * .7), (hx + hr * .6, hy - hr * .9),
                     (hx + hr * .5, hy - hr * 1.7), (hx - hr * .5, hy - hr * 1.7), (hx - hr * .6, hy - hr * .9)],
                    fill=INK, outline=c, w=w * .5, key=key + "hat")
    # hand props
    def along(p, deg, l):
        a = math.radians(deg)
        return (p[0] + math.sin(a) * l * facing, p[1] + math.cos(a) * l)
    if fall:
        fa_dir += -90 * fall
    if "sword" in props:
        tip = along(h1, fa_dir, 150 * s)
        if fall: tip = (h1[0] - 150 * s * facing, h1[1] - 10)
        cv.line(h1, tip, w * .8, (90, 90, 96), key + "sw")
        g1, g2 = along(h1, fa_dir + 90, 22 * s), along(h1, fa_dir - 90, 22 * s)
        cv.line(g1, g2, w * .8, c, key + "guard")
    if "spear" in props:
        a, b = along(h1, 180, 190 * s), along(h1, 0, 150 * s)
        cv.line(b, a, w * .6, (120, 80, 40), key + "sp")
        cv.poly([a, (a[0] - 14 * s, a[1] + 30 * s), (a[0] + 14 * s, a[1] + 30 * s)], fill=GREY, outline=c, w=w * .5, key=key + "spt")
    if "scroll" in props:
        cv.poly([(h1[0] - 24 * s, h1[1] - 30 * s), (h1[0] + 24 * s, h1[1] - 30 * s), (h1[0] + 24 * s, h1[1] + 30 * s), (h1[0] - 24 * s, h1[1] + 30 * s)],
                fill=(240, 225, 190), outline=c, w=w * .5, key=key + "scr")
    if "torch" in props:
        top = along(h1, 180, 70 * s)
        cv.line(h1, top, w * .8, (120, 80, 40), key + "tor")
        cv.circle((top[0], top[1] - 18 * s), 20 * s, fill=(245, 150, 40), outline=RED, w=w * .4, key=key + "fl")
    if "flag" in props or "whiteflag" in props:      # plain red (or white = surrender) banner on a pole
        top = along(h1, 180, 230 * s)
        bot = along(h1, 0, 60 * s)
        cv.line(bot, top, w * .7, (120, 80, 40), key + "fpole")
        wv = math.sin(st.get("t", 0) * 6) * 10 * s
        fx = 1 if facing > 0 else -1
        cv.poly([top, (top[0] + 150 * s * fx, top[1] + 15 * s + wv), (top[0] + 145 * s * fx, top[1] + 100 * s + wv), (top[0], top[1] + 90 * s)],
                fill=WHITE if "whiteflag" in props else RED, outline=c, w=w * .5, key=key + "flag")
    if "musket" in props:
        tip = along(h1, fa_dir, 190 * s); butt = along(h1, fa_dir + 180, 70 * s)
        cv.line(butt, tip, w * .9, (110, 75, 45), key + "mus")
        cv.line(along(h1, fa_dir, 90 * s), tip, w * .45, (90, 90, 96), key + "barrel")
    if "camera" in props:
        cv.poly([(h1[0] - 34 * s, h1[1] - 28 * s), (h1[0] + 34 * s, h1[1] - 28 * s), (h1[0] + 34 * s, h1[1] + 22 * s), (h1[0] - 34 * s, h1[1] + 22 * s)],
                fill=INK, outline=c, w=w * .4, key=key + "cam")
        cv.circle((h1[0] + 20 * s * facing, h1[1] - 3 * s), 14 * s, fill=GREY, outline=c, w=w * .3, key=key + "lens")
    for wi, (ee, hh) in enumerate(((e1, h1), (e2, h2))):
        if "watches" in props or ("watch" in props and wi == 0):
            wp = (lerp(ee[0], hh[0], .78), lerp(ee[1], hh[1], .78))
            cv.circle(wp, 22 * s, fill=GOLD, outline=c, w=w * .45, key=key + f"wat{wi}")
    if "shield" in props:
        cv.circle(h2, 52 * s, fill=RED, outline=c, w=w * .7, key=key + "sh")
        cv.circle(h2, 14 * s, fill=GOLD, outline=c, w=w * .4, key=key + "shb")
    # front arm last (over body / props)
    cv.polyline([shoulder, e1, h1], w, c, key + "fa")
    if st.get("label") and not fall:
        top = min(head_c[1] - L["head"], hip[1]) - (70 * s if any(p in props for p in ("crown", "helmet", "hat", "tyrolhat", "bicorne", "cap", "steelhelmet", "firehelmet", "ushanka")) else 30 * s)
        lx = max(40 + len(st["label"]) * 14 * s, min(W - 40 - len(st["label"]) * 14 * s, head_c[0]))
        cv.text((lx, top - 22 * s), st["label"], size=int(52 * max(.7, s)), c=st.get("label_color", INK))
    return head_c, L["head"]


def draw_face(cv, c, r, facing, face, key, w):
    x, y = c
    ex = x + r * .28 * facing
    e1, e2 = (ex - r * .22, y - r * .12), (ex + r * .22, y - r * .12)
    if face == "dead":
        for e in (e1, e2):
            q = r * .14
            cv.line((e[0] - q, e[1] - q), (e[0] + q, e[1] + q), w * .45, key=key + "x1")
            cv.line((e[0] - q, e[1] + q), (e[0] + q, e[1] - q), w * .45, key=key + "x2")
        cv.line((ex - r * .2, y + r * .4), (ex + r * .2, y + r * .4), w * .45, key=key + "m")
        return
    er = r * .085
    if face == "shock" or face == "scared":
        er = r * .12
    cv.dot(e1, er); cv.dot(e2, er)
    m = (ex, y + r * .38)
    if face in ("happy", "laugh"):
        pts = [(m[0] - r * .3, m[1] - r * .08), (m[0], m[1] + r * .14), (m[0] + r * .3, m[1] - r * .08)]
        if face == "laugh":
            cv.poly(pts, fill=INK, outline=INK, w=w * .4, key=key + "m")
        else:
            cv.polyline(pts, w * .45, key=key + "m")
    elif face in ("sad",):
        cv.polyline([(m[0] - r * .25, m[1] + r * .1), (m[0], m[1] - r * .06), (m[0] + r * .25, m[1] + r * .1)], w * .45, key=key + "m")
    elif face in ("shock", "scared"):
        cv.circle(m, r * .14, fill=INK, outline=None, key=key + "m")
    elif face == "angry":
        cv.line((e1[0] - r * .12, e1[1] - r * .25), (e1[0] + r * .14, e1[1] - r * .12), w * .45, key=key + "b1")
        cv.line((e2[0] + r * .12, e2[1] - r * .25), (e2[0] - r * .14, e2[1] - r * .12), w * .45, key=key + "b2")
        cv.line((m[0] - r * .22, m[1]), (m[0] + r * .22, m[1]), w * .45, key=key + "m")
    elif face == "smug":
        cv.polyline([(m[0] - r * .25, m[1]), (m[0] + r * .1, m[1]), (m[0] + r * .28, m[1] - r * .12)], w * .45, key=key + "m")
    elif face == "neutral":
        cv.line((m[0] - r * .16, m[1]), (m[0] + r * .16, m[1]), w * .45, key=key + "m")

# ----------------------------------------------------------------------------
# Objects / scenery
# ----------------------------------------------------------------------------
def draw_object(cv, o, t, key):
    typ = o["type"]
    x = o.get("x", .5) * W
    s = o.get("scale", 1.0)
    c = col(o.get("color"), INK)
    appear = o.get("t", 0)
    if t < appear:
        return
    pop = ease((t - appear) / 0.25) if o.get("pop", True) and appear > 0 else 1
    s *= (0.6 + 0.4 * pop) if pop < 1 else 1
    if o.get("until") is not None and t > o["until"]:
        return
    if typ == "hill":
        wd = o.get("w", .6) * W * s; ht = o.get("h", 200) * s
        pts = [(x - wd / 2 + wd * i / 20, GROUND_Y - math.sin(math.pi * i / 20) ** 1.3 * ht) for i in range(21)]
        cv.poly(pts, fill=o.get("fill") and col(o["fill"]) or (232, 226, 205), outline=c, key=key)
    elif typ == "wall":
        wd = o.get("w", .5) * W * s; ht = o.get("h", 220) * s
        x0, y0 = x - wd / 2, GROUND_Y - ht
        cv.poly([(x0, GROUND_Y), (x0, y0), (x0 + wd, y0), (x0 + wd, GROUND_Y)], fill=(225, 205, 170), outline=c, key=key)
        rows = int(ht // 55)
        for r_ in range(1, rows + 1):
            yy = y0 + r_ * ht / (rows + 1)
            cv.line((x0, yy), (x0 + wd, yy), LINE_W * .5, c, key + f"r{r_}")
        # battlements
        n = max(3, int(wd // 70))
        for i in range(0, n, 2):
            bx = x0 + i * wd / n
            cv.poly([(bx, y0), (bx, y0 - 40 * s), (bx + wd / n, y0 - 40 * s), (bx + wd / n, y0)], fill=(225, 205, 170), outline=c, key=key + f"b{i}")
    elif typ == "city":
        wd = o.get("w", .7) * W * s
        x0 = x - wd / 2
        heights = [260, 380, 300, 460, 280, 340]
        n = len(heights)
        for i, hh in enumerate(heights):
            bx = x0 + i * wd / n; bw = wd / n * .92; hh *= s
            cv.poly([(bx, GROUND_Y), (bx, GROUND_Y - hh), (bx + bw, GROUND_Y - hh), (bx + bw, GROUND_Y)], fill=(236, 226, 205), outline=c, key=key + f"h{i}")
            if i % 2 == 1:
                cv.poly([(bx - 8, GROUND_Y - hh), (bx + bw / 2, GROUND_Y - hh - 70 * s), (bx + bw + 8, GROUND_Y - hh)], fill=RED, outline=c, key=key + f"rf{i}")
            cv.poly([(bx + bw * .35, GROUND_Y), (bx + bw * .35, GROUND_Y - 60 * s), (bx + bw * .65, GROUND_Y - 60 * s), (bx + bw * .65, GROUND_Y)], fill=INK, outline=c, w=4, key=key + f"d{i}")
        if o.get("label"):
            cv.text((x, GROUND_Y - 560 * s), o["label"], size=70, c=c)
    elif typ == "temple":
        wd = o.get("w", .55) * W * s; ht = 330 * s
        x0 = x - wd / 2
        cv.poly([(x0 - 20, GROUND_Y - ht), (x, GROUND_Y - ht - 120 * s), (x0 + wd + 20, GROUND_Y - ht)], fill=(240, 232, 214), outline=c, key=key + "p")
        cv.line((x0 - 20, GROUND_Y - ht), (x0 + wd + 20, GROUND_Y - ht), LINE_W, c, key + "ar")
        for i in range(5):
            cx_ = x0 + 30 + i * (wd - 60) / 4
            cv.poly([(cx_ - 16, GROUND_Y), (cx_ - 16, GROUND_Y - ht), (cx_ + 16, GROUND_Y - ht), (cx_ + 16, GROUND_Y)], fill=WHITE, outline=c, w=6, key=key + f"c{i}")
    elif typ == "throne":
        cv.poly([(x - 80 * s, GROUND_Y), (x - 80 * s, GROUND_Y - 330 * s), (x + 80 * s, GROUND_Y - 330 * s), (x + 80 * s, GROUND_Y)], fill=GOLD, outline=c, key=key)
        cv.poly([(x - 100 * s, GROUND_Y - 140 * s), (x + 100 * s, GROUND_Y - 140 * s), (x + 100 * s, GROUND_Y - 110 * s), (x - 100 * s, GROUND_Y - 110 * s)], fill=RED, outline=c, w=6, key=key + "s")
    elif typ == "grave":
        cv.poly([(x - 75 * s, GROUND_Y), (x - 75 * s, GROUND_Y - 150 * s), (x - 40 * s, GROUND_Y - 200 * s), (x + 40 * s, GROUND_Y - 200 * s), (x + 75 * s, GROUND_Y - 150 * s), (x + 75 * s, GROUND_Y)],
                fill=(205, 202, 195), outline=c, key=key)
        gt = o.get("text", "RIP")
        cv.text((x, GROUND_Y - 120 * s), gt, size=int(min(46, 190 / max(1, len(gt))) * s), c=INK)
    elif typ == "tree":
        cv.line((x, GROUND_Y), (x, GROUND_Y - 260 * s), LINE_W * 1.6, (110, 75, 45), key + "tr")
        cv.circle((x, GROUND_Y - 330 * s), 120 * s, fill=GREEN, outline=c, key=key + "cr")
        if o.get("blossom"):
            r = cv.rng(key + "bl")
            for i in range(14):
                a = r.uniform(0, 2 * math.pi); rr = r.uniform(0, 95) * s
                cv.circle((x + math.cos(a) * rr, GROUND_Y - 330 * s + math.sin(a) * rr), 13 * s, fill=WHITE, outline=c, w=3, key=key + f"bs{i}")
    elif typ == "sun":
        cx_, cy_ = x, o.get("y", .24) * H
        for i in range(10):
            a = i * math.pi / 5 + t * .3
            cv.line((cx_ + math.cos(a) * 95, cy_ + math.sin(a) * 95), (cx_ + math.cos(a) * 135, cy_ + math.sin(a) * 135), LINE_W * .7, GOLD, key + f"ray{i}")
        cv.circle((cx_, cy_), 75, fill=GOLD, outline=c, key=key)
    elif typ == "river":
        y = GROUND_Y + 40
        for j in range(3):
            pts = [(i * W / 24, y + j * 45 + math.sin(i * .9 + t * 3 + j) * 10) for i in range(25)]
            cv.polyline(pts, LINE_W * .7, BLUE, key + f"rv{j}")
    elif typ == "basket":
        y = GROUND_Y - 10 - o.get("bob", 0) * abs(math.sin(t * 3))
        cv.poly([(x - 90 * s, y - 70 * s), (x + 90 * s, y - 70 * s), (x + 65 * s, y), (x - 65 * s, y)], fill=(205, 160, 95), outline=c, key=key)
        for i in range(o.get("babies", 2)):
            bx = x - 30 * s + i * 60 * s
            cv.circle((bx, y - 95 * s), 26 * s, fill=WHITE, outline=c, w=6, key=key + f"bb{i}")
            cv.dot((bx + 8 * s, y - 98 * s), 3.5 * s)
    elif typ == "wolf":
        # simple sketchy wolf: body ellipse, head triangle, legs, tail
        f = o.get("facing", 1)
        by = GROUND_Y - 120 * s
        body = [(x + math.cos(math.radians(a)) * 130 * s, by + math.sin(math.radians(a)) * 55 * s) for a in range(0, 360, 20)]
        cv.poly(body, fill=(160, 155, 150), outline=c, key=key + "b")
        hx = x + 135 * s * f
        cv.poly([(hx - 10 * s * f, by - 60 * s), (hx + 75 * s * f, by - 20 * s), (hx - 5 * s * f, by + 20 * s)], fill=(160, 155, 150), outline=c, key=key + "h")
        cv.poly([(hx - 5 * s * f, by - 55 * s), (hx + 10 * s * f, by - 105 * s), (hx + 25 * s * f, by - 45 * s)], fill=(160, 155, 150), outline=c, key=key + "e")
        cv.dot((hx + 25 * s * f, by - 25 * s), 6 * s)
        for i, lx in enumerate((-80, -45, 50, 85)):
            cv.line((x + lx * s, by + 40 * s), (x + lx * s, GROUND_Y), LINE_W, c, key + f"l{i}")
        cv.polyline([(x - 125 * s * f, by - 10 * s), (x - 190 * s * f, by - 60 * s - 10 * math.sin(t * 8) * s)], LINE_W * 1.3, c, key + "t")
    elif typ == "sign":
        y = GROUND_Y
        cv.line((x, y), (x, y - 260 * s), LINE_W * 1.2, (120, 80, 40), key + "post")
        lines = wrap(o.get("text", ""), 12)
        bw, bh = 300 * s, (60 + 58 * len(lines)) * s
        top = y - 250 * s - bh
        cv.poly([(x - bw / 2, top), (x + bw / 2, top), (x + bw / 2, top + bh), (x - bw / 2, top + bh)], fill=(238, 214, 165), outline=c, key=key)
        for i, ln in enumerate(lines):
            cv.text((x, top + 55 * s + i * 58 * s), ln, size=int(48 * s), c=INK)
    elif typ == "text":
        y = o.get("y", .3) * H
        size = o.get("size", 90)
        lines = wrap(o["text"], o.get("wrap", 18))
        for i, ln in enumerate(lines):
            cv.text((x, y + i * size * 1.1), ln, size=size, c=c, angle=o.get("angle", 0), stroke=o.get("stroke", 0))
    elif typ == "arrow":
        p1 = (o["x1"] * W, o["y1"] * H); p2 = (o["x2"] * W, o["y2"] * H)
        k = ease((t - appear) / 0.35) if appear > 0 else 1
        p2 = (lerp(p1[0], p2[0], k), lerp(p1[1], p2[1], k))
        cv.line(p1, p2, LINE_W, c, key)
        a = math.atan2(p2[1] - p1[1], p2[0] - p1[0])
        for da in (2.6, -2.6):
            cv.line(p2, (p2[0] + math.cos(a + da) * 45, p2[1] + math.sin(a + da) * 45), LINE_W, c, key + f"h{da}")
    elif typ == "map":
        # very rough boot of Italy + label, for "where" moments
        cx_, cy_ = x, o.get("y", .42) * H
        k = 1.0 * s
        boot = [(0, -300), (60, -260), (90, -150), (150, -40), (230, 60), (290, 130), (250, 170), (190, 120),
                (200, 230), (150, 300), (110, 220), (60, 120), (-10, 20), (-80, -100), (-130, -230), (-60, -290)]
        cv.poly([(cx_ + px * k, cy_ + py * k) for px, py in boot], fill=(225, 235, 205), outline=c, key=key)
        if o.get("pin"):
            px, py = o["pin"]
            pin = (cx_ + px * k, cy_ + py * k)
            cv.circle(pin, 18, fill=RED, outline=c, w=5, key=key + "pin")
            if o.get("label"):
                cv.text((pin[0] + 130, pin[1] - 40), o["label"], size=60, c=RED)
    elif typ == "blood":
        # cartoon splat (kept cartoonish on purpose)
        cx_, cy_ = x, o.get("y", (GROUND_Y - 10) / H) * H
        pts = []
        r = cv.rng(key)
        for i in range(16):
            a = i / 16 * 2 * math.pi
            rr = (60 if i % 2 else 34) * s * r.uniform(.8, 1.2)
            pts.append((cx_ + math.cos(a) * rr * 1.6, cy_ + math.sin(a) * rr * .45))
        cv.poly(pts, fill=RED, outline=None, key=key)
    elif typ == "bunker":
        wd = o.get("w", .5) * W * s; ht = o.get("h", 170) * s
        x0 = x - wd / 2
        cv.poly([(x0, GROUND_Y), (x0 + 30 * s, GROUND_Y - ht), (x0 + wd - 30 * s, GROUND_Y - ht), (x0 + wd, GROUND_Y)], fill=(190, 186, 176), outline=c, key=key)
        cv.poly([(x - 45 * s, GROUND_Y), (x - 45 * s, GROUND_Y - 95 * s), (x + 45 * s, GROUND_Y - 95 * s), (x + 45 * s, GROUND_Y)], fill=INK, outline=c, w=5, key=key + "door")
        if o.get("label"):
            cv.text((x, GROUND_Y - ht - 50 * s), o["label"], size=int(52 * s), c=c)
    elif typ == "reichstag":
        wd = o.get("w", .7) * W * s; ht = 300 * s
        x0 = x - wd / 2
        cv.poly([(x0, GROUND_Y), (x0, GROUND_Y - ht), (x0 + wd, GROUND_Y - ht), (x0 + wd, GROUND_Y)], fill=(232, 222, 200), outline=c, key=key + "b")
        for i in (0, 1):   # corner towers
            tx = x0 + i * (wd - wd * .16)
            cv.poly([(tx, GROUND_Y - ht), (tx, GROUND_Y - ht - 110 * s), (tx + wd * .16, GROUND_Y - ht - 110 * s), (tx + wd * .16, GROUND_Y - ht)], fill=(232, 222, 200), outline=c, key=key + f"t{i}")
        # portico columns
        for i in range(6):
            cx_ = x - wd * .22 + i * wd * .44 / 5
            cv.line((cx_, GROUND_Y), (cx_, GROUND_Y - ht * .78), LINE_W * .6, c, key + f"c{i}")
        cv.poly([(x - wd * .27, GROUND_Y - ht * .78), (x, GROUND_Y - ht - 20 * s), (x + wd * .27, GROUND_Y - ht * .78)], fill=(240, 232, 214), outline=c, key=key + "ped")
        # (bombed-out) dome skeleton
        dr = wd * .2
        for a in range(0, 181, 30):
            ar = math.radians(180 + a)
            cv.line((x, GROUND_Y - ht - 20 * s), (x + math.cos(ar) * dr, GROUND_Y - ht - 20 * s + math.sin(ar) * dr * .9), LINE_W * .5, c, key + f"d{a}")
        cv.polyline([(x + math.cos(math.radians(a)) * dr, GROUND_Y - ht - 20 * s + math.sin(math.radians(a)) * dr * .9) for a in range(180, 361, 15)], LINE_W * .6, c, key + "dome")
    elif typ == "warmap":   # board on an easel, generic front lines + optional little division boxes
        wd = o.get("w", .42) * W * s; ht = wd * .75
        cy_ = o.get("y", .44) * H
        x0, y0 = x - wd / 2, cy_ - ht / 2
        cv.line((x - wd * .3, GROUND_Y), (x - wd * .2, y0 + ht), LINE_W * .8, (120, 80, 40), key + "leg1")
        cv.line((x + wd * .3, GROUND_Y), (x + wd * .2, y0 + ht), LINE_W * .8, (120, 80, 40), key + "leg2")
        cv.poly([(x0, y0), (x0 + wd, y0), (x0 + wd, y0 + ht), (x0, y0 + ht)], fill=(225, 235, 205), outline=c, key=key)
        cv.circle((x0 + wd * .55, y0 + ht * .5), 26 * s, fill=RED, outline=c, w=5, key=key + "pin")
        for i, (bx, by) in enumerate(o.get("boxes", [])):
            px, py = x0 + bx * wd, y0 + by * ht
            cv.poly([(px - 28 * s, py - 18 * s), (px + 28 * s, py - 18 * s), (px + 28 * s, py + 18 * s), (px - 28 * s, py + 18 * s)], fill=BLUE, outline=c, w=4, key=key + f"bx{i}")
        if o.get("label"):
            cv.text((x, y0 - 40 * s), o["label"], size=int(50 * s), c=c)
    elif typ == "towers":   # two tall outlined towers (memorial-style, no impact imagery)
        ht = o.get("h", 620) * s; tw = 120 * s; gap = 40 * s
        fill = o.get("fill") and col(o["fill"]) or (220, 224, 230)
        for i, dx in enumerate((-(tw + gap / 2), gap / 2)):
            x0 = x + dx; hh = ht if i == 0 else ht * .97
            cv.poly([(x0, GROUND_Y), (x0, GROUND_Y - hh), (x0 + tw, GROUND_Y - hh), (x0 + tw, GROUND_Y)], fill=fill, outline=c, key=key + f"tw{i}")
            for j in range(1, 4):
                cv.line((x0 + tw * j / 4, GROUND_Y - hh + 10), (x0 + tw * j / 4, GROUND_Y - 10), LINE_W * .3, GREY, key + f"tl{i}{j}")
        if i == 0 and o.get("antenna", True):
            pass
        cv.line((x - tw / 2 - gap / 2, GROUND_Y - ht), (x - tw / 2 - gap / 2, GROUND_Y - ht - 110 * s), LINE_W * .6, c, key + "ant")
    elif typ == "lights":   # Tribute in Light: two vertical beams
        for i, dx in enumerate((-80, 80)):
            bx = x + dx * s
            cv.poly([(bx - 40 * s, GROUND_Y), (bx - 70 * s, 260), (bx + 70 * s, 260), (bx + 40 * s, GROUND_Y)], fill=(200, 220, 245), outline=None, key=key + f"li{i}")
    elif typ == "boat":
        f = o.get("facing", 1)
        y = GROUND_Y + 30 + math.sin(t * 2.5 + x) * 6
        wd = 260 * s
        cv.poly([(x - wd / 2, y - 60 * s), (x + wd / 2, y - 60 * s), (x + wd * .38, y), (x - wd * .38, y)], fill=o.get("fill") and col(o["fill"]) or WHITE, outline=c, key=key + "hull")
        cv.poly([(x - wd * .25, y - 60 * s), (x - wd * .25, y - 130 * s), (x + wd * .2, y - 130 * s), (x + wd * .2, y - 60 * s)], fill=(236, 226, 205), outline=c, key=key + "cab")
        for i in range(3):
            cv.circle((x - wd * .15 + i * wd * .14, y - 95 * s), 11 * s, fill=BLUE, outline=c, w=3, key=key + f"win{i}")
    elif typ == "plane":
        f = o.get("facing", 1); y = o.get("y", (GROUND_Y - 70) / H) * H
        L = 300 * s
        body = [(x + math.cos(math.radians(a)) * L / 2, y + math.sin(math.radians(a)) * 34 * s) for a in range(0, 360, 20)]
        cv.poly(body, fill=WHITE, outline=c, key=key + "body")
        cv.poly([(x - 30 * s * f, y), (x + 20 * s * f, y), (x - 60 * s * f, y + 110 * s), (x - 95 * s * f, y + 110 * s)], fill=(220, 224, 230), outline=c, w=6, key=key + "wing")
        cv.poly([(x - L * .38 * f, y - 20 * s), (x - L * .5 * f, y - 100 * s), (x - L * .42 * f, y - 100 * s), (x - L * .28 * f, y - 20 * s)], fill=(220, 224, 230), outline=c, w=6, key=key + "tail")
        for i in range(5):
            cv.dot((x - 60 * s * f + i * 35 * s * f, y - 8 * s), 6 * s, fill=BLUE)
    elif typ == "mountains":   # Alps backdrop with snow caps
        wd = o.get("w", 1.1) * W; ht = o.get("h", 420) * s
        peaks = [(-.45, .75), (-.18, 1.0), (.1, .8), (.35, .95)]
        for i, (dx, hk) in enumerate(peaks):
            px, ph = x + dx * wd, ht * hk; bw = wd * .22
            cv.poly([(px - bw, GROUND_Y), (px, GROUND_Y - ph), (px + bw, GROUND_Y)], fill=(205, 205, 200), outline=c, key=key + f"m{i}")
            cv.poly([(px - bw * .28, GROUND_Y - ph * .72), (px, GROUND_Y - ph), (px + bw * .28, GROUND_Y - ph * .72), (px + bw * .1, GROUND_Y - ph * .78), (px - bw * .08, GROUND_Y - ph * .7)], fill=WHITE, outline=c, w=5, key=key + f"sn{i}")
    elif typ == "hut":
        wd = 260 * s; ht = 170 * s; x0 = x - wd / 2
        cv.poly([(x0, GROUND_Y), (x0, GROUND_Y - ht), (x0 + wd, GROUND_Y - ht), (x0 + wd, GROUND_Y)], fill=(170, 120, 75), outline=c, key=key + "w")
        for j in range(1, 4):
            cv.line((x0, GROUND_Y - ht * j / 4), (x0 + wd, GROUND_Y - ht * j / 4), LINE_W * .4, c, key + f"lg{j}")
        cv.poly([(x0 - 30 * s, GROUND_Y - ht), (x, GROUND_Y - ht - 110 * s), (x0 + wd + 30 * s, GROUND_Y - ht)], fill=(120, 85, 55), outline=c, key=key + "roof")
        cv.poly([(x - 30 * s, GROUND_Y), (x - 30 * s, GROUND_Y - 100 * s), (x + 30 * s, GROUND_Y - 100 * s), (x + 30 * s, GROUND_Y)], fill=INK, outline=c, w=5, key=key + "door")
    elif typ == "pedestal":
        wd = o.get("w", .3) * W * s; ht = o.get("h", 200) * s; x0 = x - wd / 2
        cv.poly([(x0, GROUND_Y), (x0, GROUND_Y - ht), (x0 + wd, GROUND_Y - ht), (x0 + wd, GROUND_Y)], fill=(205, 202, 195), outline=c, key=key)
        if o.get("text"):
            cv.text((x, GROUND_Y - ht / 2), o["text"], size=int(min(50, 260 / max(1, len(o["text"]))) * s), c=INK)
    elif typ == "army":
        n = o.get("n", 6); x0 = o.get("x0", .1) * W; x1 = o.get("x1", .9) * W
        f = o.get("facing", 1)
        for i in range(n):
            xx = lerp(x0, x1, i / max(1, n - 1))
            st = dict(x=xx, facing=f, scale=o.get("scale", .45), angles=pose_angles(o.get("pose", "stand"), t + i * .3),
                      props=o.get("props", ["helmet", "spear"]), face=o.get("face", "angry"), color=c)
            st["y_off"] = -o.get("depth", 0) * 1.0
            draw_figure(cv, st, key + f"s{i}")

# ----------------------------------------------------------------------------
# Actor timeline evaluation
# ----------------------------------------------------------------------------
BLEND = 0.18

def actor_state(a, t):
    st = dict(x=a.get("x", .5) * W, facing=a.get("facing", 1), scale=a.get("scale", 1.0),
              props=list(a.get("props", [])), face=a.get("face", "neutral"), label=a.get("label"),
              color=col(a.get("color"), INK), y_off=a.get("y_off", 0), fallen=0, visible=True, label_color=col(a.get("label_color"), INK))
    pose, prev_pose, pose_t = a.get("pose", "stand"), a.get("pose", "stand"), -10
    bubble = None
    enter = a.get("enter")
    if enter is not None:
        et = enter.get("t", 0)
        if t < et:
            st["visible"] = False
    for ac in sorted(a.get("actions", []), key=lambda q: q["t"]):
        at = ac["t"]
        if t < at:
            break
        do = ac["do"]
        dur = ac.get("dur", 0.6)
        k = ease((t - at) / dur) if dur > 0 else 1
        if do == "pose":
            prev_pose, pose, pose_t = pose, ac["pose"], at
        elif do == "face":
            st["face"] = ac["face"]
        elif do == "flip":
            st["facing"] *= -1
        elif do == "walk_to" or do == "run_to":
            x0 = st["x"]; x1 = ac["x"] * W
            st["x"] = lerp(x0, x1, ((t - at) / dur) if t - at < dur else 1)
            if t - at < dur:
                st["_anim"] = "walk" if do == "walk_to" else "run"
                st["facing"] = 1 if x1 > x0 else -1
            elif ac.get("face_after"):
                st["facing"] = ac["face_after"]
        elif do == "say":
            if t - at < ac.get("dur", 1.6):
                bubble = dict(text=ac["text"], age=t - at, shout=ac.get("shout", False))
        elif do == "fall":
            st["fallen"] = k
            st["face"] = "dead" if ac.get("dead", True) else st["face"]
        elif do == "jump":
            if t - at < dur:
                q = (t - at) / dur
                st["y_off"] = math.sin(q * math.pi) * ac.get("h", 120)
        elif do == "add_prop":
            st["props"].append(ac["prop"])
        elif do == "remove_prop":
            st["props"] = [p for p in st["props"] if p != ac["prop"]]
        elif do == "shake":
            if t - at < dur:
                st["x"] += math.sin((t - at) * 60) * 8
        elif do == "exit":
            st["visible"] = t - at < dur
            st["x"] += (W * .8 * ac.get("dir", 1)) * ((t - at) / dur)
            st["_anim"] = "run"
        elif do == "grow":
            st["scale"] = lerp(st["scale"], ac.get("scale", 1.4), k)
        elif do == "hide":
            st["visible"] = False
        elif do == "show":
            st["visible"] = True
    if enter is not None and t >= enter.get("t", 0):
        et, ed = enter.get("t", 0), enter.get("dur", .55)
        q = (t - et) / ed
        if q < 1:
            if enter.get("from") == "pop":
                st["scale"] *= .3 + .7 * ease(q) + math.sin(q * math.pi) * .12
            else:
                side = -1 if enter.get("from", "left") == "left" else 1
                st["x"] = lerp(W / 2 + side * W * .75, st["x"], q)
                st["_anim"] = "run"
                st["facing"] = -side
    anim = st.pop("_anim", None)
    ang_now = pose_angles(anim or pose, t)
    if not anim and pose not in ANIMATED and prev_pose != pose and t - pose_t < BLEND:
        ang_now = blend(pose_angles(prev_pose, t), pose_angles(pose, t), ease((t - pose_t) / BLEND))
    if st["fallen"] > 0:
        limp = dict(fa=(10, 0), ba=(-6, 0), fl=(5, 0), bl=(-5, 0), lean=0)
        ang_now = blend(ang_now, limp, min(1, st["fallen"] * 1.5))
    st["angles"] = ang_now
    if pose in ("cheer", "laugh") and st["face"] == "neutral":
        st["face"] = "happy"
    return st, bubble


def draw_bubble(cv, head, hr, b, facing, key, scale=1.0):
    lines = wrap(b["text"], 14)
    size = 54
    lw = max(len(l) for l in lines) * size * .55 + 70
    lh = len(lines) * size * 1.15 + 50
    pop = min(1, .6 + b["age"] / .15 * .4)
    lw, lh = lw * pop, lh * pop
    cx = head[0] + 60 * facing
    cx = max(lw / 2 + 30, min(W - lw / 2 - 30, cx))
    cy = head[1] - hr - lh / 2 - 150
    pts = []
    for i in range(30):
        a = i / 30 * 2 * math.pi
        pts.append((cx + math.cos(a) * lw / 2, cy + math.sin(a) * lh / 2))
    tail = [(cx - 20, cy + lh / 2 - 12), (head[0] + 10 * facing, head[1] - hr - 10), (cx + 30, cy + lh / 2 - 12)]
    fill = (255, 245, 160) if b["shout"] else WHITE
    cv.poly(tail, fill=fill, outline=INK, w=6, key=key + "t")
    cv.poly(pts, fill=fill, outline=INK, w=7, key=key + "b")
    cv.poly(tail, fill=fill, outline=None, key=key + "t")
    for i, ln in enumerate(lines):
        cv.text((cx, cy - (len(lines) - 1) * size * .575 + i * size * 1.15), ln, size=int(size * pop), c=RED if b["shout"] else INK)

# ----------------------------------------------------------------------------
# HUD: header, year stamp, captions, end card
# ----------------------------------------------------------------------------
def draw_header(d, img, scene, t):
    if scene.get("year"):
        f = font(58)
        s = scene["year"]
        bb = d.textbbox((0, 0), s, font=f)
        w_, h_ = bb[2] - bb[0] + 50, bb[3] - bb[1] + 34
        x0, y0 = 70, 260
        d.rounded_rectangle([x0, y0, x0 + w_, y0 + h_], radius=14, fill=INK)
        d.text((x0 + w_ / 2, y0 + h_ / 2 + 2), s, font=f, fill=PAPER, anchor="mm")
    if scene.get("title"):
        k = ease(t / .3)
        f = font(int(96 * (0.85 + .15 * k)))
        lines = wrap(scene["title"], 16)
        for i, ln in enumerate(lines):
            d.text((W / 2, 440 + i * 105), ln, font=f, fill=INK, anchor="mm")


def draw_captions(d, words, t):
    """words: list of dict(word, start, end) in scene-local time."""
    if not words:
        return
    # chunk into groups of max 3 words, break after punctuation
    chunks, cur = [], []
    for w_ in words:
        cur.append(w_)
        if len(cur) == 3 or w_["word"][-1:] in ".,!?;:":
            chunks.append(cur); cur = []
    if cur:
        chunks.append(cur)
    active = None
    for ch in chunks:
        if ch[0]["start"] - 0.05 <= t < ch[-1]["end"] + 0.12:
            active = ch
    if active is None:
        return
    y = 1530
    parts = [w_["word"].upper() for w_ in active]
    size = 84
    while True:
        f = font(size)
        widths = [d.textlength(p + " ", font=f) for p in parts]
        total = sum(widths) - d.textlength(" ", font=f)
        if total < W - 110 or size <= 50:
            break
        size -= 4
    x = W / 2 - total / 2
    for w_, p, wd in zip(active, parts, widths):
        on = w_["start"] <= t
        cur = w_["start"] <= t < w_["end"] + 0.05
        fill = RED if cur else (INK if on else (INK))
        lift = -6 if cur else 0
        d.text((x, y + lift), p, font=f, fill=fill, anchor="lm", stroke_width=8, stroke_fill=PAPER)
        x += wd


def draw_endcard(d, handle, k):
    if not handle:
        return
    f = font(64)
    s = f"Follow {handle}"
    bb = d.textbbox((0, 0), s, font=f)
    w_, h_ = bb[2] - bb[0] + 70, bb[3] - bb[1] + 44
    x0, y0 = W / 2 - w_ / 2, 1680 + (1 - ease(k)) * 120
    d.rounded_rectangle([x0, y0, x0 + w_, y0 + h_], radius=26, fill=RED)
    d.text((W / 2, y0 + h_ / 2 + 2), s, font=f, fill=WHITE, anchor="mm")

# ----------------------------------------------------------------------------
# Scene → frames → mp4
# ----------------------------------------------------------------------------
def render_frame(scene, t, frame_idx, words, dur, handle=None, is_last=False, global_t=0):
    img = paper().copy()
    zoom = 1.0 + scene.get("zoom", 0.05) * (t / max(dur, .01))
    shake = (0, 0)
    for sh in scene.get("shakes", []):
        if sh <= t < sh + .3:
            q = 1 - (t - sh) / .3
            shake = (math.sin(t * 90) * 18 * q, math.cos(t * 70) * 12 * q)
    cv = Canvas(img, zoom=zoom, boil_seed=frame_idx // BOIL_EVERY, shake=shake)
    if scene.get("ground", True):
        cv.line((-60, GROUND_Y + 4), (W + 60, GROUND_Y + 4), LINE_W, INK, "ground", amp=3)
    objs = scene.get("objects", [])
    for i, o in enumerate([o for o in objs if not o.get("front")]):
        draw_object(cv, o, t, f"o{i}{o['type']}")
    for i, a in enumerate(scene.get("actors", [])):
        st, bubble = actor_state(a, t)
        if not st["visible"]:
            continue
        if bubble:
            st["label"] = None          # the bubble already says who is talking
        head, hr = draw_figure(cv, st, f"a{i}")
        if bubble:
            draw_bubble(cv, head, hr, bubble, st["facing"], f"bub{i}")
    for i, o in enumerate([o for o in objs if o.get("front")]):
        draw_object(cv, o, t, f"of{i}{o['type']}")
    d = ImageDraw.Draw(img)
    draw_header(d, img, scene, t)
    draw_captions(d, words, t)
    if is_last and handle and t > dur - 2.2:
        draw_endcard(d, handle, (t - (dur - 2.2)) / .4)
    return img


def render_scene(scene, dur, words, out_path, handle=None, is_last=False):
    n = int(round(dur * FPS))
    tmp = out_path[:-4] + ".part.mp4"
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-r", str(FPS), tmp]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(n):
        t = i / FPS
        img = render_frame(scene, t, i, words, dur, handle, is_last)
        p.stdin.write(img.tobytes())
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError(f"ffmpeg failed for {out_path}")
    os.replace(tmp, out_path)
    return n
