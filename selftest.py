"""上传前的自检脚本 —— 不需要 iPhone，验证除「真机推点」之外的所有东西。

跑法：
    py -3 -X utf8 selftest.py

会检查：
  1. 所有 .py 语法 + Python 3.9 兼容
  2. 所有脚本的 --help 能正常退出（不抛 traceback）
  3. 路线文件能加载、能算周长、坐标系能转换
  4. web_gui 起得来，各 API 返回正常
  5. 保存/删除路线落在当前目录
  6. 前端 HTML 结构、JS 语法
  7. 没有硬编码的本机路径和个人信息

退出码 0 = 全过。
"""

import ast
import glob
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

PASS = 0
FAIL = 0
SKIP = 0
DETAIL = []


def ok(name, extra=""):
    global PASS
    PASS += 1
    print("  [PASS] %s%s" % (name, ("  " + extra) if extra else ""))


def bad(name, why):
    global FAIL
    FAIL += 1
    DETAIL.append("%s: %s" % (name, why))
    print("  [FAIL] %s  -> %s" % (name, why))


def skip(name, why):
    global SKIP
    SKIP += 1
    print("  [SKIP] %s  (%s)" % (name, why))


def section(t):
    print("\n" + "=" * 62)
    print(" " + t)
    print("=" * 62)


def _py_files():
    """仓库里的正式脚本：跳过 _ 开头的临时验证文件（_smoke*.py / _t*.py 等）。"""
    return [f for f in sorted(glob.glob("*.py")) if not f.startswith("_")]


# ---------------------------------------------------------------- 1. 语法
def t_syntax():
    section("1. 语法与兼容性")
    for f in _py_files():
        src = io.open(f, encoding="utf-8").read()
        try:
            ast.parse(src, filename=f)
            ok("%s 语法" % f)
        except SyntaxError as e:
            bad("%s 语法" % f, "line %s: %s" % (e.lineno, e.msg))
            continue
        try:
            ast.parse(src, filename=f, feature_version=(3, 9))
            ok("%s 兼容 Python 3.9" % f)
        except SyntaxError as e:
            bad("%s 兼容 Python 3.9" % f, "line %s: %s" % (e.lineno, e.msg))


