# -*- coding: utf-8 -*-
"""循环虚拟定位 · 图形界面

地图上直接看路线、拖动改位置、滑杆改配速，点按钮开始/暂停/停止。

依赖：Python 3 + tkinter（标准库）。有 Pillow 时支持卫星图，没有就只用路网图。
"""
import json
import math
import os
import queue
import sys
import threading
import time
import urllib.request

import tkinter as tk
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from PIL import Image, ImageTk
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

import coord                     # GCJ-02 <-> WGS-84
import ios_loop_gps as M         # usbmux / lockdown / PointStream
import presets                   # 预设形状（跑道/椭圆/圆形/矩形/8字/往返）


# --------------------------------------------------------------------------- #
# 日志：pythonw 下没有 stdout，print 会炸，统一写到文件
# --------------------------------------------------------------------------- #
class _Tee:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, s):
        if not s:
            return
        with self.lock:
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(s)
            except Exception:
                pass

    def flush(self):
        pass


LOG_PATH = os.path.join(HERE, "gui.log")
try:
    open(LOG_PATH, "w", encoding="utf-8").close()
except Exception:
    pass
if sys.stdout is None or not hasattr(sys.stdout, "write"):
    sys.stdout = _Tee(LOG_PATH)
if sys.stderr is None or not hasattr(sys.stderr, "write"):
    sys.stderr = _Tee(LOG_PATH)


# --------------------------------------------------------------------------- #
# Web Mercator
# --------------------------------------------------------------------------- #
def ll2px(lat, lon, z):
    n = 256.0 * (2.0 ** z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(max(-85.05, min(85.05, lat))))
    y = (0.5 - math.log((1.0 + s) / (1.0 - s)) / (4.0 * math.pi)) * n
    return x, y


