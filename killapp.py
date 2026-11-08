# -*- coding: utf-8 -*-
"""通过 DTX 通道结束手机上的 Keep（或任意 App）。

原理
----
iOS 上 Instruments / XCUITest 用的是一条 DTX 协议通道：

    lockdown StartService("com.apple.instruments.remoteserver.DVTSecureSocketProxy")
        → 裸 socket（再套一层 TLS，客户端证书用配对记录）
        → DTX 握手（_notifyOfPublishedCapabilities:）
        → _requestChannelWithCode:identifier: 打开
              "com.apple.instruments.server.services.processcontrol"
        → 调 killPid: / processIdentifierForBundleIdentifier:

这条通道在**未越狱**的机器上就能用（Xcode 调试、Instruments 用的就是它），
但它需要 Developer Disk Image 已挂载（否则 processcontrol 会被拒）。

协议格式见 PROTOCOL.md；纯标准库实现，不依赖 pymobiledevice3。
"""
import plistlib
import struct
import time

import ios_loop_gps as M

MAGIC = 0x1F3D5B79
FRAG_HDR = 32
PAY_HDR = 16

MSG_OK = 0
MSG_DISPATCH = 2
MSG_OBJECT = 3
MSG_ERROR = 4

PROXY_SVC = "com.apple.instruments.remoteserver.DVTSecureSocketProxy"
SVC_DEVICEINFO = "com.apple.instruments.server.services.deviceinfo"
SVC_PROCESSCONTROL = "com.apple.instruments.server.services.processcontrol"

CAPS = {
    "com.apple.private.DTXBlockCompression": 0,
    "com.apple.private.DTXConnection": 1,
}


# --------------------------------------------------------------------------- #
# NSKeyedArchiver（plistlib 就够，不引第三方）
# --------------------------------------------------------------------------- #
class _Arch:
    def __init__(self):
        self.objects = ["$null"]
        self._cls = {}

    def _class(self, name, bases=("NSObject",)):
        key = (name, bases)
        if key in self._cls:
            return self._cls[key]
        idx = len(self.objects)
        self.objects.append({"$classname": name, "$classes": [name] + list(bases)})
        self._cls[key] = plistlib.UID(idx)
        return self._cls[key]

    def add(self, obj):
        if obj is None:
            return plistlib.UID(0)
        if isinstance(obj, bool):
            self.objects.append(int(obj))
            return plistlib.UID(len(self.objects) - 1)
        if isinstance(obj, (int, float, str)):
            self.objects.append(obj)
            return plistlib.UID(len(self.objects) - 1)
        if isinstance(obj, (bytes, bytearray)):
            idx = len(self.objects)
            self.objects.append({"$class": self._class("NSData", ("NSData", "NSObject")),
                                 "NS.data": bytes(obj)})
            return plistlib.UID(idx)
        if isinstance(obj, dict):
            cls = self._class("NSDictionary", ("NSDictionary", "NSObject"))
            idx = len(self.objects)
            self.objects.append(None)       # 先占位，防止自引用时索引错乱
            keys = [self.add(k) for k in obj.keys()]
            vals = [self.add(v) for v in obj.values()]
            self.objects[idx] = {"$class": cls, "NS.keys": keys, "NS.objects": vals}
            return plistlib.UID(idx)
        if isinstance(obj, (list, tuple)):
            cls = self._class("NSArray", ("NSArray", "NSObject"))
            idx = len(self.objects)
            self.objects.append(None)
            vals = [self.add(v) for v in obj]
            self.objects[idx] = {"$class": cls, "NS.objects": vals}
            return plistlib.UID(idx)
        raise TypeError("不支持的类型: %r" % (type(obj),))

    def dumps(self, obj):
        root = self.add(obj)
        return plistlib.dumps({
            "$archiver": "NSKeyedArchiver",
            "$version": 100000,
            "$top": {"root": root},
            "$objects": self.objects,
        }, fmt=plistlib.FMT_BINARY)


def archive(obj):
    """把一个 Python 对象编成 NSKeyedArchiver（bplist）字节串。"""
    return _Arch().dumps(obj)


