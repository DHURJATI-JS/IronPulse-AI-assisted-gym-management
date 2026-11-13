#!/usr/bin/env python3
"""Render IronPulse's "AI-generated" exercise demonstration videos - no external assets, no downloads.

A stylised mannequin (rounded, gradient-shaded limbs with a soft glow on a dark gradient stage) is driven
by keyframed joint angles through forward kinematics, with two-bone IK for hands and feet that must stay
planted. Frames are rasterised with numpy signed-distance fields at 2x supersampling, finished with Pillow
(HUD, titles) and encoded to H.264 by the ffmpeg binary that ships with imageio-ffmpeg.

    python scripts/generate_videos.py                         # every video -> app/static/videos
    python scripts/generate_videos.py --only push-up          # one video (repeatable, or a comma list)
    python scripts/generate_videos.py --quick --out /tmp/v    # fast low-fps preview (used by the tests)
    python scripts/generate_videos.py --list                  # catalogue with planned lengths
    python scripts/generate_videos.py --only push-up --still 6 --still 9   # PNG stills for design checks

Output per video: ``<slug>.mp4`` (960x540, 24 fps, H.264 yuv420p, crf 26, +faststart) and ``<slug>.jpg``
(poster), plus ``manifest.json`` ``{"videos": [{slug, title, duration, file, poster, ...}]}`` which
``app.services.content.sync_videos`` reads. Deterministic (fixed seeds, no clocks in the output) and
cross-platform (pathlib only; fonts fall back to Pillow's built-in face when no system font is found).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

from app.data.videos import VIDEO_BY_SLUG, VIDEOS, VideoSpec  # noqa: E402

W, H = 960, 540
SS = 2  # supersampling factor for the figure layer
FPS = 24
QUICK_FPS = 8
CRF = 26
DEFAULT_OUT = ROOT / "app" / "static" / "videos"
MANIFEST = "manifest.json"
WATERMARK = "IronPulse – AI-generated demo"
PI = math.pi

# ===================================================================================== small math
Vec = tuple[float, float, float]


def rad(deg: float) -> float:
    return deg * PI / 180.0


def deg(r: float) -> float:
    return r * 180.0 / PI


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def ease_io(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_sine(t: float) -> float:
    return 0.5 - 0.5 * math.cos(PI * clamp(t, 0.0, 1.0))


def ease_out(t: float) -> float:
    return 1 - (1 - clamp(t, 0.0, 1.0)) ** 3


def smooth(e0: float, e1: float, x: float) -> float:
    t = clamp((x - e0) / (e1 - e0), 0.0, 1.0) if e1 != e0 else float(x >= e1)
    return t * t * (3 - 2 * t)


def bump(u: float) -> float:
    """0 -> 1 -> 0 over one period (sin^2), periodic in u."""
    return math.sin(PI * u) ** 2


def track(keys: list[tuple[float, float]], x: float, ease: Callable[[float], float] = ease_io) -> float:
    """Piecewise eased interpolation of scalar keys ``[(x, value), ...]`` (clamped at the ends)."""
    if x <= keys[0][0]:
        return keys[0][1]
    for (x0, v0), (x1, v1) in zip(keys, keys[1:]):
        if x <= x1:
            return v1 if x1 <= x0 else lerp(v0, v1, ease((x - x0) / (x1 - x0)))
    return keys[-1][1]


def bisect(fn: Callable[[float], float], lo: float, hi: float, iters: int = 40) -> float:
    """Root of a monotonic ``fn`` on [lo, hi] (returns the closer end when there is no sign change)."""
    flo, fhi = fn(lo), fn(hi)
    if flo == 0:
        return lo
    if (flo > 0) == (fhi > 0):
        return lo if abs(flo) < abs(fhi) else hi
    for _ in range(iters):
        mid = (lo + hi) / 2
        fm = fn(mid)
        if (fm > 0) == (flo > 0):
            lo, flo = mid, fm
        else:
            hi = mid
    return (lo + hi) / 2


def vadd(a: Vec, b: Vec) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def vsub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def vmul(a: Vec, k: float) -> Vec:
    return (a[0] * k, a[1] * k, a[2] * k)


def vdot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def vlen(a: Vec) -> float:
    return math.sqrt(vdot(a, a))


def vnorm(a: Vec) -> Vec:
    n = vlen(a)
    return (a[0] / n, a[1] / n, a[2] / n) if n > 1e-12 else (0.0, 0.0, 0.0)


def vcross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def vcomb(*pairs: tuple[Vec, float]) -> Vec:
    x = y = z = 0.0
    for v, k in pairs:
        x += v[0] * k
        y += v[1] * k
        z += v[2] * k
    return (x, y, z)


# ===================================================================================== the mannequin rig
# Proportions (metres) of a ~1.73 m figure. World: y up, x = facing direction, z = side nearer the camera.
HIP_W, THIGH, SHIN = 0.09, 0.44, 0.43
HEEL_BACK, HEEL_DOWN, TOE_FWD, TOE_DOWN = 0.035, 0.045, 0.17, 0.04
R_HEEL, R_TOE = 0.034, 0.025
ANKLE_H = R_HEEL + HEEL_DOWN  # ankle height with the foot flat on the floor
LUMBAR, THORAX, SH_DROP, SH_W = 0.22, 0.28, 0.045, 0.185
NECK, HEAD_UP, HEAD_R = 0.07, 0.095, 0.105
UPPER, FORE, HAND_OFF, PALM_LEN = 0.30, 0.26, 0.07, 0.075
R_HAND, R_KNEE, R_ELBOW, WRIST_H = 0.042, 0.052, 0.045, 0.040
STAND_Y = ANKLE_H + SHIN + THIGH  # pelvis (hip joint) height when standing tall
TORSO_LEN = LUMBAR + THORAX - SH_DROP  # pelvis -> shoulder line
ARM_REACH = UPPER + FORE + HAND_OFF
# (front-back half depth, side-to-side half width) of the torso cross-sections
SEC_PELVIS, SEC_WAIST, SEC_CHEST = (0.112, 0.155), (0.100, 0.138), (0.122, 0.172)

NEUTRAL: dict[str, float] = dict(
    x=0.0, y=STAND_Y, lean=0.0, spine=0.0, neck=0.0,
    hip_n=0.0, knee_n=0.0, ankle_n=0.0, hip_f=0.0, knee_f=0.0, ankle_f=0.0,
    sh_n=4.0, el_n=10.0, sh_f=4.0, el_f=10.0,
    abd_hip_n=3.0, abd_hip_f=3.0, abd_sh_n=5.0, abd_sh_f=5.0,
)


def P(**kw: float) -> dict[str, float]:
    p = dict(NEUTRAL)
    p.update(kw)
    return p


class Body:
    """Joint positions (``pts``), unit directions (``dirs``) and hand styles of one posed mannequin."""

    __slots__ = ("pts", "dirs", "palm")

    def __init__(self) -> None:
        self.pts: dict[str, Vec] = {}
        self.dirs: dict[str, Vec] = {}
        self.palm: dict[str, bool] = {"n": False, "f": False}

    def shift(self, dx: float, dy: float = 0.0, dz: float = 0.0) -> Body:
        self.pts = {k: (v[0] + dx, v[1] + dy, v[2] + dz) for k, v in self.pts.items()}
        return self

    def foot_floor(self, side: str = "n") -> float:
        """Height of the lowest point of a foot above the floor."""
        return min(self.pts["heel_" + side][1] - R_HEEL, self.pts["toe_" + side][1] - R_TOE)


def fk(p: dict[str, float]) -> Body:
    """Forward kinematics. Angles in degrees; limb angles are measured inside the limb's plane from the
    torso's "down" axis towards its front (flexion positive), knees fold backwards, elbows forwards."""
    b = Body()
    th, half = rad(p["lean"]), rad(p.get("spine", 0.0)) * 0.5
    st, ct = math.sin(th), math.cos(th)
    up, fw, lat = (st, ct, 0.0), (ct, -st, 0.0), (0.0, 0.0, 1.0)
    ch, sh = math.cos(half), math.sin(half)
    pelvis = (p["x"], p["y"], 0.0)
    lum = vcomb((up, ch), (fw, -sh))
    thd = vcomb((up, ch), (fw, sh))
    fwt = vcomb((fw, ch), (up, -sh))
    waist = vadd(pelvis, vmul(lum, LUMBAR))
    neck_base = vadd(waist, vmul(thd, THORAX))
    shc = vsub(neck_base, vmul(thd, SH_DROP))
    nk = rad(p.get("neck", 0.0))
    nd = vcomb((thd, math.cos(nk)), (fwt, math.sin(nk)))
    face = vcomb((fwt, math.cos(nk)), (thd, -math.sin(nk)))
    neck_top = vadd(neck_base, vmul(nd, NECK))
    head = vadd(neck_top, vmul(nd, HEAD_UP))
    b.pts.update(pelvis=pelvis, waist=waist, neck_base=neck_base, neck_top=neck_top, head=head, shc=shc)
    b.dirs.update(up=up, fw=fw, lat=lat, thd=thd, fwt=fwt, face=face, headup=nd, lum=lum)
    for side, s in (("n", 1.0), ("f", -1.0)):
        a = rad(p.get("abd_sh_" + side, 0.0))
        down = vcomb((thd, -math.cos(a)), (lat, s * math.sin(a)))
        shoulder = vadd(shc, (0.0, 0.0, s * SH_W))
        a1 = rad(p.get("sh_" + side, 0.0))
        a2 = a1 + rad(p.get("el_" + side, 0.0))
        u1 = vcomb((fwt, math.sin(a1)), (down, math.cos(a1)))
        u2 = vcomb((fwt, math.sin(a2)), (down, math.cos(a2)))
        elbow = vadd(shoulder, vmul(u1, UPPER))
        wrist = vadd(elbow, vmul(u2, FORE))
        b.pts.update({"sh_" + side: shoulder, "el_" + side: elbow, "wr_" + side: wrist,
                      "hand_" + side: vadd(wrist, vmul(u2, HAND_OFF))})
        a = rad(p.get("abd_hip_" + side, 0.0))
        down = vcomb((up, -math.cos(a)), (lat, s * math.sin(a)))
        hip = vadd(pelvis, (0.0, 0.0, s * HIP_W))
        h = rad(p.get("hip_" + side, 0.0))
        k = h - rad(p.get("knee_" + side, 0.0))
        fa = k + PI / 2 - rad(p.get("ankle_" + side, 0.0))
        t1 = vcomb((fw, math.sin(h)), (down, math.cos(h)))
        t2 = vcomb((fw, math.sin(k)), (down, math.cos(k)))
        fdir = vcomb((fw, math.sin(fa)), (down, math.cos(fa)))
        fdown = vcomb((fw, -math.cos(fa)), (down, math.sin(fa)))
        knee = vadd(hip, vmul(t1, THIGH))
        ankle = vadd(knee, vmul(t2, SHIN))
        b.pts.update({"hip_" + side: hip, "knee_" + side: knee, "ankle_" + side: ankle,
                      "heel_" + side: vcomb((ankle, 1.0), (fdir, -HEEL_BACK), (fdown, HEEL_DOWN)),
                      "toe_" + side: vcomb((ankle, 1.0), (fdir, TOE_FWD), (fdown, TOE_DOWN))})
    return b


def two_bone(root: Vec, target: Vec, l1: float, l2: float, pole: Vec) -> tuple[Vec, Vec]:
    """Analytic two-bone IK: (middle joint, reached end) with the middle joint bending towards ``pole``."""
    v = vsub(target, root)
    d = vlen(v)
    u = vmul(v, 1.0 / d) if d > 1e-9 else vnorm(pole)
    dc = clamp(d, abs(l1 - l2) + 1e-4, l1 + l2 - 1e-4)
    a = (l1 * l1 - l2 * l2 + dc * dc) / (2 * dc)
    h = math.sqrt(max(0.0, l1 * l1 - a * a))
    pp = vsub(pole, vmul(u, vdot(pole, u)))
    if vlen(pp) < 1e-6:
        pp = vcross(u, (0.0, 0.0, 1.0)) if abs(u[2]) < 0.9 else vcross(u, (1.0, 0.0, 0.0))
    pp = vnorm(pp)
    return vadd(vadd(root, vmul(u, a)), vmul(pp, h)), vadd(root, vmul(u, dc))


def ik_arm(b: Body, side: str, target: Vec, pole: Vec, *, palm: bool = False, palm_dir: Vec = (1.0, 0.0, 0.0)) -> None:
    """Grip: ``target`` is the hand (fist) centre. Palm: ``target`` is the wrist, the hand lies flat along palm_dir."""
    shoulder = b.pts["sh_" + side]
    elbow, end = two_bone(shoulder, target, UPPER, FORE if palm else FORE + HAND_OFF, vnorm(pole))
    if palm:
        wrist, hand = end, vadd(end, vmul(vnorm(palm_dir), PALM_LEN))
    else:
        wrist, hand = vadd(elbow, vmul(vnorm(vsub(end, elbow)), FORE)), end
    b.pts.update({"el_" + side: elbow, "wr_" + side: wrist, "hand_" + side: hand})
    b.palm[side] = palm


def foot_frame(toe_down_deg: float) -> tuple[Vec, Vec]:
    """(heel->toe direction, sole-down direction) for a foot pitched toes-down by ``toe_down_deg``."""
    a = rad(toe_down_deg)
    return (math.cos(a), -math.sin(a), 0.0), (-math.sin(a), -math.cos(a), 0.0)


def ik_leg(b: Body, side: str, ankle: Vec, pole: Vec, foot: tuple[Vec, Vec] | None = None) -> None:
    knee, end = two_bone(b.pts["hip_" + side], ankle, THIGH, SHIN, vnorm(pole))
    fdir, fdown = foot or foot_frame(0.0)
    b.pts.update({"knee_" + side: knee, "ankle_" + side: end,
                  "heel_" + side: vcomb((end, 1.0), (fdir, -HEEL_BACK), (fdown, HEEL_DOWN)),
                  "toe_" + side: vcomb((end, 1.0), (fdir, TOE_FWD), (fdown, TOE_DOWN))})


def heel_on_floor(b: Body, side: str = "n", x: float | None = None) -> Body:
    """Translate so the side's foot touches the floor (and its ankle sits at ``x`` when given)."""
    dx = 0.0 if x is None else x - b.pts["ankle_" + side][0]
    return b.shift(dx, -b.foot_floor(side))


# ===================================================================================== camera
class Camera:
    """Orthographic camera: ``yaw`` turns towards the figure's front, ``pitch`` looks down. ``scale`` is 1x px/m;
    (ox, oy) is the 1x screen position of the world origin (on the floor)."""

    def __init__(self, yaw: float, pitch: float, scale: float = 1.0, ox: float = 0.0, oy: float = 0.0) -> None:
        c, s, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
        self.yaw, self.pitch = yaw, pitch
        self.m = ((c, 0.0, -s), (-s * sp, cp, -c * sp), (s * cp, sp, c * cp))
        self.scale, self.ox, self.oy = scale, ox, oy

    def raw(self, p: Vec) -> tuple[float, float, float]:
        m = self.m
        return (m[0][0] * p[0] + m[0][2] * p[2],
                m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2],
                m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2])

    def p2(self, p: Vec) -> tuple[float, float, float]:
        """(x, y) in 2x supersampled pixels + depth (larger = nearer the camera)."""
        x, y, d = self.raw(p)
        return (self.ox + x * self.scale) * SS, (self.oy - y * self.scale) * SS, d

    def p1(self, p: Vec) -> tuple[float, float]:
        x, y, _ = self.raw(p)
        return self.ox + x * self.scale, self.oy - y * self.scale

    def d2(self, v: Vec) -> tuple[float, float]:
        """Screen vector (2x px, y down) of a world vector."""
        x, y, _ = self.raw(v)
        return x * self.scale * SS, -y * self.scale * SS

    def normal(self, n: Vec) -> Vec:
        """World normal -> screen-space normal (x right, y down, z towards the viewer)."""
        x, y, d = self.raw(n)
        return (x, -y, d)


