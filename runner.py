# -*- coding: utf-8 -*-
"""推点引擎（GUI / Web 界面共用）。

把「按配速沿闭合路线循环推点」这件事独立出来，不依赖任何界面库。
界面只需要提供两个回调：on_log(str) 和 on_state()。
"""
import threading
import time

import ios_loop_gps as M


class Runner:
    """后台线程沿闭合路线循环推点。"""

    def __init__(self, on_log=None, on_state=None):
        self.on_log = on_log or (lambda s: None)
        self.on_state = on_state or (lambda: None)
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

    # -- 生命周期 ----------------------------------------------------------- #
    def start(self):
        if self.running:
            # 暂停中点「继续」：只解除暂停，别新开线程
            if self.paused:
                self.paused = False
                self._log("继续推点")
                self._state()
            return
        # 上一轮是「跑够目标距离自动停」的话，这一次算新一轮，计数归零
        if self.finished:
            self.finished = False
            self.dist = 0.0
            self.sent = 0
            self.lap = 0
            self._log("开始新一轮（里程已归零）")
        self.running = True
        self.paused = False
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
                with self.lock:
                    self.idx = (idx + 1) % n
                    goal = self.goal
                idx = (idx + 1) % n
                steps += 1
                self._state()
                # 跑够目标距离就自动停（在推完这个点之后停，位置留在终点上）
                if goal > 0 and self.dist >= goal:
                    self.running = False
                    self.paused = False
                    self.finished = True
                    self._log("已跑够 %.2f km，自动停止（停在路线上当前位置）"
                              % (self.dist / 1000.0))
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
            }
