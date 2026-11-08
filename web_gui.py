# -*- coding: utf-8 -*-
"""循环虚拟定位 · Web 界面

浏览器里操作：地图上看路线、画路线、拖动移动、滑杆改配速、开始/暂停/停止。

启动： py -3 -X utf8 web_gui.py            （默认 http://127.0.0.1:8765）
       py -3 -X utf8 web_gui.py --port 9000 --open

只用标准库（http.server + urllib），不装任何东西。
"""
import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import coord                     # GCJ-02 <-> WGS-84
import presets                   # 预设形状路线
import geosearch                 # 地点搜索（百度 -> GCJ-02）
import ios_loop_gps as M         # usbmux / lockdown / PointStream
from runner import Runner

# 路线文件目录。
# 默认直接用当前目录（仓库是扁平的，方便网页上传）；
# 如果你喜欢把路线收在 routes/ 子目录里，建一个 routes/ 就会自动优先用它。
_ROUTES_SUB = os.path.join(HERE, "routes")
ROUTES_DIR = _ROUTES_SUB if os.path.isdir(_ROUTES_SUB) else HERE

# 不是路线的 json，别列进下拉框
_NOT_ROUTES = {"last_route.json"}

# 随仓库分发的示例路线：文件 → 显示名
BUILTIN_ROUTES = [
    ("示例环线 · 天安门（1.2 km）", "example-tiananmen.json"),
    ("示例跑道 · 标准 400 m", "example-track-400m.json"),
]

LAST_CFG = os.path.join(HERE, "last_route.json")

# 手机当前位置离路线起点超过这个距离（米）就拦一下，防止把跨城瞬移记成跑步里程
JUMP_WARN_M = 50000.0


def _route_path(name):
    return os.path.join(ROUTES_DIR, os.path.basename(name))


def _builtin_label(name):
    """内置路线用写死的好看名字，别被文件名覆盖。"""
    for label, fn in BUILTIN_ROUTES:
        if fn == name:
            return label
    return None


def _route_label(name):
    """从 json 里读长度，拼一个「文件名（周长）」的标签。"""
    fixed = _builtin_label(name)
    if fixed:
        return fixed
    try:
        raw, crs = M._load_route_raw(_route_path(name))
        wgs = M._apply_crs(raw, crs, quiet=True)
        pts, total = M.build_loop(wgs, 2.8)
        stem = name.replace("route-", "").replace(".json", "")
        if total >= 1000:
            return "%s（%.2f km）" % (stem, total / 1000.0)
        return "%s（%d m）" % (stem, int(round(total)))
    except Exception:
        return name


def list_routes():
    """routes/ 目录里所有 *.json。内置的排前面，其余按名字排。"""
    out = []
    seen = set()
    for label, fn in BUILTIN_ROUTES:
        if os.path.exists(_route_path(fn)):
            out.append({"name": label, "file": fn})
            seen.add(fn)
    try:
        for fn in sorted(os.listdir(ROUTES_DIR)):
            if fn.endswith(".json") and fn not in seen and fn not in _NOT_ROUTES:
                out.append({"name": _route_label(fn), "file": fn})
                seen.add(fn)
    except Exception:
        pass
    return out


def load_last_route():
    try:
        with open(LAST_CFG, "r", encoding="utf-8") as f:
            fn = json.load(f).get("file") or ""
        if fn and os.path.exists(_route_path(fn)):
            return fn
    except Exception:
        pass
    return None


def save_last_route(fn):
    try:
        with open(LAST_CFG, "w", encoding="utf-8") as f:
            json.dump({"file": fn}, f, ensure_ascii=False)
    except Exception:
        pass


# 兼容旧代码里的 ROUTES 引用
ROUTES = list_routes()

LOG = deque(maxlen=200)
LOG_SEQ = [0]
LOG_LOCK = threading.Lock()

