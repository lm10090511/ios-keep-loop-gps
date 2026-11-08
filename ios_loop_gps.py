#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ios_loop_gps.py —— iPhone 循环虚拟定位（纯 Python 标准库，零第三方依赖）

用途：让 iPhone 的定位沿一条自定义路线持续、循环地移动，供 Keep 等运动 App
     采集足够多的轨迹样本（爱思助手只能单段 起点→终点，不支持循环）。

原理：直连 Apple 的 usbmuxd(127.0.0.1:27015) → lockdown(62078) → StartSession(TLS)
     → StartService("com.apple.dt.simulatelocation") → 按 iOS 私有字节格式推送坐标。
     报文格式由 idm_other.dll 逆向得到（见 ios_set_simulatelocation @0x18001f390）：
        [len(lat)][0]*7 [lat][0]*3 [len(lon)][lon]      总长 = len(lat)+len(lon)+12
     恢复真实定位 = 发 4 字节 01 00 00 00。

依赖：仅 Python 3.9+ 标准库（plistlib / ssl / socket / struct）。
     Windows 上需要 Apple Mobile Device Service 或 爱思助手 已启动（提供 27015）。

用法：
    py -3 ios_loop_gps.py --probe                  # 体检：看设备/配对/服务能否启动
    py -3 ios_loop_gps.py --list                   # 列出已连接的 iOS 设备
    py -3 ios_loop_gps.py --route route.json       # 无限循环跑路线
    py -3 ios_loop_gps.py --route route.json --loops 3 --speed 6 --hz 2
    py -3 ios_loop_gps.py --reset                  # 恢复真实定位