def px2ll(x, y, z):
    n = 256.0 * (2.0 ** z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lat, lon


def fmt_pace(sec_per_km):
    m = int(sec_per_km // 60)
    s = int(round(sec_per_km - m * 60))
    if s >= 60:
        m += 1
        s -= 60
    return "%d'%02d\"" % (m, s)


def haversine(a, b):
    return M.haversine(a[0], a[1], b[0], b[1])


# --------------------------------------------------------------------------- #
# 瓦片缓存
# --------------------------------------------------------------------------- #
class TileCache:
    """高德瓦片：style=6 卫星(JPEG)，style=8 路网(PNG)。"""

    HOSTS_SAT = ["webst01", "webst02", "webst03", "webst04"]
    HOSTS_ROAD = ["webrd01", "webrd02", "webrd03", "webrd04"]

    def __init__(self, root_dir, style):
        self.dir = root_dir
        self.style = style
        self.mem = {}
        self.pending = set()
        self.lock = threading.Lock()
        self.errors = 0
        # 后台线程下完一片就往这里塞一个信号，主线程轮询后重绘
        # （tkinter 不是线程安全的，后台线程里不能直接碰 canvas）
        self.ready = queue.Queue()

    def set_style(self, style):
        self.style = style

    def _path(self, z, x, y):
        ext = "jpg" if self.style == 6 else "png"
        return os.path.join(self.dir, str(self.style), str(z), str(x), "%d.%s" % (y, ext))

    def _url(self, z, x, y):
        if self.style == 6:
            host = self.HOSTS_SAT[(x + y) % 4]
            return ("https://%s.is.autonavi.com/appmaptile?style=6&x=%d&y=%d&z=%d"
                    % (host, x, y, z))
        host = self.HOSTS_ROAD[(x + y) % 4]
        return ("https://%s.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8"
                "&x=%d&y=%d&z=%d" % (host, x, y, z))

    def _download(self, z, x, y):
        p = self._path(z, x, y)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            req = urllib.request.Request(self._url(z, x, y),
                                         headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=12).read()
            if len(data) < 200:
                raise ValueError("too small")
            with open(p, "wb") as f:
                f.write(data)
        except Exception:
            self.errors += 1
            with self.lock:
                self.pending.discard((z, x, y))
            return None
        with self.lock:
            self.pending.discard((z, x, y))
        return p

    def _load(self, path):
        try:
            if path.lower().endswith(".jpg"):
                if not HAVE_PIL:
                    return None
                img = Image.open(path).convert("RGB")
                return ImageTk.PhotoImage(img)
            return tk.PhotoImage(file=path)
        except Exception:
            return None

    def get(self, z, x, y, on_ready):
        """返回 PhotoImage；没有就后台下载，完成后把重绘请求投进 ready 队列。"""
        key = (z, x, y)
        if key in self.mem:
            return self.mem[key]
        p = self._path(z, x, y)
        if os.path.exists(p):
            img = self._load(p)
            if img is not None:
                self.mem[key] = img
                return img
        with self.lock:
            if key in self.pending:
                return None
            self.pending.add(key)
        threading.Thread(target=self._bg, args=(z, x, y), daemon=True).start()
        return None

    def _bg(self, z, x, y):
        p = self._download(z, x, y)
        if not p:
            return
        # 提前把图片读进 mem（PIL 的 Image.open 可以在后台线程做，PhotoImage 不行），
        # 真正的 ImageTk.PhotoImage 留到主线程 _load_ready() 里做
        try:
            self.ready.put((z, x, y, p))
        except Exception:
            pass

    def _load_ready(self, z, x, y, p):
        """必须在主线程调用：把下好的瓦片变成 PhotoImage 并缓存。"""
        key = (z, x, y)
        if key in self.mem:
            return
        img = self._load(p)
        if img is not None:
            self.mem[key] = img


# --------------------------------------------------------------------------- #
# 地图控件
# --------------------------------------------------------------------------- #
class MapView(tk.Canvas):
    MIN_Z, MAX_Z = 14, 19

    def __init__(self, master, cache, on_click=None, on_drag_end=None,
                 on_draw_done=None, **kw):
        super().__init__(master, **kw)
        self.cache = cache
        self.center = (39.908722, 116.397499)   # 示例中心（天安门），加载路线后会自动适配
        self.z = 16
        self.route_gcj = []          # [(lat, lon)] 显示用
        self.marker = None           # (lat, lon) 当前推的点
        self.track = []              # 走过的点
        self.mode = "view"           # view | move | rotate | scale | draw
        self.on_click = on_click
        self.on_drag_end = on_drag_end
        self.on_draw_done = on_draw_done
        self._drag = None
        self._moved = False
        self._move_base = None
        self._edit_base = None
        self._edit_center = None
        self._edit_angle = 0.0
        self._edit_scale = 1.0
        self._draw_pts = []
        self._a0 = 0.0
        self._d0 = 1.0
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._motion)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-Button-1>", self._dblclick)
        self.bind("<Button-3>", self._right)
        self.bind("<MouseWheel>", self._wheel)

    # -- 坐标换算 ---------------------------------------------------------- #
    def to_screen(self, lat, lon):
        px, py = ll2px(lat, lon, self.z)
        cx, cy = ll2px(self.center[0], self.center[1], self.z)
        w = self.winfo_width() or 1
        h = self.winfo_height() or 1
        return px - cx + w / 2.0, py - cy + h / 2.0

    def to_latlon(self, sx, sy):
        cx, cy = ll2px(self.center[0], self.center[1], self.z)
        w = self.winfo_width() or 1
        h = self.winfo_height() or 1
        return px2ll(cx + sx - w / 2.0, cy + sy - h / 2.0, self.z)

    # -- 绘制 -------------------------------------------------------------- #
    def redraw(self):
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 10 or h < 10:
            return
        self.delete("all")
        cx, cy = ll2px(self.center[0], self.center[1], self.z)
        ox, oy = cx - w / 2.0, cy - h / 2.0
        x0 = int(math.floor(ox / 256.0))
        y0 = int(math.floor(oy / 256.0))
        x1 = int(math.floor((ox + w) / 256.0))
        y1 = int(math.floor((oy + h) / 256.0))
        nmax = 2 ** self.z
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                if ty < 0 or ty >= nmax:
                    continue
                img = self.cache.get(self.z, tx, ty, None)
                if img is not None:
                    self.create_image(tx * 256 - ox, ty * 256 - oy,
                                      image=img, anchor="nw")
        self._draw_route()
        self._draw_track()
        self._draw_marker()
        self._draw_scalebar()

    def _draw_route(self):
        pts = self.route_gcj
        if len(pts) >= 2:
            xy = []
            for lat, lon in pts:
                xy.extend(self.to_screen(lat, lon))
            xy.extend(self.to_screen(pts[0][0], pts[0][1]))
            self.create_line(*xy, fill="#19c37d", width=4, smooth=False,
                             joinstyle="round", tags="route")
        if len(pts) >= 2:
            sx, sy = self.to_screen(pts[0][0], pts[0][1])
            self.create_oval(sx - 6, sy - 6, sx + 6, sy + 6, outline="#0b8f57",
                             width=2, tags="route")

    def _draw_track(self):
        if len(self.track) >= 2:
            xy = []
            for lat, lon in self.track[-1200:]:
                xy.extend(self.to_screen(lat, lon))
            self.create_line(*xy, fill="#ff375f", width=3, tags="track")

    def _draw_marker(self):
        if not self.marker:
            return
        sx, sy = self.to_screen(self.marker[0], self.marker[1])
        self.create_oval(sx - 11, sy - 11, sx + 11, sy + 11, outline="#ff375f",
                         width=3, tags="marker")
        self.create_oval(sx - 5, sy - 5, sx + 5, sy + 5, fill="#ff375f",
                         outline="white", width=2, tags="marker")

    def _draw_scalebar(self):
        h = self.winfo_height()
        mpp = 156543.03392 * math.cos(math.radians(self.center[0])) / (2 ** self.z)
        target = 100.0
        for cand in (10, 20, 50, 100, 200, 500, 1000):
            if cand / mpp <= 160:
                target = cand
        px = target / mpp
        x0, y0 = 16, h - 26
        self.create_line(x0, y0, x0 + px, y0, fill="#111", width=3)
        self.create_line(x0, y0 - 4, x0, y0 + 4, fill="#111", width=2)
        self.create_line(x0 + px, y0 - 4, x0 + px, y0 + 4, fill="#111", width=2)
        txt = "%d m" % target if target < 1000 else "%.0f km" % (target / 1000.0)
        self.create_text(x0 + px / 2.0, y0 - 12, text=txt, fill="#111",
                         font=("Microsoft YaHei", 9, "bold"))

    def update_marker(self, lat, lon):
        self.marker = (lat, lon)
        self.delete("marker")
        self._draw_marker()

    def add_track(self, lat, lon):
        self.track.append((lat, lon))
        if len(self.track) > 5000:
            self.track = self.track[-2500:]
        self.delete("track")
        self._draw_track()

    def clear_track(self):
        self.track = []
        self.delete("track")

    # -- 交互 -------------------------------------------------------------- #
    def reset_edit(self):
        """清掉旋转/缩放的基准与手画点。"""
        self._edit_base = None
        self._edit_center = None
        self._edit_angle = 0.0
        self._edit_scale = 1.0
        self._draw_pts = []
        self.delete("draft")
        self.configure(cursor="crosshair" if self.mode != "view" else "")

    def _begin_edit(self):
        if not self.route_gcj:
            return
        self._edit_base = list(self.route_gcj)
        lats = [p[0] for p in self._edit_base]
        lons = [p[1] for p in self._edit_base]
        self._edit_center = ((min(lats) + max(lats)) / 2.0,
                             (min(lons) + max(lons)) / 2.0)
        self._edit_angle = 0.0
        self._edit_scale = 1.0

    def _apply_edit(self):
        if not self._edit_base or not self._edit_center:
            return
        cl, cn = self._edit_center
        ky = 111320.0
        kx = ky * math.cos(math.radians(cl))
        ca, sa = math.cos(self._edit_angle), math.sin(self._edit_angle)
        out = []
        for la, lo in self._edit_base:
            dx = (lo - cn) * kx
            dy = (la - cl) * ky
            rx = dx * ca - dy * sa
            ry = dx * sa + dy * ca
            out.append((cl + ry * self._edit_scale / ky,
                        cn + rx * self._edit_scale / kx))
        self.route_gcj = out
        self.delete("route")
        self._draw_route()

    def _press(self, e):
        self._drag = (e.x, e.y)
        self._moved = False
        if self.mode == "move" and self.route_gcj:
            self._move_base = list(self.route_gcj)
        elif self.mode in ("rotate", "scale"):
            if not self._edit_base:
                self._begin_edit()
            if self._edit_center:
                cl, cn = self._edit_center
                mlat, mlon = self.to_latlon(e.x, e.y)
                ky = 111320.0
                kx = ky * math.cos(math.radians(cl))
                self._a0 = math.atan2((mlat - cl) * ky, (mlon - cn) * kx)
                self._d0 = max(1.0, math.hypot((mlon - cn) * kx,
                                               (mlat - cl) * ky))
        self.configure(cursor="fleur" if self.mode == "move" else "crosshair")

    def _motion(self, e):
        if self._drag is None:
            return
        if abs(e.x - self._drag[0]) > 3 or abs(e.y - self._drag[1]) > 3:
            self._moved = True
        if self.mode == "move" and self._move_base:
            dlat, dlon = self._delta_ll(self._drag[0], self._drag[1], e.x, e.y)
            self.route_gcj = [(la + dlat, lo + dlon) for la, lo in self._move_base]
            self.delete("route")
            self._draw_route()
            return
        if self.mode in ("rotate", "scale") and self._edit_base and self._edit_center:
            cl, cn = self._edit_center
            mlat, mlon = self.to_latlon(e.x, e.y)
            ky = 111320.0
            kx = ky * math.cos(math.radians(cl))
            if self.mode == "rotate":
                a1 = math.atan2((mlat - cl) * ky, (mlon - cn) * kx)
                self._edit_angle = a1 - self._a0
            else:
                d1 = math.hypot((mlon - cn) * kx, (mlat - cl) * ky)
                self._edit_scale = min(8.0, max(0.15, d1 / max(1e-6, self._d0)))
            self._apply_edit()
            return
        dx = e.x - self._drag[0]
        dy = e.y - self._drag[1]
        cx, cy = ll2px(self.center[0], self.center[1], self.z)
        self.center = px2ll(cx - dx, cy - dy, self.z)
        self._drag = (e.x, e.y)
        self.redraw()

    def _release(self, e):
        self._drag = None
        self.configure(cursor="crosshair" if self.mode != "view" else "")
        if self.mode == "move":
            if self._move_base:
                self._move_base = None
                if self.on_drag_end:
                    self.on_drag_end(list(self.route_gcj))
            return
        if self.mode in ("rotate", "scale"):
            if self._moved and self.on_drag_end:
                self.on_drag_end(list(self.route_gcj))
            self._edit_base = None
            self._begin_edit()
            return
        if self.mode == "draw" and not self._moved:
            lat, lon = self.to_latlon(e.x, e.y)
            self._draw_pts.append((lat, lon))
            self._draw_draft()

    def _draw_draft(self):
        self.delete("draft")
        if not self._draw_pts:
            return
        pts = [self.to_screen(la, lo) for la, lo in self._draw_pts]
        if len(pts) > 1:
            self.create_line(*[c for xy in pts for c in xy], fill="#ffc454",
                             width=2, dash=(6, 5), tags="draft")
        for x, y in pts:
            self.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#ffc454",
                             outline="#2a1c00", tags="draft")

    def _delta_ll(self, x0, y0, x1, y1):
        a = self.to_latlon(x0, y0)
        b = self.to_latlon(x1, y1)
        return b[0] - a[0], b[1] - a[1]

    def _dblclick(self, e):
        """画路线模式：双击结束并生成闭合路线。"""
        if self.mode != "draw":
            return
        if len(self._draw_pts) >= 3:
            pts = list(self._draw_pts)
            self._draw_pts = []
            self.delete("draft")
            if self.on_draw_done:
                self.on_draw_done(pts)
        else:
            self._draw_pts = []
            self.delete("draft")

    def _right(self, e):
        """画路线模式：右键撤销上一个点。"""
        if self.mode != "draw" or not self._draw_pts:
            return
        self._draw_pts.pop()
        self._draw_draft()

    def _click(self, e):
        pass

    def _wheel(self, e):
        dz = 1 if e.delta > 0 else -1
        self.zoom_at(e.x, e.y, dz)

    def zoom_at(self, sx, sy, dz):
        nz = max(self.MIN_Z, min(self.MAX_Z, self.z + dz))
        if nz == self.z:
            return
        lat, lon = self.to_latlon(sx, sy)
        self.z = nz
        px, py = ll2px(lat, lon, nz)
        w = self.winfo_width() or 1
        h = self.winfo_height() or 1
        self.center = px2ll(px + (w / 2.0 - sx), py + (h / 2.0 - sy), nz)
        self.redraw()

    def set_route(self, pts):
        self.route_gcj = list(pts)
        if pts:
            self.center = (sum(p[0] for p in pts) / len(pts),
                           sum(p[1] for p in pts) / len(pts))
            # 自动选一个能看全的缩放级别
            lats = [p[0] for p in pts]
            lons = [p[1] for p in pts]
            span = max(max(lats) - min(lats), max(lons) - min(lons))
            if span > 0:
                w = max(self.winfo_width(), 400)
                # 从最细的级别往下试，第一个"装得下"的就是最合适的
                self.z = 14
                for cand in (18, 17, 16, 15, 14):
                    mpp = 156543.03392 * math.cos(math.radians(self.center[0])) / (2 ** cand)
                    if span * 111000.0 / mpp < w * 0.75:
                        self.z = cand
                        break
        self.redraw()