DDI = {"state": "unknown", "detail": "", "udid": "", "checking": False}
DDI_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        LOG_SEQ[0] += 1
        LOG.append({"i": LOG_SEQ[0], "t": time.strftime("%H:%M:%S"), "m": str(msg)})
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# 瓦片
# --------------------------------------------------------------------------- #
TILE_DIR = os.path.join(HERE, "tilecache")
HOSTS_SAT = ["webst01", "webst02", "webst03", "webst04"]
HOSTS_ROAD = ["webrd01", "webrd02", "webrd03", "webrd04"]
TILE_LOCKS = {}
TILE_LOCKS_GUARD = threading.Lock()


def tile_path(style, z, x, y):
    ext = "jpg" if style == 6 else "png"
    return os.path.join(TILE_DIR, str(style), str(z), str(x), "%d.%s" % (y, ext))


def tile_url(style, z, x, y):
    if style == 6:
        host = HOSTS_SAT[(x + y) % 4]
        return ("https://%s.is.autonavi.com/appmaptile?style=6&x=%d&y=%d&z=%d"
                % (host, x, y, z))
    host = HOSTS_ROAD[(x + y) % 4]
    return ("https://%s.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1"
            "&style=8&x=%d&y=%d&z=%d" % (host, x, y, z))


def get_tile(style, z, x, y):
    p = tile_path(style, z, x, y)
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                data = f.read()
            if len(data) > 200:
                return data
        except Exception:
            pass
    key = (style, z, x, y)
    with TILE_LOCKS_GUARD:
        lk = TILE_LOCKS.setdefault(key, threading.Lock())
    with lk:
        # 双检：可能别的线程刚下完
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    data = f.read()
                if len(data) > 200:
                    return data
            except Exception:
                pass
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            req = urllib.request.Request(tile_url(style, z, x, y),
                                         headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=10).read()
            if len(data) < 200:
                return None
            tmp = p + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, p)
            return data
        except Exception:
            return None


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #
class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.runner = Runner(on_log=log, on_state=lambda: None)
        self.route_name = BUILTIN_ROUTES[0][0]
        self.route_file = BUILTIN_ROUTES[0][1]
        self.route_wgs = []      # 推送用
        self.route_gcj = []      # 显示用（高德瓦片是 GCJ-02）
        self.style = 8           # 8 路网 / 6 卫星

    def load_route_file(self, name):
        raw, crs = M._load_route_raw(_route_path(name))
        wgs = M._apply_crs(raw, crs)
        disp = raw if not crs.upper().replace("-", "").startswith("WGS") \
            else [coord.wgs2gcj(p[0], p[1]) for p in wgs]
        with self.lock:
            self.route_file = name
            self.route_name = _route_label(name)
            self.route_wgs = wgs
            self.route_gcj = disp
        self.runner.set_route(wgs)
        self.runner.lap = 0
        self.runner.sent = 0
        self.runner.dist = 0.0
        save_last_route(name)
        return len(wgs), self.runner.total

    def set_route_gcj(self, pts, name=None):
        """界面画的点是 GCJ-02，转成 WGS-84 再推。"""
        wgs = [coord.gcj2wgs(p[0], p[1]) for p in pts]
        with self.lock:
            self.route_gcj = [list(p) for p in pts]
            self.route_wgs = wgs
            self.route_file = ""
            self.route_name = name or ("自定义路线（%d 点）" % len(pts))
        self.runner.set_route(wgs)
        self.runner.lap = 0
        self.runner.sent = 0
        self.runner.dist = 0.0
        return len(pts), self.runner.total

    def snapshot(self, since=0):
        snap = self.runner.snapshot()
        with self.lock:
            route = self.route_gcj
            name = self.route_name
            style = self.style
            route_file = self.route_file
        with DDI_LOCK:
            ddi = dict(DDI)
        with LOG_LOCK:
            logs = [e for e in LOG if e["i"] > since]
        cur = snap.get("current")
        if cur:
            try:
                snap["currentGcj"] = list(coord.wgs2gcj(cur[0], cur[1]))
            except Exception:
                snap["currentGcj"] = None
        else:
            snap["currentGcj"] = None
        return {
            "runner": snap,
            "route": route,
            "routeName": name,
            "style": style,
            "ddi": ddi,
            "logs": logs,
            "routes": list_routes(),
            "routeFile": route_file,
            "shapes": presets.SHAPES,
            "pace": 1000.0 / snap["speed"] if snap["speed"] else 0,
        }