# ---------------------------------------------------------------- 2. --help
def t_help():
    section("2. 各脚本 --help 不抛异常")
    for f in _py_files():
        # runner.py / coord.py 是库，没有 CLI；selftest.py 是自己，跳过避免递归
        if f in ("runner.py", "coord.py", "selftest.py"):
            continue
        try:
            p = subprocess.run([sys.executable, "-X", "utf8", f, "--help"],
                               capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            bad("%s --help" % f, "60 秒没返回（可能进了交互流程）")
            continue
        blob = (p.stdout or "") + (p.stderr or "")
        if "Traceback (most recent call last)" in blob:
            bad("%s --help" % f, "抛了 traceback")
        elif p.returncode != 0:
            bad("%s --help" % f, "exit=%d" % p.returncode)
        else:
            ok("%s --help" % f)


# ---------------------------------------------------------------- 3. 路线
def t_routes():
    section("3. 路线文件")
    import ios_loop_gps as M
    import coord
    files = sorted(f for f in glob.glob("*.json")
                   if not f.startswith(("last_route", "route-"))
                   and f != "package.json")
    if not files:
        bad("有路线文件", "一个都没有")
        return
    for f in files:
        try:
            raw, crs = M._load_route_raw(f)
            wgs = M._apply_crs(raw, crs)
            pts, total = M.build_loop(wgs, 2.8)
        except Exception as e:
            bad(os.path.basename(f), "%s: %s" % (type(e).__name__, e))
            continue
        if len(raw) < 3:
            bad(os.path.basename(f), "只有 %d 个点" % len(raw))
        elif total < 50:
            bad(os.path.basename(f), "周长只有 %.0f m" % total)
        else:
            ok(os.path.basename(f), "%d 点 / %.1f m / %s" % (len(raw), total, crs))

    # 坐标系换算自检
    la, lo = 39.908722, 116.397499
    wa, wo = coord.gcj2wgs(la, lo)
    d = ((la - wa) ** 2 + (lo - wo) ** 2) ** 0.5 * 111000
    if 300 < d < 800:
        ok("GCJ→WGS 偏移量", "%.0f m（正常 400–600）" % d)
    else:
        bad("GCJ→WGS 偏移量", "%.0f m 不在正常范围" % d)
    ba, bo = coord.wgs2gcj(wa, wo)
    err = max(abs(ba - la), abs(bo - lo))
    if err < 1e-9:
        ok("GCJ→WGS→GCJ 回代", "误差 %.1e" % err)
    else:
        bad("GCJ→WGS→GCJ 回代", "误差 %.1e 太大" % err)


# ---------------------------------------------------------------- 4. Web API
def free_port(start=8900):
    for p in range(start, start + 60):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            s.close()
    return None


def t_web():
    section("4. Web 界面与 API")
    port = free_port()
    if not port:
        skip("Web API", "找不到空闲端口")
        return
    base = "http://127.0.0.1:%d" % port
    proc = subprocess.Popen([sys.executable, "-X", "utf8", "web_gui.py",
                             "--port", str(port)],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        up = False
        for _ in range(60):
            time.sleep(0.25)
            try:
                urllib.request.urlopen(base + "/api/state", timeout=2).read()
                up = True
                break
            except Exception:
                if proc.poll() is not None:
                    break
        if not up:
            bad("web_gui 启动", "10 秒内没起来")
            return
        ok("web_gui 启动", "port=%d" % port)

        def get(path):
            with urllib.request.urlopen(base + path, timeout=10) as r:
                return r.status, r.read()

        def post(path, obj):
            data = json.dumps(obj).encode()
            req = urllib.request.Request(base + path, data=data,
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status, json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode() or "{}")

        # 首页
        st, body = get("/")
        if st == 200 and b"</html>" in body:
            ok("GET / 首页", "%d B" % len(body))
        else:
            bad("GET / 首页", "status=%d len=%d" % (st, len(body)))

        # state
        st, body = get("/api/state")
        d = json.loads(body.decode())
        for k in ("runner", "route", "routeName", "routes", "ddi"):
            if k not in d:
                bad("/api/state 字段 %s" % k, "缺失")
        ok("/api/state", "%d B, %d 条路线" % (len(body), len(d.get("routes", []))))

        # 瓦片
        st, body = get("/tile?style=8&z=16&x=53951&y=24804")
        if st == 200 and len(body) > 1000:
            ok("GET /tile 路网", "%d B" % len(body))
        else:
            bad("GET /tile 路网", "status=%d len=%d" % (st, len(body)))
        st, body = get("/tile?style=6&z=16&x=53951&y=24804")
        if st == 200 and body[:2] == b"\xff\xd8":
            ok("GET /tile 卫星（JPEG）", "%d B" % len(body))
        else:
            bad("GET /tile 卫星", "status=%d magic=%r" % (st, body[:2]))

        # z19 应该 404
        try:
            get("/tile?style=8&z=19&x=215804&y=99216")
            bad("GET /tile z19 应 404", "却返回了 200")
        except urllib.error.HTTPError as e:
            ok("GET /tile z19 返回 404", "（高德只到 z18）")

        # 预设形状
        st, body = get("/api/state")
        d = json.loads(body.decode())
        shapes = d.get("shapes") or []
        if len(shapes) >= 6 and all(k in shapes[0] for k in ("id", "name", "size", "min", "max")):
            ok("/api/state 带预设形状", "%d 种" % len(shapes))
        else:
            bad("/api/state 预设形状", "只有 %d 种" % len(shapes))

        shape_err = []
        for s in shapes:
            st, r = post("/api/route", {"shape": s["id"], "lat": 39.9087,
                                        "lon": 116.3975, "size": s["size"]})
            if not r.get("ok"):
                shape_err.append("%s: %s" % (s["id"], r.get("error")))
                continue
            want = s["size"]
            got = r.get("total", 0)
            if abs(got - want) / want > 0.02:
                shape_err.append("%s 周长 %.1f 应为 %d" % (s["id"], got, want))
        if shape_err:
            bad("预设形状周长", "; ".join(shape_err))
        else:
            ok("预设形状周长", "%d 种误差均 < 2%%" % len(shapes))

        # 目标距离
        st, r = post("/api/goal", {"goal": 3000})
        if r.get("ok") and abs(r.get("goal", 0) - 3000) < 1:
            ok("POST /api/goal", "3000 m")
        else:
            bad("POST /api/goal", str(r))
        st, r = post("/api/goal", {"goal": 999999})
        if r.get("ok") and abs(r.get("goal", 0) - 200000) < 1:
            ok("目标距离上限夹取", "200 km")
        else:
            bad("目标距离上限夹取", str(r))
        st, r = post("/api/goal", {"goal": 0})
        if r.get("ok") and abs(r.get("goal", -1)) < 1e-6:
            ok("目标距离清零（不限）")
        else:
            bad("目标距离清零", str(r))

        # 地名搜索（网络可能不通，不通不算失败）
        st, r = post("/api/search", {"q": "天安门", "limit": 3})
        if r.get("ok") and r.get("results"):
            hit = r["results"][0]
            if 39.7 < hit["lat"] < 40.1 and 116.2 < hit["lon"] < 116.6:
                ok("POST /api/search", "%s %.6f,%.6f" % (hit["name"], hit["lat"], hit["lon"]))
            else:
                bad("地名搜索坐标", "%s %.6f,%.6f 偏离天安门" % (hit["name"], hit["lat"], hit["lon"]))
        elif r.get("ok") is False:
            skip("地名搜索（网络不通）", str(r.get("error"))[:40])
        else:
            bad("POST /api/search", str(r))

        # 改配速 / 频率
        st, r = post("/api/speed", {"speed": 5.0})
        if r.get("ok") and abs(r.get("speed", 0) - 5.0) < 0.01:
            ok("POST /api/speed")
        else:
            bad("POST /api/speed", str(r))
        st, r = post("/api/hz", {"hz": 2.0})
        if r.get("ok"):
            ok("POST /api/hz")
        else:
            bad("POST /api/hz", str(r))

        # 保存路线
        pts = [[39.9075, 116.3955], [39.9075, 116.3995],
               [39.9099, 116.3995], [39.9099, 116.3955]]
        st, r = post("/api/save", {"points": pts, "name": "route-selftest.json"})
        target = "route-selftest.json"
        if r.get("ok") and os.path.exists(target):
            ok("POST /api/save 存下路线")
        else:
            bad("POST /api/save", "ok=%s 文件存在=%s" % (r.get("ok"), os.path.exists(target)))

        # 出现在列表里
        st, body = get("/api/state")
        d = json.loads(body.decode())
        names = [x["file"] for x in d.get("routes", [])]
        if "route-selftest.json" in names:
            ok("保存的路线出现在列表")
        else:
            bad("保存的路线出现在列表", "列表里没有")

        # 内置不能删
        st, r = post("/api/delete", {"file": "example-tiananmen.json"})
        if st == 400:
            ok("内置路线拒绝删除")
        else:
            bad("内置路线拒绝删除", "status=%d" % st)

        # 删自己的
        st, r = post("/api/delete", {"file": "route-selftest.json"})
        if r.get("ok") and not os.path.exists(target):
            ok("删除自己画的路线")
        else:
            bad("删除自己画的路线", str(r))

        # 删除后服务还活着（防死锁）
        st, _ = get("/api/state")
        if st == 200:
            ok("删除后服务未死锁")
        else:
            bad("删除后服务未死锁", "status=%d" % st)

        # 无设备时 start/stop 不崩
        for api in ("/api/start", "/api/pause", "/api/stop", "/api/reset"):
            st, r = post(api, {})
            if st == 200:
                ok("POST %s（无设备不崩）" % api)
            else:
                bad("POST %s" % api, "status=%d" % st)

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # 清理可能的残留
        for f in ("route-selftest.json", "last_route.json"):
            if os.path.exists(f) and f.startswith("route-selftest"):
                os.remove(f)


# ---------------------------------------------------------------- 5. 前端
def t_front():
    section("5. 前端页面")
    html = io.open("web_ui.html", encoding="utf-8").read()
    m = re.search(r"<script>(.*?)</script>", html, re.S)
    if not m:
        bad("web_ui.html 有 <script>", "没找到")
        return
    ok("web_ui.html 有 <script>", "%d 字符" % len(m.group(1)))
    for tag in ("div", "aside", "main", "canvas", "button"):
        o = len(re.findall(r"<%s[\s>]" % tag, html))
        c = len(re.findall(r"</%s>" % tag, html))
        if o == c:
            ok("HTML <%s> 标签配平" % tag, "%d 对" % o)
        else:
            bad("HTML <%s> 标签配平" % tag, "open=%d close=%d" % (o, c))

    # 前端引用的每个 API 后端都得有
    backend = io.open("web_gui.py", encoding="utf-8").read()
    used = set(re.findall(r"/api/(\w+)", m.group(1)))
    for name in sorted(used):
        if ('"/api/%s"' % name) in backend or ("'/api/%s'" % name) in backend:
            ok("前端调的 /api/%s 后端存在" % name)
        else:
            bad("前端调的 /api/%s 后端存在" % name, "后端没有这个路由")

    # 关键元素 id 都存在
    for eid in ("btnDdi", "btnDdiMount", "selRoute", "btnSave", "btnDel",
                "rngPace", "btnStart", "btnPause", "btnStop", "btnReset"):
        if ('id="%s"' % eid) in html:
            ok("元素 #%s 存在" % eid)
        else:
            bad("元素 #%s 存在" % eid, "HTML 里没有")


# ---------------------------------------------------------------- 6. 隐私
def t_privacy():
    section("6. 隐私与可移植性")
    # 这些模式是「不该出现在仓库里的东西」，拼起来写，免得本文件自己被扫到
    pats = {
        "本机用户目录": r"C:" + r"\\Users\\",
        "本仓库绝对路径": r"[A-Z]:\\[^\s\"']*" + "dsh" + r"-workspace",
        "iPhone UDID": r"c11a" + r"13c9",
        "配对 HostID": r"FC3A" + r"2295",
        "IMEI": r"8640" + r"39043488918",
        "GitHub 用户名": r"lm10" + r"090511",
        "真实校名": r"泉" + r"州|晋" + r"江|内" + r"坑|职业" + r"技术大学",
    }
    files = []
    for ext in ("*.py", "*.html", "*.md", "*.bat"):
        files += glob.glob(ext)
    files += glob.glob("*.json")
    files = [f for f in files if os.path.isfile(f) and f != "selftest.py"]

    # README 顶部的 Star 徽章必须带仓库所有者名（shields.io 的 URL 就是这么拼的），
    # 而仓库地址本来就是公开的，所以这一行不算泄露。其余地方一律不许出现。
    allow = {
        "GitHub 用户名": [("README.md", r"img\.shields\.io/github/stars/")],
    }

    for label, pat in pats.items():
        hits = []
        for f in files:
            try:
                txt = io.open(f, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            for m in re.finditer(pat, txt):
                line = txt[:m.start()].count("\n") + 1
                src = txt.splitlines()[line - 1] if line - 1 < len(txt.splitlines()) else ""
                if any(f == af and re.search(apat, src) for af, apat in allow.get(label, ())):
                    continue
                hits.append("%s:%d" % (f, line))
        if hits:
            bad("无「%s」" % label, ", ".join(hits[:3]))
        else:
            ok("无「%s」" % label)

    # 唯一允许的第三方依赖是 PIL，且必须是 try 包裹
    g = io.open("gps_gui.py", encoding="utf-8").read()
    if "try:" in g and "from PIL import" in g:
        ok("PIL 是可选的（try 包裹）")
    else:
        bad("PIL 是可选的", "import 没被 try 包住")

    # 除 PIL 外不应有第三方 import
    third = set()
    for f in _py_files():
        txt = io.open(f, encoding="utf-8").read()
        for m in re.finditer(r"^\s*(?:from|import)\s+([a-zA-Z_][\w.]*)", txt, re.M):
            top = m.group(1).split(".")[0]
            third.add(top)
    local = {"coord", "ios_loop_gps", "runner", "check_ddi",
             "mount_ddi_auto", "web_gui", "gps_gui", "presets", "geosearch",
             "killapp"}
    std = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else set()
    unknown = sorted(third - local - std - {"PIL"})
    if unknown:
        bad("只用标准库", "可疑第三方: %s" % unknown)
    else:
        ok("只用标准库", "（PIL 可选）")


# ---------------------------------------------------------------- 7. 设备
def t_device():
    section("7. 真机（没有就跳过）")
    try:
        import ios_loop_gps as M
        mux = M.Usbmux()
        devs = mux.list_devices()
    except Exception as e:
        skip("usbmuxd / 设备", "%s: %s" % (type(e).__name__, e))
        return
    if not devs:
        skip("iPhone 已连接", "usbmuxd 可用但没有设备")
        return
    ok("iPhone 已连接", "DeviceID=%s" % devs[0].get("id"))
    # 不推点、不改状态，只报个信息
    skip("推点与 DDI", "自检不动手机，请手动跑 check_ddi.py / web_gui.py")


def main():
    print("=" * 62)
    print(" 上传前自检")
    print("=" * 62)
    t_syntax()
    t_help()
    t_routes()
    t_web()
    t_front()
    t_privacy()
    t_device()

    print("\n" + "=" * 62)
    print(" 结果：PASS=%d  FAIL=%d  SKIP=%d" % (PASS, FAIL, SKIP))
    if DETAIL:
        print("\n 失败明细：")
        for d in DETAIL:
            print("   - " + d)
    print("=" * 62)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    import argparse as _ap
    _p = _ap.ArgumentParser(
        description="上传前自检：语法、--help、路线、Web API、前端、隐私、设备")
    _p.parse_args()
    sys.exit(main())