def unarchive(data):
    """把 NSKeyedArchiver 字节串解回 Python 对象；不是归档就原样返回。"""
    if not data:
        return None
    try:
        pl = plistlib.loads(bytes(data))
    except Exception:                                  # noqa: BLE001
        return None
    if not isinstance(pl, dict) or "$objects" not in pl:
        return pl
    objs = pl["$objects"]

    def res(v, depth=0):
        if depth > 40:
            return None
        if isinstance(v, plistlib.UID):
            return res(objs[v.data], depth + 1)
        if isinstance(v, str):
            return None if v == "$null" else v
        if isinstance(v, dict):
            if "$classname" in v:
                return v["$classname"]
            if "NS.keys" in v:
                return {res(k, depth + 1): res(x, depth + 1)
                        for k, x in zip(v["NS.keys"], v["NS.objects"])}
            if "NS.objects" in v:
                return [res(x, depth + 1) for x in v["NS.objects"]]
            if "NS.data" in v:
                return v["NS.data"]
            if "NS.string" in v:
                return v["NS.string"]
            return {k: res(x, depth + 1) for k, x in v.items() if not k.startswith("$")}
        if isinstance(v, list):
            return [res(x, depth + 1) for x in v]
        return v

    try:
        return res(pl["$top"]["root"])
    except Exception:                                  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# PrimitiveDictionary（DTX 的 aux 参数容器）
# --------------------------------------------------------------------------- #
def p_null():
    return struct.pack("<I", 10)


def p_str(s):
    b = s.encode("utf-8")
    return struct.pack("<II", 1, len(b)) + b


def p_buf(b):
    return struct.pack("<II", 2, len(b)) + bytes(b)


def p_i32(v):
    return struct.pack("<II", 3, int(v) & 0xFFFFFFFF)


def p_i64(v):
    return struct.pack("<I", 6) + struct.pack("<Q", int(v) & 0xFFFFFFFFFFFFFFFF)


def build_aux(args):
    if not args:
        return b""
    body = b"".join(p_null() + a for a in args)
    return struct.pack("<IIQ", 0x1F0, 0, len(body)) + body


def _read_prim(data, off):
    code = struct.unpack_from("<I", data, off)[0]
    off += 4
    t = code & 0xFF
    if t == 10:
        return None, off
    if t == 1:
        n = struct.unpack_from("<I", data, off)[0]
        off += 4
        return data[off:off + n].decode("utf-8", "replace"), off + n
    if t == 2:
        n = struct.unpack_from("<I", data, off)[0]
        off += 4
        return bytes(data[off:off + n]), off + n
    if t == 3:
        return struct.unpack_from("<I", data, off)[0], off + 4
    if t == 6:
        return struct.unpack_from("<Q", data, off)[0], off + 8
    if t == 9:
        return struct.unpack_from("<d", data, off)[0], off + 8
    raise ValueError("未知 primitive 类型 0x%x" % code)


def parse_aux(data):
    if len(data) < 16:
        return []
    _magic, _unk, blen = struct.unpack_from("<IIQ", data, 0)
    off, end, out = 16, 16 + blen, []
    while off < end:
        _key, off = _read_prim(data, off)
        val, off = _read_prim(data, off)
        out.append(val)
    return out