ST = State()


def check_ddi_bg(mount=False):
    with DDI_LOCK:
        if DDI["checking"]:
            return
        DDI["checking"] = True
        DDI["state"] = "checking"
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
        with DDI_LOCK:
            DDI.update(state="mounted" if mounted else "missing",
                       detail="", udid=dev["udid"], checking=False)
        log("DDI: %s（设备 …%s）" % ("已挂载" if mounted else "未挂载", dev["udid"][-4:]))
        if not mounted and mount:
            log("自动挂载 DDI ...")
            import mount_ddi_auto
            # 传 [] 让它别去解析本程序的 argv（本程序有 --port 等参数）
            mount_ddi_auto.main([])
            check_ddi_bg(mount=False)
    except Exception as e:
        with DDI_LOCK:
            DDI.update(state="error", detail=str(e), checking=False)
        log("DDI 检查失败: %s" % e)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "LoopGPS/1.0"

    def log_message(self, fmt, *args):
        pass

    # -- 工具 --------------------------------------------------------------- #
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- 路由 --------------------------------------------------------------- #
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html"):
            p = os.path.join(HERE, "web_ui.html")
            try:
                with open(p, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, "web_ui.html 读不到: %s" % e, "text/plain; charset=utf-8")
            return

        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return

        if path == "/tile":
            try:
                style = int(q.get("style", ["8"])[0])
                z = int(q.get("z", ["16"])[0])
                x = int(q.get("x", ["0"])[0])
                y = int(q.get("y", ["0"])[0])
            except Exception:
                self._send(400, "bad tile args", "text/plain")
                return
            if z > 18:
                # 高德没有 z19+，直接回 404 让前端标灰，不去联网撞墙
                self._send(404, b"", "image/png")
                return
            data = get_tile(style, z, x, y)
            if data is None:
                self._send(404, b"", "image/png")
                return
            ctype = "image/jpeg" if style == 6 else "image/png"
            self._send(200, data, ctype, {"Cache-Control": "max-age=86400"})
            return

        if path == "/api/state":
            try:
                since = int(q.get("since", ["0"])[0])
            except Exception:
                since = 0
            self._json(ST.snapshot(since))
            return

        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        d = self._body()

        try:
            if path == "/api/route":
                if d.get("file"):
                    n, total = ST.load_route_file(d["file"])
                    log("已加载 %s：%d 点，周长 %.1f m" % (d["file"], n, total))
                elif d.get("shape"):
                    lat = float(d.get("lat") or 0.0)
                    lon = float(d.get("lon") or 0.0)
                    pts = presets.make_points(
                        d["shape"], lat, lon,
                        size=d.get("size"), ratio=d.get("ratio"),
                        angle=float(d.get("angle") or 0.0))
                    nm = presets.label(d["shape"], d.get("size"))
                    n, total = ST.set_route_gcj(pts, name=nm)
                    log("预设路线 %s：%d 点，周长 %.1f m" % (nm, n, total))
                elif d.get("points"):
                    pts = [(float(p[0]), float(p[1])) for p in d["points"]]
                    if len(pts) < 3:
                        self._json({"ok": False, "error": "至少要 3 个点"}, 400)
                        return
                    nm = (d.get("name") or "").strip() or None
                    n, total = ST.set_route_gcj(pts, name=nm)
                    log("自定义路线：%d 点，周长 %.1f m" % (n, total))
                else:
                    self._json({"ok": False, "error": "缺 file / shape / points"}, 400)
                    return
                self._json({"ok": True, "total": ST.runner.total,
                            "points": len(ST.route_gcj),
                            "name": ST.route_name})
                return

            if path == "/api/search":
                kw = (d.get("q") or "").strip()
                if not kw:
                    self._json({"ok": False, "error": "请输入地点名称"}, 400)
                    return
                try:
                    hits = geosearch.search(kw, limit=int(d.get("limit") or 8))
                except Exception as e:
                    log("地点搜索失败（%s）：%s" % (kw, e))
                    self._json({"ok": False,
                                "error": "搜索服务连不上，可以直接拖地图到目标位置"}, 200)
                    return
                log("搜索「%s」：%d 个结果" % (kw, len(hits)))
                self._json({"ok": True, "q": kw, "results": hits})
                return

            if path == "/api/speed":
                sp = float(d.get("speed") or 2.8)
                sp = max(0.3, min(12.0, sp))
                # 只改采样步长，不动进度：rebuild 会按当前百分比换算新索引
                ST.runner.set_speed(sp)
                log("配速改为 %.1f m/s（%s/km），当前进度 %.0f%%"
                    % (sp, fmt_pace(1000.0 / sp),
                       100.0 * ST.runner.idx / max(1, len(ST.runner.loop_pts))))
                self._json({"ok": True, "speed": sp,
                            "progress": ST.runner.idx / max(1, len(ST.runner.loop_pts))})
                return

            if path == "/api/hz":
                hz = float(d.get("hz") or 1.0)
                hz = max(0.2, min(10.0, hz))
                ST.runner.set_hz(hz)
                self._json({"ok": True, "hz": hz})
                return

            if path == "/api/goal":
                g = float(d.get("goal") or 0.0)
                g = max(0.0, min(200000.0, g))
                ST.runner.set_goal(g)
                log("目标距离设为 %s" % ("不限" if g <= 0 else "%.2f km" % (g / 1000.0)))
                self._json({"ok": True, "goal": g})
                return

            if path == "/api/autokill":
                on = bool(d.get("on", True))
                bundle = (d.get("bundle") or "").strip() or None
                ST.runner.set_auto_kill(on, bundle)
                log("跑够后自动结束 App：%s" % ("开" if on else "关"))
                self._json({"ok": True, "on": on,
                            "bundle": ST.runner.kill_bundle})
                return

            if path == "/api/start":
                if not ST.route_wgs:
                    self._json({"ok": False, "error": "还没有路线"}, 400)
                    return
                # 防跨城瞬移：手机上次停在 A，路线起点在 B，一按开始就是一次
                # A→B 的瞬移，跑步 App 会把它记成真实里程（曾出现过 1600 km 的记录）。
                jump = ST.runner.jump_distance()
                if jump is not None and jump > JUMP_WARN_M and not d.get("force"):
                    self._json({
                        "ok": False, "needConfirm": True, "jump": jump,
                        "error": "手机现在的位置和这条路线的起点差了 %.0f 公里。"
                                 "直接开始会是一次瞬移，Keep 会把这段距离记成跑步里程。"
                                 % (jump / 1000.0),
                    })
                    return
                ST.runner.start()
                self._json({"ok": True})
                return

            if path == "/api/pause":
                ST.runner.toggle_pause()
                self._json({"ok": True, "paused": ST.runner.paused})
                return

            if path == "/api/stop":
                ST.runner.stop()
                self._json({"ok": True})
                return

            if path == "/api/reset":
                def work():
                    try:
                        ps = M.PointStream(udid=None)
                        ps.reset()
                        ps.close()
                        # 手机已经回到真实位置，我们不知道它在哪了 —— 清掉记录，
                        # 免得下次开始拿旧坐标算距离，误报「差了 1600 公里」。
                        ST.runner.forget_last_point()
                        log("已发送『恢复真实定位』")
                    except Exception as e:
                        log("恢复失败：%s" % e)
                threading.Thread(target=work, daemon=True).start()
                self._json({"ok": True})
                return

            if path == "/api/ddi":
                threading.Thread(target=check_ddi_bg,
                                 args=(bool(d.get("mount")),), daemon=True).start()
                self._json({"ok": True})
                return

            if path == "/api/save":
                pts = d.get("points") or []
                if len(pts) < 3:
                    self._json({"ok": False, "error": "至少要 3 个点"}, 400)
                    return
                # 名字里带时间，画几条就存几条，互不覆盖
                name = str(d.get("name") or "").strip()
                if not name:
                    name = "route-draw-%s.json" % time.strftime("%m%d-%H%M%S")
                elif not name.endswith(".json"):
                    name += ".json"
                if not name.startswith("route-"):
                    name = "route-" + name
                # 只允许纯文件名，防止写到别处
                name = os.path.basename(name)
                out = _route_path(name)
                obj = {
                    "_说明": "Web 界面里画/移动出来的路线",
                    "_坐标系": "GCJ-02",
                    "points": [{"lat": round(float(p[0]), 7),
                                "lon": round(float(p[1]), 7)} for p in pts],
                }
                with open(out, "w", encoding="utf-8") as f:
                    json.dump(obj, f, ensure_ascii=False, indent=1)
                # 存完立刻把它设为当前路线，并记住下次启动就用它
                with ST.lock:
                    ST.route_file = name
                    ST.route_name = _route_label(name)
                save_last_route(name)
                log("路线已保存：%s（%d 点）" % (name, len(pts)))
                self._json({"ok": True, "path": out, "file": name,
                            "name": _route_label(name)})
                return

            if path == "/api/delete":
                name = os.path.basename(str(d.get("file") or ""))
                if not name.startswith("route-") or not name.endswith(".json"):
                    self._json({"ok": False, "error": "只能删 route-*.json"}, 400)
                    return
                if name in [f for _, f in BUILTIN_ROUTES]:
                    self._json({"ok": False, "error": "内置路线不能删"}, 400)
                    return
                p = _route_path(name)
                if os.path.exists(p):
                    os.remove(p)
                    # 删掉的正好是当前路线，就切回第一条内置路线，别留个死引用。
                    # 注意：load_route_file 内部自己会拿 ST.lock，这里不能持锁调用。
                    if ST.route_file == name:
                        fallback = None
                        for _, fn in BUILTIN_ROUTES:
                            if os.path.exists(_route_path(fn)):
                                fallback = fn
                                break
                        if fallback:
                            ST.load_route_file(fallback)
                    log("已删除路线：%s" % name)
                    self._json({"ok": True})
                else:
                    self._json({"ok": False, "error": "文件不存在"}, 404)
                return

            if path == "/api/style":
                ST.style = int(d.get("style") or 8)
                self._json({"ok": True, "style": ST.style})
                return

            self._send(404, "not found", "text/plain; charset=utf-8")
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._json({"ok": False, "error": str(e)}, 500)


