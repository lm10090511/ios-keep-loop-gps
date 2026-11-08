# -*- coding: utf-8 -*-
"""预设形状路线。

给一个中心点（GCJ-02）和尺寸，生成一圈闭合坐标，交给界面直接拖动放置。
不用用户自己点，是因为大部分人只想「在操场上跑一圈」，
自己点十几个点很难点圆。

几何都在「以中心为原点的平面」上按米算，再换算成经纬度。
半径最多一两公里，用等距圆柱近似（111320 m/度）误差在厘米级，
和 coord.py 里的换算口径一致。
"""
import math

M_PER_DEG = 111320.0


# --------------------------------------------------------------------------- #
# 形状目录（前端据此渲染按钮和滑杆）
# --------------------------------------------------------------------------- #
SHAPES = [
    {
        "id": "track",
        "name": "跑道",
        "desc": "标准 400 m 田径场：两条直道 + 两个半圆",
        "size": 400, "min": 200, "max": 1600,
        "ratio": 0.0,
    },
    {
        "id": "oval",
        "name": "椭圆",
        "desc": "椭圆形闭合圈，长宽比可调",
        "size": 1000, "min": 200, "max": 5000,
        "ratio": 1.8,
    },
    {
        "id": "circle",
        "name": "圆形",
        "desc": "正圆，适合小范围绕圈",
        "size": 800, "min": 100, "max": 5000,
        "ratio": 0.0,
    },
    {
        "id": "rect",
        "name": "矩形",
        "desc": "长方形街区绕圈，长宽比可调",
        "size": 1200, "min": 200, "max": 8000,
        "ratio": 1.6,
    },
    {
        "id": "eight",
        "name": "8 字",
        "desc": "两个相切的圆，用来测折返与重叠路段",
        "size": 1000, "min": 300, "max": 5000,
        "ratio": 0.0,
    },
    {
        "id": "line",
        "name": "往返",
        "desc": "极细的往返直线，模拟折返跑",
        "size": 800, "min": 100, "max": 5000,
        "ratio": 0.0,
    },
]

SHAPE_MAP = {s["id"]: s for s in SHAPES}