"""

import argparse
import json
import math
import os
import plistlib
import socket
import ssl
import struct
import sys
import time
import xml.etree.ElementTree as ET

USBMUX_HOST = os.environ.get("USBMUX_HOST", "127.0.0.1")
USBMUX_PORT = int(os.environ.get("USBMUX_PORT", "27015"))
LOCKDOWN_PORT = 62078
SERVICE = "com.apple.dt.simulatelocation"

PAIR_DIRS = [
    r"C:\ProgramData\Apple\Lockdown",
    os.path.expanduser(r"~\AppData\Roaming\Apple Computer\Lockdown"),
    os.path.expanduser(r"~\.config\libimobiledevice"),
    os.path.expanduser(r"~/.config/libimobiledevice"),
]

LABEL = "ios_loop_gps"


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def log(msg=""):
    print(msg, flush=True)


class SockReader:
    """从 socket 增量读数据的小封装。

    lockdown 的报文格式是「4 字节大端长度 + plist 体」，不是 usbmux 的
    16 字节帧头，也不是「读到 </plist> 为止」。用错格式的典型症状是：
    隧道能建，但一发出数据设备就 EOF。
    """

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    def read_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise EOFError("连接被对端关闭（设备可能已断开）")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


# --------------------------------------------------------------------------- #
# usbmux 协议（Apple 的 USB/TCP 复用层）
# --------------------------------------------------------------------------- #
class Usbmux:
    # 默认值在调用时才解析，便于测试里替换 USBMUX_PORT
    def __init__(self, host=None, port=None, timeout=10.0):
        self.host = USBMUX_HOST if host is None else host
        self.port = USBMUX_PORT if port is None else port
        self.timeout = timeout

    def _open(self):
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        s.settimeout(self.timeout)
        return s

    # usbmux 帧头是 16 字节：length / version(1) / message_type(8=plist) / tag
    HDR = 16
    MSG_PLIST = 8

    def _send(self, sock, obj, tag=1):
        payload = plistlib.dumps(obj, fmt=plistlib.FMT_BINARY)
        sock.sendall(struct.pack("<IIII", self.HDR + len(payload),
                                 1, self.MSG_PLIST, tag) + payload)

    def _recv(self, sock):
        hdr = b""
        while len(hdr) < self.HDR:
            chunk = sock.recv(self.HDR - len(hdr))
            if not chunk:
                raise EOFError("usbmuxd 关闭了连接")
            hdr += chunk
        total, _ver, _msg, _tag = struct.unpack("<IIII", hdr)
        body = b""
        while len(body) < total - self.HDR:
            chunk = sock.recv(total - self.HDR - len(body))
            if not chunk:
                raise EOFError("usbmuxd 关闭了连接")
            body += chunk
        return plistlib.loads(body)

    def list_devices(self):
        sock = self._open()
        try:
            self._send(sock, {
                "MessageType": "ListDevices",
                "ClientVersionString": LABEL,
                "ProgName": LABEL,
                "kLibUSBMuxVersion": 3,
            })
            resp = self._recv(sock)
        finally:
            sock.close()
        out = []
        for item in resp.get("DeviceList", []):
            props = item.get("Properties", {}) or {}
            out.append({
                "id": item.get("DeviceID") or props.get("DeviceID"),
                "udid": props.get("SerialNumber") or props.get("UDID"),
                "conn": props.get("ConnectionType", "?"),
            })
        return out

    def connect(self, device_id, port, swapped=None):
        """建立到设备某个 TCP 端口的隧道，返回原始 socket（已脱离 usbmux 帧）。

        PortNumber 是网络字节序：本机 usbmuxd 收到的是 htons 之后的值，
        直发 62078 会被回 Result Number=3。所以默认发 socket.htons(port)；
        swapped=False 时发原值（给不严格的实现留后路）。
        """
        sock = self._open()
        p = port if swapped is False else socket.htons(port)
        self._send(sock, {
            "MessageType": "Connect",
            "ClientVersionString": LABEL,
            "ProgName": LABEL,
            "DeviceID": device_id,
            "PortNumber": p,
        })
        resp = self._recv(sock)
        if resp.get("MessageType") != "Result" or resp.get("Number") != 0:
            sock.close()
            raise RuntimeError("usbmux Connect 失败: %r" % (resp,))
        sock.settimeout(self.timeout)
        return sock


# --------------------------------------------------------------------------- #
# lockdown 协议（配对 / 会话 / 起服务）
# --------------------------------------------------------------------------- #
def ld_send(sock, obj):
    """lockdown 报文：4 字节大端长度 + plist（XML 或 binary 都行）。

    这是本脚本最关键的一处。实测（抓 i4Tools 与 usbmuxd 的明文报文）：
    握手阶段是 usbmux 16 字节帧头包 plist；一旦 usbmux Connect 建好隧道，
    隧道内换成「4 字节大端长度 + plist」。用 16 字节头或纯 plist 都会让
    设备立刻 EOF。
    """
    data = plistlib.dumps(obj, fmt=plistlib.FMT_XML)
    sock.sendall(struct.pack(">I", len(data)) + data)


_READERS = {}


def _reader_for(sock):
    """同一个 socket 复用同一个 SockReader，避免缓冲区里已读到的下一帧被丢掉。"""
    key = id(sock)
    rd = _READERS.get(key)
    if rd is None:
        rd = SockReader(sock)
        _READERS[key] = rd
    return rd


def ld_recv(sock):
    rd = _reader_for(sock)
    n = struct.unpack(">I", rd.read_exact(4))[0]
    if n <= 0 or n > 4 * 1024 * 1024:
        raise RuntimeError("lockdown 长度字段异常: %d" % n)
    return plistlib.loads(rd.read_exact(n))


def ld_query_type(sock):
    ld_send(sock, {"Label": LABEL, "Request": "QueryType"})
    return ld_recv(sock)


def load_pairing(udid):
    for d in PAIR_DIRS:
        p = os.path.join(d, udid + ".plist")
        if os.path.exists(p):
            with open(p, "rb") as f:
                rec = plistlib.load(f)
            rec["__path"] = p
            return rec
    return None


def system_buid():
    for d in PAIR_DIRS:
        p = os.path.join(d, "SystemConfiguration.plist")
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    return plistlib.load(f).get("SystemBUID")
            except Exception:
                pass
    return None


def _pem_to_file(data, suffix):
    """把 PEM 文本写到临时文件（ssl.load_cert_chain 只认文件路径）。

    注意：不要用 tempfile.mkstemp —— 某些受限环境里它建出来的目录 ACL 异常，
    写文件会 PermissionError。这里固定用一个子目录。
    """
    if isinstance(data, (bytes, bytearray)):
        text = bytes(data).decode("utf-8", "replace")
    else:
        text = str(data)
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_pemt")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "c_%d%s" % (os.getpid(), suffix))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text.strip() + "\n")
    return path


def tls_wrap(sock, pairing):
    """用配对记录里的 HostCertificate + HostPrivateKey 做客户端证书握手。"""
    cert = _pem_to_file(pairing["HostCertificate"], ".pem")
    key = _pem_to_file(pairing["HostPrivateKey"], ".key")
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.set_ciphers("ALL:@SECLEVEL=0")
    except Exception:
        pass
    ctx.load_cert_chain(certfile=cert, keyfile=key)
    try:
        return ctx.wrap_socket(sock, server_hostname=None, do_handshake_on_connect=True)
    finally:
        for p in (cert, key):
            try:
                os.unlink(p)
            except OSError:
                pass


class Lockdown:
    """一次完整的 lockdown 会话：握手 → TLS → 起服务。"""

    def __init__(self, mux, device, pairing):
        self.mux = mux
        self.device = device
        self.pairing = pairing
        self.raw = None
        self.sock = None
        self.session_id = None

    def open(self, swapped=None):
        # PortNumber 是网络字节序，Usbmux.connect 默认已经发 htons(port)；
        # 这里只需按默认来一次，失败再退回原值试一遍。
        order = (None, False) if swapped is None else (swapped,)
        last = None
        for sw in order:
            try:
                self.raw = self.mux.connect(self.device["id"], LOCKDOWN_PORT, swapped=sw)
                break
            except Exception as exc:                   # noqa: BLE001
                last = exc
                self.raw = None
        if self.raw is None:
            raise RuntimeError("连不上 lockdown 端口(%d)：%s" % (LOCKDOWN_PORT, last))
        resp = ld_query_type(self.raw)
        if resp.get("Type") != "com.apple.mobile.lockdown":
            self.raw.close()
            raise RuntimeError("lockdown 握手异常: %r" % (resp,))
        self.product = resp
        sb = system_buid() or self.pairing.get("SystemBUID")
        ld_send(self.raw, {
            "Label": LABEL,
            "Request": "StartSession",
            "HostID": self.pairing.get("HostID"),
            "SystemBUID": sb,
        })
        resp = ld_recv(self.raw)
        if "Error" in resp:
            self.raw.close()
            raise RuntimeError("StartSession 失败: %s" % resp.get("Error"))
        self.session_id = resp.get("SessionID")
        if resp.get("EnableSessionSSL"):
            self.sock = tls_wrap(self.raw, self.pairing)
        else:
            self.sock = self.raw
        return self

    def start_service(self, name):
        ld_send(self.sock, {"Label": LABEL, "Request": "StartService", "Service": name})
        resp = ld_recv(self.sock)
        if "Error" in resp:
            raise RuntimeError("StartService(%s) 失败: %s" % (name, resp.get("Error")))
        return resp

    def close(self):
        for s in (self.sock, self.raw):
            try:
                if s:
                    s.close()
            except Exception:
                pass


def open_service_port(mux, device, port, enable_ssl, pairing):
    """连到 StartService 返回的端口；必要时做二次 TLS。"""
    attempts = []
    for swapped in (None, False):
        try:
            sock = mux.connect(device["id"], port, swapped=swapped)
            if enable_ssl:
                sock = tls_wrap(sock, pairing)
            attempts.append((port, sock))
            return sock
        except Exception as exc:                       # noqa: BLE001
            attempts.append((port, "失败: %s" % exc))
            continue
    raise RuntimeError("服务端口连接失败，尝试过: %r" % (attempts,))


# --------------------------------------------------------------------------- #
# simulatelocation 报文
# --------------------------------------------------------------------------- #
def send_point(sock, lat, lon):
    """推送一个坐标点。

    报文格式来自 idm_other.dll 的 ios_set_simulatelocation（0x18001f390）。
    反编译里是：
        local_88 = CONCAT17((char)lVar5, (undefined7)local_88);   // 长度写进 8 字节头的最高字节
        memcpy(&uStack_80, local_b0, len1);                       // lat 从偏移 8 开始
        *(char*)(&uStack_80 + len1 + 3) = (char)len2;             // lon 长度写进 4 字节头的最高字节
        memcpy(&uStack_80 + len1 + 4, local_a0, len2);            // lon 从偏移 8+len1+4 开始
        iVar7 = (len1-1) + 0xd + len2;                            // 总长 = len1+len2+12

    在 x86 小端内存里，CONCAT17 的「高字节」落在偏移 7，等价于
    8 字节大端长度；lon 前面那 4 字节同理是 4 字节大端长度。即：
        [8 字节 BE len(lat)][lat][4 字节 BE len(lon)][lon]
    早期版本把 len(lat) 写在 byte0（成了 0x0C00000000000000），设备会当成
    长度为天文数字的包而丢弃，表现为「脚本在跑、地图不动」。
    """
    s_lat = ("%.9f" % lat).encode("ascii")
    s_lon = ("%.9f" % lon).encode("ascii")
    pkt = (struct.pack(">Q", len(s_lat)) + s_lat
           + struct.pack(">I", len(s_lon)) + s_lon)
    assert len(pkt) == len(s_lat) + len(s_lon) + 12
    sock.sendall(pkt)
    return len(pkt)


def send_reset(sock):
    """还原真实定位。

    报文来自 idm_other.dll 的 ios_reset_simulatelocation（0x18001f4f0）：
        local_res8[0] = 0x1000000;
        FUN_180006140(*param_1, local_res8, 4, ...);
    0x01000000 按小端写进 4 字节 = 00 00 00 01（大端就是 0x00000001）。
    早期版本这里误写成 01 00 00 00，会让设备侧定位卡死，必须保持 00 00 00 01。
    """
    sock.sendall(b"\x00\x00\x00\x01")


class PointStream:
    """向 com.apple.dt.simulatelocation 持续推点。

    实测要点（必须记住，否则会踩坑）：
    - 服务连接由设备侧控制生命周期：连上后约 2–3 秒就会被对端关闭
      （发到第 3–4 个点时 ConnectionAbortedError）。快速连发 20 个点
      （间隔 50ms）反而全部成功，所以是「空闲超时」而不是「计数上限」。
    - 每点重建一次完整链路（usbmux + lockdown + StartSession + TLS）约 90–190 ms，
      但每点重建 lockdown 会话会在设备侧堆积大量会话，实测会让 iPhone 的
      定位服务异常（表现为「手机没有定位」）。所以：
        * lockdown 会话**只建一次**，反复用它 StartService（实测 6 轮无问题）；
        * 只重建服务端口那一条 socket；
        * 退出时发 StopSession，并尽量 StopService 释放设备侧实例。
    - MAX_AGE 取 1.5 秒（实测 2–3 秒才断），留余量。
    """

    MAX_AGE = 2.0      # 秒。实测服务 socket 空闲 1–3 秒就被设备关掉，
                       # 所以主动在 2 秒内换一条新的，避免发到一半断掉。
                       # （只重建服务 socket，lockdown 会话仍然复用。）

    def __init__(self, args=None, udid=None):
        self.udid = udid
        self.sock = None
        self.ld = None
        self.dev = None
        self.pairing = None
        self.mux = None
        self.service_port = None
        self.born = 0.0
        self.reconnects = 0

    # -- lockdown 会话：只建一次，长期复用 -------------------------------- #
    def _ensure_session(self):
        if self.ld is not None:
            return
        if self.mux is None:
            self.mux = Usbmux()
        if self.dev is None:
            self.dev = _pick_device(self.mux, self.udid)
            self.pairing = load_pairing(self.dev["udid"])
            if not self.pairing:
                raise RuntimeError("找不到配对记录，请先用爱思助手连一次")
        self.ld = Lockdown(self.mux, self.dev, self.pairing).open()

    def _connect(self):
        """建一条新的服务连接。

        实测：设备侧也会把 **lockdown 会话本身** 关掉（跑久了之后
        StartService 直接 ConnectionAbortedError）。所以这里必须把
        「会话失效」也当成可重试的情况：整个会话重开一次再 StartService。
        """
        last = None
        for attempt in (1, 2):
            try:
                self._ensure_session()
                resp = self.ld.start_service(SERVICE)
                port = resp.get("Port")
                if port is None:
                    raise RuntimeError("StartService 响应里没有 Port: %r" % (resp,))
                self.service_port = int(port)
                self.sock = open_service_port(self.mux, self.dev, self.service_port,
                                              bool(resp.get("EnableServiceSSL")),
                                              self.pairing)
                self.born = time.monotonic()
                self.reconnects += 1
                return
            except (ConnectionError, OSError, EOFError, ssl.SSLError) as e:
                last = e
                self._reset_session()
                if attempt == 2:
                    raise
        raise last  # pragma: no cover

    def _reset_session(self):
        """丢掉当前 lockdown 会话（含 TLS socket），下次 _ensure_session 重开。"""
        try:
            if self.ld and self.ld.sock:
                self.ld.sock.close()
        except Exception:
            pass
        self.ld = None

    def hard_reset(self):
        """把 usbmux 连接也丢掉，下次重新枚举设备。

        用途：USB 掉线/重新枚举（DeviceID 会变）、或被别的程序抢占了
        usbmuxd 连接之后，光重开 lockdown 会话是不够的 —— mux 这条
        socket 本身也可能已经死了。
        """
        self._drop()
        try:
            if self.ld and self.ld.sock:
                self.ld.sock.close()
        except Exception:
            pass
        self.ld = None
        self.dev = None
        self.pairing = None
        self.mux = None

    def _stop_service(self):
        """关闭服务 socket 即可，**不要**发 StopService。

        实测：发 StopService 之后，同一个 lockdown 会话上的下一次 StartService
        会被设备直接 EOF（会话被作废）。而单纯 close() 服务 socket、让设备自己
        回收，则同一个会话可以连续 StartService 很多轮（实测 6 轮无问题）。
        """
        self.service_port = None

    def _drop(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None
        self._stop_service()

    def ensure(self):
        if self.sock is None or (time.monotonic() - self.born) > self.MAX_AGE:
            self._drop()
            self._connect()

    def point(self, lat, lon):
        """发一个点；连接失效则重建后重发一次。"""
        for attempt in (1, 2):
            try:
                self.ensure()
                return send_point(self.sock, lat, lon)
            except (ConnectionError, OSError, EOFError, ssl.SSLError):
                self._drop()
                if attempt == 2:
                    raise
        return 0

    def reset(self):
        self.ensure()
        try:
            send_reset(self.sock)
        except (ConnectionError, OSError, EOFError, ssl.SSLError):
            self._drop()
            self._connect()
            send_reset(self.sock)

    def close(self):
        self._drop()
        # 正常收尾，别把会话泄漏在设备上
        try:
            if self.ld and self.ld.sock:
                ld_send(self.ld.sock, {"Label": LABEL, "Request": "StopSession",
                                       "SessionID": self.ld.session_id})
                ld_recv(self.ld.sock)
        except Exception:
            pass
        try:
            if self.ld:
                self.ld.close()
        except Exception:
            pass
        self.ld = None


# --------------------------------------------------------------------------- #
# 路线：加载 / 插值 / 循环
# --------------------------------------------------------------------------- #
def haversine(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _points_from_obj(obj):
    pts = []
    if isinstance(obj, dict):
        for k in ("route", "points", "path", "track", "coordinates"):
            if k in obj:
                return _points_from_obj(obj[k])
        if "lat" in obj and "lon" in obj:
            return [(float(obj["lat"]), float(obj["lon"]))]
        raise ValueError("无法识别的路线结构: %r" % (list(obj)[:8],))
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                pts.append((float(item[0]), float(item[1])))
            elif isinstance(item, dict):
                if "lat" in item and "lon" in item:
                    pts.append((float(item["lat"]), float(item["lon"])))
                elif "latitude" in item and "longitude" in item:
                    pts.append((float(item["latitude"]), float(item["longitude"])))
            elif isinstance(item, str) and "," in item:
                a, b = item.split(",")[:2]
                pts.append((float(a), float(b)))
    return pts


def load_route(path):
    pts, crs = _load_route_raw(path)
    return _apply_crs(pts, crs)


def _load_route_raw(path):
    """返回 (点列表, 坐标系标记)。

    坐标系标记来自 JSON 里的 `_坐标系` / `crs` 字段，取值：
      "WGS-84"（默认）—— GPS / iPhone 原生坐标
      "GCJ-02"        —— 高德 / 腾讯地图取出来的点，需要转换
    高德卫星瓦片量出来的点一定是 GCJ-02，忘记标记就会偏 400–600 米。
    """
    if path and os.path.exists(path):
        if path.lower().endswith(".gpx"):
            root = ET.parse(path).getroot()
            pts = []
            for node in root.iter():
                if node.tag.split("}")[-1] in ("trkpt", "rtept", "wpt"):
                    lat, lon = node.get("lat"), node.get("lon")
                    if lat and lon:
                        pts.append((float(lat), float(lon)))
            if not pts:
                raise ValueError("GPX 里没有 trkpt/rtept/wpt")
            return pts, "WGS-84"
        with open(path, "r", encoding="utf-8-sig") as f:
            obj = json.load(f)
        crs = "WGS-84"
        if isinstance(obj, dict):
            crs = str(obj.get("_坐标系") or obj.get("crs") or "WGS-84").upper()
        return _points_from_obj(obj), crs
    # 内置兜底：北京天安门广场周边一个约 1.2km 的闭合矩形环（示例用）
    return [
        (39.9075, 116.3955), (39.9075, 116.3995),
        (39.9099, 116.3995), (39.9099, 116.3955),
        (39.9075, 116.3955),
    ], "WGS-84"


def _apply_crs(pts, crs):
    if crs in ("WGS-84", "WGS84", "WGS", "GPS"):
        return pts
    if crs in ("GCJ-02", "GCJ02", "GCJ", "AMAP", "高德"):
        from coord import gcj2wgs
        log("路线坐标系是 GCJ-02，自动转成 WGS-84（否则会偏 400–600 米）")
        return [gcj2wgs(la, lo) for la, lo in pts]
    raise ValueError("不认识的坐标系标记: %r（可用 WGS-84 / GCJ-02）" % (crs,))


def build_loop(route, step_m):
    """把路线按 step_m 采样成一个闭合环，返回点列表（一次完整循环）。"""
    pts = list(route)
    if not pts:
        raise ValueError("路线为空")
    if len(pts) < 2:
        pts = pts * 2
    closed = haversine(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]) < 1.0
    if not closed:
        pts.append(pts[0])
    total = sum(haversine(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
                for i in range(len(pts) - 1))
    if total <= 0:
        raise ValueError("路线总长为 0")
    n = max(2, int(round(total / step_m)))
    out = []
    for k in range(n):
        target = total * k / n
        acc = 0.0
        for i in range(len(pts) - 1):
            seg = haversine(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1])
            if seg <= 0:
                continue
            if acc + seg >= target:
                t = (target - acc) / seg
                out.append((
                    pts[i][0] + (pts[i + 1][0] - pts[i][0]) * t,
                    pts[i][1] + (pts[i + 1][1] - pts[i][1]) * t,
                ))
                break
            acc += seg
        else:
            out.append(pts[-1])
    return out, total


# --------------------------------------------------------------------------- #
# 命令
# --------------------------------------------------------------------------- #
def cmd_list(args):
    mux = Usbmux()
    try:
        devs = mux.list_devices()
    except Exception as exc:                           # noqa: BLE001
        log("无法连接 usbmuxd(%s:%d)：%s" % (USBMUX_HOST, USBMUX_PORT, exc))
        log("→ 请先启动「爱思助手」或确认 Apple Mobile Device Service 正在运行。")
        return 2
    if not devs:
        log("usbmuxd 已连上，但没有发现 iOS 设备。请插上 iPhone 并信任此电脑。")
        return 1
    for d in devs:
        log("设备 DeviceID=%s  UDID=%s  连接=%s" % (d["id"], d["udid"], d["conn"]))
        log("  配对记录: %s" % (load_pairing(d["udid"]) and "已找到" or "缺失"))
    return 0


def _pick_device(mux, udid):
    devs = mux.list_devices()
    if not devs:
        raise RuntimeError("没有发现 iOS 设备（插上 iPhone、解锁并点『信任』）")
    if udid:
        for d in devs:
            if d["udid"] == udid:
                return d
        raise RuntimeError("没有找到 UDID=%s 的设备，现有: %s"
                           % (udid, [d["udid"] for d in devs]))
    return devs[0]


def cmd_probe(args):
    log("=== 1. usbmuxd ===")
    mux = Usbmux()
    try:
        devs = mux.list_devices()
        log("OK  usbmuxd 可用，发现 %d 台设备" % len(devs))
    except Exception as exc:                           # noqa: BLE001
        log("FAIL 连不上 usbmuxd(%s:%d)：%s" % (USBMUX_HOST, USBMUX_PORT, exc))
        log("     → 启动爱思助手，或 services.msc 里启动 Apple Mobile Device Service")
        return 2
    if not devs:
        log("FAIL 无设备。插上 iPhone，解锁，点『信任此电脑』")
        return 1
    for d in devs:
        log("  DeviceID=%s UDID=%s 连接=%s" % (d["id"], d["udid"], d["conn"]))

    dev = devs[0]
    log("\n=== 2. 配对记录 ===")
    pairing = load_pairing(dev["udid"])
    if not pairing:
        log("FAIL 找不到配对记录（%s.plist）" % dev["udid"])
        log("     → 用爱思助手连一次（会触发配对），或 iTunes 信任一次")
        return 1
    log("OK  %s" % pairing["__path"])

    log("\n=== 3. lockdown StartSession ===")
    ld = Lockdown(mux, dev, pairing)
    try:
        ld.open()
        log("OK  ProductVersion=%s  BuildVersion=%s"
            % (ld.product.get("ProductVersion"), ld.product.get("BuildVersion")))
    except Exception as exc:                           # noqa: BLE001
        log("FAIL %s" % exc)
        return 1

    log("\n=== 4. StartService %s ===" % SERVICE)
    try:
        resp = ld.start_service(SERVICE)
        log("OK  %r" % (resp,))
    except Exception as exc:                           # noqa: BLE001
        log("FAIL %s" % exc)
        log("     → 常见原因：DeveloperDiskImage 未挂载。")
        log("       解决：打开爱思助手 → 虚拟定位 → 设一个点 → 点『立即模拟』一次，")
        log("       让爱思把 DDI/dylib 推到设备，然后再跑本脚本。")
        ld.close()
        return 1
    finally:
        try:
            ld.close()
        except Exception:
            pass
    log("\n全部检查通过，可以跑 --route 了。")
    return 0


def _open_stream(args):
    """返回 (service_socket, device, lockdown)。lockdown 必须保持打开，
    否则会话一关，服务端口也会失效。"""
    mux = Usbmux()
    dev = _pick_device(mux, args.udid)
    pairing = load_pairing(dev["udid"])
    if not pairing:
        raise RuntimeError("找不到配对记录，请先用爱思助手连一次")
    ld = Lockdown(mux, dev, pairing).open()
    try:
        resp = ld.start_service(SERVICE)
        port = resp.get("Port")
        if port is None:
            raise RuntimeError("StartService 响应里没有 Port: %r" % (resp,))
        sock = open_service_port(mux, dev, int(port),
                                 bool(resp.get("EnableServiceSSL")), pairing)
    except Exception:
        ld.close()
        raise
    return sock, dev, ld


def cmd_reset(args):
    ps = PointStream(udid=args.udid)
    ps.reset()
    ps.close()
    log("已向 %s 发送『恢复真实定位』" % (ps.dev["udid"] if ps.dev else "设备"))
    return 0


def cmd_run(args):
    route = load_route(args.route)
    step_m = args.speed / float(args.hz)
    loop_pts, total = build_loop(route, step_m)
    log("路线点 %d 个，闭合环长 %.1f m；采样 %d 点/圈（步长 %.2f m）"
        % (len(route), total, len(loop_pts), step_m))
    log("速度 %.2f m/s（%.2f 分/公里），频率 %.1f Hz，循环 %s"
        % (args.speed, (1000.0 / args.speed) / 60.0, args.hz,
           "无限" if args.loops <= 0 else args.loops))

    ps = PointStream(udid=args.udid)
    for i in range(1, 61):
        try:
            ps.ensure()
            break
        except Exception as e:
            if i == 1:
                log("等待设备连接 ...（%s）" % e)
            if i == 60:
                raise
            time.sleep(2.0)
    log("已连上 %s，开始推送定位（Ctrl+C 停止）" % ps.dev["udid"])

    interval = 1.0 / float(args.hz)
    t_next = time.monotonic()
    sent = 0
    lap = 0
    lost = 0          # 连续失败次数
    try:
        while True:
            for lat, lon in loop_pts:
                if args.loops > 0 and lap >= args.loops:
                    raise KeyboardInterrupt
                try:
                    ps.point(lat, lon)
                    lost = 0
                except Exception as e:
                    # 掉线（拔线/被其它程序占用/设备重新枚举）不退出，等它回来。
                    lost += 1
                    if lost == 1 or lost % 30 == 0:
                        log("[warn] 推点失败 %d 次：%s" % (lost, e))
                    if lost % 15 == 0:
                        log("[warn] 重连中 ...（USB 掉了就插回来，脚本会自己接上）")
                        try:
                            ps.hard_reset()
                        except Exception:
                            pass
                    time.sleep(1.0)
                    continue
                sent += 1
                if args.verbose and sent % 20 == 1:
                    log("  #%d lap=%d  %.6f,%.6f" % (sent, lap + 1, lat, lon))
                t_next += interval
                delay = t_next - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    t_next = time.monotonic()
            lap += 1
            log("完成第 %d 圈（累计 %d 个点，%.1f m，重建连接 %d 次）"
                % (lap, sent, total * lap, ps.reconnects))
    except KeyboardInterrupt:
        log("\n停止推送。注意：iOS 过一段时间会自动恢复真实定位；")
        log("如需立即恢复，跑：py -3 ios_loop_gps.py --reset")
    finally:
        ps.close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="iPhone 循环虚拟定位")
    ap.add_argument("--list", action="store_true", help="列出已连接设备")
    ap.add_argument("--probe", action="store_true", help="体检：设备/配对/服务")
    ap.add_argument("--reset", action="store_true", help="恢复真实定位")
    ap.add_argument("--route", default=None, help="路线文件 .json 或 .gpx")
    ap.add_argument("--udid", default=None, help="指定设备 UDID（默认第一台）")
    ap.add_argument("--speed", type=float, default=6.0, help="配速，m/s（默认 6 ≈ 6'00\"/km）")
    ap.add_argument("--hz", type=float, default=2.0, help="推送频率（默认 2 Hz）")
    ap.add_argument("--loops", type=int, default=0, help="循环圈数，0=无限")
    ap.add_argument("--verbose", action="store_true", help="打印每个点")
    args = ap.parse_args(argv)

    if args.list:
        return cmd_list(args)
    if args.probe:
        return cmd_probe(args)
    if args.reset:
        return cmd_reset(args)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