# ===================================================================================== lighting & materials
_L = (-0.46, -0.66, 0.60)
_ln = math.sqrt(sum(c * c for c in _L))
LX, LY, LZ = (c / _ln for c in _L)
_h = (LX, LY, LZ + 1.0)
_hn = math.sqrt(sum(c * c for c in _h))
HX, HY, HZ = (c / _hn for c in _h)
_lxy = math.hypot(LX, LY)
LXN, LYN = LX / _lxy, LY / _lxy


@dataclass(frozen=True)
class Mat:
    base: tuple[float, float, float]
    amb: float = 0.34
    dif: float = 0.72
    spec: float = 0.26
    shin: float = 22.0
    rim: float = 0.45
    rim_col: tuple[float, float, float] = (0.55, 0.8, 1.0)
    opacity: float = 1.0


def _rgb(hex_: str) -> tuple[float, float, float]:
    hex_ = hex_.lstrip("#")
    return tuple(int(hex_[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(a: Iterable[float], b: Iterable[float], t: float) -> tuple[float, float, float]:
    return tuple(x + (y - x) * t for x, y in zip(a, b))  # type: ignore[return-value]


@dataclass(frozen=True)
class Look:
    """Per-video palette (goal colours) and the materials derived from it."""

    goal_label: str
    accent: tuple[float, float, float]
    accent2: tuple[float, float, float]

    @property
    def rim(self) -> tuple[float, float, float]:
        return _mix(self.accent2, (1.0, 1.0, 1.0), 0.35)

    @property
    def near(self) -> Mat:
        return Mat(base=(0.86, 0.885, 0.93), rim=0.42, rim_col=self.rim)

    @property
    def far(self) -> Mat:
        return Mat(base=(0.47, 0.51, 0.60), amb=0.36, dif=0.62, spec=0.12, rim=0.26, rim_col=self.rim)

    @property
    def visor(self) -> Mat:
        return Mat(base=_mix(self.accent, self.accent2, 0.35), amb=0.75, dif=0.35, spec=0.85, shin=30, rim=0.6,
                   rim_col=_mix(self.accent2, (1, 1, 1), 0.5))

    @property
    def metal(self) -> Mat:
        return Mat(base=(0.70, 0.73, 0.79), amb=0.30, dif=0.62, spec=0.95, shin=36, rim=0.30, rim_col=self.rim)

    @property
    def guide(self) -> Mat:
        c = _mix(self.accent, (1, 1, 1), 0.25)
        return Mat(base=c, amb=1.0, dif=0.0, spec=0.0, rim=0.0, opacity=0.75)


GOAL_LOOKS: dict[str, Look] = {
    "calisthenics": Look("Calisthenics", _rgb("30d158"), _rgb("64d2ff")),
    "bulking": Look("Muscle building", _rgb("0a84ff"), _rgb("5e5ce6")),
    "weight_loss": Look("Fat loss & conditioning", _rgb("ff9f0a"), _rgb("ff375f")),
    "general_fitness": Look("General fitness", _rgb("5e5ce6"), _rgb("bf5af2")),
    "recovery": Look("Recovery & mobility", _rgb("64d2ff"), _rgb("bf5af2")),
}


# ===================================================================================== primitives (2x space)
class Prim:
    __slots__ = ("depth", "bbox")

    def draw(self, X: np.ndarray, Y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:  # pragma: no cover - abstract
        raise NotImplementedError


def _shade_round(d: np.ndarray, gx: np.ndarray, gy: np.ndarray, rloc: Any, mat: Mat) -> tuple[np.ndarray, np.ndarray]:
    """Cylinder/sphere shading from a signed distance (px), its unit gradient and the local radius."""
    cov = np.clip(0.5 - d, 0.0, 1.0)
    s = np.clip(1.0 + d / rloc, 0.0, 1.0)
    s2 = s * s
    nz = np.sqrt(1.0 - s2)
    nx, ny = gx * s, gy * s
    lam = np.clip(nx * LX + ny * LY + nz * LZ, 0.0, 1.0)
    k = mat.amb + mat.dif * lam
    spec = np.clip(nx * HX + ny * HY + nz * HZ, 0.0, 1.0) ** mat.shin * mat.spec
    back = np.clip(-(gx * LXN + gy * LYN), 0.0, 1.0)
    rim = (s2 * s2) * (0.3 + 0.7 * back) * mat.rim
    rgb = np.empty(d.shape + (3,), np.float32)
    for c in range(3):
        rgb[..., c] = mat.base[c] * k + spec + rim * mat.rim_col[c]
    if mat.opacity < 1.0:
        cov = cov * mat.opacity
    return cov, rgb


class Capsule(Prim):
    """Tapered capsule (uneven capsule SDF, Inigo Quilez) with cylinder shading."""

    __slots__ = ("ax", "ay", "bx", "by", "ra", "rb", "mat", "clip")

    def __init__(self, ax: float, ay: float, bx: float, by: float, ra: float, rb: float, mat: Mat, depth: float,
                 clip: tuple[float, float, float] | None = None) -> None:
        self.ax, self.ay, self.bx, self.by, self.ra, self.rb, self.mat, self.depth, self.clip = ax, ay, bx, by, ra, rb, mat, depth, clip
        r = max(ra, rb) + 2
        self.bbox = (min(ax, bx) - r, min(ay, by) - r, max(ax, bx) + r, max(ay, by) + r)

    def draw(self, X: np.ndarray, Y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        ax, ay, ra, rb = self.ax, self.ay, self.ra, self.rb
        dx, dy = self.bx - ax, self.by - ay
        h = math.hypot(dx, dy)
        px, py = X - ax, Y - ay
        if h < 1e-3 or abs(ra - rb) >= h * 0.999:
            r = max(ra, rb)
            cx, cy = (px, py) if ra >= rb else (X - self.bx, Y - self.by)
            dist = np.sqrt(cx * cx + cy * cy) + 1e-6
            d, gx, gy, rloc = dist - r, cx / dist, cy / dist, r
        else:
            uy0, uy1 = dx / h, dy / h
            ux0, ux1 = uy1, -uy0
            qx = px * ux0 + py * ux1
            qy = px * uy0 + py * uy1
            sgn = np.where(qx < 0, -1.0, 1.0).astype(np.float32)
            qa = np.abs(qx)
            b = (ra - rb) / h
            a = math.sqrt(1 - b * b)
            k = -b * qa + a * qy
            l1 = np.sqrt(qa * qa + qy * qy) + 1e-6
            qy2 = qy - h
            l2 = np.sqrt(qa * qa + qy2 * qy2) + 1e-6
            m1, m2 = k < 0, k > a * h
            d = np.where(m1, l1 - ra, np.where(m2, l2 - rb, qa * a + qy * b - ra))
            gxl = np.where(m1, qa / l1, np.where(m2, qa / l2, a)) * sgn
            gyl = np.where(m1, qy / l1, np.where(m2, qy2 / l2, b))
            gx = gxl * ux0 + gyl * uy0
            gy = gxl * ux1 + gyl * uy1
            rloc = ra + (rb - ra) * np.clip(qy / h, 0.0, 1.0)
        cov, rgb = _shade_round(d, gx, gy, rloc, self.mat)
        if self.clip is not None:
            cx, cy, cr = self.clip
            cov = cov * np.clip(cr - np.sqrt((X - cx) ** 2 + (Y - cy) ** 2) + 0.5, 0.0, 1.0)
        return cov, rgb


class Sphere(Prim):
    __slots__ = ("cx", "cy", "r", "mat")

    def __init__(self, cx: float, cy: float, r: float, mat: Mat, depth: float) -> None:
        self.cx, self.cy, self.r, self.mat, self.depth = cx, cy, r, mat, depth
        self.bbox = (cx - r - 2, cy - r - 2, cx + r + 2, cy + r + 2)

    def draw(self, X: np.ndarray, Y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        px, py = X - self.cx, Y - self.cy
        dist = np.sqrt(px * px + py * py) + 1e-6
        return _shade_round(dist - self.r, px / dist, py / dist, self.r, self.mat)


class Poly(Prim):
    """Convex polygon with a flat colour (and an optional vertical gradient)."""

    __slots__ = ("edges", "rgb", "rgb2", "y0", "y1", "opacity")

    def __init__(self, pts: list[tuple[float, float]], rgb: tuple[float, float, float], depth: float,
                 rgb2: tuple[float, float, float] | None = None, opacity: float = 1.0) -> None:
        area = sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))
        if area < 0:
            pts = pts[::-1]
        edges = []
        for i in range(len(pts)):
            x0, y0 = pts[i]
            x1, y1 = pts[(i + 1) % len(pts)]
            ex, ey = x1 - x0, y1 - y0
            n = math.hypot(ex, ey)
            if n < 1e-6:
                continue
            edges.append((x0, y0, ey / n, -ex / n))  # outward normal for counter-clockwise (y-down) winding
        self.edges, self.rgb, self.rgb2, self.depth, self.opacity = edges, rgb, rgb2, depth, opacity
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self.y0, self.y1 = min(ys), max(ys)
        self.bbox = (min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2)

    def draw(self, X: np.ndarray, Y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        d = None
        for x0, y0, nx, ny in self.edges:
            e = (X - x0) * nx + (Y - y0) * ny
            d = e if d is None else np.maximum(d, e)
        cov = np.clip(0.5 - d, 0.0, 1.0) * self.opacity
        rgb = np.empty(cov.shape + (3,), np.float32)
        if self.rgb2 is None:
            rgb[...] = self.rgb
        else:
            t = np.clip((Y - self.y0) / max(1.0, self.y1 - self.y0), 0.0, 1.0)
            t = np.broadcast_to(t, cov.shape)
            for c in range(3):
                rgb[..., c] = self.rgb[c] + (self.rgb2[c] - self.rgb[c]) * t
        return cov, rgb


class Disc(Prim):
    """Projected disc (an ellipse = affine image of the unit circle) for weight plates.

    style: ``face`` (shaded plate face), ``band`` (plate edge), ``glass`` (translucent near plate),
    ``hub`` (metal sleeve end)."""

    __slots__ = ("cx", "cy", "inv", "style", "rgb", "accent", "lit", "opacity")

    def __init__(self, cx: float, cy: float, a: tuple[float, float, float, float], style: str,
                 rgb: tuple[float, float, float], accent: tuple[float, float, float], lit: float, depth: float,
                 opacity: float = 1.0) -> None:
        a00, a01, a10, a11 = a
        det = a00 * a11 - a01 * a10
        if abs(det) < 1e-9:
            det = 1e-9
        self.inv = (a11 / det, -a01 / det, -a10 / det, a00 / det)
        self.cx, self.cy, self.style, self.rgb, self.accent, self.lit, self.depth, self.opacity = cx, cy, style, rgb, accent, lit, depth, opacity
        ex, ey = math.hypot(a00, a01) + 2, math.hypot(a10, a11) + 2
        self.bbox = (cx - ex, cy - ey, cx + ex, cy + ey)

    def draw(self, X: np.ndarray, Y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        i00, i01, i10, i11 = self.inv
        px, py = X - self.cx, Y - self.cy
        qx = i00 * px + i01 * py
        qy = i10 * px + i11 * py
        rq = np.sqrt(qx * qx + qy * qy) + 1e-6
        gx = (i00 * qx + i10 * qy) / rq
        gy = (i01 * qx + i11 * qy) / rq
        gn = np.sqrt(gx * gx + gy * gy) + 1e-6
        d = (rq - 1.0) / gn
        cov = np.clip(0.5 - d, 0.0, 1.0)
        rgb = np.empty(cov.shape + (3,), np.float32)
        base = self.rgb
        if self.style == "band":
            for c in range(3):
                rgb[..., c] = base[c] * self.lit
        elif self.style == "hub":
            shade = 0.75 + 0.35 * (1 - rq) - 0.25 * (rq > 0.55)
            for c in range(3):
                rgb[..., c] = base[c] * shade
        else:
            # radial light falloff towards the lower right + machined grooves + accent rim ring
            light = 1.0 + 0.22 * np.clip(-(qx * LXN + qy * LYN), -1.0, 1.0) * rq
            grooves = 1.0 - 0.06 * (np.sin(rq * 46.0) > 0.6) * (rq > 0.3)
            ring = np.clip(1.0 - np.abs(rq - 0.86) / 0.03, 0.0, 1.0)
            edge = np.clip((rq - 0.93) / 0.07, 0.0, 1.0)
            for c in range(3):
                rgb[..., c] = base[c] * self.lit * light * grooves + ring * self.accent[c] * 0.55 + edge * 0.12
            if self.style == "glass":
                cov = cov * (0.18 + 0.55 * ring + 0.45 * edge + 0.25 * (rq < 0.2))
        if self.opacity < 1.0:
            cov = cov * self.opacity
        return cov, rgb


# ===================================================================================== rasteriser
def rasterize(prims: list[Prim], pad: int = 72) -> tuple[int, int, np.ndarray, np.ndarray] | None:
    """Paint primitives back-to-front into a premultiplied 2x layer covering their union bbox (+ glow pad)."""
    if not prims:
        return None
    x0 = min(p.bbox[0] for p in prims) - pad
    y0 = min(p.bbox[1] for p in prims) - pad
    x1 = max(p.bbox[2] for p in prims) + pad
    y1 = max(p.bbox[3] for p in prims) + pad
    x0, y0 = max(0, int(x0) // 2 * 2), max(0, int(y0) // 2 * 2)
    x1, y1 = min(W * SS, (int(x1) + 2) // 2 * 2), min(H * SS, (int(y1) + 2) // 2 * 2)
    if x1 <= x0 or y1 <= y0:
        return None
    col = np.zeros((y1 - y0, x1 - x0, 3), np.float32)
    alpha = np.zeros((y1 - y0, x1 - x0), np.float32)
    for p in sorted(prims, key=lambda q: q.depth):
        bx0, by0 = max(x0, int(p.bbox[0])), max(y0, int(p.bbox[1]))
        bx1, by1 = min(x1, int(p.bbox[2]) + 1), min(y1, int(p.bbox[3]) + 1)
        if bx1 <= bx0 or by1 <= by0:
            continue
        X = (np.arange(bx0, bx1, dtype=np.float32) + 0.5)[None, :]
        Y = (np.arange(by0, by1, dtype=np.float32) + 0.5)[:, None]
        cov, rgb = p.draw(X, Y)
        cov = np.broadcast_to(cov, (by1 - by0, bx1 - bx0))
        sl = (slice(by0 - y0, by1 - y0), slice(bx0 - x0, bx1 - x0))
        inv = 1.0 - cov
        c = col[sl]
        c *= inv[..., None]
        c += rgb * cov[..., None]
        a = alpha[sl]
        a *= inv
        a += cov
    return x0, y0, col, alpha


def composite(frame: np.ndarray, layer: tuple[int, int, np.ndarray, np.ndarray] | None,
              glow: tuple[float, float, float], glow_strength: float = 0.55) -> None:
    """Downsample the 2x layer (box filter = anti-aliasing), add a soft coloured glow, alpha-blend into frame."""
    if layer is None:
        return
    x0, y0, col, alpha = layer
    h2, w2 = alpha.shape
    h, w = h2 // 2, w2 // 2
    a1 = (alpha[0::2, 0::2] + alpha[1::2, 0::2] + alpha[0::2, 1::2] + alpha[1::2, 1::2]) * 0.25
    c1 = (col[0::2, 0::2] + col[1::2, 0::2] + col[0::2, 1::2] + col[1::2, 1::2]) * 0.25
    fx, fy = x0 // 2, y0 // 2
    region = frame[fy:fy + h, fx:fx + w]
    if glow_strength > 0 and h >= 8 and w >= 8:
        small = Image.fromarray((a1 * 255).astype(np.uint8), "L").resize((max(1, w // 4), max(1, h // 4)), Image.BOX)
        small = small.filter(ImageFilter.GaussianBlur(4.5))
        g = np.asarray(small.resize((w, h), Image.BILINEAR), dtype=np.float32) / 255.0
        g = g * (1.0 - a1)
        region += g[..., None] * (np.asarray(glow, np.float32) * glow_strength)
    region *= (1.0 - a1)[..., None]
    region += c1


def apply_shadows(frame: np.ndarray, blobs: list[tuple[float, float, float, float, float]]) -> None:
    """Soft multiplicative contact shadows: (cx, cy, rx, ry, strength) in 1x px."""
    for cx, cy, rx, ry, s in blobs:
        if s < 0.01:
            continue
        bx0, bx1 = max(0, int(cx - 3 * rx)), min(W, int(cx + 3 * rx) + 1)
        by0, by1 = max(0, int(cy - 3 * ry)), min(H, int(cy + 3 * ry) + 1)
        if bx1 <= bx0 or by1 <= by0:
            continue
        X = (np.arange(bx0, bx1, dtype=np.float32) + 0.5 - cx)[None, :] / rx
        Y = (np.arange(by0, by1, dtype=np.float32) + 0.5 - cy)[:, None] / ry
        g = np.exp(-0.5 * (X * X + Y * Y)) * s
        frame[by0:by1, bx0:bx1] *= (1.0 - g)[..., None]


# ===================================================================================== scene building
LIMBS = (  # (from, to, r_from, r_to)
    ("hip", "knee", 0.078, 0.054), ("knee", "ankle", 0.052, 0.037), ("heel", "toe", R_HEEL, R_TOE),
    ("sh", "el", 0.052, 0.043), ("el", "wr", 0.042, 0.033),
)


def _section_radius(cam: Camera, a: Vec, b: Vec, front: Vec, lat: Vec, sec: tuple[float, float]) -> float:
    """Projected half-thickness (metres) of an elliptic torso section seen across the segment a->b."""
    sx, sy = cam.d2(vsub(b, a))
    n = math.hypot(sx, sy)
    if n < 1e-6:
        return max(sec)
    ex, ey = -sy / n, sx / n
    fx, fy = cam.d2(front)
    lx, ly = cam.d2(lat)
    k = cam.scale * SS
    return math.sqrt((sec[0] * (fx * ex + fy * ey) / k) ** 2 + (sec[1] * (lx * ex + ly * ey) / k) ** 2)


def figure_prims(b: Body, cam: Camera, look: Look) -> list[Prim]:
    prims: list[Prim] = []
    P2 = {k: cam.p2(v) for k, v in b.pts.items()}
    k = cam.scale * SS
    near, far = look.near, look.far
    front_view = abs(cam.yaw) > 1.0  # both sides face the camera

    def cap(a: str, c: str, ra: float, rb: float, mat: Mat, bias: float = 0.0) -> None:
        pa, pc = P2[a], P2[c]
        prims.append(Capsule(pa[0], pa[1], pc[0], pc[1], ra * k, rb * k, mat, (pa[2] + pc[2]) / 2 + bias))

    for side in ("f", "n"):
        mat = near if (side == "n" or front_view) else far
        for a, c, ra, rb in LIMBS[:3]:
            cap(f"{a}_{side}", f"{c}_{side}", ra, rb, mat, bias=-0.004)
        sd = P2["sh_" + side]
        prims.append(Sphere(sd[0], sd[1], 0.056 * k, mat, sd[2] + 0.002))
        for a, c, ra, rb in LIMBS[3:]:
            cap(f"{a}_{side}", f"{c}_{side}", ra, rb, mat=mat, bias=0.004)
        if b.palm[side]:
            cap("wr_" + side, "hand_" + side, 0.031, 0.022, mat=mat, bias=0.005)
        else:
            hd = P2["hand_" + side]
            prims.append(Sphere(hd[0], hd[1], R_HAND * k, mat, hd[2] + 0.005))

    d = b.dirs
    pel, wai, nb = b.pts["pelvis"], b.pts["waist"], b.pts["neck_base"]
    r_p = _section_radius(cam, pel, wai, d["fw"], d["lat"], SEC_PELVIS)
    r_w = _section_radius(cam, pel, wai, d["fw"], d["lat"], SEC_WAIST)
    r_w2 = _section_radius(cam, wai, nb, d["fwt"], d["lat"], SEC_WAIST)
    r_c = _section_radius(cam, wai, nb, d["fwt"], d["lat"], SEC_CHEST)
    cap("pelvis", "waist", r_p, r_w, near)
    cap("waist", "neck_base", r_w2, r_c, near, bias=0.001)
    cap("neck_base", "neck_top", 0.047, 0.045, near, bias=0.002)
    hp = P2["head"]
    prims.append(Sphere(hp[0], hp[1], HEAD_R * k, near, hp[2] + 0.01))
    # glossy visor marks the face direction (clipped to the head silhouette)
    face, lat, hu = d["face"], d["lat"], d["headup"]
    if cam.normal(face)[2] > -0.35:
        c3 = vcomb((b.pts["head"], 1.0), (face, 0.068), (hu, 0.014))
        e1 = cam.p2(vadd(c3, vmul(lat, 0.06)))
        e2 = cam.p2(vsub(c3, vmul(lat, 0.06)))
        prims.append(Capsule(e1[0], e1[1], e2[0], e2[1], 0.023 * k, 0.023 * k, look.visor, hp[2] + 0.011,
                             clip=(hp[0], hp[1], HEAD_R * k - 1.5)))
    return prims


def body_shadow_points(b: Body) -> list[tuple[Vec, float]]:
    pts = b.pts
    out = [(pts["pelvis"], 0.11), (pts["waist"], 0.10), (pts["neck_base"], 0.12), (pts["head"], HEAD_R)]
    for s in ("n", "f"):
        out += [(pts["heel_" + s], R_HEEL), (pts["toe_" + s], R_TOE), (pts["ankle_" + s], 0.04),
                (pts["knee_" + s], R_KNEE), (pts["el_" + s], R_ELBOW), (pts["wr_" + s], 0.035), (pts["hand_" + s], R_HAND)]
    return out


def shadow_blobs(points: list[tuple[Vec, float]], cam: Camera) -> list[tuple[float, float, float, float, float]]:
    blobs = []
    for p, r in points:
        h = max(0.0, p[1] - r)
        s = 0.40 * math.exp(-h / 0.11)
        if s < 0.012:
            continue
        cx, cy = cam.p1((p[0] + 0.16 * h, 0.0, p[2]))
        rx = (r * 0.95 + 0.035 + 0.45 * h) * cam.scale
        blobs.append((cx, cy, rx, rx * 0.26 + 1.2, s))
    return blobs


# ----------------------------------------------------------------------------------- props
@dataclass(frozen=True)
class Bar:
    """Horizontal bar along z (barbell shaft, pull-up bar) at (x, y)."""

    x: float
    y: float
    z0: float
    z1: float
    r: float = 0.016
    metal: str = "metal"
    caps: bool = True


@dataclass(frozen=True)
class Plates:
    """Pair of weight plates on a barbell at (x, y); the near one is drawn as translucent glass."""

    x: float
    y: float
    radius: float
    z: float = 0.84
    thick: float = 0.055


@dataclass(frozen=True)
class Box:
    """Axis-aligned box (bench pad, frame, step, mat, upright)."""

    x0: float
    x1: float
    y0: float
    y1: float
    z0: float
    z1: float
    rgb: tuple[float, float, float]
    bias: float = 0.0
    shadow: bool = True


@dataclass(frozen=True)
class Guide:
    """Alignment guide line drawn over the figure (e.g. the straight plank line)."""

    a: Vec
    b: Vec


PLATE_RGB = (0.16, 0.17, 0.205)


def prop_prims(prop: Any, cam: Camera, look: Look) -> list[Prim]:
    k = cam.scale * SS
    out: list[Prim] = []
    if isinstance(prop, Bar):
        mat = getattr(look, prop.metal)
        n = max(2, int(abs(prop.z1 - prop.z0) / 0.3))
        zs = [prop.z0 + (prop.z1 - prop.z0) * i / n for i in range(n + 1)]
        for za, zb in zip(zs, zs[1:]):
            a, c = cam.p2((prop.x, prop.y, za)), cam.p2((prop.x, prop.y, zb))
            out.append(Capsule(a[0], a[1], c[0], c[1], prop.r * k, prop.r * k, mat, (a[2] + c[2]) / 2))
    elif isinstance(prop, Plates):
        ex, ey = cam.d2((1.0, 0.0, 0.0))
        fx, fy = cam.d2((0.0, 1.0, 0.0))
        r = prop.radius
        amat = (ex * r, fx * r, ey * r, fy * r)
        hub = (ex * 0.03, fx * 0.03, ey * 0.03, fy * 0.03)
        lit = 0.55 + 0.6 * max(0.0, vdot(cam.normal((0.0, 0.0, 1.0)), (LX, LY, LZ)))
        for zc, glass in ((-prop.z, False), (prop.z, True)):
            layers = (zc - prop.thick / 2, zc - prop.thick / 6, zc + prop.thick / 6)
            for zz in layers if not glass else ():
                cx, cy, dd = cam.p2((prop.x, prop.y, zz))
                out.append(Disc(cx, cy, amat, "band", PLATE_RGB, look.accent, 0.62, dd - 0.0005))
            cx, cy, dd = cam.p2((prop.x, prop.y, zc + prop.thick / 2))
            style = "glass" if glass else "face"
            rgb = (0.82, 0.86, 0.95) if glass else PLATE_RGB
            out.append(Disc(cx, cy, amat, style, rgb, look.accent, lit if not glass else 1.0, dd))
            hx, hy, hd = cam.p2((prop.x, prop.y, zc + prop.thick / 2 + (0.08 if glass else 0.0)))
            out.append(Disc(hx, hy, hub, "hub", (0.78, 0.8, 0.86), look.accent, 1.0, hd + 0.0005))
    elif isinstance(prop, Box):
        xs, ys, zs = (prop.x0, prop.x1), (prop.y0, prop.y1), (prop.z0, prop.z1)
        corners = {(i, j, l): cam.p2((xs[i], ys[j], zs[l])) for i in (0, 1) for j in (0, 1) for l in (0, 1)}
        faces = (((1, 0, 0), [(1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)]),
                 ((-1, 0, 0), [(0, 0, 0), (0, 1, 0), (0, 1, 1), (0, 0, 1)]),
                 ((0, 1, 0), [(0, 1, 0), (1, 1, 0), (1, 1, 1), (0, 1, 1)]),
                 ((0, -1, 0), [(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)]),
                 ((0, 0, 1), [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)]),
                 ((0, 0, -1), [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]))
        center_depth = cam.p2(((prop.x0 + prop.x1) / 2, (prop.y0 + prop.y1) / 2, (prop.z0 + prop.z1) / 2))[2]
        for normal, idx in faces:
            ns = cam.normal(normal)
            if ns[2] <= 0.01:
                continue
            lam = max(0.0, ns[0] * LX + ns[1] * LY + ns[2] * LZ)
            shade = 0.45 + 0.75 * lam
            top = _mix(prop.rgb, (1, 1, 1), 0.06) if normal[1] > 0 else prop.rgb
            rgb = tuple(c * shade for c in top)
            rgb2 = tuple(c * 0.82 for c in rgb)
            pts = [(corners[i][0], corners[i][1]) for i in idx]
            out.append(Poly(pts, rgb, center_depth + prop.bias + 0.0001 * ns[2], rgb2=rgb2))  # type: ignore[arg-type]
    elif isinstance(prop, Guide):
        a, c = cam.p2(prop.a), cam.p2(prop.b)
        out.append(Capsule(a[0], a[1], c[0], c[1], 1.7, 1.7, look.guide, 10.0))
    return out


def prop_shadow_points(prop: Any) -> list[tuple[Vec, float]]:
    if isinstance(prop, Plates):
        return [((prop.x, prop.y - prop.radius + 0.02, z), 0.05) for z in (-prop.z, prop.z)]
    if isinstance(prop, Box) and prop.shadow and prop.y0 < 0.02:
        pts = []
        for x in (prop.x0, (prop.x0 + prop.x1) / 2, prop.x1):
            for z in (prop.z0, prop.z1):
                pts.append(((x, 0.03, z), 0.04))
        return pts
    return []


# ===================================================================================== poses (one per exercise)
@dataclass
class Frame3D:
    body: Body | None
    props: list[Any] = field(default_factory=list)


def squat_pose(ph: float, *, depth: float = 1.0, arms: str = "reach", bar: bool = False) -> Frame3D:
    """Bodyweight / back squat. depth 1 = thighs about parallel."""
    u = track([(0.0, 0.0), (0.42, 1.0), (0.52, 1.0), (0.92, 0.0)], ph)
    T = 90.0 * depth * u  # thigh world angle (0 = vertical, 90 = horizontal forward)
    S = -33.0 * depth * u  # shin world angle (negative = knees forward)
    p = P(neck=-22.0 * u, abd_hip_n=8 + 5 * u, abd_hip_f=8 + 5 * u)
    for s in ("n", "f"):
        p["knee_" + s] = T - S
        p["ankle_" + s] = S
    if arms == "reach":
        for s in ("n", "f"):
            p["sh_" + s] = 6 + 116 * u
            p["el_" + s] = 10 - 6 * u
            p["abd_sh_" + s] = 5 + 3 * u

    def build(lean: float) -> Body:
        q = dict(p)
        q["lean"] = lean
        for s in ("n", "f"):
            q["hip_" + s] = T + lean
        return heel_on_floor(fk(q), "n", 0.0)

    if not bar:
        b = build(3.0 + 39.0 * u)
        return Frame3D(b)
    midfoot = 0.065

    def bar_pos(bb: Body) -> Vec:
        return vcomb((bb.pts["neck_base"], 1.0), (bb.dirs["fwt"], -0.085), (bb.dirs["thd"], -0.03))

    lean = bisect(lambda ln: bar_pos(build(ln))[0] - midfoot, -5.0, 70.0)
    b = build(lean)
    bp = bar_pos(b)
    for s, sgn in (("n", 1.0), ("f", -1.0)):
        ik_arm(b, s, (bp[0], bp[1], sgn * 0.40), vcomb((b.dirs["thd"], -1.0), (b.dirs["fwt"], -0.45), ((0, 0, 1), sgn * 0.8)))
    return Frame3D(b, [Bar(bp[0], bp[1], -1.1, 1.1), Plates(bp[0], bp[1], 0.17, z=0.78)])


def _plank_body(beta: float, *, neck: float = -6.0, spine: float = 0.0) -> Body:
    p = P(lean=90.0 - beta, spine=spine, neck=neck, hip_n=0.0, hip_f=0.0, knee_n=0.0, knee_f=0.0,
          ankle_n=-6.0, ankle_f=-6.0, abd_hip_n=2.5, abd_hip_f=2.5)
    b = fk(p)
    toe = b.pts["toe_n"]
    return b.shift(-toe[0], R_TOE - toe[1])


_PUSH_TOP, _PUSH_BOTTOM = 25.0, 6.5


@lru_cache(maxsize=1)
def _pushup_hands() -> tuple[Vec, Vec]:
    b = _plank_body(_PUSH_TOP)
    sh = b.pts["shc"]
    return (sh[0] + 0.03, WRIST_H, SH_W + 0.07), (sh[0] + 0.03, WRIST_H, -(SH_W + 0.07))


def pushup_pose(ph: float) -> Frame3D:
    beta = track([(0.0, _PUSH_TOP), (0.46, _PUSH_BOTTOM), (0.54, _PUSH_BOTTOM), (0.86, _PUSH_TOP)], ph)
    b = _plank_body(beta)
    hn, hf = _pushup_hands()
    for s, target, sgn in (("n", hn, 1.0), ("f", hf, -1.0)):
        ik_arm(b, s, target, vnorm((-0.75, 0.15, sgn * 0.75)), palm=True)
    return Frame3D(b)


def _forearm_plank(beta: float, spine: float = 0.0) -> Body:
    p = P(lean=90.0 - beta, spine=spine, neck=-4.0, hip_n=2.0, hip_f=2.0, knee_n=0.0, knee_f=0.0,
          ankle_n=-4.0, ankle_f=-4.0, abd_hip_n=2.5, abd_hip_f=2.5)
    lean = rad(p["lean"])
    for s in ("n", "f"):
        p["sh_" + s] = deg(lean) + 2.0  # upper arm vertical
        p["el_" + s] = 88.0  # forearm forward along the floor
        p["abd_sh_" + s] = 0.0
    b = fk(p)
    toe = b.pts["toe_n"]
    return b.shift(-toe[0], R_TOE - toe[1])


@lru_cache(maxsize=1)
def _forearm_beta() -> float:
    return bisect(lambda be: _forearm_plank(be).pts["el_n"][1] - R_ELBOW, 4.0, 30.0)


def plank_pose(t: float) -> Frame3D:
    breath = math.sin(2 * PI * t / 4.0)
    b = _forearm_plank(_forearm_beta() + 0.25 * breath, spine=1.4 * breath)
    for s in ("n", "f"):
        b.palm[s] = True
        wr = b.pts["wr_" + s]
        b.pts["hand_" + s] = (wr[0] + PALM_LEN, wr[1] - 0.004, wr[2])
    guide = Guide(vadd(b.pts["ankle_n"], (0.0, 0.07, 0.25)), vadd(b.pts["head"], (0.0, 0.0, 0.25)))
    return Frame3D(b, [Box(-0.35, 1.75, 0.0, 0.012, -0.42, 0.42, (0.10, 0.11, 0.14), bias=-5.0, shadow=False), guide])


# --- inverted row / pull-up -------------------------------------------------------------------------------
ROW_BAR_Y = 0.95


def _row_body(beta: float) -> Body:
    p = P(lean=beta - 90.0, neck=-6.0, hip_n=2.0, hip_f=2.0, knee_n=3.0, knee_f=3.0, ankle_n=-14.0, ankle_f=-14.0,
          abd_hip_n=3.0, abd_hip_f=3.0)
    b = fk(p)
    heel = b.pts["heel_n"]
    return b.shift(-heel[0], R_HEEL - heel[1])


def _sternum(b: Body) -> Vec:
    return vcomb((b.pts["shc"], 1.0), (b.dirs["fwt"], 0.125), (b.dirs["thd"], -0.10))


@lru_cache(maxsize=1)
def _row_geometry() -> tuple[float, float, float]:
    top = bisect(lambda be: _sternum(_row_body(be))[1] - (ROW_BAR_Y - 0.03), 5.0, 60.0)
    bar_x = _sternum(_row_body(top))[0]
    grip = (bar_x, ROW_BAR_Y)

    def reach(be: float) -> float:
        sh = _row_body(be).pts["sh_n"]
        return math.hypot(sh[0] - grip[0], sh[1] - grip[1], 0.26 - SH_W) - (ARM_REACH - 0.015)

    bottom = bisect(reach, 0.0, top)
    return top, bottom, bar_x


def row_pose(ph: float) -> Frame3D:
    top, bottom, bar_x = _row_geometry()
    beta = track([(0.0, bottom), (0.40, top), (0.52, top), (0.90, bottom)], ph)
    b = _row_body(beta)
    for s, sgn in (("n", 1.0), ("f", -1.0)):
        ik_arm(b, s, (bar_x, ROW_BAR_Y, sgn * 0.26), vnorm((0.9, -1.0, sgn * 0.6)))
    return Frame3D(b, [Bar(bar_x, ROW_BAR_Y, -0.82, 0.62, r=0.018),
                       Box(bar_x - 0.03, bar_x + 0.03, 0.0, ROW_BAR_Y + 0.08, -0.88, -0.82, (0.24, 0.26, 0.31))])


PULL_BAR_Y = 2.22


def pullup_pose(ph: float) -> Frame3D:
    u = track([(0.0, 0.0), (0.42, 1.0), (0.56, 1.0), (0.94, 0.0)], ph)
    p = P(lean=lerp(2.0, -11.0, u), neck=lerp(-4.0, -12.0, u), hip_n=lerp(-4.0, 8.0, u), hip_f=lerp(-4.0, 8.0, u),
          knee_n=38.0, knee_f=30.0, ankle_n=28.0, ankle_f=24.0, abd_hip_n=2.0, abd_hip_f=2.0)
    b = fk(p)
    sh = b.pts["shc"]
    target_y = lerp(PULL_BAR_Y - (ARM_REACH - 0.02), PULL_BAR_Y - 0.10, u)
    target_x = lerp(-0.03, -0.10, u)
    b.shift(target_x - sh[0], target_y - sh[1])
    for s, sgn in (("n", 1.0), ("f", -1.0)):
        ik_arm(b, s, (0.0, PULL_BAR_Y, sgn * 0.33), vnorm((0.35, -1.0, sgn * 0.8)))
    return Frame3D(b, [Bar(0.0, PULL_BAR_Y, -0.92, 0.66, r=0.018),
                       Box(-0.03, 0.03, 0.0, PULL_BAR_Y + 0.12, -0.98, -0.92, (0.24, 0.26, 0.31))])


# --- bench press -------------------------------------------------------------------------------------------
BENCH_TOP = 0.44


def _bench_props() -> list[Any]:
    pad = (0.16, 0.17, 0.205)
    frame = (0.30, 0.32, 0.37)
    return [
        Box(-0.78, 0.46, BENCH_TOP - 0.075, BENCH_TOP, -0.145, 0.145, pad, bias=-0.2),
        Box(-0.70, 0.40, BENCH_TOP - 0.13, BENCH_TOP - 0.075, -0.05, 0.05, frame, bias=-0.25),
        Box(-0.70, -0.64, 0.0, BENCH_TOP - 0.12, -0.05, 0.05, frame, bias=-0.25),
        Box(0.33, 0.39, 0.0, BENCH_TOP - 0.12, -0.05, 0.05, frame, bias=-0.25),
        Box(-0.74, -0.60, 0.0, 0.03, -0.24, 0.24, frame, bias=-0.26),
        Box(0.29, 0.43, 0.0, 0.03, -0.24, 0.24, frame, bias=-0.26),
    ]


def _bench_body() -> Body:
    p = P(lean=-86.0, spine=-10.0, neck=12.0, abd_hip_n=14.0, abd_hip_f=14.0)
    b = fk(p)
    b.shift(0.22 - b.pts["pelvis"][0], BENCH_TOP + 0.105 - b.pts["pelvis"][1])
    for s, sgn in (("n", 1.0), ("f", -1.0)):
        ik_leg(b, s, (0.80, ANKLE_H, sgn * 0.24), vnorm((0.3, 1.0, sgn * 0.5)))
    return b


@lru_cache(maxsize=1)
def _bench_path() -> tuple[Vec, Vec]:
    b = _bench_body()
    chest = vcomb((b.pts["shc"], 1.0), (b.dirs["fwt"], 0.135), (b.dirs["thd"], -0.12))
    bottom = (chest[0], chest[1] + 0.035, 0.0)
    sh = b.pts["shc"]
    top = (sh[0] + 0.01, sh[1] + ARM_REACH - 0.02, 0.0)
    return bottom, top


def bench_pose(ph: float) -> Frame3D:
    b = _bench_body()
    bottom, top = _bench_path()
    u = track([(0.0, 0.0), (0.44, 1.0), (0.52, 1.0), (0.86, 0.0)], ph)  # 1 = bar on the chest
    x = lerp(top[0], bottom[0], 1 - (1 - u) ** 2 if u > 0 else 0.0)
    y = lerp(top[1], bottom[1], u)
    for s, sgn in (("n", 1.0), ("f", -1.0)):
        ik_arm(b, s, (x, y, sgn * 0.40), vnorm((0.15, -1.0, sgn * 0.9)))
    return Frame3D(b, _bench_props() + [Bar(x, y, -1.1, 1.1), Plates(x, y, 0.225, z=0.86)])


# --- deadlift ----------------------------------------------------------------------------------------------
DL_BAR_X = 0.10
DL_FLOOR_Y = 0.225


def deadlift_body(bar_y: float) -> Body:
    """Pose with straight arms holding the bar at (DL_BAR_X, bar_y): shins, hips and torso solved by IK."""
    lift = smooth(DL_FLOOR_Y, 0.74, bar_y)
    shin = -13.0 * (1 - smooth(DL_FLOOR_Y, 0.52, bar_y))
    delta = lerp(0.075, -0.025, lift)
    arm = ARM_REACH - 0.004
    sh_target = (DL_BAR_X + delta, bar_y + math.sqrt(max(0.0, arm * arm - delta * delta)), 0.0)
    ankle = (0.0, ANKLE_H, 0.0)
    knee = (ankle[0] - SHIN * math.sin(rad(shin)), ankle[1] + SHIN * math.cos(rad(shin)), 0.0)
    hip, sh_reached = two_bone(knee, sh_target, THIGH, TORSO_LEN, (-1.0, 0.25, 0.0))
    tv = vsub(sh_reached, hip)
    lean = deg(math.atan2(tv[0], tv[1]))
    thigh = deg(math.atan2(knee[0] - hip[0], -(knee[1] - hip[1])))
    p = P(lean=lean, neck=-0.25 * lean + 6.0, abd_hip_n=6.0, abd_hip_f=6.0)
    for s in ("n", "f"):
        p["hip_" + s] = thigh + lean
        p["knee_" + s] = thigh - shin
        p["ankle_" + s] = shin
    b = fk(p)
    b.shift(hip[0] - b.pts["pelvis"][0], hip[1] - b.pts["pelvis"][1])
    for s, sgn in (("n", 1.0), ("f", -1.0)):
        ik_arm(b, s, (DL_BAR_X, bar_y, sgn * 0.27), vnorm((-0.6, -0.2, sgn * 0.6)))
    return b


def deadlift_pose(ph: float) -> Frame3D:
    top_y = 0.80
    y = track([(0.0, DL_FLOOR_Y), (0.08, DL_FLOOR_Y), (0.46, top_y), (0.60, top_y), (0.94, DL_FLOOR_Y)], ph)
    b = deadlift_body(y)
    return Frame3D(b, [Bar(DL_BAR_X, y, -1.1, 1.1), Plates(DL_BAR_X, y, DL_FLOOR_Y, z=0.84)])


# --- overhead press ----------------------------------------------------------------------------------------
OHP_BAR_X = 0.065


def ohp_body(u: float) -> tuple[Body, Vec]:
    """u: 0 = bar racked on the front shoulders, 1 = locked out overhead."""
    def build(lean: float, neck: float) -> Body:
        p = P(lean=lean, neck=neck, abd_hip_n=4.0, abd_hip_f=4.0, knee_n=2.0, knee_f=2.0)
        for s in ("n", "f"):
            p["hip_" + s] = lean + 2.0
            p["ankle_" + s] = 0.0
        return heel_on_floor(fk(p), "n", 0.0)

    rack_y = STAND_Y + TORSO_LEN + 0.04
    lock_y = STAND_Y + TORSO_LEN + ARM_REACH - 0.02
    y = lerp(rack_y, lock_y, u)
    passing = smooth(0.06, 0.25, u) * (1 - smooth(0.42, 0.62, u))
    neck = -16.0 * passing + 2.0 * smooth(0.6, 1.0, u)
    sh_x = lerp(OHP_BAR_X - 0.115, OHP_BAR_X - 0.005, smooth(0.25, 0.85, u))
    lean = bisect(lambda ln: build(ln, neck).pts["shc"][0] - sh_x, -12.0, 12.0)
    return build(lean, neck), (OHP_BAR_X, y, 0.0)


def ohp_pose(ph: float) -> Frame3D:
    u = track([(0.0, 0.0), (0.08, 0.0), (0.42, 1.0), (0.56, 1.0), (0.92, 0.0)], ph,
              ease=lambda t: ease_out(t) if t < 1 else 1.0)
    b, bar = ohp_body(u)
    pole_fw = 1.0 - smooth(0.3, 0.9, u)
    for s, sgn in (("n", 1.0), ("f", -1.0)):
        pole = vcomb((b.dirs["fwt"], 0.4 + pole_fw), (b.dirs["thd"], -0.8), ((0, 0, 1), sgn * 0.7))
        ik_arm(b, s, (bar[0], bar[1], sgn * 0.27), vnorm(pole))
    return Frame3D(b, [Bar(bar[0], bar[1], -1.05, 1.05), Plates(bar[0], bar[1], 0.16, z=0.80)])


# --- conditioning ------------------------------------------------------------------------------------------
def jack_pose(t: float, period: float) -> Frame3D:
    u = (t / period) % 1.0
    open_ = ease_sine(u / 0.5) if u < 0.5 else 1 - ease_sine((u - 0.5) / 0.5)
    frac = (u % 0.5) / 0.5
    hop = math.sin(PI * frac)
    knee = 5.0 + 15.0 * (1 - hop) ** 2
    p = P(lean=1.5, neck=-2.0)
    for s in ("n", "f"):
        p["abd_sh_" + s] = 9.0 + 161.0 * open_
        p["sh_" + s] = 4.0 + 6.0 * open_
        p["el_" + s] = 12.0 - 6.0 * open_
        p["abd_hip_" + s] = 3.0 + 18.0 * open_
        p["hip_" + s] = knee * 0.5
        p["knee_" + s] = knee
        p["ankle_" + s] = 12.0 * hop
    b = fk(p)
    floor = min(b.foot_floor("n"), b.foot_floor("f"))
    return Frame3D(b.shift(0.0, -floor + 0.075 * hop))


@lru_cache(maxsize=1)
def _climber_base() -> tuple[float, float, tuple[Vec, Vec], dict[str, Vec]]:
    b = _plank_body(24.0, neck=-10.0)
    sh = b.pts["shc"]
    hands = ((sh[0] + 0.02, WRIST_H, SH_W + 0.05), (sh[0] + 0.02, WRIST_H, -(SH_W + 0.05)))
    ankles = {s: b.pts["ankle_" + s] for s in ("n", "f")}
    return b.pts["pelvis"][0], b.pts["pelvis"][1], hands, ankles


def climber_pose(t: float, period: float) -> Frame3D:
    """High plank; each foot travels on an arc above the floor from the extended position to under the hips."""
    px, py, hands, ankles = _climber_base()
    u = (t / period) % 1.0
    b = fk(P(x=px, y=py + 0.012 * math.sin(4 * PI * u), lean=66.0, neck=-10.0, abd_hip_n=2.5, abd_hip_f=2.5))
    for s, ph in (("n", u), ("f", u + 0.5)):
        tri = 1.0 - abs(2.0 * (ph % 1.0) - 1.0)  # 0 -> 1 -> 0, eased: quick swing, short pauses at the ends
        drive = ease_io(tri)
        ext = ankles[s]
        tuck = (px + 0.02, 0.29, ext[2] * 0.9)
        target = (lerp(ext[0], tuck[0], drive), lerp(ext[1], tuck[1], drive) + 0.10 * math.sin(PI * drive), lerp(ext[2], tuck[2], drive))
        ik_leg(b, s, target, (1.0, -0.15, 0.0), foot_frame(60.0 + 14.0 * drive))
    for s, target, sgn in (("n", hands[0], 1.0), ("f", hands[1], -1.0)):
        ik_arm(b, s, target, vnorm((-0.6, 0.1, sgn * 0.8)), palm=True)
    return Frame3D(b)


def high_knees_pose(t: float, period: float) -> Frame3D:
    u = (t / period) % 1.0
    up_n, up_f = bump(u), bump(u + 0.5)
    p = P(lean=5.0, neck=-3.0, abd_hip_n=4.0, abd_hip_f=4.0)
    for s, a, o in (("n", up_n, up_f), ("f", up_f, up_n)):
        p["hip_" + s] = 8.0 + 90.0 * a
        p["knee_" + s] = 14.0 + 92.0 * a
        p["ankle_" + s] = 24.0 - 30.0 * a
        p["sh_" + s] = -28.0 + 78.0 * o
        p["el_" + s] = 88.0
        p["abd_sh_" + s] = 8.0
    b = fk(p)
    support = "n" if up_n < up_f else "f"
    hop = 0.03 * (1 - abs(math.cos(2 * PI * u))) ** 1.5
    toe = b.pts["toe_" + support]
    return Frame3D(b.shift(0.10 - toe[0], R_TOE - toe[1] + hop))


def lunge_pose(rep: int, ph: float) -> Frame3D:
    back, front = ("n", "f") if rep % 2 == 0 else ("f", "n")
    depth = track([(0.0, 0.0), (0.14, 0.04), (0.30, 0.45), (0.50, 1.0), (0.60, 1.0), (0.78, 0.45), (0.92, 0.04), (1.0, 0.0)], ph,
                  ease=ease_sine)
    pel_x = lerp(-0.02, -0.40, depth)
    pel_y = lerp(STAND_Y - 0.004, 0.565, depth ** 1.15)
    lean = 4.0 + 7.0 * depth
    p = P(x=pel_x, y=pel_y, lean=lean, neck=-0.6 * lean)
    b = fk(p)
    zs = {"n": HIP_W + 0.02, "f": -(HIP_W + 0.02)}
    ik_leg(b, front, (0.0, ANKLE_H, zs[front]), (1.0, 0.0, 0.0))
    step = track([(0.0, 0.0), (0.06, 0.0), (0.30, 1.0), (0.78, 1.0), (0.96, 0.0)], ph)
    lift = math.sin(PI * clamp((ph - 0.06) / 0.24, 0, 1)) + math.sin(PI * clamp((ph - 0.78) / 0.18, 0, 1))
    toe_down = 58.0 * smooth(0.12, 0.30, ph) * (1 - smooth(0.80, 0.96, ph))
    fdir, fdown = foot_frame(toe_down)
    ank_y = max(ANKLE_H, R_TOE - TOE_FWD * fdir[1] - TOE_DOWN * fdown[1])  # keeps the toe (or heel) on the floor
    ank = (lerp(0.0, -0.80, step), ank_y + 0.09 * lift, zs[back])
    ik_leg(b, back, ank, (1.0, -0.15, 0.0), (fdir, fdown))
    for s, sgn in (("n", 1.0), ("f", -1.0)):
        target = vcomb((b.pts["pelvis"], 1.0), (b.dirs["fw"], 0.03), (b.dirs["up"], 0.10), ((0, 0, 1), sgn * 0.175))
        ik_arm(b, s, target, vnorm((-0.7, -0.1, sgn * 1.0)))
    return Frame3D(b)


# --- floor / mobility --------------------------------------------------------------------------------------
QUAD_LEAN = 80.0


def _quad_body(spine: float = 0.0, neck: float = 0.0) -> Body:
    p = P(lean=QUAD_LEAN, spine=spine, neck=neck, abd_hip_n=3.0, abd_hip_f=3.0)
    for s in ("n", "f"):
        p["hip_" + s] = QUAD_LEAN
        p["knee_" + s] = 90.0
        p["ankle_" + s] = 88.0
    b = fk(p)
    return b.shift(0.0, R_KNEE - b.pts["knee_n"][1])


@lru_cache(maxsize=1)
def _quad_hands() -> tuple[Vec, Vec]:
    sh = _quad_body().pts["shc"]
    return (sh[0] + 0.03, WRIST_H, SH_W + 0.02), (sh[0] + 0.03, WRIST_H, -(SH_W + 0.02))


def _mat(x0: float = -0.9, x1: float = 1.0) -> Box:
    return Box(x0, x1, 0.0, 0.012, -0.40, 0.40, (0.10, 0.11, 0.14), bias=-5.0, shadow=False)


def catcow_pose(ph: float) -> Frame3D:
    s = track([(0.0, 0.0), (0.25, 1.0), (0.5, 0.0), (0.75, -1.0), (1.0, 0.0)], ph, ease=ease_sine)
    spine = 34.0 * s if s > 0 else 24.0 * s
    neck = 34.0 * s if s > 0 else 26.0 * s
    b = _quad_body(spine, neck)
    hands = _quad_hands()
    for side, target, sgn in (("n", hands[0], 1.0), ("f", hands[1], -1.0)):
        ik_arm(b, side, target, vnorm((-1.0, 0.1, sgn * 0.3)), palm=True)
    return Frame3D(b, [_mat(-0.75, 0.95)])


def birddog_pose(rep: int, ph: float) -> Frame3D:
    """Quadruped; one arm reaches forward (hand lifted on an arc) while the opposite leg extends back."""
    arm, leg = ("n", "f") if rep % 2 == 0 else ("f", "n")
    support = "f" if arm == "n" else "n"
    e = track([(0.0, 0.0), (0.38, 1.0), (0.62, 1.0), (1.0, 0.0)], ph, ease=ease_sine)
    p = P(lean=QUAD_LEAN - 2.0 * e, neck=-8.0 * e, abd_hip_n=3.0, abd_hip_f=3.0)
    for s in ("n", "f"):
        p["hip_" + s] = QUAD_LEAN
        p["knee_" + s] = 90.0
        p["ankle_" + s] = 88.0
    p["hip_" + leg] = lerp(QUAD_LEAN, QUAD_LEAN - 92.0, e)
    p["knee_" + leg] = lerp(90.0, 4.0, e)
    p["ankle_" + leg] = lerp(88.0, 62.0, e)
    b = fk(p)
    b.shift(0.0, R_KNEE - b.pts["knee_" + arm][1])  # the knee on the reaching arm's side stays down
    hands = {"n": _quad_hands()[0], "f": _quad_hands()[1]}
    sgn = {"n": 1.0, "f": -1.0}
    ik_arm(b, support, hands[support], vnorm((-1.0, 0.1, sgn[support] * 0.3)), palm=True)
    sh = b.pts["sh_" + arm]
    reach = vnorm((1.0, 0.06, 0.0))
    extended = vadd(sh, vmul(reach, (UPPER + FORE) * 0.985))
    planted = hands[arm]
    target = vadd(vcomb((planted, 1 - e), (extended, e)), (0.0, 0.09 * math.sin(PI * e), 0.0))
    palm_dir = vnorm(vcomb(((1.0, 0.0, 0.0), 1 - e), (reach, e)))
    ik_arm(b, arm, target, vnorm((-0.8, -0.6, sgn[arm] * 0.3)), palm=True, palm_dir=palm_dir)
    return Frame3D(b, [_mat(-0.95, 1.05)])


def bridge_pose(ph: float) -> Frame3D:
    rise = track([(0.0, 0.0), (0.36, 1.0), (0.56, 1.0), (0.90, 0.0)], ph)
    rho = rad(30.0 * rise)
    shc = (-0.20, 0.124, 0.0)
    u = (-math.cos(rho), -math.sin(rho), 0.0)
    lean = deg(math.atan2(u[0], u[1]))
    pelvis = vsub(shc, vmul(u, TORSO_LEN))
    p = P(x=pelvis[0], y=pelvis[1], lean=lean, neck=deg(rho) * 0.9 + 4.0)
    b = fk(p)
    feet_x = -0.20 + TORSO_LEN + 0.47
    for s, sgn in (("n", 1.0), ("f", -1.0)):
        ik_leg(b, s, (feet_x, ANKLE_H, sgn * 0.13), vnorm((0.4, 1.0, sgn * 0.15)))
        ik_arm(b, s, (-0.20 + TORSO_LEN + 0.02, 0.036, sgn * 0.25), vnorm((0.0, 1.0, sgn * 0.6)), palm=True)
    return Frame3D(b, [_mat(-0.85, 1.0)])


STEP_TOP = 0.26


def hamstring_pose(t: float, lean_u: float) -> Frame3D:
    breath = math.sin(2 * PI * t / 4.0) * smooth(0.6, 1.0, lean_u)
    lean = 3.0 + 33.0 * lean_u + 1.6 * breath
    p = P(x=lerp(-0.01, -0.07, lean_u), y=STAND_Y - 0.012 - 0.02 * lean_u, lean=lean, neck=-0.35 * lean,
          abd_hip_n=3.0, abd_hip_f=3.0)
    b = fk(p)
    ik_leg(b, "f", (0.0, ANKLE_H, -HIP_W - 0.01), (1.0, 0.0, 0.0))
    fdir, fdown = foot_frame(-48.0)
    heel_target = (0.60, STEP_TOP + R_HEEL, HIP_W + 0.01)
    ankle = vcomb((heel_target, 1.0), (fdir, HEEL_BACK), (fdown, -HEEL_DOWN))
    ik_leg(b, "n", ankle, (1.0, 0.6, 0.0), (fdir, fdown))
    for s, sgn in (("n", 1.0), ("f", -1.0)):
        knee, hip = b.pts["knee_n"], b.pts["hip_n"]
        on = vcomb((hip, 0.38 - 0.06 * lean_u), (knee, 0.62 + 0.06 * lean_u))
        target = (on[0], on[1] + 0.075, on[2] + sgn * 0.02)
        ik_arm(b, s, target, vnorm((-0.4, -0.3, sgn * 1.0)))
    return Frame3D(b, [Box(0.48, 0.92, 0.0, STEP_TOP, -0.22, 0.34, (0.18, 0.19, 0.23), bias=-0.4)])


# ===================================================================================== segments & programs
@dataclass
class Segment:
    """One exercise block of a video. ``pose(rep, phase, t)`` returns the 3D frame."""

    name: str
    exercise: str
    pose: Callable[[int, float, float], Frame3D]
    reps: int | None
    rep_s: float
    yaw: float = 0.40
    pitch: float = 0.10
    lead: float = 0.7
    tail: float = 0.5
    cues: tuple[int, int] = (0, 99)
    cue_fn: Callable[[float], tuple[int, float]] | None = None
    counter: str = "REP"
    max_scale: float = 290.0
    floor: bool = True  # frame the floor (False: crop to the figure, e.g. hanging from a high bar)
    cam: Camera | None = None
    bg: np.ndarray | None = None

    @property
    def duration(self) -> float:
        return self.lead + self.rep_s * (self.reps or 1) + self.tail

    def state(self, t: float) -> tuple[int, float]:
        active = t - self.lead
        if active < 0:
            return 0, 0.0
        if self.reps:
            k = int(active // self.rep_s)
            if k >= self.reps:
                return self.reps - 1, 1.0
            return k, (active - k * self.rep_s) / self.rep_s
        return 0, clamp(active / self.rep_s, 0.0, 1.0)

    def frame3d(self, t: float) -> Frame3D:
        rep, ph = self.state(t)
        return self.pose(rep, ph, t)


@dataclass
class Program:
    spec: VideoSpec
    look: Look
    segments: list[Segment]
    hero: tuple[int, float] = (0, 1.0)
    breathing: bool = False
    intro: tuple[float, float] = (2.6, 0.5)  # (card length, cross-fade into the live scene)
    outro: tuple[float, float] = (2.8, 0.5)


def _seg(name: str, ex: str, pose: Callable[[int, float, float], Frame3D], reps: int | None, rep_s: float, **kw: Any) -> Segment:
    return Segment(name=name, exercise=ex, pose=pose, reps=reps, rep_s=rep_s, **kw)


def build_program(spec: VideoSpec, quick: bool = False) -> Program:
    look = GOAL_LOOKS.get(spec.goal, GOAL_LOOKS["general_fitness"])
    slug = spec.slug
    segs: list[Segment]
    hero = (0, 1.0)
    if slug == "push-up":
        segs = [_seg("Push-up", "push-up", lambda r, ph, t: pushup_pose(ph), 8, 2.8, yaw=0.46)]
        hero = (0, 0.7 + 2.8 * 0.30)
    elif slug == "bodyweight-squat":
        segs = [_seg("Bodyweight squat", "bodyweight-squat", lambda r, ph, t: squat_pose(ph), 7, 3.0, yaw=0.50)]
        hero = (0, 0.7 + 3.0 * 0.30)
    elif slug == "forearm-plank":
        segs = [_seg("Forearm plank", "forearm-plank", lambda r, ph, t: plank_pose(t), None, 22.0, yaw=0.46,
                     counter="HOLD")]
        hero = (0, 5.0)
    elif slug == "pull-up-progression":
        segs = [_seg("Inverted row", "inverted-row", lambda r, ph, t: row_pose(ph), 4, 2.7, yaw=-0.42, cues=(0, 3)),
                _seg("Pull-up", "pull-up", lambda r, ph, t: pullup_pose(ph), 3, 3.2, yaw=0.42, cues=(3, 6), floor=False)]
        hero = (0, 0.7 + 2.7 * 0.42)
    elif slug == "barbell-back-squat":
        segs = [_seg("Barbell back squat", "barbell-back-squat", lambda r, ph, t: squat_pose(ph, depth=1.03, arms="bar", bar=True),
                     6, 3.4, yaw=0.22)]
        hero = (0, 0.7 + 3.4 * 0.30)
    elif slug == "barbell-bench-press":
        segs = [_seg("Barbell bench press", "barbell-bench-press", lambda r, ph, t: bench_pose(ph), 6, 3.3, yaw=-0.26,
                     pitch=0.16)]
        hero = (0, 0.7 + 3.3 * 0.30)
    elif slug == "barbell-deadlift":
        segs = [_seg("Barbell deadlift", "barbell-deadlift", lambda r, ph, t: deadlift_pose(ph), 5, 4.1, yaw=0.24)]
        hero = (0, 0.7 + 4.1 * 0.22)
    elif slug == "barbell-overhead-press":
        segs = [_seg("Overhead press", "barbell-overhead-press", lambda r, ph, t: ohp_pose(ph), 7, 3.0, yaw=0.22)]
        hero = (0, 0.7 + 3.0 * 0.30)
    elif slug == "jumping-jacks":
        segs = [_seg("Jumping jacks", "jumping-jacks", lambda r, ph, t: jack_pose(t - 0.7, 0.92), 22, 0.92, yaw=1.42,
                     pitch=0.07, lead=0.7)]
        hero = (0, 0.7 + 0.92 * 0.25)
    elif slug == "mountain-climbers":
        segs = [_seg("Mountain climbers", "mountain-climbers", lambda r, ph, t: climber_pose(t - 0.7, 1.2), 34, 0.6,
                     yaw=0.46)]
        hero = (0, 0.7 + 1.2 * 0.5)
    elif slug == "high-knees":
        segs = [_seg("High knees", "high-knees", lambda r, ph, t: high_knees_pose(t - 0.7, 1.0), 40, 0.5, yaw=0.44)]
        hero = (0, 0.7 + 0.5)
    elif slug == "reverse-lunges":
        segs = [_seg("Reverse lunge", "reverse-lunge", lambda r, ph, t: lunge_pose(r, ph), 7, 3.1, yaw=0.48)]
        hero = (0, 0.7 + 3.1 * 0.52)
    elif slug == "full-body-starter":
        segs = [_seg("Bodyweight squat", "bodyweight-squat", lambda r, ph, t: squat_pose(ph), 3, 2.9, yaw=0.50, cues=(0, 2)),
                _seg("Glute bridge", "glute-bridge", lambda r, ph, t: bridge_pose(ph), 3, 2.8, yaw=-0.46, pitch=0.16, cues=(2, 4)),
                _seg("Bird dog", "bird-dog", lambda r, ph, t: birddog_pose(r, ph), 4, 2.5, yaw=0.46, pitch=0.13, cues=(4, 6))]
        hero = (1, 0.7 + 2.8 * 0.45)
    elif slug == "spine-hamstring-reset":
        def catcow_cue(t: float) -> tuple[int, float]:
            if t < 1.6:
                return 0, t
            ph = ((t - 0.7) / 4.0) % 1.0
            return (1 if ph < 0.5 else 2), min((ph % 0.5) * 4.0, t - 1.6)

        def ham(r: int, ph: float, t: float) -> Frame3D:
            lean_u = track([(0.0, 0.0), (0.22, 1.0), (0.82, 1.0), (1.0, 0.7)], ph, ease=ease_sine)
            return hamstring_pose(t, lean_u)

        segs = [_seg("Cat-cow", "cat-cow", lambda r, ph, t: catcow_pose(ph), 4, 4.0, yaw=0.48, pitch=0.13, cues=(0, 3),
                     cue_fn=catcow_cue),
                _seg("Hamstring stretch", "hamstring-stretch", ham, None, 9.0, yaw=0.50, cues=(3, 6), counter="HOLD")]
        hero = (0, 0.7 + 4.0 * 0.25)
    elif slug == "box-breathing":
        # two full 4-4-4-4 rounds (32 s) - shorter title cards keep the video within 35 s
        segs = [_seg("Box breathing", "box-breathing", lambda r, ph, t: Frame3D(None), 2, 16.0, lead=0.3, tail=0.0,
                     counter="ROUND")]
        return _quickify(Program(spec, look, segs, hero=(0, 0.3 + 2.0), breathing=True, intro=(1.8, 0.5),
                                 outro=(1.8, 0.4)), quick)
    else:  # pragma: no cover - every catalogue entry has a program (tested)
        raise KeyError(f"no animation program for video {slug!r}")
    return _quickify(Program(spec, look, segs, hero=hero), quick)


def _quickify(prog: Program, quick: bool) -> Program:
    if not quick:
        return prog
    for seg in prog.segments:
        if prog.breathing:
            seg.reps, seg.rep_s = 1, 4.0
        elif seg.reps:
            seg.reps = min(seg.reps, 2 if seg.rep_s >= 2 else 4)
        else:
            seg.rep_s = min(seg.rep_s, 3.0)
        seg.lead, seg.tail = 0.3, 0.2
    prog.hero = (prog.hero[0], min(prog.hero[1], prog.segments[prog.hero[0]].duration * 0.5))
    return prog


# ===================================================================================== framing & backgrounds
FLOOR_Y = 424.0
STAGE = (96.0, 72.0, 864.0, FLOOR_Y)  # x0, y0, x1, y1 (1x px) where the figure lives


def frame_camera(seg: Segment) -> Camera:
    """Fit every sampled pose of the segment into the stage (auto framing, floor on FLOOR_Y)."""
    unit = Camera(seg.yaw, seg.pitch, 1.0, 0.0, 0.0)
    xs: list[float] = []
    ys: list[float] = []
    n = 36
    for i in range(n + 1):
        f = seg.frame3d(seg.duration * i / n)
        pts: list[tuple[Vec, float]] = []
        if f.body is not None:
            pts += [(v, 0.12) for v in f.body.pts.values()]
        for pr in f.props:
            if isinstance(pr, Plates):
                pts += [((pr.x, pr.y + dy, z), pr.radius * 0.3) for z in (-pr.z, pr.z) for dy in (-pr.radius, pr.radius)]
            elif isinstance(pr, Bar):
                pts += [((pr.x, pr.y, pr.z0), 0.03), ((pr.x, pr.y, pr.z1), 0.03)]
            elif isinstance(pr, Box) and pr.bias > -1:
                pts += [((x, y, z), 0.0) for x in (pr.x0, pr.x1) for y in (pr.y0, pr.y1) for z in (pr.z0, pr.z1)]
        for v, r in pts:
            x, y, _ = unit.raw(v)
            xs += [x - r, x + r]
            ys += [y - r, y + r]
    x0, x1 = min(xs), max(xs)
    y_top = max(ys)
    floor = unit.raw((0.0, 0.0, 0.0))[1]
    bottom = floor if seg.floor else min(ys) - 0.05
    width, height = max(0.2, x1 - x0), max(0.2, y_top - bottom)
    sx0, sy0, sx1, sy1 = STAGE
    scale = min((sx1 - sx0) / width, (sy1 - sy0) / height, seg.max_scale)
    ox = (sx0 + sx1) / 2 - (x0 + x1) / 2 * scale
    oy = sy1 + floor * scale + (bottom - floor) * scale
    return Camera(seg.yaw, seg.pitch, scale, ox, oy)


def make_background(look: Look, floor_y: float = FLOOR_Y, cx: float = W / 2) -> np.ndarray:
    yy = (np.arange(H, dtype=np.float32) + 0.5)[:, None]
    xx = (np.arange(W, dtype=np.float32) + 0.5)[None, :]
    top, bottom = np.array(_rgb("0c1222"), np.float32), np.array(_rgb("05070d"), np.float32)
    t = (yy / H) ** 0.9
    img = top[None, None, :] * (1 - t[..., None]) + bottom[None, None, :] * t[..., None]
    img = np.broadcast_to(img, (H, W, 3)).copy()
    acc, acc2 = np.array(look.accent, np.float32), np.array(look.accent2, np.float32)
    g1 = np.exp(-(((xx - cx) / 300.0) ** 2 + ((yy - (floor_y - 150)) / 210.0) ** 2) / 2)
    g2 = np.exp(-(((xx - (cx + 300)) / 260.0) ** 2 + ((yy - 70) / 170.0) ** 2) / 2)
    img += g1[..., None] * acc * 0.20 + g2[..., None] * acc2 * 0.09
    # stage floor: soft spotlight ellipse + faint horizon line
    spot = np.exp(-(((xx - cx) / 330.0) ** 2 + ((yy - floor_y - 6) / 34.0) ** 2) / 2)
    img += spot[..., None] * np.array((0.12, 0.13, 0.17), np.float32) * 0.85
    line = np.exp(-((yy - floor_y) / 1.1) ** 2) * np.exp(-((xx - cx) / 380.0) ** 2)
    img += line[..., None] * np.array((0.10, 0.12, 0.16), np.float32)
    below = np.clip((yy - floor_y) / 120.0, 0.0, 1.0)
    img *= (1.0 - 0.28 * below)[..., None]
    vig = 1.0 - 0.38 * (((xx - W / 2) / (W * 0.62)) ** 2 + ((yy - H * 0.46) / (H * 0.78)) ** 2)
    img *= np.clip(vig, 0.45, 1.0)[..., None]
    rng = np.random.default_rng(1729)
    img += rng.normal(0.0, 0.0024, img.shape).astype(np.float32)  # dither against banding
    return np.clip(img, 0.0, 1.0).astype(np.float32)


# ===================================================================================== fonts & HUD
_FONT_CANDIDATES: dict[str, list[tuple[str, str | None, int]]] = {
    "regular": [("SFNS.ttf", "Regular", 0), ("HelveticaNeue.ttc", None, 0), ("segoeui.ttf", None, 0),
                ("Inter-Regular.ttf", None, 0), ("NotoSans-Regular.ttf", None, 0), ("DejaVuSans.ttf", None, 0),
                ("Arial.ttf", None, 0), ("arial.ttf", None, 0)],
    "medium": [("SFNS.ttf", "Medium", 0), ("HelveticaNeue.ttc", None, 10), ("seguisb.ttf", None, 0),
               ("Inter-Medium.ttf", None, 0), ("NotoSans-Medium.ttf", None, 0), ("DejaVuSans.ttf", None, 0),
               ("Arial.ttf", None, 0), ("arial.ttf", None, 0)],
    "semibold": [("SFNS.ttf", "Semibold", 0), ("HelveticaNeue.ttc", None, 10), ("seguisb.ttf", None, 0),
                 ("Inter-SemiBold.ttf", None, 0), ("NotoSans-SemiBold.ttf", None, 0), ("DejaVuSans-Bold.ttf", None, 0),
                 ("Arial Bold.ttf", None, 0), ("arialbd.ttf", None, 0)],
    "bold": [("SFNS.ttf", "Bold", 0), ("HelveticaNeue.ttc", None, 1), ("segoeuib.ttf", None, 0),
             ("Inter-Bold.ttf", None, 0), ("NotoSans-Bold.ttf", None, 0), ("DejaVuSans-Bold.ttf", None, 0),
             ("Arial Bold.ttf", None, 0), ("arialbd.ttf", None, 0)],
}


@lru_cache(maxsize=1)
def _font_index() -> dict[str, Path]:
    dirs = [Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"), Path("/Library/Fonts"),
            Path.home() / "Library" / "Fonts"]
    for env in ("WINDIR", "SystemRoot"):
        if os.environ.get(env):
            dirs.append(Path(os.environ[env]) / "Fonts")
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "Windows" / "Fonts")
    dirs += [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts",
             Path.home() / ".local" / "share" / "fonts"]
    wanted = {name.lower() for options in _FONT_CANDIDATES.values() for name, _, _ in options}
    index: dict[str, Path] = {}
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            for root, _subdirs, files in os.walk(d):
                for f in files:
                    if f.lower() in wanted and f.lower() not in index:
                        index[f.lower()] = Path(root) / f
                if root.count(os.sep) - str(d).count(os.sep) > 3:
                    _subdirs[:] = []
        except OSError:
            continue
    return index


@lru_cache(maxsize=64)
def font(weight: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    index = _font_index()
    for name, variation, face_index in _FONT_CANDIDATES.get(weight, _FONT_CANDIDATES["regular"]):
        path = index.get(name.lower())
        if path is None:
            continue
        try:
            f = ImageFont.truetype(str(path), size, index=face_index)
            if variation:
                try:
                    f.set_variation_by_name(variation)
                except Exception:  # noqa: BLE001 - not a variable font
                    pass
            return f
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def _u8(c: Iterable[float], a: int | None = None) -> tuple[int, ...]:
    rgb = tuple(int(round(clamp(x, 0, 1) * 255)) for x in c)
    return rgb + ((a,) if a is not None else ())


@lru_cache(maxsize=512)
def rr_mask(w: int, h: int, r: int) -> Image.Image:
    """Anti-aliased rounded-rectangle mask (drawn at 4x, box-reduced)."""
    s = 4
    m = Image.new("L", (max(1, w) * s, max(1, h) * s), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, w * s - 1, h * s - 1), radius=max(0, r) * s, fill=255)
    return m.reduce(s)


def _scaled(mask: Image.Image, alpha: float) -> Image.Image:
    return mask.point(lambda v: int(v * clamp(alpha, 0, 1)))


def glass(img: Image.Image, box: tuple[int, int, int, int], radius: int, alpha: float = 1.0, fill: int = 30,
          border: int = 46, blur: float = 9.0) -> None:
    """Frosted-glass panel: blur what is behind, tint, hairline border - all anti-aliased."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    if w <= 2 or h <= 2 or alpha <= 0.01:
        return
    mask = rr_mask(w, h, radius)
    region = img.crop(box).filter(ImageFilter.GaussianBlur(blur))
    img.paste(region, (x0, y0), _scaled(mask, alpha))
    img.paste((255, 255, 255), (x0, y0, x1, y1), _scaled(mask, alpha * fill / 255))
    inner = Image.new("L", (w, h), 0)
    inner.paste(rr_mask(w - 2, h - 2, max(0, radius - 1)), (1, 1))
    ring = Image.fromarray(np.clip(np.asarray(mask, np.int16) - np.asarray(inner, np.int16), 0, 255).astype(np.uint8), "L")
    img.paste((255, 255, 255), (x0, y0, x1, y1), _scaled(ring, alpha * border / 255))


def text(draw: ImageDraw.ImageDraw, xy: tuple[float, float], s: str, f: Any, fill: tuple[int, ...], anchor: str = "la",
         shadow: int = 0) -> None:
    if shadow:
        draw.text((xy[0], xy[1] + 1), s, font=f, fill=(0, 0, 0, shadow), anchor=anchor)
    draw.text(xy, s, font=f, fill=fill, anchor=anchor)


def spaced(draw: ImageDraw.ImageDraw, xy: tuple[float, float], s: str, f: Any, fill: tuple[int, ...], tracking: float,
           anchor_y: str = "m") -> float:
    """Letter-spaced text (Pillow has no tracking); returns the end x."""
    x, y = xy
    for ch in s:
        draw.text((x, y), ch, font=f, fill=fill, anchor="l" + anchor_y)
        x += draw.textlength(ch, font=f) + tracking
    return x - tracking


def spaced_width(draw: ImageDraw.ImageDraw, s: str, f: Any, tracking: float) -> float:
    return sum(draw.textlength(ch, font=f) for ch in s) + tracking * max(0, len(s) - 1)


@lru_cache(maxsize=8)
def logo_mark(size: int, accent: tuple[float, float, float], accent2: tuple[float, float, float]) -> Image.Image:
    """IronPulse mark: gradient squircle with a white pulse line (RGBA, anti-aliased)."""
    s = 4
    n = size * s
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32) / n
    t = np.clip((xx + yy) / 2, 0, 1)[..., None]
    rgb = np.array(accent, np.float32) * (1 - t) + np.array(accent2, np.float32) * t
    img = Image.fromarray((rgb * 255).astype(np.uint8), "RGB").convert("RGBA")
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, n - 1, n - 1), radius=int(n * 0.28), fill=255)
    img.putalpha(mask)
    d = ImageDraw.Draw(img)
    pts = [(0.17, 0.56), (0.36, 0.56), (0.45, 0.30), (0.57, 0.76), (0.66, 0.46), (0.83, 0.46)]
    d.line([(x * n, y * n) for x, y in pts], fill=(255, 255, 255, 255), width=max(2, int(n * 0.085)), joint="curve")
    return img.resize((size, size), Image.LANCZOS)


@lru_cache(maxsize=4)
def sparkle(size: int) -> Image.Image:
    s = 4
    n = size * s
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    c, r, w = n / 2, n / 2 - 1, n * 0.13
    star = [(c, c - r), (c + w, c - w), (c + r, c), (c + w, c + w), (c, c + r), (c - w, c + w), (c - r, c), (c - w, c - w)]
    ImageDraw.Draw(img).polygon(star, fill=(255, 255, 255, 255))
    return img.resize((size, size), Image.LANCZOS)


def _fmt(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


@lru_cache(maxsize=4)
def progress_gradient(width: int, accent: tuple[float, float, float], accent2: tuple[float, float, float]) -> Image.Image:
    t = np.linspace(0, 1, max(1, width), dtype=np.float32)[None, :, None]
    rgb = np.array(accent, np.float32) * (1 - t) + np.array(accent2, np.float32) * t
    return Image.fromarray((np.repeat(rgb, 4, axis=0) * 255).astype(np.uint8), "RGB")


# ===================================================================================== the renderer
SEG_FADE = 0.6  # cross-fade between the segments of a multi-exercise video
M = 36  # HUD margin


@dataclass
class Timeline:
    starts: list[float]
    live_start: float
    live_end: float
    total: float
    intro: float
    intro_fade: float
    outro: float
    outro_fade: float


def make_timeline(prog: Program, quick: bool = False) -> Timeline:
    intro, intro_fade = (1.2, 0.4) if quick else prog.intro
    outro, outro_fade = (1.2, 0.4) if quick else prog.outro
    starts, tau = [], 0.0
    for i, seg in enumerate(prog.segments):
        starts.append(tau)
        tau += seg.duration - (SEG_FADE if i < len(prog.segments) - 1 else 0.0)
    live_start = intro - intro_fade
    live_end = live_start + tau
    return Timeline(starts, live_start, live_end, live_end + outro - outro_fade, intro, intro_fade, outro, outro_fade)


def planned_duration(spec: VideoSpec) -> float:
    """Length (seconds) of the full-quality render of ``spec``."""
    return make_timeline(build_program(spec)).total


class Renderer:
    def __init__(self, spec: VideoSpec, quick: bool = False) -> None:
        self.spec, self.quick = spec, quick
        self.prog = build_program(spec, quick)
        self.look = self.prog.look
        self.tl = make_timeline(self.prog, quick)
        self.intro_len, self.intro_fade = self.tl.intro, self.tl.intro_fade
        self.outro_len, self.outro_fade = self.tl.outro, self.tl.outro_fade
        for seg in self.prog.segments:
            if self.prog.breathing:
                seg.cam = Camera(0.0, 0.0, 1.0, W / 2, FLOOR_Y)
                seg.bg = make_background(self.look, floor_y=FLOOR_Y + 60)
            else:
                seg.cam = frame_camera(seg)
                seg.bg = make_background(self.look, floor_y=seg.cam.oy, cx=W / 2)
        self._intro_bg: Image.Image | None = None
        self._outro_bg: Image.Image | None = None

    # ------------------------------------------------------------------ live scene
    def scene(self, seg: Segment, t: float) -> np.ndarray:
        assert seg.cam is not None and seg.bg is not None
        if self.prog.breathing:
            return self.breathing_scene(seg, t)
        f = seg.frame3d(t)
        frame = seg.bg.copy()
        cam, look = seg.cam, self.look
        prims: list[Prim] = []
        shadow_pts: list[tuple[Vec, float]] = []
        if f.body is not None:
            prims += figure_prims(f.body, cam, look)
            shadow_pts += body_shadow_points(f.body)
        for pr in f.props:
            prims += prop_prims(pr, cam, look)
            shadow_pts += prop_shadow_points(pr)
        # floor-level props (mats) are painted first, under the shadows
        floor_props = [p for p in prims if p.depth < -4.0]
        rest = [p for p in prims if p.depth >= -4.0]
        if floor_props:
            composite(frame, rasterize(floor_props, pad=4), (0, 0, 0), 0.0)
        apply_shadows(frame, shadow_blobs(shadow_pts, cam))
        composite(frame, rasterize(rest), look.accent2, 0.42)
        return frame

    def breathing_scene(self, seg: Segment, t: float) -> np.ndarray:
        assert seg.bg is not None
        frame = seg.bg.copy()
        look = self.look
        phase_len = 1.0 if self.quick else 4.0
        active = t - seg.lead
        cx, cy = W / 2 * SS, 262.0 * SS
        smin, smax = 128.0 * SS, 214.0 * SS
        if active < 0:
            ph_i, ph_u = 0, 0.0
        else:
            k = int(active // phase_len)
            if k >= 4 * (seg.reps or 1):
                ph_i, ph_u = 3, 1.0
            else:
                ph_i, ph_u = k % 4, (active - k * phase_len) / phase_len
        if ph_i == 0:
            size = lerp(smin, smax, ease_sine(ph_u))
        elif ph_i == 1:
            size = smax
        elif ph_i == 2:
            size = lerp(smax, smin, ease_sine(ph_u))
        else:
            size = smin
        half = size / 2
        rad_px = 34.0 * SS
        x0, y0, x1, y1 = int(cx - smax / 2 - 90), int(cy - smax / 2 - 90), int(cx + smax / 2 + 90), int(cy + smax / 2 + 90)
        X = (np.arange(x0, x1, dtype=np.float32) + 0.5)[None, :] - cx
        Y = (np.arange(y0, y1, dtype=np.float32) + 0.5)[:, None] - cy
        qx, qy = np.abs(X) - (half - rad_px), np.abs(Y) - (half - rad_px)
        outside = np.sqrt(np.maximum(qx, 0) ** 2 + np.maximum(qy, 0) ** 2)
        sd = outside + np.minimum(np.maximum(qx, qy), 0.0) - rad_px  # rounded-square SDF
        acc, acc2 = np.array(look.accent, np.float32), np.array(look.accent2, np.float32)
        # gradient along the diagonal
        t_grad = np.clip((X + Y) / (2 * smax) + 0.5, 0, 1)[..., None]
        grad = acc * (1 - t_grad) + acc2 * t_grad
        fill = np.clip(0.5 - sd, 0, 1) * (0.10 + 0.07 * (1 - np.clip(-sd / half, 0, 1)))
        stroke_w = 3.2 * SS
        ring = np.clip(stroke_w - np.abs(sd) + 0.5, 0, 1)
        glow_ring = np.exp(-(sd / (16.0 * SS)) ** 2) * 0.35
        # moving dot along the perimeter: inhale = up the left side, hold = across the top, exhale = down the right,
        # hold = back along the bottom
        e = ease_sine(ph_u) if ph_i in (0, 2) else ph_u
        corners = [(-half, half), (-half, -half), (half, -half), (half, half)]
        ax_, ay_ = corners[ph_i]
        bx_, by_ = corners[(ph_i + 1) % 4]
        dxp, dyp = _onto_rounded_square(lerp(ax_, bx_, e), lerp(ay_, by_, e), half, rad_px)
        trail_mask = None
        if ph_i in (0, 2):
            sx = ax_
            ymin, ymax = sorted((ay_, dyp))
            trail_mask = (np.abs(X - sx) < stroke_w * 3) & (Y >= ymin) & (Y <= ymax)
        else:
            sy = ay_
            xmin, xmax = sorted((ax_, dxp))
            trail_mask = (np.abs(Y - sy) < stroke_w * 3) & (X >= xmin) & (X <= xmax)
        trail = ring * trail_mask
        dd = np.sqrt((X - dxp) ** 2 + (Y - dyp) ** 2)
        dot = np.clip(11.0 * SS - dd + 0.5, 0, 1)
        dot_glow = np.exp(-(dd / (26.0 * SS)) ** 2)
        layer = (fill[..., None] * grad + ring[..., None] * 0.28 + glow_ring[..., None] * grad * 0.55
                 + trail[..., None] * grad * 1.6 + dot_glow[..., None] * grad * 0.9)
        layer = layer * (1 - dot[..., None]) + dot[..., None] * np.array((1.0, 1.0, 1.0), np.float32)
        h2, w2 = layer.shape[:2]
        h2, w2 = h2 // 2 * 2, w2 // 2 * 2
        small = layer[:h2, :w2].reshape(h2 // 2, 2, w2 // 2, 2, 3).mean(axis=(1, 3))
        fx, fy = x0 // 2, y0 // 2
        region = frame[fy:fy + small.shape[0], fx:fx + small.shape[1]]
        region += small[: region.shape[0], : region.shape[1]]
        self._breath = (ph_i, ph_u, active)
        return frame

    # ------------------------------------------------------------------ HUD
    def hud(self, img: Image.Image, seg_i: int, t: float, now: float) -> None:
        seg = self.prog.segments[seg_i]
        spec, look = self.spec, self.look
        d = ImageDraw.Draw(img, "RGBA")
        white = (255, 255, 255)
        # eyebrow + title
        dot_c = _u8(look.accent, 255)
        d.ellipse((M, 33, M + 8, 41), fill=dot_c)
        eyebrow = f"{look.goal_label.upper()} · {spec.level.upper()}"
        spaced(d, (M + 15, 37.5), eyebrow, font("semibold", 11), white + (165,), 1.6)
        text(d, (M - 1, 50), spec.title, font("bold", 27), white + (250,), shadow=70)
        if len(self.prog.segments) > 1:
            label = f"{seg_i + 1}/{len(self.prog.segments)}  ·  {seg.name}"
            f = font("semibold", 13)
            w = int(d.textlength(label, font=f)) + 26
            glass(img, (M, 92, M + w, 118), 13)
            d = ImageDraw.Draw(img, "RGBA")
            text(d, (M + 13, 105), label, f, white + (235,), anchor="lm")
        # counter card (top right) - width follows the content ("3 / 8", "0:12 / 0:22")
        rep, ph = seg.state(t)
        big, sub, flash = self._counter(seg, t, rep, ph)
        fb, fs = font("bold", 34), font("semibold", 17)
        bw, sw = d.textlength(big, font=fb), d.textlength(sub, font=fs)
        x1, y0 = W - M, 30
        x0 = x1 - max(136, int(bw + sw + 40))
        glass(img, (x0, y0, x1, y0 + 76), 18)
        d = ImageDraw.Draw(img, "RGBA")
        spaced(d, (x0 + 16, y0 + 17), seg.counter, font("semibold", 10), white + (150,), 1.8)
        col = tuple(int(lerp(255, c, flash)) for c in _u8(look.accent)) + (255,)
        text(d, (x0 + 15, y0 + 50), big, fb, col, anchor="lm")
        text(d, (x0 + 21 + bw, y0 + 54), sub, fs, white + (140,), anchor="lm")
        self.cue_pill(img, seg, t)
        self.footer(img, now)

    def footer(self, img: Image.Image, now: float) -> None:
        d = ImageDraw.Draw(img, "RGBA")
        white = (255, 255, 255)
        f = font("medium", 12)
        text(d, (M, 504), f"{_fmt(now)} / {_fmt(round(self.tl.total))}", f, white + (150,), anchor="lm")
        tw = d.textlength(WATERMARK, font=f)
        text(d, (W - M, 504), WATERMARK, f, white + (130,), anchor="rm")
        sp = sparkle(12)
        img.paste((255, 255, 255), (int(W - M - tw - 18), 498), _scaled(sp.getchannel("A"), 0.55))
        bar_w = W - 2 * M
        img.paste((255, 255, 255), (M, 518, M + bar_w, 522), _scaled(rr_mask(bar_w, 4, 2), 0.13))
        fill_w = int(round(bar_w * clamp(now / self.tl.total, 0, 1)))
        if fill_w >= 4:
            grad = progress_gradient(bar_w, self.look.accent, self.look.accent2).crop((0, 0, fill_w, 4))
            img.paste(grad, (M, 518), rr_mask(fill_w, 4, 2))

    def _counter(self, seg: Segment, t: float, rep: int, ph: float) -> tuple[str, str, float]:
        if self.prog.breathing:
            phase_len = 1.0 if self.quick else 4.0
            active = max(0.0, t - seg.lead)
            rnd = min(seg.reps or 1, int(active // (4 * phase_len)) + 1)
            return str(rnd), f"/ {seg.reps}", 0.0
        if seg.reps is None:
            active = clamp(t - seg.lead, 0.0, seg.rep_s)
            return _fmt(active), f"/ {_fmt(seg.rep_s)}", 0.0
        active = t - seg.lead
        since = active - rep * seg.rep_s if active >= 0 else 9.0
        flash = (1 - smooth(0.0, 0.45, since)) if rep > 0 and active < seg.reps * seg.rep_s else 0.0
        return str(rep + 1), f"/ {seg.reps}", flash

    def _cue_index(self, seg: Segment, t: float) -> tuple[int, float]:
        """(index into the segment's cue lines, seconds since that cue appeared)."""
        n = len(self.spec.cues[seg.cues[0]:seg.cues[1]])
        if n == 0:
            return 0, t
        if self.prog.breathing:
            phase_len = 1.0 if self.quick else 4.0
            active = t - seg.lead
            phases = 4 * (seg.reps or 1)
            if active < 0:
                return 0, t
            k = int(active // phase_len)
            if k >= phases - 1:
                return min(5, n - 1), active - (phases - 1) * phase_len
            return min(1 + k % 4, n - 1), active - k * phase_len
        if seg.cue_fn is not None:
            idx, since = seg.cue_fn(t)
            return int(clamp(idx, 0, n - 1)), since
        span = seg.duration / n
        idx = min(n - 1, int(t // span))
        return idx, t - idx * span

    def cue_pill(self, img: Image.Image, seg: Segment, t: float) -> None:
        """Bottom cue pill: the text cross-fades and the pill width eases between cue lines."""
        cues = self.spec.cues[seg.cues[0]:seg.cues[1]]
        if not cues:
            return
        x_fade = 0.42
        idx, since = self._cue_index(seg, t)
        prev = None
        if since < x_fade and t - since - 0.02 >= 0:
            pidx, _ = self._cue_index(seg, t - since - 0.02)
            if pidx != idx:
                prev = cues[pidx]
        cur = cues[idx]
        fc = font("medium", 19)
        d = ImageDraw.Draw(img, "RGBA")
        wc = d.textlength(cur, font=fc)
        if prev is not None:
            m = since / x_fade
            tw = lerp(d.textlength(prev, font=fc), wc, ease_io(m))
            a_prev, a_cur = 1 - smooth(0.0, 0.5, m), smooth(0.45, 1.0, m)
        else:
            tw, a_prev, a_cur = wc, 0.0, 1.0
        w = int(tw) + 54
        bx0 = int(W / 2 - w / 2)
        glass(img, (bx0, 440, bx0 + w, 484), 22)
        d = ImageDraw.Draw(img, "RGBA")
        d.ellipse((bx0 + 19, 459, bx0 + 25, 465), fill=_u8(self.look.accent, 255))
        white = (255, 255, 255)
        if prev is not None and a_prev > 0.01:
            text(d, (bx0 + 33, 462), prev, fc, white + (int(250 * a_prev),), anchor="lm")
        if a_cur > 0.01:
            text(d, (bx0 + 33, 462), cur, fc, white + (int(250 * a_cur),), anchor="lm")

    def breathing_overlay(self, img: Image.Image, with_label: bool = True) -> None:
        """Count (1-4, each number fading in on its second) inside the square + the phase name below it."""
        ph_i, ph_u, active = getattr(self, "_breath", (0, 0.0, -1.0))
        d = ImageDraw.Draw(img, "RGBA")
        white = (255, 255, 255)
        if active < 0:
            label, num, alpha = "Get comfortable", "", 0.0
        else:
            label = ("Breathe in", "Hold", "Breathe out", "Hold")[ph_i]
            num = str(1 + min(3, int(ph_u * 4)))
            alpha = 0.6 + 0.4 * smooth(0.0, 0.3, (ph_u * 4) % 1.0)
        if num:
            text(d, (W / 2, 262), num, font("semibold", 64), white + (int(255 * alpha),), anchor="mm")
        if with_label:
            text(d, (W / 2, 262 + 214 / 2 + 44), label, font("bold", 30), white + (245,), anchor="mm", shadow=60)

    # ------------------------------------------------------------------ cards
    def _blurred(self, frame: np.ndarray, dim: float) -> Image.Image:
        im = Image.fromarray(to_u8(frame), "RGB").filter(ImageFilter.GaussianBlur(16))
        arr = np.asarray(im, np.float32) / 255.0 * dim
        return Image.fromarray(to_u8(arr), "RGB")

    def intro_card(self, t: float) -> Image.Image:
        if self._intro_bg is None:
            seg = self.prog.segments[0]
            self._intro_bg = self._blurred(self.scene(seg, 0.0), 0.42)
        img = self._intro_bg.copy()
        d = ImageDraw.Draw(img, "RGBA")
        white = (255, 255, 255)
        look, spec = self.look, self.spec
        k = 0.55 if self.quick else 1.0

        def appear(start: float) -> tuple[float, float]:
            u = ease_out(clamp((t - start * k) / (0.55 * k), 0, 1))
            return u, (1 - u) * 14

        u, dy = appear(0.0)
        img.paste(logo_mark(26, look.accent, look.accent2), (M, 28), _scaled(logo_mark(26, look.accent, look.accent2).getchannel("A"), u))
        text(d, (M + 36, 41), "IronPulse", font("semibold", 17), white + (int(240 * u),), anchor="lm")
        u, dy = appear(0.12)
        eyebrow = f"{look.goal_label.upper()} · {spec.level.upper()}"
        fe = font("semibold", 12)
        ew = int(spaced_width(d, eyebrow, fe, 1.8)) + 40
        ex0 = int(W / 2 - ew / 2)
        glass(img, (ex0, int(184 + dy), ex0 + ew, int(212 + dy)), 14, alpha=u)
        d = ImageDraw.Draw(img, "RGBA")
        d.ellipse((ex0 + 14, 195 + dy, ex0 + 20, 201 + dy), fill=_u8(look.accent, int(255 * u)))
        spaced(d, (ex0 + 27, 198 + dy), eyebrow, fe, white + (int(220 * u),), 1.8)
        u, dy = appear(0.24)
        text(d, (W / 2, 252 + dy), spec.title, font("bold", 46), white + (int(255 * u),), anchor="mm", shadow=int(80 * u))
        u, dy = appear(0.36)
        text(d, (W / 2, 302 + dy), spec.sets_reps, font("medium", 19), white + (int(190 * u),), anchor="mm")
        u, _ = appear(0.48)
        lw = int(140 * u)
        if lw > 2:
            img.paste(progress_gradient(140, look.accent, look.accent2).crop((0, 0, lw, 3)), (int(W / 2 - 70), 334),
                      rr_mask(lw, 3, 1))
        text(d, (W / 2, 362), "AI-generated demonstration · follow along at your own pace", font("regular", 13),
             white + (int(140 * u),), anchor="mm")
        return img

    def outro_card(self, t: float) -> Image.Image:
        if self._outro_bg is None:
            last = self.prog.segments[-1]
            self._outro_bg = self._blurred(self.scene(last, last.duration), 0.38)
        img = self._outro_bg.copy()
        d = ImageDraw.Draw(img, "RGBA")
        white = (255, 255, 255)
        look, spec = self.look, self.spec
        k = 0.55 if self.quick else 1.0
        u = ease_out(clamp((t - 0.05) / (0.5 * k), 0, 1))
        cx, cy, r = W / 2, 176, 30
        ring = Image.new("L", (r * 8, r * 8), 0)
        rd = ImageDraw.Draw(ring)
        rd.ellipse((4, 4, r * 8 - 5, r * 8 - 5), outline=255, width=14)
        rd.line([(r * 8 * 0.30, r * 8 * 0.52), (r * 8 * 0.44, r * 8 * 0.66), (r * 8 * 0.71, r * 8 * 0.36)], fill=255,
                width=16, joint="curve")
        ring = ring.reduce(4)
        grad = progress_gradient(2 * r, look.accent, look.accent2).resize((2 * r, 2 * r))
        img.paste(grad, (int(cx - r), int(cy - r)), _scaled(ring, u))
        u2 = ease_out(clamp((t - 0.15 * k) / (0.5 * k), 0, 1))
        dy = (1 - u2) * 12
        text(d, (cx, 244 + dy), "Session complete", font("bold", 40), white + (int(255 * u2),), anchor="mm", shadow=int(70 * u2))
        u3 = ease_out(clamp((t - 0.3 * k) / (0.5 * k), 0, 1))
        text(d, (cx, 290), spec.sets_reps, font("medium", 18), white + (int(195 * u3),), anchor="mm")
        text(d, (cx, 326), "Stop if you feel pain, dizziness or chest discomfort.", font("regular", 14),
             white + (int(150 * u3),), anchor="mm")
        f = font("medium", 12)
        note = f"{WATERMARK} · not medical advice"
        tw = d.textlength(note, font=f)
        text(d, (cx + 9, 486), note, f, white + (int(140 * u3),), anchor="mm")
        img.paste((255, 255, 255), (int(cx - tw / 2 - 9), 480), _scaled(sparkle(12).getchannel("A"), 0.6 * u3))
        return img

    # ------------------------------------------------------------------ frames
    def live_frame(self, tau: float, now: float) -> Image.Image:
        segs, starts = self.prog.segments, self.tl.starts
        idx = 0
        for i, s0 in enumerate(starts):
            if tau >= s0:
                idx = i
        seg = segs[idx]
        local = min(tau - starts[idx], seg.duration)
        img = self._live_one(idx, local, now)
        if idx > 0 and tau - starts[idx] < SEG_FADE:  # cross-fade from the previous segment
            prev = segs[idx - 1]
            a = ease_sine((tau - starts[idx]) / SEG_FADE)
            other = self._live_one(idx - 1, prev.duration, now)
            img = Image.blend(other, img, a)
        return img

    def _live_one(self, idx: int, local: float, now: float) -> Image.Image:
        seg = self.prog.segments[idx]
        img = Image.fromarray(to_u8(self.scene(seg, local)), "RGB")
        if self.prog.breathing:
            self.breathing_overlay(img)
        self.hud(img, idx, local, now)
        return img

    def frame(self, now: float) -> Image.Image:
        tl = self.tl
        if now < self.intro_len - self.intro_fade:
            return self.intro_card(now)
        if now < self.intro_len:
            a = ease_sine((now - (self.intro_len - self.intro_fade)) / self.intro_fade)
            return Image.blend(self.intro_card(now), self.live_frame(now - tl.live_start, now), a)
        if now < tl.live_end:
            return self.live_frame(now - tl.live_start, now)
        if now < tl.live_end + self.outro_fade:
            a = ease_sine((now - tl.live_end) / self.outro_fade)
            return Image.blend(self.live_frame(tl.live_end - tl.live_start, now), self.outro_card(now - tl.live_end), a)
        img = self.outro_card(now - tl.live_end)
        fade = smooth(tl.total - 0.4, tl.total, now)
        if fade > 0:
            img = Image.blend(img, Image.new("RGB", img.size, (0, 0, 0)), fade)
        return img

    def poster(self) -> Image.Image:
        seg_i, t = self.prog.hero
        seg = self.prog.segments[seg_i]
        img = Image.fromarray(to_u8(self.scene(seg, t)), "RGB")
        if self.prog.breathing:
            self.breathing_overlay(img, with_label=False)
        # bottom scrim + title
        scrim = np.linspace(0, 1, 220, dtype=np.float32) ** 1.6
        arr = np.asarray(img, np.float32)
        arr[H - 220:] *= (1 - 0.72 * scrim)[:, None, None]
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
        d = ImageDraw.Draw(img, "RGBA")
        white = (255, 255, 255)
        look, spec = self.look, self.spec
        img.paste(logo_mark(30, look.accent, look.accent2), (M, 30), logo_mark(30, look.accent, look.accent2))
        text(d, (M + 40, 45), "IronPulse", font("semibold", 19), white + (240,), anchor="lm")
        tag = "AI-generated demo"
        ft = font("semibold", 13)
        tw = int(d.textlength(tag, font=ft)) + 44
        glass(img, (W - M - tw, 30, W - M, 60), 15)
        d = ImageDraw.Draw(img, "RGBA")
        img.paste((255, 255, 255), (W - M - tw + 14, 39), _scaled(sparkle(12).getchannel("A"), 0.85))
        text(d, (W - M - tw + 32, 45), tag, ft, white + (230,), anchor="lm")
        eyebrow = f"{look.goal_label.upper()} · {spec.level.upper()}"
        d.ellipse((M, 436, M + 9, 445), fill=_u8(look.accent, 255))
        spaced(d, (M + 17, 440.5), eyebrow, font("semibold", 13), white + (200,), 1.8)
        text(d, (M - 2, 486), spec.title, font("bold", 44), white + (255,), anchor="lm", shadow=90)
        return img


def _onto_rounded_square(px: float, py: float, half: float, r: float) -> tuple[float, float]:
    """Project a point of the square's straight outline onto the rounded corners (keeps the dot on the stroke)."""
    inner = half - r
    qx, qy = abs(px) - inner, abs(py) - inner
    if qx > 0 and qy > 0:
        n = math.hypot(qx, qy)
        cx, cy = math.copysign(inner, px), math.copysign(inner, py)
        return cx + (px - cx) * r / n, cy + (py - cy) * r / n
    return px, py


def to_u8(frame: np.ndarray) -> np.ndarray:
    return np.clip(frame * 255.0 + 0.5, 0, 255).astype(np.uint8)


# ===================================================================================== output
def render_video(slug: str, out_dir: str, quick: bool = False, stills: tuple[float, ...] = ()) -> dict[str, Any]:
    """Render one video (or PNG stills) into ``out_dir``. Returns its manifest entry."""
    import imageio_ffmpeg

    spec = VIDEO_BY_SLUG[slug]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    r = Renderer(spec, quick=quick)
    if stills:
        files = []
        for s in stills:
            path = out / f"{slug}-{s:05.2f}s.png"
            r.frame(min(s, r.tl.total - 0.01)).save(path)
            files.append(path.name)
        poster = out / f"{slug}-poster.png"
        r.poster().save(poster)
        return {"slug": slug, "stills": files + [poster.name], "seconds": round(time.perf_counter() - started, 2)}
    fps = QUICK_FPS if quick else FPS
    n_frames = max(1, int(round(r.tl.total * fps)))
    mp4, jpg = out / f"{slug}.mp4", out / f"{slug}.jpg"
    tmp = out / f".{slug}.rendering.mp4"
    writer = imageio_ffmpeg.write_frames(
        str(tmp), (W, H), pix_fmt_in="rgb24", pix_fmt_out="yuv420p", fps=fps, codec="libx264", quality=None,
        macro_block_size=1, ffmpeg_log_level="error",
        output_params=["-crf", str(CRF), "-preset", "medium", "-g", str(fps * 2), "-movflags", "+faststart",
                       "-vf", "scale=out_color_matrix=bt709:out_range=tv", "-colorspace", "bt709",
                       "-color_primaries", "bt709", "-color_trc", "bt709", "-map_metadata", "-1", "-fflags", "+bitexact"],
    )
    writer.send(None)
    try:
        for i in range(n_frames):
            writer.send(np.ascontiguousarray(np.asarray(r.frame(i / fps), dtype=np.uint8)))
        writer.close()
    except BaseException:
        writer.close()
        tmp.unlink(missing_ok=True)
        raise
    _replace(tmp, mp4)
    tmp_jpg = out / f".{slug}.rendering.jpg"
    r.poster().save(tmp_jpg, "JPEG", quality=88, optimize=True, progressive=True)
    _replace(tmp_jpg, jpg)
    return {
        "slug": slug, "title": spec.title, "goal": spec.goal, "level": spec.level,
        "duration": round(n_frames / fps, 2), "frames": n_frames, "fps": fps, "width": W, "height": H,
        "file": mp4.name, "poster": jpg.name, "size_bytes": mp4.stat().st_size, "quick": quick,
        "render_seconds": round(time.perf_counter() - started, 2),
    }


def _replace(src: Path, dst: Path, attempts: int = 20) -> None:
    """Atomic rename; retries briefly on Windows, where a file being streamed by the server is locked."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.25)


def write_manifest(out_dir: Path, entries: list[dict[str, Any]]) -> Path:
    path = out_dir / MANIFEST
    existing: dict[str, dict[str, Any]] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for item in raw.get("videos", []) if isinstance(raw, dict) else []:
            if isinstance(item, dict) and isinstance(item.get("slug"), str):
                existing[item["slug"]] = item
    except (OSError, ValueError):
        pass
    for e in entries:
        existing[e["slug"]] = {k: v for k, v in e.items() if k != "render_seconds"}
    order = {v.slug: i for i, v in enumerate(VIDEOS)}
    videos = sorted(existing.values(), key=lambda v: (order.get(v["slug"], 999), v["slug"]))
    payload = {"version": 1, "generator": "scripts/generate_videos.py", "videos": videos}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _replace(tmp, path)
    return path


def _parse_only(values: list[str] | None) -> list[str]:
    slugs: list[str] = []
    for v in values or []:
        slugs += [s.strip() for s in v.split(",") if s.strip()]
    return slugs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render IronPulse's AI-generated exercise demo videos.")
    ap.add_argument("--only", action="append", metavar="SLUG", help="render only this video (repeatable or comma list)")
    ap.add_argument("--quick", action="store_true", help=f"fast preview: {QUICK_FPS} fps, two reps per exercise")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output folder (default: {DEFAULT_OUT.relative_to(ROOT)})")
    ap.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1), help="videos rendered in parallel")
    ap.add_argument("--list", action="store_true", help="list the catalogue and planned durations, then exit")
    ap.add_argument("--still", type=float, action="append", metavar="SECONDS", help="write PNG stills instead of video")
    args = ap.parse_args(argv)

    if args.list:
        for v in VIDEOS:
            print(f"{v.slug:24} {v.goal:16} {planned_duration(v):5.1f} s  {v.title}")
        return 0
    slugs = _parse_only(args.only) or [v.slug for v in VIDEOS]
    unknown = [s for s in slugs if s not in VIDEO_BY_SLUG]
    if unknown:
        print(f"unknown video slug(s): {', '.join(unknown)}  (see --list)", file=sys.stderr)
        return 2
    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    stills = tuple(args.still or ())
    if args.quick and not stills and out_dir == DEFAULT_OUT.resolve():
        print("[videos] note: --quick previews are replacing the real videos in the default folder", flush=True)
    jobs = max(1, min(args.jobs, len(slugs)))
    mode = "stills" if stills else ("quick preview" if args.quick else f"{W}x{H} @ {FPS} fps")
    print(f"[videos] rendering {len(slugs)} video(s) ({mode}) with {jobs} worker(s) -> {out_dir}", flush=True)
    started = time.perf_counter()
    results: list[dict[str, Any]] = []
    failures = 0

    def report(i: int, res: dict[str, Any]) -> None:
        if stills:
            print(f"[{i:2}/{len(slugs)}] {res['slug']:24} stills: {', '.join(res['stills'])}", flush=True)
        else:
            print(f"[{i:2}/{len(slugs)}] {res['slug']:24} {res['duration']:5.1f} s  {res['size_bytes'] / 1e6:5.2f} MB  "
                  f"rendered in {res['render_seconds']:.1f} s", flush=True)

    if jobs == 1:
        for i, slug in enumerate(slugs, 1):
            try:
                res = render_video(slug, str(out_dir), args.quick, stills)
                results.append(res)
                report(i, res)
            except Exception as exc:  # noqa: BLE001 - report and continue with the other videos
                failures += 1
                print(f"[{i:2}/{len(slugs)}] {slug:24} FAILED: {exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
    else:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(render_video, slug, str(out_dir), args.quick, stills): slug for slug in slugs}
            for i, fut in enumerate(as_completed(futures), 1):
                slug = futures[fut]
                try:
                    res = fut.result()
                    results.append(res)
                    report(i, res)
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    print(f"[{i:2}/{len(slugs)}] {slug:24} FAILED: {exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
    if results and not stills:
        path = write_manifest(out_dir, results)
        total = sum(r["size_bytes"] for r in results)
        print(f"[videos] {len(results)} video(s), {total / 1e6:.1f} MB, manifest: {path}", flush=True)
    print(f"[videos] done in {time.perf_counter() - started:.1f} s" + (f" - {failures} failed" if failures else ""), flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