# --------------------------------------------------------------------------- #
# 推点线程
# --------------------------------------------------------------------------- #
class Runner:
    def __init__(self, on_log, on_state):
        self.on_log = on_log
        self.on_state = on_state
        self.lock = threading.Lock()
        self.route_wgs = []
        self.speed = 2.8
        self.hz = 1.0
        self.loop_pts = []
        self.total = 0.0
        self.gen = 0
        self.lap = 0
        self.sent = 0
        self.dist = 0.0
        self.current = None
        self.running = False
        self.paused = False
        self.ps = None
        self.err = None
        self.thread = None

    def rebuild(self):
        with self.lock:
            if not self.route_wgs:
                self.loop_pts = []
                self.total = 0.0
                return
            step = max(0.4, self.speed / max(0.2, self.hz))
            pts, total = M.build_loop(self.route_wgs, step)
            self.loop_pts = pts
            self.total = total
            self.gen += 1

    def set_route(self, wgs_pts):
        with self.lock:
            self.route_wgs = list(wgs_pts)
        self.rebuild()

    def set_speed(self, speed):
        with self.lock:
            self.speed = float(speed)
        self.rebuild()

    def set_hz(self, hz):
        with self.lock:
            self.hz = float(hz)
        self.rebuild()

    def start(self):
        if self.running:
            return
        self.running = True
        self.paused = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def stop(self):
        self.running = False
        self.paused = False

    def _log(self, s):
        try:
            self.on_log(s)
        except Exception:
            pass

    def _state(self):
        try:
            self.on_state()
        except Exception:
            pass

    def _ensure_ps(self):
        if self.ps is None:
            self.ps = M.PointStream(udid=None)
        self.ps.ensure()

    def _run(self):
        self._log("连接手机 ...")
        try:
            for i in range(1, 31):
                try:
                    self._ensure_ps()
                    break
                except Exception as e:
                    if i == 1:
                        self._log("等待设备：%s" % e)
                    if i == 30:
                        raise
                    time.sleep(2.0)
        except Exception as e:
            self.err = str(e)
            self.running = False
            self._log("连接失败：%s" % e)
            self._state()
            return
        self._log("已连接，开始推送")
        self.err = None
        self._state()

        lost = 0
        while self.running:
            with self.lock:
                pts = list(self.loop_pts)
                gen = self.gen
                interval = 1.0 / max(0.2, self.hz)
            if not pts:
                time.sleep(0.3)
                continue
            t_next = time.monotonic()
            for lat, lon in pts:
                if not self.running:
                    break
                while self.paused and self.running:
                    time.sleep(0.15)
                if not self.running:
                    break
                with self.lock:
                    if self.gen != gen:
                        break
                try:
                    self._ensure_ps()
                    M.send_point(self.ps.sock, lat, lon)
                    lost = 0
                except Exception as e:
                    lost += 1
                    if lost == 1 or lost % 20 == 0:
                        self._log("推点失败 %d 次：%s" % (lost, e))
                    if lost % 10 == 0:
                        try:
                            self.ps.hard_reset()
                        except Exception:
                            pass
                        self._log("重连中 ...（USB 掉了插回来会自动接上）")
                    time.sleep(1.0)
                    continue
                self.current = (lat, lon)
                self.sent += 1
                if len(pts) > 1:
                    self.dist += self.total / len(pts)
                self._state()
                t_next += interval
                d = t_next - time.monotonic()
                if d > 0:
                    time.sleep(d)
                else:
                    t_next = time.monotonic()
            else:
                self.lap += 1
                self._log("完成第 %d 圈（累计 %.2f km）" % (self.lap, self.dist / 1000.0))
                continue
            # gen 变了 / 停止 —— 回外层重新取点
        if self.ps:
            try:
                self.ps.close()
            except Exception:
                pass
        self._log("已停止推送")
        self._state()


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
ROUTES_DIR = os.path.join(HERE, "routes")