# --------------------------------------------------------------------------- #
# 平面几何：全部返回 [(x, y), ...]，单位米，x 向东、y 向北
# --------------------------------------------------------------------------- #
def _stadium(L, r, n=180):
    """体育场形：两条直道（长 L）+ 两个半径 r 的半圆。"""
    pts = []
    half = L / 2.0
    nb = max(8, n // 4)          # 每条直道
    na = max(12, n // 4)         # 每个半圆

    # 下直道：从 (half,-r) 到 (-half,-r)
    for i in range(nb):
        x = half - L * i / float(nb)
        pts.append((x, -r))
    # 左半圆：角度 -90° → -270°
    for i in range(na):
        th = math.radians(-90.0 - 180.0 * i / float(na))
        pts.append((-half + r * math.cos(th), r * math.sin(th)))
    # 上直道：从 (-half,r) 到 (half,r)
    for i in range(nb):
        x = -half + L * i / float(nb)
        pts.append((x, r))
    # 右半圆：角度 90° → -90°
    for i in range(na):
        th = math.radians(90.0 - 180.0 * i / float(na))
        pts.append((half + r * math.cos(th), r * math.sin(th)))
    return pts


def _ellipse(a, b, n=160):
    return [(a * math.cos(2 * math.pi * i / n),
             b * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _ellipse_perimeter(a, b):
    """Ramanujan 近似，椭圆周长。"""
    h = ((a - b) ** 2) / ((a + b) ** 2)
    return math.pi * (a + b) * (1 + 3 * h / (10 + math.sqrt(4 - 3 * h)))


def _solve_ellipse_b(perimeter, ratio):
    """给定周长和长短轴比，二分求短半轴 b。"""
    lo, hi = 1.0, perimeter / (2 * math.pi) * 2.0
    for _ in range(80):
        mid = (lo + hi) / 2.0
        if _ellipse_perimeter(ratio * mid, mid) < perimeter:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


# --------------------------------------------------------------------------- #
# 各形状 → 平面点
# --------------------------------------------------------------------------- #
def _shape_points(shape, size, ratio):
    size = max(20.0, float(size))

    if shape == "track":
        # 标准 400 m 跑道（第 1 道跑进线）：直道 84.39 m + 半径 36.8 m 的弯道
        s = size / 400.0
        return _stadium(84.39 * s, 36.8 * s, 180)

    if shape == "oval":
        ratio = ratio if ratio and ratio > 0 else 1.8
        b = _solve_ellipse_b(size, ratio)
        return _ellipse(ratio * b, b, 160)

    if shape == "circle":
        return _ellipse(size / (2 * math.pi), size / (2 * math.pi), 120)

    if shape == "rect":
        ratio = ratio if ratio and ratio > 0 else 1.6
        h = size / (2.0 * (1.0 + ratio))
        w = ratio * h
        n = 40
        pts = []
        for i in range(n):                       # 下边
            pts.append((-w / 2 + w * i / n, -h / 2))
        for i in range(n):                       # 右边
            pts.append((w / 2, -h / 2 + h * i / n))
        for i in range(n):                       # 上边
            pts.append((w / 2 - w * i / n, h / 2))
        for i in range(n):                       # 左边
            pts.append((-w / 2, h / 2 - h * i / n))
        return pts

    if shape == "eight":
        r = size / (4 * math.pi)
        n = 90
        pts = []
        for i in range(n):                       # 左圈，从切点开始绕一圈
            th = 2 * math.pi * i / n
            pts.append((-r + r * math.cos(th), r * math.sin(th)))
        for i in range(n):                       # 右圈，从切点反向绕
            th = math.pi - 2 * math.pi * i / n
            pts.append((r + r * math.cos(th), r * math.sin(th)))
        return pts

    if shape == "line":
        # 极窄的体育场形：周长 = 2L + 2πr，解出直道长度
        r = 1.5
        L = max(2.0, (size - 2 * math.pi * r) / 2.0)
        return _stadium(L, r, 60)

    raise ValueError("不认识的形状：%s" % shape)


# --------------------------------------------------------------------------- #
# 平面 → 经纬度
# --------------------------------------------------------------------------- #
def make_points(shape, lat, lon, size=None, ratio=None, angle=0.0):
    """生成一圈闭合的 GCJ-02 坐标。

    shape  : SHAPES 里的 id
    lat/lon: 中心点（GCJ-02，和高德瓦片一致）
    size   : 周长（米），不传就用该形状的默认值
    ratio  : 长宽比，只对 oval / rect 有效
    angle  : 整体旋转角度（度，逆时针）
    """
    meta = SHAPE_MAP.get(shape)
    if not meta:
        raise ValueError("不认识的形状：%s" % shape)
    if size is None:
        size = meta["size"]
    if ratio is None:
        ratio = meta["ratio"] or 1.6

    size = float(size)
    if size < meta["min"]:
        size = float(meta["min"])
    if size > meta["max"]:
        size = float(meta["max"])

    raw = _shape_points(shape, size, ratio)

    a = math.radians(float(angle or 0.0))
    ca, sa = math.cos(a), math.sin(a)
    k = M_PER_DEG * math.cos(math.radians(lat))

    out = []
    for x, y in raw:
        xr = x * ca - y * sa
        yr = x * sa + y * ca
        out.append((lat + yr / M_PER_DEG, lon + xr / k))
    return out


def label(shape, size):
    meta = SHAPE_MAP.get(shape)
    if not meta:
        return "预设路线"
    if size and abs(float(size) - meta["size"]) > 1:
        return "%s %d m" % (meta["name"], int(round(float(size))))
    return meta["name"]