def fmt_pace(sec):
    sec = int(round(sec))
    return "%d'%02d\"" % (sec // 60, sec % 60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args()

    # 单实例保护：端口已被占用就退出，避免两个进程同时推点（定位会乱闪）
    import socket as _sock
    probe = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        if probe.connect_ex((args.host, args.port)) == 0:
            print("=" * 58, flush=True)
            print("  已经有一个在跑了：http://%s:%d/" % (args.host, args.port), flush=True)
            print("  两个同时开会抢着推点，定位会乱闪。", flush=True)
            print("  要重启的话，先把那个窗口关掉。", flush=True)
            print("=" * 58, flush=True)
            try:
                webbrowser.open("http://%s:%d/" % (args.host, args.port))
            except Exception:
                pass
            return 1
    except Exception:
        pass
    finally:
        probe.close()

    # 上次用的路线优先，没有就第一条示例路线
    first = load_last_route()
    if not first:
        for _label, _fn in BUILTIN_ROUTES:
            if os.path.exists(_route_path(_fn)):
                first = _fn
                break
    if first:
        try:
            ST.load_route_file(first)
            log("已加载 %s（%d 点，周长 %.1f m）"
                % (first, len(ST.route_gcj), ST.runner.total))
        except Exception as e:
            log("路线加载失败：%s" % e)

    threading.Thread(target=check_ddi_bg, daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d/" % (args.host, args.port)
    print("=" * 58, flush=True)
    print("  循环虚拟定位 · Web 界面已启动", flush=True)
    print("  %s" % url, flush=True)
    print("  关掉这个窗口就停止（推点也会停）", flush=True)
    print("=" * 58, flush=True)
    if args.open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ST.runner.close()
        srv.server_close()
    return 0


if __name__ == "__main__":
    main()