# 示例路线（随仓库分发），显示名 → 文件名
ROUTES = [
    ("示例环线 · 天安门（1.2 km）", "example-tiananmen.json"),
    ("示例跑道 · 标准 400 m", "example-track-400m.json"),
]


class App:
    def __init__(self, root):
        self.root = root
        root.title("循环虚拟定位")
        root.geometry("1120x820")
        root.minsize(900, 700)

        self.cache = TileCache(os.path.join(HERE, "tilecache"), 8)
        self.runner = Runner(self._log_async, self._state_async)
        self.msgs = queue.Queue()
        self.route_path = None
        self.route_raw = []      # GCJ 显示用
        self.route_wgs = []      # 推送用
        self.ddi_state = "未知"
        self._last_track = None
        self._finished_said = False
        self._kill_said = ""

        self._build_ui()
        self._load_route(ROUTES[0][1])
        threading.Thread(target=self._check_ddi_bg, daemon=True).start()
        self.root.after(120, self._pump)
        self.root.after(300, self._tick)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- UI ---------------------------------------------------------------- #
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        top.pack(fill="x")

        ttk.Label(top, text="路线").pack(side="left")
        self.route_var = tk.StringVar(value=ROUTES[0][0])
        self.route_box = ttk.Combobox(top, textvariable=self.route_var, width=28,
                                      state="readonly",
                                      values=[r[0] for r in ROUTES])
        self.route_box.pack(side="left", padx=(6, 4))
        self.route_box.bind("<<ComboboxSelected>>", self._on_route_change)

        ttk.Button(top, text="重新加载", width=9,
                   command=self._reload_route).pack(side="left", padx=2)
        self.move_btn = ttk.Button(top, text="拖动", width=6,
                                   command=lambda: self._set_mode("move"))
        self.move_btn.pack(side="left", padx=2)
        self.rot_btn = ttk.Button(top, text="旋转", width=6,
                                  command=lambda: self._set_mode("rotate"))
        self.rot_btn.pack(side="left", padx=2)
        self.scale_btn = ttk.Button(top, text="缩放", width=6,
                                    command=lambda: self._set_mode("scale"))
        self.scale_btn.pack(side="left", padx=2)
        self.draw_btn = ttk.Button(top, text="画路线", width=7,
                                   command=lambda: self._set_mode("draw"))
        self.draw_btn.pack(side="left", padx=2)
        ttk.Button(top, text="保存路线", width=9,
                   command=self._save_route).pack(side="left", padx=2)

        # 预设形状
        ttk.Label(top, text="  形状").pack(side="left", padx=(14, 0))
        self.shape_var = tk.StringVar(value=presets.SHAPES[0]["name"])
        self._shape_size = presets.SHAPES[0]["size"]
        self.shape_box = ttk.Combobox(top, textvariable=self.shape_var, width=7,
                                      state="readonly",
                                      values=[s["name"] for s in presets.SHAPES])
        self.shape_box.pack(side="left", padx=(6, 2))
        self.shape_box.bind("<<ComboboxSelected>>", self._on_shape_change)
        self.size_lbl = ttk.Label(top, text="%d m" % presets.SHAPES[0]["size"], width=7)
        self.size_lbl.pack(side="left", padx=(2, 2))
        ttk.Button(top, text="＋ 放到中心", width=11,
                   command=self._place_shape).pack(side="left", padx=2)

        self.view_var = tk.StringVar(value="road")
        ttk.Radiobutton(top, text="路网", value="road", variable=self.view_var,
                        command=self._switch_view).pack(side="right", padx=2)
        ttk.Radiobutton(top, text="卫星", value="sat", variable=self.view_var,
                        command=self._switch_view).pack(side="right", padx=2)

        # 地图
        mapwrap = ttk.Frame(self.root, padding=(10, 0, 10, 0))
        mapwrap.pack(fill="both", expand=True)
        self.map = MapView(mapwrap, self.cache,
                           on_drag_end=self._route_moved,
                           on_draw_done=self._route_drawn,
                           bg="#e8e8e8", highlightthickness=1,
                           highlightbackground="#cccccc")
        self.map.pack(fill="both", expand=True)

        # 控制区
        ctrl = ttk.Frame(self.root, padding=(10, 8, 10, 6))
        ctrl.pack(fill="x")

        row1 = ttk.Frame(ctrl)
        row1.pack(fill="x")
        ttk.Label(row1, text="配速").pack(side="left")
        self.pace_var = tk.IntVar(value=357)   # 5'57"
        self.pace_scale = tk.Scale(row1, from_=900, to=180, orient="horizontal",
                                   showvalue=False, resolution=5, length=340,
                                   variable=self.pace_var, command=self._on_pace)
        self.pace_scale.pack(side="left", padx=(6, 8))
        self.pace_lbl = ttk.Label(row1, text="5'57\"/km", width=12,
                                  font=("Microsoft YaHei", 11, "bold"))
        self.pace_lbl.pack(side="left")
        self.speed_lbl = ttk.Label(row1, text="2.80 m/s", width=12, foreground="#666")
        self.speed_lbl.pack(side="left")

        ttk.Label(row1, text="频率").pack(side="left", padx=(18, 2))
        self.hz_var = tk.StringVar(value="1 Hz")
        hz = ttk.Combobox(row1, textvariable=self.hz_var, width=7, state="readonly",
                          values=["1 Hz", "2 Hz", "5 Hz"])
        hz.pack(side="left")
        hz.bind("<<ComboboxSelected>>", self._on_hz)

        row2 = ttk.Frame(ctrl)
        row2.pack(fill="x", pady=(8, 0))
        self.btn_start = ttk.Button(row2, text="▶  开始", width=12,
                                    command=self._start)
        self.btn_start.pack(side="left")
        self.btn_pause = ttk.Button(row2, text="⏸  暂停", width=12,
                                    command=self._pause, state="disabled")
        self.btn_pause.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(row2, text="⏹  停止", width=12,
                                   command=self._stop, state="disabled")
        self.btn_stop.pack(side="left")
        ttk.Button(row2, text="⟲  恢复真实定位", width=16,
                   command=self._reset).pack(side="left", padx=(18, 0))
        ttk.Button(row2, text="检查 DDI", width=11,
                   command=self._check_ddi_now).pack(side="left", padx=6)

        row3 = ttk.Frame(ctrl)
        row3.pack(fill="x", pady=(8, 0))
        ttk.Label(row3, text="目标距离").pack(side="left")
        self.goal_var = tk.StringVar(value="0")
        self.goal_entry = ttk.Entry(row3, textvariable=self.goal_var, width=8,
                                    justify="right")
        self.goal_entry.pack(side="left", padx=(6, 2))
        self.goal_entry.bind("<Return>", lambda _e: self._on_goal())
        self.goal_entry.bind("<FocusOut>", lambda _e: self._on_goal())
        ttk.Label(row3, text="km 自动停").pack(side="left")
        for txt, km in (("1", 1), ("3", 3), ("5", 5), ("10", 10), ("不限", 0)):
            ttk.Button(row3, text=txt, width=5,
                       command=lambda k=km: self._set_goal(k)).pack(side="left", padx=2)
        self.goal_lbl = ttk.Label(row3, text="", foreground="#2a7",
                                  font=("Microsoft YaHei", 9))
        self.goal_lbl.pack(side="left", padx=(10, 0))

        row4 = ttk.Frame(ctrl)
        row4.pack(fill="x", pady=(6, 0))
        self.autokill_var = tk.BooleanVar(value=True)
        self.autokill_chk = ttk.Checkbutton(
            row4, text="跑够后自动结束 Keep（清理后台）",
            variable=self.autokill_var, command=self._on_autokill)
        self.autokill_chk.pack(side="left")
        self.kill_lbl = ttk.Label(row4, text="", foreground="#666",
                                  font=("Microsoft YaHei", 9))
        self.kill_lbl.pack(side="left", padx=(10, 0))

        # 状态栏
        bar = ttk.Frame(self.root, padding=(10, 4, 10, 8))
        bar.pack(fill="x")
        self.st_ddi = ttk.Label(bar, text="DDI: 检查中 ...", width=18)
        self.st_ddi.pack(side="left")
        self.st_dev = ttk.Label(bar, text="设备: 未连接", width=26)
        self.st_dev.pack(side="left", padx=10)
        self.st_dist = ttk.Label(bar, text="已跑 0.00 km", width=16,
                                 font=("Microsoft YaHei", 10, "bold"))
        self.st_dist.pack(side="left")
        self.st_lap = ttk.Label(bar, text="第 0 圈", width=10)
        self.st_lap.pack(side="left")
        self.st_center = ttk.Label(bar, text="", foreground="#888")
        self.st_center.pack(side="right")

    # -- 路线 -------------------------------------------------------------- #
    def _load_route(self, name):
        path = os.path.join(ROUTES_DIR, os.path.basename(name))
        if not os.path.exists(path):
            messagebox.showerror("找不到路线", path)
            return
        try:
            raw, crs = M._load_route_raw(path)
            wgs = M._apply_crs(raw, crs)
        except Exception as e:
            messagebox.showerror("路线读取失败", "%s\n%s" % (path, e))
            return
        # 显示统一用 GCJ-02（高德瓦片是 GCJ）
        if crs.upper().replace("-", "").startswith("WGS"):
            disp = [coord.wgs2gcj(p[0], p[1]) for p in wgs]
        else:
            disp = list(raw)
        self.route_path = path
        self.route_raw = disp
        self.route_wgs = wgs
        self.map.set_route(disp)
        self.runner.set_route(wgs)
        self.runner.lap = 0
        self.runner.sent = 0
        self.runner.dist = 0.0
        self.map.clear_track()
        total = M.build_loop(wgs, max(0.4, self.runner.speed / self.runner.hz))[1]
        self._log("已加载 %s：%d 点，周长 %.1f m" % (name, len(disp), total))
        self._state()

    def _on_route_change(self, _e=None):
        label = self.route_var.get()
        for text, fname in ROUTES:
            if text == label:
                self._load_route(fname)
                return

    def _reload_route(self):
        if self.route_path:
            self._load_route(os.path.basename(self.route_path))

    def _on_shape_change(self, _e=None):
        name = self.shape_var.get()
        s = next((x for x in presets.SHAPES if x["name"] == name), None)
        if not s:
            return
        self._shape_size = s["size"]
        self.size_lbl.configure(text="%d m" % s["size"])
        self._log("形状 %s：%s（周长 %d m，落地后用「缩放」调大小）"
                  % (s["name"], s["desc"], s["size"]))

    def _place_shape(self):
        """在当前位置生成预设形状，然后自动进入拖动模式。"""
        name = self.shape_var.get()
        s = next((x for x in presets.SHAPES if x["name"] == name), None)
        if not s:
            return
        lat, lon = self.map.center
        size = int(getattr(self, "_shape_size", s["size"]))
        pts = presets.make_points(s["id"], lat, lon, size=size)
        self.route_raw = pts
        self.route_wgs = [coord.gcj2wgs(p[0], p[1]) for p in pts]
        self.map.set_route(pts)
        self.runner.set_route(self.route_wgs)
        self.runner.lap = 0
        self.runner.sent = 0
        self.runner.dist = 0.0
        self.map.clear_track()
        self.map.mode = "move"
        self.route_var.set("（%s）" % presets.label(s["id"], size))
        total = M.build_loop(self.route_wgs,
                             max(0.4, self.runner.speed / self.runner.hz))[1]
        self._log("已生成「%s」%d 点，周长 %.1f m —— 拖动地图可整条移动"
                  % (presets.label(s["id"], size), len(pts), total))
        self._state()

    def _set_mode(self, m):
        """切换地图交互模式：view / move / rotate / scale / draw。"""
        if self.map.mode == m:
            m = "view"
        self.map.mode = m
        self.map.reset_edit()
        for name, btn in (("move", self.move_btn), ("rotate", self.rot_btn),
                          ("scale", self.scale_btn), ("draw", self.draw_btn)):
            btn.configure(style="Accent.TButton" if m == name else "TButton")
        tips = {
            "move": "拖动模式：按住地图拖，整条路线跟着平移",
            "rotate": "旋转模式：按住地图绕路线中心画弧，松手生效",
            "scale": "缩放模式：按住往外拖放大，往里拖缩小，松手生效",
            "draw": "画路线：在地图上逐个点，双击结束，右键撤销上一个点",
        }
        self._log(tips.get(m, "浏览模式"))

    def _toggle_move(self):
        self._set_mode("move")

    def _route_drawn(self, pts):
        """手画路线结束：GCJ-02 点直接当路线用。"""
        if len(pts) < 3:
            return
        self.route_raw = list(pts)
        self.route_wgs = [coord.gcj2wgs(p[0], p[1]) for p in pts]
        self.map.set_route(self.route_raw)
        self.runner.set_route(self.route_wgs)
        self.runner.lap = 0
        self.runner.sent = 0
        self.runner.dist = 0.0
        self.map.clear_track()
        self.map.mode = "view"
        self.route_var.set("（手画路线 %d 点）" % len(pts))
        total = M.build_loop(self.route_wgs,
                             max(0.4, self.runner.speed / self.runner.hz))[1]
        self._log("手画路线：%d 点，周长 %.1f m" % (len(pts), total))
        self._state()

    def _route_moved(self, pts):
        self.route_raw = list(pts)
        self.route_wgs = [coord.gcj2wgs(p[0], p[1]) for p in pts]
        self.runner.set_route(self.route_wgs)
        self.runner.lap = 0
        self.runner.sent = 0
        self.runner.dist = 0.0
        self.map.clear_track()
        self._log("路线已移动，中心 %.6f,%.6f" % (pts[0][0], pts[0][1]))
        self._state()

    def _save_route(self):
        if not self.route_raw:
            return
        path = os.path.join(ROUTES_DIR, "route-custom.json")
        data = {
            "_说明": "图形界面里画/移动出来的路线",
            "_坐标系": "GCJ-02",
            "points": [{"lat": round(p[0], 7), "lon": round(p[1], 7)}
                       for p in self.route_raw],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        self._log("已保存到 %s" % path)
        messagebox.showinfo("已保存", path)

    def _switch_view(self):
        style = 6 if self.view_var.get() == "sat" else 8
        if style == 6 and not HAVE_PIL:
            messagebox.showwarning("不支持卫星图",
                                   "当前 Python 没有 Pillow，无法解码 JPEG 瓦片。\n"
                                   "请用「循环定位-界面.bat」启动（它用带 Pillow 的 Python）。")
            self.view_var.set("road")
            return
        self.cache.set_style(style)
        self.cache.mem.clear()
        self.map.redraw()

    # -- 速度 -------------------------------------------------------------- #
    def _on_pace(self, _v=None):
        sec = int(self.pace_var.get())
        self.pace_lbl.configure(text=fmt_pace(sec) + "/km")
        speed = 1000.0 / sec
        self.speed_lbl.configure(text="%.2f m/s" % speed)
        self.runner.set_speed(speed)
        self._state()

    def _on_hz(self, _e=None):
        txt = self.hz_var.get()
        hz = float(txt.split()[0])
        self.runner.set_hz(hz)

    # -- 目标距离 ---------------------------------------------------------- #
    def _on_goal(self):
        try:
            km = float(self.goal_var.get().strip() or "0")
        except ValueError:
            km = 0.0
        if km < 0:
            km = 0.0
        self.runner.set_goal(km * 1000.0)
        self._log("目标距离：%s" % ("不限" if km <= 0 else "%.1f km（跑够自动停）" % km))
        self._state()

    def _set_goal(self, km):
        self.goal_var.set("%g" % km)
        self._on_goal()

    def _on_autokill(self):
        on = bool(self.autokill_var.get())
        self.runner.set_auto_kill(on)
        self._log("跑够后自动结束 App：%s" % ("开" if on else "关"))
        if on:
            self._log("（跑够后会结束 Keep 进程，它会停在跑够时的记录；"
                      "回 Keep 点「继续」就能看到这条记录）")
        self._state()

    # -- 按钮 -------------------------------------------------------------- #
    def _start(self):
        if not self.route_wgs:
            messagebox.showwarning("没有路线", "先选一条路线，或者用「形状」生成一条。")
            return
        if self.ddi_state != "已挂载":
            if not messagebox.askyesno(
                    "DDI 未挂载",
                    "手机的 DDI 好像没挂上，这时候推点不会生效（地图不会动）。\n\n"
                    "要现在自动挂载吗？（需要手机连着、已信任）"):
                pass
            else:
                self._check_ddi_now(mount=True)
        self.runner.err = None
        self.runner.start()
        if self.runner.holding:
            # 还处在「定位冻结在终点」：点开始只是开新一轮，服务一直在跑
            self.btn_start.configure(state="normal", text="▶  新一轮")
            self.btn_pause.configure(state="disabled", text="⏸  暂停")
            self.btn_stop.configure(state="normal")
        else:
            self.btn_start.configure(state="disabled", text="▶  开始")
            self.btn_pause.configure(state="normal", text="⏸  暂停")
            self.btn_stop.configure(state="normal")

    def _pause(self):
        if self.runner.paused:
            self.runner.resume()
            self.btn_pause.configure(text="⏸  暂停")
        else:
            self.runner.pause()
            self.btn_pause.configure(text="▶  继续")

    def _stop(self):
        self.runner.stop()
        self.btn_start.configure(state="normal", text="▶  开始")
        self.btn_pause.configure(state="disabled", text="⏸  暂停")
        self.btn_stop.configure(state="disabled")

    def _reset(self):
        def work():
            try:
                ps = M.PointStream(udid=None)
                ps.reset()
                ps.close()
                self._log_async("已发送『恢复真实定位』")
            except Exception as e:
                self._log_async("恢复失败：%s" % e)
        threading.Thread(target=work, daemon=True).start()

    # -- DDI --------------------------------------------------------------- #
    def _check_ddi_bg(self, mount=False):
        try:
            mux = M.Usbmux()
            dev = M._pick_device(mux, None)
            pairing = M.load_pairing(dev["udid"])
            ld = M.Lockdown(mux, dev, pairing).open()
            mounted = False
            try:
                resp = ld.start_service("com.apple.mobile.mobile_image_mounter")
                s = M.open_service_port(mux, dev, int(resp["Port"]),
                                        bool(resp.get("EnableServiceSSL")), pairing)
                M.ld_send(s, {"Command": "LookupImage", "ImageType": "Developer"})
                r = M.ld_recv(s)
                s.close()
                mounted = bool(isinstance(r, dict) and r.get("ImageSignature"))
            finally:
                try:
                    M.ld_send(ld.sock, {"Label": M.LABEL, "Request": "StopSession",
                                        "SessionID": ld.session_id})
                    M.ld_recv(ld.sock)
                except Exception:
                    pass
                ld.close()
            self.msgs.put(("ddi", mounted, dev["udid"]))
            if not mounted and mount:
                import mount_ddi_auto
                self._log_async("自动挂载 DDI ...")
                # 传 [] 让它别去解析本程序的 argv
                mount_ddi_auto.main([])
                self._check_ddi_bg(mount=False)
        except Exception as e:
            self.msgs.put(("ddi_err", str(e), None))

    def _check_ddi_now(self, mount=False):
        self.st_ddi.configure(text="DDI: 检查中 ...")
        threading.Thread(target=self._check_ddi_bg, args=(mount,),
                         daemon=True).start()

    # -- 消息泵 ------------------------------------------------------------ #
    def _log(self, s):
        """主线程内直接打日志（print 会被 _Tee 同时写进 gui.log）。"""
        print(s)

    def _log_async(self, s):
        self.msgs.put(("log", s, None))

    def _state(self):
        """主线程内直接刷新状态栏。"""
        self._refresh_state()

    def _state_async(self):
        self.msgs.put(("state", None, None))

    def _pump(self):
        # 先把后台下好的瓦片转成 PhotoImage（必须在主线程做）
        got = False
        while True:
            try:
                z, x, y, p = self.cache.ready.get_nowait()
            except queue.Empty:
                break
            try:
                self.cache._load_ready(z, x, y, p)
                got = True
            except Exception:
                pass
        if got:
            self.map.redraw()

        try:
            while True:
                kind, a, b = self.msgs.get_nowait()
                if kind == "log":
                    print(a)
                elif kind == "state":
                    self._refresh_state()
                elif kind == "ddi":
                    self.ddi_state = "已挂载" if a else "未挂载"
                    self.st_ddi.configure(
                        text="DDI: " + self.ddi_state,
                        foreground="#0b8f57" if a else "#d70015")
                    self.st_dev.configure(text="设备: " + (b or "")[:12] + "…")
                elif kind == "ddi_err":
                    self.st_ddi.configure(text="DDI: 检查失败", foreground="#d70015")
                    print("DDI 检查失败: %s" % a)
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def _refresh_state(self):
        r = self.runner
        self.st_dist.configure(text="已跑 %.2f km" % (r.dist / 1000.0))
        self.st_lap.configure(text="第 %d 圈" % r.lap)
        if r.err:
            self.st_dev.configure(text="设备: 出错", foreground="#d70015")
        # 目标距离进度
        if r.goal > 0:
            pct = min(100.0, 100.0 * r.dist / r.goal)
            self.goal_lbl.configure(
                text="%.0f%% (%.2f / %.1f km)" % (pct, r.dist / 1000.0, r.goal / 1000.0),
                foreground="#0b8f57" if pct >= 100 else "#2a7")
        else:
            self.goal_lbl.configure(text="不限", foreground="#666")
        # 跑够目标距离后进入「定位冻结在终点」状态：不是停了，服务还在推同一个点。
        if r.finished and self._finished_said is False:
            self._finished_said = True
            self.btn_start.configure(state="normal", text="▶  新一轮")
            self.btn_pause.configure(state="disabled", text="⏸  暂停")
            self.btn_stop.configure(state="normal")
            self._log("已跑够 %.2f km —— 定位停在终点不动。" % (r.dist / 1000.0))
            if not r.auto_kill:
                self._log("现在去手机上把 Keep 结束掉，回来点「停止」恢复真实定位。")
        if not r.finished:
            self._finished_said = False
            if self.btn_start.cget("text") == "▶  新一轮":
                self.btn_start.configure(text="▶  开始")
        # 自动结束 App 的结果
        ks = getattr(r, "kill_state", "") or ""
        if ks != getattr(self, "_kill_said", ""):
            self._kill_said = ks
            self.kill_lbl.configure(
                text=ks, foreground="#d70015" if ("失败" in ks or ks.startswith("✗"))
                else ("#0b8f57" if ks else "#666"))

    def _tick(self):
        r = self.runner
        if r.current:
            self.map.update_marker(r.current[0], r.current[1])
            key = (round(r.current[0], 6), round(r.current[1], 6))
            if key != self._last_track:
                self._last_track = key
                self.map.add_track(r.current[0], r.current[1])
        self.st_center.configure(text="中心 %.6f, %.6f   z%d"
                                 % (self.map.center[0], self.map.center[1], self.map.z))
        self.root.after(300, self._tick)

    def _on_close(self):
        self.runner.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except Exception:
        pass
    # 「当前模式」按钮的高亮样式（ttk 默认没有 Accent.TButton）
    try:
        st = ttk.Style(root)
        st.configure("Accent.TButton", foreground="#06111f", background="#3ddc97")
        st.map("Accent.TButton",
               background=[("active", "#5ee9a0"), ("!disabled", "#3ddc97")],
               foreground=[("!disabled", "#06111f")])
    except Exception:
        pass
    App(root)
    root.mainloop()
    return 0


def cli():
    import argparse
    ap = argparse.ArgumentParser(
        description="循环虚拟定位 · tkinter 桌面界面（离线备选；网页版更好用）")
    ap.add_argument("--version", action="version", version="ios-keep-loop-gps 1.0.0")
    ap.parse_args()
    try:
        return main()
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(cli())
