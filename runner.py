# -*- coding: utf-8 -*-
"""推点引擎（GUI / Web 界面共用）。

把「按配速沿闭合路线循环推点」这件事独立出来，不依赖任何界面库。
界面只需要提供两个回调：on_log(str) 和 on_state()。
"""
import json
import os
import threading
import time

import ios_loop_gps as M

HERE = os.path.dirname(os.path.abspath(__file__))
LAST_PT = os.path.join(HERE, "last_point.json")


class Runner:
    """后台线程沿闭合路线循环推点。"""

    def __init__(self, on_log=None, on_state=None, on_point=None):
        self.on_log = on_log or (lambda s: None)
        self.on_state = on_state or (lambda: None)
        # 每推成功一个点回调一次 (lat, lon)。界面用它记住「手机最后停在哪」，
        # 下次开始前就能判断这一次会不会是一次跨城瞬移。
        self.on_point = on_point or (lambda lat, lon: None)
        self.lock = threading.Lock()
        self.route_wgs = []
        self.speed = 2.8
        self.hz = 1.0
        self.loop_pts = []
        self.total = 0.0
        self.gen = 0
        self.idx = 0            # 当前跑到采样点列表的第几个（改配速时保持进度）
        self.lap = 0
        self.sent = 0
        self.dist = 0.0
        self.current = None
        self.running = False
        self.paused = False
        self.ps = None
        self.err = None
        self.thread = None
        self.lost = 0
        self.goal = 0.0         # 目标距离（米），0 = 不限，跑到就自动停
        self.finished = False   # 是否因为跑够目标距离而自动停
        self.holding = False    # 跑够之后是否正在「停在终点保持定位」
        self.auto_kill = True   # 跑够后是否自动结束手机上的 App（清理后台）
        self.kill_bundle = "com.gotokeep.keep"
        self.kill_state = ""    # 自动结束的结果说明，给界面显示
        self.kill_thread = None
        self._last_pt = None        # 内存里最后一次推成功的位置
        self._last_pt_save = 0.0

    # -- 路线 / 参数 ------------------------------------------------------- #
    def rebuild(self):
        with self.lock:
            if not self.route_wgs:
                self.loop_pts = []
                self.total = 0.0
                self.idx = 0
                return
            # 记住当前跑到路线的百分之几，重建后从同一进度接着跑，
            # 否则改配速 / 改频率会瞬间跳回起点。
            frac = 0.0
            if self.loop_pts:
                frac = (self.idx % len(self.loop_pts)) / float(len(self.loop_pts))
            step = max(0.4, self.speed / max(0.2, self.hz))
            pts, total = M.build_loop(self.route_wgs, step)
            self.loop_pts = pts
            self.total = total
            self.idx = int(frac * len(pts)) % max(1, len(pts))
            self.gen += 1

    def set_route(self, wgs_pts):
        with self.lock:
            self.route_wgs = list(wgs_pts)
            self.idx = 0          # 换路线才从起点开始
            self.loop_pts = []    # 清掉旧采样，避免 rebuild 里按旧进度换算
        self.rebuild()

    def set_speed(self, speed):
        with self.lock:
            self.speed = float(speed)
        self.rebuild()

    def set_hz(self, hz):
        with self.lock:
            self.hz = float(hz)
        self.rebuild()

    def set_goal(self, meters):
        """目标距离（米）。0 或负数 = 不限。"""
        with self.lock:
            self.goal = max(0.0, float(meters or 0.0))

    def set_auto_kill(self, on, bundle=None):
        """跑够目标后是否自动结束手机上那个 App（清理后台让它暂停）。"""
        with self.lock:
            self.auto_kill = bool(on)
            if bundle:
                self.kill_bundle = str(bundle)

    # -- 生命周期 ----------------------------------------------------------- #
    def load_last_point(self):
        """读上次最后推到的位置（手机定位停在哪）。没有就返回 None。"""
        if self._last_pt:
            return self._last_pt
        try:
            with open(LAST_PT, "r", encoding="utf-8") as f:
                o = json.load(f)
            self._last_pt = (float(o["lat"]), float(o["lon"]))
            return self._last_pt
        except Exception:
            return None

    def _save_last_point(self, lat, lon):
        # 内存里立刻更新，磁盘上最多 2 秒写一次 —— 5Hz 推点时没必要每个点都落盘
        self._last_pt = (lat, lon)
        now = time.monotonic()
        if now - self._last_pt_save < 2.0:
            return
        self._last_pt_save = now
        try:
            with open(LAST_PT, "w", encoding="utf-8") as f:
                json.dump({"lat": lat, "lon": lon}, f)
        except Exception:
            pass

    def _flush_last_point(self):
        if not self._last_pt:
            return
        try:
            with open(LAST_PT, "w", encoding="utf-8") as f:
                json.dump({"lat": self._last_pt[0], "lon": self._last_pt[1]}, f)
        except Exception:
            pass

    def forget_last_point(self):
        """忘掉「手机最后停在哪」。发过『恢复真实定位』之后必须调。"""
        self._last_pt = None
        try:
            os.remove(LAST_PT)
        except Exception:
            pass

    def jump_distance(self):
        """路线起点 vs 手机当前位置的距离（米）。任一方未知返回 None。

        这个数字用来防一个坑：如果手机现在停在 A 市、而路线在 B 市，
        一按开始就是一次上千公里的瞬移，跑步 App 会把它记成真实里程。
        """
        last = self.load_last_point()
        if not last:
            return None
        with self.lock:
            pts = self.loop_pts
            if not pts:
                return None
            lat0, lon0 = pts[0]
        return M.haversine(last[0], last[1], lat0, lon0)

    def start(self):
        if self.running:
            # 跑够目标距离后处于「冻结在终点」状态：再点开始 = 开新一轮
            if self.holding:
                self.holding = False
                self.finished = False
                self.dist = 0.0
                self.sent = 0
                self.lap = 0
                self.idx = 0
                self._log("开始新一轮（里程已归零，从起点重新跑）")
                self._state()
                return
            # 暂停中点「继续」：只解除暂停，别新开线程
            if self.paused:
                self.paused = False
                self._log("继续推点")
                self._state()
            return
        # 上一轮是「跑够目标距离自动停」的话，这一次算新一轮，计数归零
        if self.finished:
            self.finished = False
            self.holding = False
            self.dist = 0.0
            self.sent = 0
            self.lap = 0
            self._log("开始新一轮（里程已归零）")
        self.running = True
        self.paused = False
        self.holding = False
        self.lost = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def toggle_pause(self):
        if self.paused:
            self.resume()
        else:
            self.pause()
        return self.paused

    def stop(self):
        self.running = False
        self.paused = False
        self.holding = False

    def close(self):
        self.stop()
        if self.ps:
            try:
                self.ps.close()
            except Exception:
                pass

    # -- 内部 --------------------------------------------------------------- #
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

    def _start_kill(self, bundle):
        """在后台线程里结束 App，别阻塞推点/保持定位。"""
        if self.kill_thread and self.kill_thread.is_alive():
            return

        def work():
            try:
                import killapp
                ok, msg = killapp.kill_bundle(bundle)
                self.kill_state = msg
                self._log(("✓ " if ok else "✗ ") + msg)
                if ok:
                    self._log("Keep 已被结束，它的记录停在跑够时的里程。"
                              "回 Keep 点「继续」就能看到这条记录。")
            except Exception as e:                     # noqa: BLE001
                self.kill_state = "结束失败：%s" % e
                self._log("结束 App 失败：%s" % e)
                self._log("可以在手机上手动结束跑步，回来点「停止」恢复真实定位。")
            self._state()

        self.kill_thread = threading.Thread(target=work, daemon=True)
        self.kill_thread.start()

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

        while self.running:
            # 跑够目标距离后进入「保持」状态：不再沿路线走，而是低频重推终点坐标，
            # 让 iPhone 的定位冻结在终点。这样 Keep 里看起来就是「你停下来了」，
            # 里程不再增长，也不会因为服务关闭而瞬间跳回真实位置。
            if self.holding:
                hold = self.current
                if hold:
                    self._log("定位冻结在终点 %.6f,%.6f（去手机上点结束跑步，回来点「停止」）"
                              % (hold[0], hold[1]))
                while self.running and self.holding:
                    try:
                        self._ensure_ps()
                        if hold:
                            M.send_point(self.ps.sock, hold[0], hold[1])
                        self.lost = 0
                    except Exception as e:
                        self.lost += 1
                        if self.lost == 1 or self.lost % 20 == 0:
                            self._log("保持定位失败 %d 次：%s" % (self.lost, e))
                        if self.lost % 10 == 0:
                            try:
                                self.ps.hard_reset()
                            except Exception:
                                pass
                    time.sleep(1.0)
                continue
            with self.lock:
                pts = list(self.loop_pts)
                gen = self.gen
                idx = self.idx
                interval = 1.0 / max(0.2, self.hz)
            if not pts:
                time.sleep(0.3)
                continue
            n = len(pts)
            if idx >= n:
                idx = 0
            t_next = time.monotonic()
            # 从 idx 开始跑，跑满一圈算一次 lap；中途 gen 变了（改配速/改频率/换路线）
            # 就重新取 pts 和 idx，从同一进度继续，绝不跳回起点。
            steps = 0
            while steps < n and self.running:
                with self.lock:
                    if self.gen != gen:
                        break
                lat, lon = pts[idx % n]
                while self.paused and self.running:
                    time.sleep(0.15)
                if not self.running:
                    break
                try:
                    self._ensure_ps()
                    M.send_point(self.ps.sock, lat, lon)
                    self.lost = 0
                except Exception as e:
                    self.lost += 1
                    if self.lost == 1 or self.lost % 20 == 0:
                        self._log("推点失败 %d 次：%s" % (self.lost, e))
                    if self.lost % 10 == 0:
                        try:
                            self.ps.hard_reset()
                        except Exception:
                            pass
                        self._log("重连中 ...（USB 掉了插回来会自动接上）")
                    time.sleep(1.0)
                    continue
                self.current = (lat, lon)
                self.sent += 1
                if n > 1:
                    self.dist += self.total / n
                self._save_last_point(lat, lon)
                try:
                    self.on_point(lat, lon)
                except Exception:
                    pass
                with self.lock:
                    self.idx = (idx + 1) % n
                    goal = self.goal
                idx = (idx + 1) % n
                steps += 1
                self._state()
                # 跑够目标距离：不再沿路线走，改为在终点原地保持定位。
                # 不关服务、不 break —— 一旦关掉服务，iPhone 会立刻恢复真实定位，
                # 而 Keep 还在「运动中」，就会记下一条从终点瞬移回真实位置的轨迹。
                if goal > 0 and self.dist >= goal:
                    self.holding = True
                    self.finished = True
                    self.paused = False
                    self._log("已跑够 %.2f km —— 定位会停在终点不动。"
                              % (self.dist / 1000.0))
                    with self.lock:
                        do_kill = self.auto_kill
                        bundle = self.kill_bundle
                    if do_kill:
                        self._log("正在结束手机上的 App（清理后台）...")
                        self._start_kill(bundle)
                    else:
                        self._log("现在去手机上把 Keep 结束掉，回来点「停止」恢复真实定位。")
                    self._state()
                    break
                t_next += interval
                d = t_next - time.monotonic()
                if d > 0:
                    time.sleep(d)
                else:
                    t_next = time.monotonic()
            if steps >= n:
                self.lap += 1
                self._log("完成第 %d 圈（累计 %.2f km）" % (self.lap, self.dist / 1000.0))
                self._state()
        if self.ps:
            try:
                self.ps.close()
            except Exception:
                pass
        self._flush_last_point()

    # -- 给界面读的快照 ----------------------------------------------------- #
    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "paused": self.paused,
                "speed": self.speed,
                "hz": self.hz,
                "lap": self.lap,
                "sent": self.sent,
                "dist": self.dist,
                "total": self.total,
                "points": len(self.loop_pts),
                "current": list(self.current) if self.current else None,
                "err": self.err,
                "lost": self.lost,
                "goal": self.goal,
                "finished": self.finished,
                "holding": self.holding,
                "autoKill": self.auto_kill,
                "killState": self.kill_state,
            }