# --------------------------------------------------------------------------- #
# DTX 连接
# --------------------------------------------------------------------------- #
class DtxConnection:
    """一条 DTX 会话。用 :meth:`open` 建立，用完 :meth:`close`。"""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.next_id = 1
        self.caps = {}
        self.channels = {}

    # -- 收发 --
    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("DTX 连接被设备关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, msg_type, channel_code, payload=b"", aux=b"", identifier=0, conv=0, flags=0):
        if identifier == 0:
            identifier = self.next_id
            self.next_id += 1
        wire_code = channel_code if conv % 2 == 0 else -channel_code
        body = struct.pack("<BBBBIII", msg_type, 0, 0, 0,
                           len(aux), len(aux) + len(payload), 0) + aux + payload
        self.sock.sendall(struct.pack("<IIHHIIIiI", MAGIC, FRAG_HDR, 0, 1, len(body),
                                      identifier, conv, wire_code, flags) + body)
        return identifier

    def read_msg(self):
        magic, hsize, index, count, dsize, ident, conv, ch, flags = struct.unpack(
            "<IIHHIIIiI", self._recv_exact(FRAG_HDR))
        if magic != MAGIC:
            raise RuntimeError("DTX magic 不对: 0x%x" % magic)
        if hsize > FRAG_HDR:
            self._recv_exact(hsize - FRAG_HDR)
        if count == 1:
            body = self._recv_exact(dsize)
        else:
            # 多片：index==0 的 data_size 是总长度、本身不带 body；
            # index>0 的片各带 data_size 字节（见 PROTOCOL.md）。
            if index != 0:
                raise RuntimeError("多片消息不是从第 0 片开始（index=%d count=%d）" % (index, count))
            total, parts = dsize, []
            for _ in range(count - 1):
                _m, hs2, _i, _c, ds2, _id, _cv, _ch, _f = struct.unpack(
                    "<IIHHIIIiI", self._recv_exact(FRAG_HDR))
                if hs2 > FRAG_HDR:
                    self._recv_exact(hs2 - FRAG_HDR)
                parts.append(self._recv_exact(ds2))
            body = b"".join(parts)
            if len(body) != total:
                raise RuntimeError("多片重组长度不符：期望 %d，实际 %d" % (total, len(body)))
        mtype, _fa, _fb, _rsv, asize, tot, _f = struct.unpack_from("<BBBBIII", body, 0)
        return {
            "type": mtype, "identifier": ident, "conv": conv, "channel": ch, "flags": flags,
            "aux": body[PAY_HDR:PAY_HDR + asize],
            "payload": body[PAY_HDR + asize:PAY_HDR + tot],
        }

    def _reply_ok(self, msg):
        self.send(MSG_OK, abs(msg["channel"]), identifier=msg["identifier"], conv=msg["conv"] + 1)

    def recv_until(self, pred, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            msg = self.read_msg()
            if msg["type"] == MSG_DISPATCH:
                self._reply_ok(msg)
                if pred(msg):
                    return msg
                continue
            if pred(msg):
                return msg
        raise TimeoutError("等 DTX 消息超时")

    # -- 调用 --
    def call(self, channel, method, args=(), timeout=15.0):
        ident = self.send(MSG_DISPATCH, channel,
                          payload=archive(method), aux=build_aux(list(args)), flags=1)
        msg = self.recv_until(lambda m: m["identifier"] == ident, timeout=timeout)
        if msg["type"] == MSG_ERROR:
            raise RuntimeError("%s 失败: %r" % (method, unarchive(msg["payload"])))
        return unarchive(msg["payload"])

    def call_async(self, channel, method, args=()):
        """发出去就不等回复（killPid: 这类）。"""
        self.send(MSG_DISPATCH, channel, payload=archive(method),
                  aux=build_aux(list(args)), flags=0)

    # -- 会话 --
    def handshake(self, timeout=15.0):
        self.send(MSG_DISPATCH, 0,
                  payload=archive("_notifyOfPublishedCapabilities:"),
                  aux=build_aux([p_buf(archive(CAPS))]), flags=0)
        msg = self.recv_until(lambda m: m["type"] == MSG_DISPATCH and m["channel"] == 0,
                              timeout=timeout)
        ax = parse_aux(msg["aux"])
        if ax and isinstance(ax[0], (bytes, bytearray)):
            self.caps = unarchive(ax[0]) or {}
        return self.caps

    def open_channel(self, code, identifier, timeout=20.0):
        self.call(0, "_requestChannelWithCode:identifier:",
                  [p_i32(code), p_buf(archive(identifier))], timeout=timeout)
        self.channels[code] = identifier
        return code

    def close(self):
        try:
            self.sock.close()
        except Exception:                              # noqa: BLE001
            pass


class DtxSession:
    """lockdown → DVTSecureSocketProxy → DTX 握手，一条完整会话。"""

    def __init__(self, udid=None):
        self.mux = M.Usbmux()
        self.device = M._pick_device(self.mux, udid)
        self.pairing = M.load_pairing(self.device["udid"])
        self.lockdown = M.Lockdown(self.mux, self.device, self.pairing).open()
        self.conn = None

    def open(self):
        resp = self.lockdown.start_service(PROXY_SVC)
        sock = M.open_service_port(self.mux, self.device, resp.get("Port"),
                                   resp.get("EnableServiceSSL"), self.pairing)
        sock.settimeout(20.0)
        self.conn = DtxConnection(sock)
        self.conn.handshake()
        return self.conn

    def close(self):
        if self.conn:
            self.conn.close()
        self.lockdown.close()


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def pid_of_bundle(bundle_id, session=None):
    """查某个 App 当前 pid；没在跑返回 0。"""
    own = session is None
    if own:
        session = DtxSession()
        session.open()
    try:
        session.conn.open_channel(1, SVC_DEVICEINFO)
        procs = session.conn.call(1, "runningProcesses", timeout=25.0) or []
        # 先用 processcontrol 精确查，失败再退回遍历
        try:
            session.conn.open_channel(2, SVC_PROCESSCONTROL)
            pid = session.conn.call(2, "processIdentifierForBundleIdentifier:",
                                    [p_buf(archive(bundle_id))], timeout=15.0)
            if isinstance(pid, int) and pid > 0:
                return pid
        except Exception:                              # noqa: BLE001
            pass
        for p in procs:
            if not isinstance(p, dict):
                continue
            path = str(p.get("realAppName", ""))
            if ("/%s.app/" % bundle_id.split(".")[-1]) in path or bundle_id in path:
                return int(p.get("pid") or 0)
        return 0
    finally:
        if own:
            session.close()


def kill_bundle(bundle_id, wait=3.0, udid=None):
    """杀掉某个 App。返回 (是否成功, 说明)。"""
    session = DtxSession(udid)
    try:
        session.open()
        session.conn.open_channel(1, SVC_DEVICEINFO)
        session.conn.open_channel(2, SVC_PROCESSCONTROL)
        try:
            pid = session.conn.call(2, "processIdentifierForBundleIdentifier:",
                                    [p_buf(archive(bundle_id))], timeout=15.0)
        except Exception:                              # noqa: BLE001
            pid = 0
        if not pid:
            procs = session.conn.call(1, "runningProcesses", timeout=25.0) or []
            for p in procs:
                if isinstance(p, dict) and bundle_id in str(p.get("realAppName", "")):
                    pid = int(p.get("pid") or 0)
                    break
        if not pid:
            return False, "%s 没在运行" % bundle_id

        session.conn.call_async(2, "killPid:", [p_buf(archive(pid))])
        time.sleep(wait)

        # 复查
        try:
            procs = session.conn.call(1, "runningProcesses", timeout=25.0) or []
            alive = [p for p in procs if isinstance(p, dict) and p.get("pid") == pid]
        except Exception:                              # noqa: BLE001
            return True, "已发送 killPid: %d（连接已断，视为成功）" % pid
        if alive:
            return False, "killPid: %d 已发送但进程仍在" % pid
        return True, "已结束 %s（pid %d）" % (bundle_id, pid)
    finally:
        session.close()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="通过 DTX 通道结束手机上的 App")
    ap.add_argument("--bundle", default="com.gotokeep.keep", help="App 的 bundle id")
    ap.add_argument("--list", action="store_true", help="只列出进程，不杀")
    ap.add_argument("--udid", default=None)
    args = ap.parse_args(argv)

    if args.list:
        s = DtxSession(args.udid)
        try:
            s.open()
            s.conn.open_channel(1, SVC_DEVICEINFO)
            procs = s.conn.call(1, "runningProcesses", timeout=25.0) or []
            apps = [p for p in procs if isinstance(p, dict) and p.get("isApplication")]
            print("共 %d 个进程，其中 App %d 个：" % (len(procs), len(apps)))
            for p in apps:
                print("  pid=%-6s %-20s %s" % (p.get("pid"), p.get("name"), p.get("realAppName")))
        finally:
            s.close()
        return 0

    ok, msg = kill_bundle(args.bundle, udid=args.udid)
    print(("✓ " if ok else "✗ ") + msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
